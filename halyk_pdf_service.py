
from __future__ import annotations

import re
import zlib
import random
import math
import struct
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from pdf_service import (
    build_dynamic_cmap,
    _rebuild_xref_table,
    _round_to_natural,
    _fmt_coord,
    _op_separators,
    _read_truetype_glyph,
    _patch_truetype_glyphs,
    _ttf_table_dir,
)
from pdf_service_downscale import IncomeTooLowError
from halyk_bold_digits import DIGIT_GLYPHS, DIGIT_WIDTH_1000, SOURCE_UNITS_PER_EM

# ─── Помощник: running balance на границах дней, не после каждой транзакции ──


def _halyk_dayend_min_rb(
    sorted_txs: List["HalykTransaction"],
    opening_balance: float,
    use_scaled: bool,
) -> float:
    """Минимальный running balance KZT-транзакций, замеряемый на границах дней.

    Как и в pdf_service.min_dayend_balance (Kaspi Gold) — у Halyk дата операции
    (`op_date`) не содержит времени, поэтому порядок нескольких транзакций одной
    даты в PDF произволен. Проверка "баланс ≥ 0" после КАЖДОЙ отдельной
    транзакции ложно ловит внутридневные дипы, которые исчезают к концу того
    же дня (напр. дебет перед покрывающим его кредитом того же дня). Инвариант
    имеет смысл только на границе дня: если день начинается и заканчивается
    неотрицательным, существует внутридневной порядок без ухода в минус.

    На 4 реальных Halyk-файлах (HALYKformat1-4) per-transaction и day-boundary
    минимумы совпали везде — ложных срабатываний в этой выборке не найдено, но
    архитектурная возможность есть (та же, что была реально поймана на Kaspi
    Gold: −54,17 ₸ на gold_statement.pdf). Day-boundary строго не хуже
    per-transaction (может только убрать ложные минусы, никогда не добавить
    новых), поэтому замена безопасна независимо от того, проявляется ли
    расхождение на текущих данных.

    `sorted_txs` — уже отсортированные по `op_date` (см. `_sort_key` в
    вызывающем коде). Комиссия (total_commission) сюда НЕ включается — она
    учитывается отдельным списанием в конце вызывающим кодом, как и раньше.
    """
    rb = opening_balance
    min_rb = rb
    prev_date = None
    for t in sorted_txs:
        if t.currency != "KZT":
            continue
        if prev_date is not None and t.op_date != prev_date and rb < min_rb:
            min_rb = rb
        k_val = t.new_kiri_s if use_scaled else t.kiri_s
        s_val = t.new_shyghys if use_scaled else t.shyghys
        rb = round(rb + k_val - abs(s_val), 2)
        prev_date = t.op_date
    if rb < min_rb:
        min_rb = rb
    return min_rb


# ─── Константы ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_AMT_RE = re.compile(r"-?\d[\d\s]*,\d{2}")  # "299 374,05", "-2 030 782,79"
_NOISE = 0.03

# Пара "сумма + код валюты" (op_amount колонка). Работает как для одновалютного
# (legacy, всегда KZT), так и для мультивалютного (nav) формата Halyk.
_CCY_CODES = {"KZT", "USD", "EUR", "RUB", "TRY", "GBP", "CNY", "CHF", "AED", "JPY", "KGS", "UZS"}
# op_amount иногда показан как голый "-" вместо числа (нав-формат прячет сумму
# операции для внутренних переводов, где она дублирует "Расход в валюте счета").
_OPAMT_CCY_RE = re.compile(r"(-?\d[\d\s]*,\d{2}|-)\s+([A-Z]{3})\b")

# Тег субсчёта на строке-переносе после проводки в nav-формате, напр. "(KZT)"/"(USD)".
_LEDGER_TAG_RE = re.compile(r"\(([A-Z]{3})\)")

# Мультивалютный формат (Русский, «Выписка по счету: Мультивалютный договор»)
# не имеет единой фразы для дохода — маркируем по ключевым словам категории
# поступления. «Автоконвертация» намеренно исключена — это технический перевод
# между валютными подсчётами того же клиента, а не реальные деньги.
# «Зачисление пособий» добавлено по реальному файлу (счёт типа «Спец счет для
# пособий», 13 ежемесячных зачислений НАО, 100% всех поступлений счёта) — для
# такого счёта это и есть его единственный регулярный доход. Учтите: «Зачисление»
# здесь перечислено фразами целиком, а не одним словом — «Зачисление с депозита»
# это перевод с собственного вклада, и обобщать до голого «Зачисление» нельзя.
_NAV_INCOME_KEYWORDS = ("Поступление", "Зачисление с депозита", "Зачисление пособий",
                        "Пополнение", "Взнос денег через")


class NoScalableIncomeError(Exception):
    """Ни одно поступление не распознано как масштабируемый доход.

    is_salary матчится по ключевым словам (_NAV_INCOME_KEYWORDS / казахская
    фраза legacy-формата), и список заведомо неполон. Раньше этот случай молча
    возвращал выписку без единого изменения — пользователь получал на скачивание
    свой же исходный файл и не имел ни одного признака, что ничего не произошло.
    Теперь это явный отказ: /process отдаёт 400 с перечнем нераспознанных
    формулировок, чтобы их можно было добавить в список осознанно (см. коммент
    к _NAV_INCOME_KEYWORDS — это каждый раз решение о бизнес-смысле строки,
    а не механическое расширение).
    """

    def __init__(self, descriptions: Optional[List[str]] = None, total: float = 0.0):
        self.descriptions = descriptions or []
        self.total = total
        super().__init__(
            "В выписке не найдено ни одного поступления, которое можно масштабировать. "
            f"Нераспознанные формулировки: {', '.join(self.descriptions[:5]) or '—'}"
        )

    def to_dict(self) -> dict:
        return {
            "error": str(self),
            "reason": "no_scalable_income",
            "unclassified_descriptions": self.descriptions[:10],
            "unclassified_total": round(self.total, 2),
        }


# Те же floor-правила занижения дохода, что и в pdf_service_downscale (Kaspi Gold).
# Константы продублированы локально — модуль остаётся самодостаточным.
_INCOME_K = 0.3914
_SAFETY_MARGIN = 100_000.0
_MAX_DOWNSCALE_FACTOR = 0.30

# Коэффициент безопасной ширины для чисел в СВОЕЙ, независимой от соседей,
# позиции (см. replace_callback / "своя позиция" — таблица "Сумма операции",
# "Комиссия" и т.п. — самая узкая колонка шаблона Halyk, 49.2pt, замерено по
# PDF drawings). Число "-46 000,00" (34.67pt, безопасно) и число "2 913 294
# 003,35" (56pt, вылезает за обе границы колонки 49.2pt) дают отношение
# безопасной половины ширины к половине ширины СТАРОГО числа ≈ 1.42; берём
# чуть меньше (1.35) — небольшой запас в ~1pt на сторону, чтобы новое число
# не касалось линии колонки впритык. Эта величина самокалибруется под кегль
# и фактическую ширину конкретной ячейки/строки (через старое число), а не
# завязана на одну захардкоженную ширину в pt — так работает одинаково и для
# "Сумма операции", и для более широких колонок ("Приход"/"Расход"), и для
# legacy-формата с другим кеглем.
_COLUMN_SAFETY_RATIO = 1.35
_MIN_FONT_SCALE = 0.5


def _fmt(val: float) -> str:
    """Форматирует число как '1 234 567,89'."""
    return f"{abs(val):,.2f}".replace(",", " ").replace(".", ",").replace(" ", " ")


def _clean_digits(text: str) -> str:
    """Удаляет знак, пробелы, запятую → только цифры."""
    return re.sub(r"[^0-9]", "", text)


def _extract_bracketed(text: str, start_idx: int) -> str:
    """Возвращает содержимое [ ... ], начиная с start_idx, с учётом вложенных [ ]."""
    depth = 0
    began = None
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if ch == "[":
            if began is None:
                began = idx
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and began is not None:
                return text[began + 1:idx]
    return ""


def _parse_cid_widths(w_body: str) -> Dict[int, float]:
    """Разбирает тело /W-массива CIDFontType2 (обе формы: 'c [w1 w2 ...]' и 'c1 c2 w')."""
    tokens = re.findall(r"\[|\]|\d+", w_body)
    widths: Dict[int, float] = {}
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("[", "]"):
            i += 1
            continue
        c_first = int(tok)
        if i + 1 < n and tokens[i + 1] == "[":
            cid, j = c_first, i + 2
            while j < n and tokens[j] != "]":
                widths[cid] = float(tokens[j])
                cid += 1
                j += 1
            i = j + 1
        elif i + 2 < n and tokens[i + 1] not in ("[", "]") and tokens[i + 2] not in ("[", "]"):
            w = float(tokens[i + 2])
            for cid in range(c_first, int(tokens[i + 1]) + 1):
                widths[cid] = w
            i += 3
        else:
            i += 1
    return widths


# ─── Стилевые конвенции /W и /ToUnicode для вписывания недостающих CID ─────
# Та же дисциплина, что pdf_service._op_separators/_fmt_coord (см. CLAUDE.md,
# критерий 4 «Стиль сериализации операторов»): конвенция ЧИТАЕТСЯ из самого
# файла, а не хардкодится один раз на все форматы — h6.pdf пишет /W без
# единого пробела ("19[500]21[500]"), а HALYKformat1.pdf с пробелом после
# каждого "[" и между записями ("19[ 500] 20[ 500]"); оба реальных файла.

_W_SINGLE_ENTRY_RE = re.compile(rb"(\d+)\[(\s*)(\d+)\]")


def _w_array_entries(body: bytes, start: int, end: int) -> List[Tuple[int, int, int, bytes]]:
    """Разбирает одиночные-CID записи 'cid[width]' /W-массива CIDFontType2 в
    диапазоне [start, end) байтовой строки body. Возвращает список (cid,
    entry_start, entry_end, inner_ws) в порядке появления — entry_start/
    entry_end абсолютные индексы в body, inner_ws — пробельные байты между
    '[' и цифрой ширины ЭТОЙ записи. Форма 'c1 c2 w' (диапазон одинаковых
    ширин) не разбирается — на всех 6 локальных реальных файлах Halyk /W
    всегда в одиночной форме 'cid[width]'; если встретится другая форма или
    записей меньше двух, список короче двух элементов сигналит вызывающей
    стороне отказаться от вставки (см. _w_array_insert_sorted).
    """
    return [
        (int(m.group(1)), m.start(), m.end(), m.group(2))
        for m in _W_SINGLE_ENTRY_RE.finditer(body, start, end)
    ]


def _w_array_insert_sorted(
    cidobj_bytes: bytes,
    bracket_start: int,
    close_idx: int,
    new_entries: Dict[str, float],
) -> Optional[bytes]:
    """Вставляет новые CID-записи в /W-массив В ВОЗРАСТАЮЩЕМ порядке CID (как
    во всех реальных файлах — иначе хвост массива выглядит как «кто-то
    дописал руками»), разделителем/внутренним пробелом САМОГО ЭТОГО массива,
    а не хардкодом.

    new_entries — {cid_hex: width}. Возвращает новые байты объекта CIDFont
    (cidobj_bytes с точечными splice-вставками) либо None, если конвенцию
    или записей меньше двух — недостаточно, чтобы доверять «доминантному»
    разделителю, отказ вместо угадывания.
    """
    entries = _w_array_entries(cidobj_bytes, bracket_start + 1, close_idx)
    if len(entries) < 2:
        return None
    # Разделитель МЕЖДУ соседними записями (например b"" или b" ") — берём из
    # промежутка после первой же записи; конвенция однородна по всему массиву
    # (проверено на h6.pdf и HALYKformat1.pdf).
    dominant_sep = cidobj_bytes[entries[0][2]:entries[1][1]]
    dominant_inner_ws = entries[0][3]

    # Для каждого нового CID точка вставки вычисляется из НЕИЗМЕНЁННОГО
    # списка entries (снимок до любых правок) — конец последней записи с
    # cid МЕНЬШЕ нового (если такой нет — самое начало массива, перед первой
    # записью). Применяются позже в порядке убывания позиции, чтобы splice
    # одной записи не сдвигал ещё не применённые точки вставки (тот же приём,
    # что и «запись от конца файла к началу» ниже по трём FontFile2/W/
    # ToUnicode-регионам).
    insertions = []
    for cid_hex, width in new_entries.items():
        new_cid = int(cid_hex, 16)
        pos = bracket_start + 1
        for cid, _e_start, e_end, _inner in entries:
            if cid < new_cid:
                pos = e_end
            else:
                break
        new_bytes = (
            dominant_sep
            + f"{new_cid}[".encode("ascii")
            + dominant_inner_ws
            + f"{int(width)}]".encode("ascii")
        )
        insertions.append((pos, new_bytes))

    result = bytearray(cidobj_bytes)
    for pos, new_bytes in sorted(insertions, key=lambda t: t[0], reverse=True):
        result[pos:pos] = new_bytes
    return bytes(result)


def _cmap_bf_style(body: bytes) -> Optional[Tuple[bytes, bytes]]:
    """Читает конвенции EOL и межтокенного разделителя из существующего
    beginbfrange/beginbfchar-блока ЭТОГО ToUnicode CMap-потока — тот же
    принцип, что и /W выше: h6.pdf/HALYKformat1.pdf оба пишут записи
    'beginbfrange' как '<XXXX><XXXX><YYYY>\\r\\n' (CRLF, без пробела между
    hex-токенами), но хардкодить это на все возможные Halyk-файлы неверно.

    Возвращает (entry_eol, token_sep) либо None, если в потоке нет ни
    одного bfrange/bfchar-блока хотя бы с одной записью — тогда вызывающая
    сторона обязана отказаться от вставки нового bfchar-блока, а не
    гадать про EOL/пробелы.
    """
    m = re.search(rb"beginbf(range|char)", body)
    if m is None:
        return None
    tail = body[m.end():]
    if m.group(1) == b"range":
        entry_re = re.compile(
            rb"<[0-9A-Fa-f]+>(\s*)<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>(\r\n|\r|\n)"
        )
    else:
        entry_re = re.compile(rb"<[0-9A-Fa-f]+>(\s*)<[0-9A-Fa-f]+>(\r\n|\r|\n)")
    em = entry_re.search(tail)
    if em is None:
        return None
    return em.group(2), em.group(1)


# ─── Dataclasses ───────────────────────────────────────────────────────────

@dataclass
class HalykTransaction:
    op_date: str          # "26.12.2025"
    description: str      # "Қаражаттың шотқа түсуі ..."
    kiri_s: float         # кіріс (>0 для дохода, 0 для расхода)
    shyghys: float        # шығыс (<0 для расхода, 0 для дохода)
    kiri_s_text: str      # "299 374,05"
    shyghys_text: str     # "-299 374,05" или "0,00"
    # Валюта СУБСЧЁТА, по которому реально прошли кіріс/шығыс: именно она
    # решает, входит ли строка в тенговый баланс. На строке-переносе может быть
    # перезаписана ledger-тегом «(KZT)»/«(USD)» (см. parse_halyk_statement).
    currency: str = "KZT"  # KZT для legacy-формата (одновалютный)
    # Валюта колонки «Сумма операции» — как напечатана в самой строке и НИКОГДА
    # не перезаписывается ledger-тегом. У «Автоконвертации» эти две валюты
    # расходятся (op_amount в USD, списание по тенговому субсчёту), и тогда
    # разница между «Суммой операции» и «Расходом» — это КУРС, а не комиссия.
    op_currency: str = "KZT"
    is_salary: bool = False
    is_seizure: bool = False
    new_kiri_s: float = 0.0
    new_shyghys: float = 0.0
    op_amount_val: Optional[float] = None  # «Сумма операции»; None если скрыта как "-"


@dataclass
class HalykStatementData:
    opening_balance: float
    opening_text: str              # "-61,68"
    closing_balance: float
    closing_text: str              # "1 204 086,06"
    total_kiri_s: float
    total_kiri_s_text: Optional[str]   # "3 236 278,85"; None если не найден в шапке (см. фолбэк ниже)
    total_shyghys: float
    total_shyghys_text: Optional[str]  # "-2 030 782,79"; None если не найден в шапке
    total_commission: float
    total_commission_text: Optional[str]  # "-1 200,00"; None если не найден в шапке
    transactions: List[HalykTransaction] = field(default_factory=list)
    # После пересчёта:
    new_total_kiri_s: float = 0.0
    new_total_shyghys: float = 0.0
    new_closing_balance: float = 0.0
    new_opening_balance: float = 0.0


# ─── Детектор формата ──────────────────────────────────────────────────────

def detect_halyk_format(doc) -> bool:
    """Возвращает True, если PDF — выписка Halyk Bank.

    "HSBKKZKX" — не только BIC Halyk как эмитента выписки, но и валидный
    БИК получателя/отправителя в ЛЮБОЙ чужой выписке (напр. Kaspi ИП),
    если контрагент обслуживается в Halyk. Голый substring-поиск даёт
    ложное срабатывание на такой выписке и уводит её в halyk-пайплайн,
    который не понимает её структуру и ничего не меняет. Поэтому исключаем
    документы, уже опознанные как Kaspi ИП (свои независимые маркеры
    "Лицевой счет:" + "Входящий остаток" — см. kaspi_ip_pdf_service.
    detect_kaspi_ip_format), у которых заведомо другой эмитент.
    """
    try:
        text = doc[0].get_text()
        if "Лицевой счет:" in text and "Входящий остаток" in text:
            return False
        return "HSBKKZKX" in text or "halykbank.kz" in text
    except Exception:
        return False


# ─── Парсинг ───────────────────────────────────────────────────────────────

def _group_words_by_y(page, tol: float = 2.0) -> List[Tuple[float, List[Tuple[float, str]]]]:
    """Группирует слова страницы по Y-координате (допуск ±tol).

    Слова сортируются по Y и накапливаются в текущей строке, пока их Y не
    отклонится от Y ПЕРВОГО слова строки (анкера) больше чем на tol. Раньше
    группировка шла через округление до сетки (round(y/tol)*tol) — если
    y-координаты двух слов одной визуальной строки лежали по разные стороны
    границы округления (напр. 0.99 и 1.01 при tol=2 округляются в разные
    бакеты 0 и 2), их раскидывало по двум соседним "строкам", и физическая
    строка транзакции (с двумя датами в начале) приходила в парсер неполной
    — транзакция тихо пропускалась (len(row) < 3 в parse_halyk_statement).
    Привязка к анкеру первого слова устойчива к этой границе: слова одной
    строки почти всегда лежат на одном baseline и попадают в один и тот же
    допуск от анкера независимо от его абсолютного значения.
    """
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    result: List[Tuple[float, List[Tuple[float, str]]]] = []
    current_anchor: Optional[float] = None
    current_row: List[Tuple[float, str]] = []
    for w in words:
        y = w[1]
        if current_anchor is None or abs(y - current_anchor) > tol:
            if current_row:
                result.append((current_anchor, sorted(current_row, key=lambda z: z[0])))
            current_anchor = y
            current_row = []
        current_row.append((w[0], w[4]))
    if current_row:
        result.append((current_anchor, sorted(current_row, key=lambda z: z[0])))
    return result


def _join_row(row: List[Tuple[float, str]]) -> str:
    return " ".join(t for _, t in row)


def _parse_amount(text: str) -> Optional[float]:
    """Парсит '299 374,05' или '-2 030 782,79' → float."""
    text = text.strip().replace("\xa0", " ").replace(" ", " ")
    sign = -1.0 if text.startswith("-") else 1.0
    digits = re.sub(r"[^0-9,]", "", text)
    digits = digits.replace(",", ".")
    try:
        return sign * float(digits)
    except Exception:
        return None


def _extract_header_value(lines: List[Tuple[float, str]], keyword: str) -> Tuple[Optional[float], Optional[str]]:
    """Ищет строку с ключевым словом и возвращает (float, raw_text) значение после него."""
    for y, row in lines:
        line_text = _join_row(row)
        if keyword in line_text:
            # Берём первое число после ключевого слова
            after = line_text[line_text.index(keyword) + len(keyword):]
            amounts = _AMT_RE.findall(after)
            if amounts:
                raw = amounts[0].strip()
                val = _parse_amount(raw)
                if val is not None:
                    return val, raw
    return None, None


def parse_halyk_statement(doc) -> HalykStatementData:
    """Извлекает транзакции и заголовочные данные из выписки Halyk Bank."""
    all_transactions: List[HalykTransaction] = []

    # Собираем заголовочные поля только с первой страницы
    page0_lines = _group_words_by_y(doc[0])

    # Казахский (legacy, одновалютный) / русский (nav, мультивалютный) варианты.
    opening_balance, opening_text = _extract_header_value(page0_lines, "қалдығы: ")
    if opening_balance is None:
        opening_balance, opening_text = _extract_header_value(page0_lines, "Кіріс қалдығы:")
    if opening_balance is None:
        opening_balance, opening_text = _extract_header_value(page0_lines, "Входящий остаток:")
    if opening_balance is None:
        opening_balance, opening_text = 0.0, "0,00"

    closing_balance, closing_text = _extract_header_value(page0_lines, "Шығыс қалдығы:")
    if closing_balance is None:
        closing_balance, closing_text = _extract_header_value(page0_lines, "Исходящий остаток:")
    if closing_balance is None:
        closing_balance, closing_text = 0.0, "0,00"

    # Барлығы: total_кіріс total_шығыс total_commission (legacy, одна строка)
    # Всего: (nav) — отдельная строка-заголовок, затем один ряд сумм НА КАЖДУЮ
    # валюту счёта; для скоринга нам нужен именно тенговый (KZT) ряд.
    #
    # Дефолт — None (а не 0.0/"0,00")! Если строку "Барлығы:"/"Всего:" не
    # удалось разобрать (неожиданный формат шапки), молчаливый 0.0 давал два
    # вредных эффекта: (1) HDR-ключ на запись строился бы как "HDR:" + digits
    # ("0,00") = "HDR:000" и мог случайно совпасть с любой ДРУГОЙ нулевой
    # ячейкой на странице, затерев её мусором; (2) below_balance_floor в
    # recalculate_halyk считался бы от заведомо заниженного (нулевого) общего
    # расхода, ослабляя защиту от ухода баланса в минус. Ниже, после разбора
    # транзакций, None-поля восстанавливаются суммой уже распознанных
    # транзакций (что математически корректно), но без "текста" — такое поле
    # НЕ участвует в побайтовой замене (см. process_halyk_pdf), а только в
    # арифметике (floor-проверки/дельты).
    total_kiri_s, total_kiri_s_text = None, None
    total_shyghys, total_shyghys_text = None, None
    total_commission, total_commission_text = None, None
    for idx, (y, row) in enumerate(page0_lines):
        line_text = _join_row(row)
        stripped = line_text.strip()
        if line_text.startswith("Барлығы:") or "Барлығы:" in line_text[:20]:
            amounts = _AMT_RE.findall(line_text)
            if len(amounts) >= 3:
                v0 = _parse_amount(amounts[0])
                v1 = _parse_amount(amounts[1])
                v2 = _parse_amount(amounts[2])
                if v0 is not None:
                    total_kiri_s = v0
                    total_kiri_s_text = amounts[0].strip()
                if v1 is not None:
                    total_shyghys = v1
                    total_shyghys_text = amounts[1].strip()
                if v2 is not None:
                    total_commission = v2
                    total_commission_text = amounts[2].strip()
            break
        if stripped.startswith("Всего:"):
            # Одновалютный счёт (напр. KZT-only "Зарплата" карта): суммы стоят
            # ПРЯМО на этой же строке, "Всего: 5 651 877,00 -5 650 948,20 -900,00" —
            # как у "Барлығы:" выше. Мультивалютный счёт вместо этого кладёт
            # "Всего:" отдельной строкой-заголовком, а суммы — на следующей
            # строке per-валюта ("KZT ...", "USD ...", ...). Раньше проверялась
            # ТОЛЬКО вторая раскладка (`stripped == "Всего:"`), из-за чего у
            # одновалютных nav-выписок total_kiri_s/total_shyghys оставались
            # дефолтными "0,00" и коллизировали в очереди замены с настоящими
            # нулевыми ячейками (лимиты, заблокированные суммы, нулевые
            # op_amount) — те получали чужое огромное значение при записи.
            amounts = _AMT_RE.findall(stripped)
            if len(amounts) >= 3:
                v0 = _parse_amount(amounts[0])
                v1 = _parse_amount(amounts[1])
                v2 = _parse_amount(amounts[2])
                if v0 is not None:
                    total_kiri_s = v0
                    total_kiri_s_text = amounts[0].strip()
                if v1 is not None:
                    total_shyghys = v1
                    total_shyghys_text = amounts[1].strip()
                if v2 is not None:
                    total_commission = v2
                    total_commission_text = amounts[2].strip()
                break
            for y2, row2 in page0_lines[idx + 1: idx + 12]:
                lt2 = _join_row(row2).strip()
                if lt2.startswith("KZT "):
                    amounts = _AMT_RE.findall(lt2)
                    if len(amounts) >= 3:
                        v0 = _parse_amount(amounts[0])
                        v1 = _parse_amount(amounts[1])
                        v2 = _parse_amount(amounts[2])
                        if v0 is not None:
                            total_kiri_s = v0
                            total_kiri_s_text = amounts[0].strip()
                        if v1 is not None:
                            total_shyghys = v1
                            total_shyghys_text = amounts[1].strip()
                        if v2 is not None:
                            total_commission = v2
                            total_commission_text = amounts[2].strip()
                    break
            break

    # Транзакции — по всем страницам
    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        lines = _group_words_by_y(page)
        last_tx: Optional[HalykTransaction] = None

        for y, row in lines:
            # Транзакционная строка начинается с двух дат
            if len(row) < 3:
                continue
            w0 = row[0][1]
            w1 = row[1][1] if len(row) > 1 else ""
            if not (_DATE_RE.match(w0) and _DATE_RE.match(w1)):
                # Возможно, перенос описания/счёта предыдущей транзакции. В nav
                # (мультивалютном) формате конкретный субсчёт-получатель кіріс/шығыс
                # колонок иногда явно помечен тегом "(KZT)"/"(USD)"/"(EUR)" на этой
                # строке-переносе — он достовернее, чем валюта самой op_amount
                # (например, у «Автоконвертации» op_amount в одной валюте, а списание
                # реально проведено по тенговому субсчёту).
                if last_tx is not None:
                    tag_m = _LEDGER_TAG_RE.search(_join_row(row))
                    if tag_m and tag_m.group(1) in _CCY_CODES:
                        last_tx.currency = tag_m.group(1)
                continue

            line_text = _join_row(row)
            op_date = w0
            proc_date = w1

            # Находим пару "op_amount + код валюты" (KZT/USD/EUR/TRY/...).
            # До неё = description, после = [кіріс, шығыс, comm, account].
            op_match = None
            for m in _OPAMT_CCY_RE.finditer(line_text):
                if m.group(2) in _CCY_CODES:
                    op_match = m
                    break

            if op_match is not None:
                before = line_text[:op_match.start()]
                after = line_text[op_match.end():]
                tx_currency = op_match.group(2)

                desc_start = len(op_date) + 1 + len(proc_date) + 1
                description = before[desc_start:].strip()

                amounts_after = _AMT_RE.findall(after)
                if len(amounts_after) < 3:
                    continue

                kiri_s_text = amounts_after[0].strip()
                shyghys_text = amounts_after[1].strip()

                kiri_s_val = _parse_amount(kiri_s_text) or 0.0
                shyghys_val = _parse_amount(shyghys_text) or 0.0

                # op_amount ("Сумма операции") иногда голый "-" (нав прячет её
                # для внутренних переводов, где она дублирует "Расход") — тогда
                # сверять/нормализовать не с чем, оставляем None.
                op_amount_raw = op_match.group(1)
                op_amount_val = _parse_amount(op_amount_raw) if op_amount_raw != "-" else None

            else:
                # Нет кода валюты рядом с op_amount (например, фиксированная
                # комиссия с op_amount=0 — «Пин-кодты...», «Ежемесячная комиссия»).
                amounts_all = _AMT_RE.findall(line_text)
                if len(amounts_all) < 4:
                    continue
                kiri_s_text = amounts_all[1].strip()
                shyghys_text = amounts_all[2].strip()
                kiri_s_val = _parse_amount(kiri_s_text) or 0.0
                shyghys_val = _parse_amount(shyghys_text) or 0.0
                description = ""
                tx_currency = "KZT"
                op_amount_val = None

            # Legacy (Kazakh, одновалютный): единая фраза покрывает почти все поступления.
            # Nav (Russian, мультивалютный): такой фразы нет — размечаем по категориям
            # поступления, только для KZT-операций (см. _NAV_INCOME_KEYWORDS выше).
            is_salary = "Қаражаттың шотқа түсуі" in description or (
                tx_currency == "KZT" and any(kw in description for kw in _NAV_INCOME_KEYWORDS)
            )
            is_seizure = "Оплата картотеки" in description

            tx = HalykTransaction(
                op_date=op_date,
                description=description,
                kiri_s=kiri_s_val,
                shyghys=shyghys_val,
                kiri_s_text=kiri_s_text,
                shyghys_text=shyghys_text,
                currency=tx_currency,
                op_currency=tx_currency,
                is_salary=is_salary,
                is_seizure=is_seizure,
                op_amount_val=op_amount_val,
            )
            all_transactions.append(tx)
            last_tx = tx

    print(f"[Halyk] Распознано транзакций: {len(all_transactions)}")
    salaries = [t for t in all_transactions if t.is_salary]
    seizures = [t for t in all_transactions if t.is_seizure]
    print(f"[Halyk] Зарплат: {len(salaries)}, изъятий: {len(seizures)}")

    # Диагностика для операторской видимости: is_salary матчится ТОЛЬКО по
    # ключевым словам (казахская фраза для legacy-формата, _NAV_INCOME_KEYWORDS
    # для nav-формата) — тот же класс хрупкости, что у _classify_debit_purpose
    # в kaspi_ip_pdf_service.py. Fallback безопасен (несовпадение → is_salary=
    # False → доход НЕ масштабируется, остаётся прежним — тождество баланса не
    # нарушается), но риск в другом: реальная зарплата с непривычной
    # формулировкой останется нетронутой при апскейле — итоговый средний доход
    # не дотянет до запрошенной цели (мягкая проблема эффективности, не баг
    # корректности). Печатаем сумму/образцы KZT-поступлений, не попавших в
    # is_salary, чтобы это было видно при разборе нового реального файла.
    unclassified_income = [
        t for t in all_transactions
        if t.currency == "KZT" and t.kiri_s > 0 and not t.is_salary
    ]
    if unclassified_income:
        total_income_kzt = sum(t.kiri_s for t in all_transactions if t.currency == "KZT" and t.kiri_s > 0) or 1.0
        unclass_amt = sum(t.kiri_s for t in unclassified_income)
        share = unclass_amt / total_income_kzt * 100
        samples = sorted({t.description[:60] for t in unclassified_income})[:3]
        print(
            f"[Halyk] ⚠️ Неклассифицированные KZT-поступления (не попали в is_salary): "
            f"{len(unclassified_income)} шт., Σ={unclass_amt:,.2f} ₸ ({share:.1f}% от всех поступлений). "
            f"Не масштабируются при апскейле (безопасно для баланса, но итоговый доход может "
            f"не дотянуть до цели). Примеры описаний: {samples}"
        )

    # Фолбэк для итогов шапки, не найденных выше ("Барлығы:"/"Всего:" в
    # неожиданном формате) — считаем их суммой уже распознанных транзакций.
    # *_text намеренно остаётся None: у такого значения нет известного места
    # в потоке байт, поэтому process_halyk_pdf не пытается его переписать
    # (см. комментарий у объявления total_kiri_s выше) — только использует
    # число для арифметики (floor-проверки в recalculate_halyk).
    if total_kiri_s is None:
        total_kiri_s = sum(t.kiri_s for t in all_transactions if t.kiri_s > 0 and t.currency == "KZT")
        print(f"[Halyk] ⚠️ Итого кіріс не найдено в шапке, взято суммой транзакций: {total_kiri_s:,.2f}")
    if total_shyghys is None:
        total_shyghys = -sum(abs(t.shyghys) for t in all_transactions if t.shyghys < 0 and t.currency == "KZT")
        print(f"[Halyk] ⚠️ Итого шығыс не найдено в шапке, взято суммой транзакций: {total_shyghys:,.2f}")
    if total_commission is None:
        total_commission = 0.0
        print("[Halyk] ⚠️ Итого комиссия не найдено в шапке, принято 0,00")

    return HalykStatementData(
        opening_balance=opening_balance,
        opening_text=opening_text or "0,00",
        closing_balance=closing_balance,
        closing_text=closing_text or "0,00",
        total_kiri_s=total_kiri_s,
        total_kiri_s_text=total_kiri_s_text,
        total_shyghys=total_shyghys,
        total_shyghys_text=total_shyghys_text,
        total_commission=total_commission,
        total_commission_text=total_commission_text,
        transactions=all_transactions,
    )


# ─── Пересчёт ──────────────────────────────────────────────────────────────

def recalculate_halyk(stmt: HalykStatementData, target_monthly_income: float) -> HalykStatementData:
    """
    Масштабирует зарплатные поступления до target_monthly_income.

    Единый K на весь период (global_K) с адаптивным коридором вокруг него —
    НЕ чистое помесячное K_month = target/доход_месяца (см. комментарий
    ниже, у блока "K-коэффициент на месяц"). Судебные изъятия («Оплата
    картотеки») того же дня масштабируются пропорционально тому же
    коэффициенту.
    """
    # ── Контракт: new_* ВСЕГДА осмысленны, «ничего не менять» = копия оригинала.
    # Поля new_kiri_s/new_shyghys имеют дефолт 0.0, а писатель ставит ячейку в
    # очередь по условию abs(new_X - X) > 0.005 — то есть любой путь, который
    # вышел из функции, не заполнив их, заставляет писатель ФИЗИЧЕСКИ записать
    # «0,00» в каждую ненулевую ячейку. Ровно это и случилось на реальном файле
    # (пособия НАО, ни одной строки с is_salary=True → ранний return ниже): все
    # 17 расходов были переписаны в 0,00 при неизменной шапке, Δ=863 200 ₸.
    # Инициализируем до любого ветвления, чтобы никакой будущий ранний выход не
    # мог повторить это снова.
    for _tx in stmt.transactions:
        _tx.new_kiri_s = _tx.kiri_s
        _tx.new_shyghys = _tx.shyghys

    # Группируем зарплаты по месяцу MM.YYYY
    month_salary: Dict[str, float] = defaultdict(float)
    for tx in stmt.transactions:
        if tx.is_salary and tx.kiri_s > 0:
            month_key = tx.op_date[3:]  # "MM.YYYY" из "DD.MM.YYYY"
            month_salary[month_key] += tx.kiri_s

    # Реалистичность масштабированных сумм: nav-формат (русские Kiri-keywords,
    # мультивалютные счета) в реальности НИКОГДА не показывает копейки в
    # зарплатных/изъятых суммах (проверено на 3 реальных файлах — все kiri_s
    # целые тенге), а legacy-формат (казахская фраза) — РЕАЛЬНО показывает
    # копейки (проверено на реальном файле: 299 374,05 ₸, 306 844,25 ₸). Плоский
    # round(x, 2) после масштабирования+шума даёт произвольные копейки на
    # ЛЮБОМ формате — на nav-файле это выглядит как "43 338 903,25 ₸", копейки
    # там, где реальная выписка их не показывает никогда, что визуально выдаёт
    # результат как посчитанный по формуле, а не настоящую зарплатную проводку.
    # Решаем по фактическим данным ЭТОГО файла (не по названию формата — надёжнее
    # для не встречавшихся вариантов): если ни одна реальная зарплата не имеет
    # копеек, округляем масштабированные суммы до «человеческого» шага (та же
    # функция, что и в Kaspi Gold, — реальные суммы кратны 50/100/500/1000 в её
    # ступенях, проверено на реальных цифрах nav-файла). Если копейки в
    # оригинале ЕСТЬ (legacy), round(x, 2) остаётся как есть — уже реалистично.
    _salary_has_cents = any(
        abs(tx.kiri_s - round(tx.kiri_s)) > 0.001
        for tx in stmt.transactions if tx.is_salary and tx.kiri_s > 0
    )

    def _realistic_round(val: float, original: Optional[float] = None) -> float:
        return round(val, 2) if _salary_has_cents else _round_to_natural(val, original=original)

    if not month_salary:
        # Возвращать выписку без изменений нельзя: снаружи это неотличимо от
        # успешной обработки — пользователь скачивает собственный исходник.
        # Отдаём явный отказ со списком формулировок, которые не распознались.
        stmt.new_total_kiri_s = stmt.total_kiri_s
        stmt.new_total_shyghys = stmt.total_shyghys
        stmt.new_closing_balance = stmt.closing_balance
        stmt.new_opening_balance = stmt.opening_balance
        _unmatched, _seen = [], set()
        _unmatched_sum = 0.0
        for tx in stmt.transactions:
            if tx.kiri_s > 0 and tx.currency == "KZT" and not tx.is_salary:
                _unmatched_sum += tx.kiri_s
                if tx.description not in _seen:
                    _seen.add(tx.description)
                    _unmatched.append(tx.description)
        print(f"[Halyk] Масштабируемый доход не найден: {len(_seen)} формулировок, "
              f"Σ={_unmatched_sum:,.2f} ₸ — отказ вместо тихого возврата оригинала")
        raise NoScalableIncomeError(_unmatched, _unmatched_sum)

    n_months = len(month_salary)
    current_monthly_avg = sum(month_salary.values()) / n_months
    total_out = abs(stmt.total_shyghys) + abs(stmt.total_commission)
    min_target = 0.0  # используется только для сообщения ПРОВЕРКИ 3, если понижения не было
    stmt.new_opening_balance = stmt.opening_balance

    # ── Floor-проверки — только если это занижение (target < текущего ср.) ──
    # Идентично трём проверкам в pdf_service_downscale (Kaspi Gold), адаптировано
    # под поля Halyk: opening_balance / total_shyghys / total_commission.
    if current_monthly_avg > 0 and target_monthly_income < current_monthly_avg:
        # ПРОВЕРКА 1: баланс не должен уйти в минус
        required_total_income = total_out - stmt.opening_balance + _SAFETY_MARGIN
        min_target = required_total_income / n_months if required_total_income > 0 else 0.0
        if target_monthly_income < min_target:
            raise IncomeTooLowError(
                min_target_monthly_income=min_target,
                current_expense=total_out,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="below_balance_floor",
                message=(
                    f"Слишком низкий целевой доход. При расходах "
                    f"{total_out:,.0f} ₸ и стартовом балансе "
                    f"{stmt.opening_balance:,.0f} ₸ за {n_months} мес "
                    f"минимально возможный ср. доход = "
                    f"{min_target:,.0f} ₸/мес "
                    f"(желаемый ≥ {min_target * _INCOME_K:,.0f} ₸/мес)."
                ),
            )

        # ПРОВЕРКА 2: не более 70% занижения от текущего среднего
        floor_aggressive = current_monthly_avg * _MAX_DOWNSCALE_FACTOR
        if target_monthly_income < floor_aggressive:
            raise IncomeTooLowError(
                min_target_monthly_income=floor_aggressive,
                current_expense=total_out,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="too_aggressive",
                message=(
                    f"Слишком резкое занижение: запрошено "
                    f"{target_monthly_income:,.0f} ₸/мес, текущий ср. доход "
                    f"{current_monthly_avg:,.0f} ₸/мес. Минимум "
                    f"({_MAX_DOWNSCALE_FACTOR * 100:.0f}% от текущего) = "
                    f"{floor_aggressive:,.0f} ₸/мес."
                ),
            )

    # K-коэффициент на месяц — адаптивный коридор вокруг единого global_K,
    # НЕ чистое помесячное K_month = target/доход_месяца (как было раньше).
    # Чистое помесячное K гарантированно выравнивает каждый месяц ровно к
    # target — тот же баг, что был в Kaspi Gold (см. CLAUDE.md, "Исправлено
    # 2026-08-03: помесячное выравнивание убивало естественный разброс
    # дохода") — но здесь ISI ЖЁСТКАЯ проверка (validate_halyk, порог 0.75,
    # выше чем 0.60 у Kaspi ИП), и чистый единый K (без коридора вообще)
    # рискует не пройти её на выписке с большим естественным разбросом —
    # именно это и произошло у Kaspi ИП на реальном файле (ISI упал до
    # 0.1987 при чистом едином K). Вместо жёстко подобранной константы вроде
    # _MAX_MONTH_K_SPREAD=3.5 в kaspi_ip_pdf_service (откалибрована на ОДНОМ
    # конкретном файле) — здесь коридор подбирается АДАПТИВНО под каждую
    # конкретную выписку: пробуем от минимального выравнивания (spread=1.0,
    # чистый global_K, максимум реализма) и расширяем его, пока прогнозный
    # ISI (без шума ±3%, только по помесячным суммам) не пройдёт порог с
    # запасом. Гарантированно сходится: при очень большом spread коридор
    # перестаёт что-либо ограничивать → чистое помесячное выравнивание, тот
    # же ISI≈1, что и в старом поведении.
    # НЕ проверено на реальном Halyk-файле (в этом чекауте фикстур нет,
    # tests/fixtures/ гитигнорено) — при появлении реального файла прогнать
    # validate_halyk и, если фактический ISI после шума ближе к порогу, чем
    # прогноз, увеличить _ISI_SAFETY_MARGIN.
    _ISI_TARGET = 0.75
    _ISI_SAFETY_MARGIN = 0.03
    _SPREAD_CANDIDATES = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0, 50.0, 1e6)

    def _predicted_isi(spread: float) -> float:
        vals = []
        for mk, total in month_salary.items():
            if total <= 0:
                vals.append(target_monthly_income)
                continue
            raw_k = target_monthly_income / total
            lo, hi = global_K / spread, global_K * spread
            vals.append(total * max(lo, min(hi, raw_k)))
        mu = sum(vals) / len(vals)
        if mu <= 0:
            return 1.0
        sigma = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
        return max(0.0, 1.0 - sigma / mu)

    global_K = target_monthly_income / current_monthly_avg
    chosen_spread = _SPREAD_CANDIDATES[-1]
    for _s in _SPREAD_CANDIDATES:
        if _predicted_isi(_s) >= _ISI_TARGET + _ISI_SAFETY_MARGIN:
            chosen_spread = _s
            break
    print(
        f"[Halyk] Коридор K: global_K={global_K:.4f}, подобран spread=±{chosen_spread:g} "
        f"(прогноз ISI={_predicted_isi(chosen_spread):.4f}, порог {_ISI_TARGET})"
    )

    month_k: Dict[str, float] = {}
    for month_key, total in month_salary.items():
        if total > 0:
            raw_k = target_monthly_income / total
            lo, hi = global_K / chosen_spread, global_K * chosen_spread
            month_k[month_key] = max(lo, min(hi, raw_k))
        else:
            month_k[month_key] = global_K

    print("[Halyk] Коэффициенты по месяцам:")
    for m, k in sorted(month_k.items()):
        print(f"  {m}: K={k:.4f} (доход={month_salary[m]:,.0f}₸ → цель={target_monthly_income:,.0f}₸)")

    # Применяем K к каждой транзакции
    # Карта: дата → K (для изъятий того же дня)
    date_k: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary and tx.kiri_s > 0:
            month_key = tx.op_date[3:]
            k = month_k.get(month_key, 1.0)
            noise = random.uniform(-_NOISE, _NOISE)
            new_val = _realistic_round(tx.kiri_s * k * (1 + noise), original=tx.kiri_s)
            tx.new_kiri_s = new_val
            date_k[tx.op_date] = k  # последний K для этой даты
            print(f"  [SAL] {tx.op_date} {tx.kiri_s:,.2f} → {new_val:,.2f} (K={k:.4f})")
        else:
            tx.new_kiri_s = tx.kiri_s

    for tx in stmt.transactions:
        if tx.is_seizure and tx.shyghys < 0:
            k = date_k.get(tx.op_date, month_k.get(tx.op_date[3:], 1.0))
            noise = random.uniform(-_NOISE, _NOISE)
            new_val = -_realistic_round(abs(tx.shyghys) * k * (1 + noise), original=abs(tx.shyghys))
            tx.new_shyghys = new_val
            print(f"  [SEIZ] {tx.op_date} {tx.shyghys:,.2f} → {new_val:,.2f} (K={k:.4f})")
        elif (
            tx.currency == "KZT"
            # «Сумма операции» должна быть в ТОЙ ЖЕ валюте, что и субсчёт
            # списания, иначе разница между ними — курс, а не комиссия.
            # Реальный случай (HALYKformat2): «Автоконвертация» с op_amount
            # -8,85 USD и Расходом -4 548,02 ₸ (курс 513,9 ₸/$) нормализовалась
            # в -8,85 ₸ — расход занижался, а в выписке оставалась строка, где
            # 8,85 USD равны 8,85 ₸. Гейт по tx.currency этого не ловит: на
            # строке-переносе она перезаписана ledger-тегом «(KZT)».
            and tx.op_currency == tx.currency
            and tx.shyghys < 0
            and tx.op_amount_val is not None
            and abs(abs(tx.op_amount_val) - abs(tx.shyghys)) > 0.005
        ):
            # «Сумма операции» и «Расход в валюте счета» расходятся на скрытую
            # комиссию, зашитую прямо в шаблон (напр. -103 000,00 / -103 200,00),
            # хотя колонка «Комиссия» показывает 0,00 — деньги «теряются в
            # никуда» и выписка выглядит внутренне противоречивой. Приводим
            # Расход к Сумме операции (единственному полю, которое реально
            # означает сумму перевода); дельта автоматически уходит в
            # new_total_shyghys/new_closing_balance ниже, как и для SEIZURE/SALARY.
            new_val = -abs(tx.op_amount_val)
            tx.new_shyghys = new_val
            print(f"  [FEE-NORM] {tx.op_date} Расход {tx.shyghys:,.2f} → {new_val:,.2f} (=Сумма операции)")
        else:
            tx.new_shyghys = tx.shyghys
            if (
                tx.currency == "KZT"
                and tx.op_currency != tx.currency
                and tx.shyghys < 0
                and tx.op_amount_val
                and abs(abs(tx.op_amount_val) - abs(tx.shyghys)) > 0.005
            ):
                # Наблюдаемость: строка выглядит как кандидат на FEE-NORM, но
                # отсеяна валютным гейтом выше. Печатаем подразумеваемый курс —
                # если он вдруг окажется ≈1, значит валюты размечены неверно и
                # это на самом деле комиссия, которую мы теперь пропускаем.
                rate = abs(tx.shyghys) / abs(tx.op_amount_val)
                print(
                    f"  [FX-SKIP] {tx.op_date} Расход {tx.shyghys:,.2f} ₸ vs "
                    f"Сумма операции {tx.op_amount_val:,.2f} {tx.op_currency} "
                    f"(курс ≈{rate:,.2f}) — не комиссия, оставляем как есть"
                )

    # ПРОВЕРКА 3 (post-check): цепочка running balance нигде не должна уходить
    # в минус. Идентично третьей проверке в pdf_service_downscale — если после
    # ±3% шума где-то образовался минус, точечно поднимаем зарплату (×1.02, до
    # 5 итераций). В отличие от ПРОВЕРОК 1/2 запускается БЕЗУСЛОВНО (как в
    # kaspi_ip_pdf_service): при сильно неравномерном доходе по месяцам K для
    # отдельных месяцев может оказаться < 1 даже когда target_monthly_income в
    # среднем ВЫШЕ текущего — такой месяц занижается локально и может увести
    # баланс в минус, хотя статистически это «повышение».
    #
    # Требуем баланс ≥ 0 везде, а не только "не хуже родного" — даже если в
    # исходной (нередактированной) выписке уже был свой провал: итоговый
    # документ должен сам по себе проходить проверку баланса.
    #
    # Подъём зарплаты работает только для провалов НА/ПОСЛЕ даты хотя бы одной
    # зарплатной проводки — зарплата позже по времени не может задним числом
    # поднять уже пройденный минимум. Если после 5 итераций буста провал
    # остаётся (значит он целиком ДО первой зарплаты периода), точечно поднимаем
    # входящий остаток ровно настолько, чтобы убрать именно этот провал —
    # это сдвигает всю цепочку running balance целиком, включая точки до
    # первой зарплаты, где сам буст зарплаты бессилен.
    def _sort_key(t: HalykTransaction):
        try:
            return datetime.strptime(t.op_date, "%d.%m.%Y")
        except ValueError:
            return datetime.max

    sorted_txs = sorted(stmt.transactions, key=_sort_key)

    def _min_running_balance(use_scaled: bool) -> float:
        opening = stmt.new_opening_balance if use_scaled else stmt.opening_balance
        # Минимум — на границах дней (см. _halyk_dayend_min_rb): порядок
        # нескольких транзакций одной даты произволен (нет времени в op_date),
        # так что внутридневной дип, покрытый до конца того же дня, не должен
        # ложно провоцировать буст зарплаты / подъём входящего остатка ниже.
        min_rb = _halyk_dayend_min_rb(sorted_txs, opening, use_scaled)
        rb_final = opening
        for t in sorted_txs:
            if t.currency != "KZT":
                continue
            k_val = t.new_kiri_s if use_scaled else t.kiri_s
            s_val = t.new_shyghys if use_scaled else t.shyghys
            rb_final = round(rb_final + k_val - abs(s_val), 2)
        # Комиссия (total_commission) не привязана к отдельным проводкам — в
        # выписке она только агрегирована в шапке, отдельных транзакций-комиссий
        # в txs нет. Списываем её ОДНИМ движением в КОНЦЕ цепочки, а не авансом
        # в начале: банковская абонплата не создаёт «задним числом» овердрафт в
        # середине периода, а закрывающий баланс её уже включает. Front-loading
        # давал фантомный минус ~на величину комиссии и заставлял ПРОВЕРКУ 3 зря
        # поднимать входящий остаток (проверено на реальном nav.pdf).
        rb_final = round(rb_final - abs(stmt.total_commission), 2)
        if rb_final < min_rb:
            min_rb = rb_final
        return min_rb

    native_min_rb = _min_running_balance(use_scaled=False)

    min_rb = _min_running_balance(use_scaled=True)
    if min_rb < -1.0:
        print(f"\n[Halyk] ⚠️ После пересчёта min_rb={min_rb:,.2f} (было {native_min_rb:,.2f}), поднимаем зарплату")
        for attempt in range(5):
            for tx in stmt.transactions:
                if tx.is_salary and tx.kiri_s > 0:
                    # original=tx.kiri_s (истинный оригинал) — не уже
                    # округлённый tx.new_kiri_s с прошлой итерации, иначе
                    # шаг «сползал» бы с реальной круглости на каждой
                    # итерации ×1.02 (тот же класс фикса, что в Kaspi Gold).
                    tx.new_kiri_s = _realistic_round(tx.new_kiri_s * 1.02, original=tx.kiri_s)
            min_rb = _min_running_balance(use_scaled=True)
            if min_rb >= -1.0:
                print(f"[Halyk] ✅ Скорректировано за {attempt + 1} итераций, min_rb={min_rb:,.2f}")
                break

        if min_rb < -1.0:
            # Буст зарплаты не помог — провал целиком до первой зарплаты.
            # Поднимаем входящий остаток на величину провала (+запас 1 ₸).
            bump = round(-min_rb + 1.0, 2)
            old_opening = stmt.new_opening_balance
            stmt.new_opening_balance = round(stmt.new_opening_balance + bump, 2)
            min_rb = _min_running_balance(use_scaled=True)
            print(
                f"[Halyk] ⚠️ Провал остаётся ДО первой зарплаты — поднимаем "
                f"входящий остаток на {bump:,.2f} ₸ "
                f"({old_opening:,.2f} → {stmt.new_opening_balance:,.2f}), min_rb={min_rb:,.2f}"
            )

        if min_rb < -1.0:
            new_min = max(min_target, target_monthly_income) * 1.10
            raise IncomeTooLowError(
                min_target_monthly_income=new_min,
                current_expense=total_out,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="post_check_negative_balance",
                message=(
                    f"Не удалось удержать неотрицательный баланс при "
                    f"{target_monthly_income:,.0f} ₸/мес "
                    f"(min_rb={min_rb:,.0f} ₸). Минимально рекомендуемый "
                    f"доход: {new_min:,.0f} ₸/мес."
                ),
            )

    # Пересчитываем дельты с нуля — после возможной корректировки в ПРОВЕРКЕ 3
    delta_kiri_s = sum(tx.new_kiri_s - tx.kiri_s for tx in stmt.transactions)
    delta_shyghys = sum(tx.new_shyghys - tx.shyghys for tx in stmt.transactions)
    delta_opening = stmt.new_opening_balance - stmt.opening_balance

    stmt.new_total_kiri_s = stmt.total_kiri_s + delta_kiri_s
    stmt.new_total_shyghys = stmt.total_shyghys + delta_shyghys
    stmt.new_closing_balance = stmt.closing_balance + delta_kiri_s + delta_shyghys + delta_opening

    print(f"\n[Halyk] Итого кіріс: {stmt.total_kiri_s:,.2f} → {stmt.new_total_kiri_s:,.2f}")
    print(f"[Halyk] Итого шығыс: {stmt.total_shyghys:,.2f} → {stmt.new_total_shyghys:,.2f}")
    print(f"[Halyk] Кіріс қалдығы: {stmt.opening_balance:,.2f} → {stmt.new_opening_balance:,.2f}")
    print(f"[Halyk] Шығыс қалдығы: {stmt.closing_balance:,.2f} → {stmt.new_closing_balance:,.2f}")
    return stmt


# ─── Вшивание недостающих глифов цифр в Bold-subset шрифт ──────────────────
# См. docs/superpowers/specs/2026-08-05-halyk-bold-glyph-embedding-design.md.
# Заменяет собой (частично) необходимость подмены Bold->Regular в
# replace_callback ниже: если патч удаётся, avail_cids_map после него уже
# содержит нужный CID, и needs_switch там просто не сработает — остальной
# код (needs_switch, retry-перебор шума, [guard]-репортинг) не меняется и
# остаётся страховкой на случай отказа gate'а.


def _try_patch_bold_digit_glyphs(
    ff2_bytes: bytes,
    digit_cids: Dict[str, str],
) -> Optional[Tuple[bytes, Dict[str, float]]]:
    """Пытается вписать в Bold-subset шрифт недостающие глифы цифр 0-9.

    digit_cids — {цифра: CID в виде 4-символьного hex}, тот же формат, что
    ключи avail_cids_map (обычно {'0': '0013', ..., '9': '001C'}, но
    вычисляется вызывающей стороной из FROM_UNICODE, а не жёстко здесь).

    Возвращает (новые байты FontFile2, {cid_hex: 500.0}) для реально
    допатченных цифр, либо None — если патчить нечего, или "gate" не
    позволяет доверять зашитым эталонным глифам для ЭТОГО конкретного
    файла (см. ниже). None означает «ничего не меняли», вызывающая сторона
    не отклоняется от старого поведения.
    """
    try:
        # unitsPerEm — сверяем через head-таблицу, переиспользуя разбор directory
        # из pdf_service._ttf_table_dir (избегаем дублирования).
        table_dir = _ttf_table_dir(ff2_bytes)
        if "head" not in table_dir:
            return None
        head_offset, _ = table_dir["head"]
        units_per_em = struct.unpack(">H", ff2_bytes[head_offset + 18:head_offset + 20])[0]
        if units_per_em != SOURCE_UNITS_PER_EM:
            return None

        missing_digits = []
        verified_match = False

        for digit in "0123456789":
            cid_hex = digit_cids.get(digit)
            if cid_hex is None:
                continue
            gid = int(cid_hex, 16)
            existing = _read_truetype_glyph(ff2_bytes, gid)
            baked = DIGIT_GLYPHS[digit]
            if existing == b"":
                missing_digits.append(digit)
                continue
            # Present digit — сверяем с эталоном как gate ("сначала проверь,
            # потом доверяй"): existing может быть на 1 байт длиннее (паддинг
            # до чётной длины внутри glyf-таблицы), поэтому сравниваем по
            # префиксу и требуем, чтобы хвост был нулевым И длина delta ≤ 1.
            n = len(baked)
            len_delta = len(existing) - n
            if (
                len_delta in (0, 1)
                and existing[:n] == baked
                and all(b == 0 for b in existing[n:])
            ):
                verified_match = True
            else:
                return None  # другой мастер-шрифт — не доверяем НИЧЕМУ

        if not verified_match or not missing_digits:
            return None

        glyph_patches = {int(digit_cids[d], 16): DIGIT_GLYPHS[d] for d in missing_digits}
        patched = _patch_truetype_glyphs(ff2_bytes, glyph_patches)
        added_widths = {digit_cids[d]: DIGIT_WIDTH_1000 for d in missing_digits}
        return patched, added_widths
    except Exception as exc:  # noqa: BLE001 — любой сбой здесь ЧИСТО fallback, не проброс
        print(f"[Halyk] Патч глифов Bold не применён ({exc.__class__.__name__}: {exc}) "
              f"— используется старое поведение (подмена шрифта/перебор шума).")
        return None


# ─── Сырая замена байт ─────────────────────────────────────────────────────

def _process_halyk_pdf_once(
    input_bytes: bytes, target_monthly_income: float
) -> Tuple[bytes, int, Dict[int, Dict[str, float]]]:
    """Один проход обработки. Возвращает (результат, число подмен шрифта,
    вшитые глифы).

    Второй элемент — сколько раз пришлось нарисовать число ЧУЖИМ (Regular)
    шрифтом вместо жирного, потому что в жирном subset'е не оказалось нужного
    глифа (см. `needs_switch` ниже). Вызывающая обёртка `process_halyk_pdf`
    использует его, чтобы перебрать шум и по возможности получить результат
    вообще без подмен.

    Третий элемент — {cid_xref: {cid_hex: width}} для Bold-шрифтов, в которые
    реально были вшиты недостающие глифы цифр в этом прогоне (Task 3/4) —
    используется только для отчётности автотестов (LAST_RUN_INFO), не
    прод-логикой.
    """
    from collections import deque as _deque

    doc = fitz.open(stream=input_bytes, filetype="pdf")
    TO_UNICODE, FROM_UNICODE = build_dynamic_cmap(doc)
    # Если dynamic CMap не вернул корректные CID для цифр (fallback-путь может
    # давать неверные коды из-за дублирования CID в нескольких CMap-стримах),
    # применяем эмпирически верифицированный маппинг Halyk ArialMT.
    if not all(FROM_UNICODE.get(ch) == f'{0x0013 + i:04X}' for i, ch in enumerate('0123456789')):
        for _i, _ch in enumerate('0123456789'):
            FROM_UNICODE[_ch] = f'{0x0013 + _i:04X}'
        FROM_UNICODE[','] = '000F'
        FROM_UNICODE[' '] = '0003'
        FROM_UNICODE['.'] = '0011'

    def hex_to_text(hex_str: str) -> str:
        res = ""
        for i in range(0, len(hex_str), 4):
            chunk = hex_str[i:i + 4]
            res += TO_UNICODE.get(chunk, "?")
        return res

    def text_to_hex(s: str) -> str:
        res = ""
        for c in s:
            res += FROM_UNICODE.get(c, "0000")
        return res

    stmt = parse_halyk_statement(doc)
    stmt = recalculate_halyk(stmt, target_monthly_income)

    # ── Detect Bold /F aliases PER content stream (needed before doc.close) ─
    # F0/F1 assignments differ between pages, so we build a per-xref mapping.
    # Заодно вытаскиваем РЕАЛЬНЫЕ ширины глифов (цифра/пробел, в 1000-х em) из
    # /W-массива каждого шрифта — нужны для X-сдвига при замене чисел. Шрифт
    # в nav-формате (Times New Roman CID) и в legacy (ArialMT) даёт разные
    # ширины на тот же кегль, единой захардкоженной константы недостаточно.
    _digit_cid = FROM_UNICODE.get("0")
    _space_cid = FROM_UNICODE.get("\xa0")
    _comma_cid = FROM_UNICODE.get(",")
    _xref_bold_f: Dict[int, set] = {}    # content_xref → set of bold F-names
    _xref_regular_f: Dict[int, str] = {} # content_xref → regular F-name
    _xref_digit_w: Dict[int, Dict[str, float]] = {}  # content_xref → {F-name: digit width/1000}
    _xref_space_w: Dict[int, Dict[str, float]] = {}  # content_xref → {F-name: space width/1000}
    _xref_comma_w: Dict[int, Dict[str, float]] = {}  # content_xref → {F-name: comma width/1000}
    # Множество CID (hex, 4 симв., uppercase), реально присутствующих в subset
    # каждого шрифта — ключи /W-массива CIDFont'а. Subset жирного шрифта
    # содержит ТОЛЬКО те глифы, что были в оригинальном документе жирным: если
    # в исходных «Всего»/балансах не встречалась, скажем, цифра «3», её глифа
    # в шрифте нет, и наша замена с этой цифрой отрисуется пустым «.notdef».
    # Нужно, чтобы такие токены переключать на Regular-шрифт (там глиф есть).
    _xref_avail_cids: Dict[int, Dict[str, set]] = {}  # content_xref → {F-name: {cid_hex}}
    # content_xref → {F-name: (cid_font_xref, FontFile2_xref, ToUnicode_xref)}
    # — только для Bold-шрифтов, собирается заодно с _page_avail_cids ниже;
    # используется ниже (до doc.close()) для построения множества уникальных
    # троек шрифтов, которые нужно попытаться допатчить недостающими глифами
    # цифр. ToUnicode_xref — объект CMap-потока Type0-обёртки (не CIDFont-
    # потомка): без него вписанный глиф рисуется корректно, но текстовый слой
    # (извлечение текста/копипаст/поиск, а также ЛЮБАЯ проверка в этом
    # проекте, читающая PDF через fitz.get_text — все, т.к. они не парсят
    # глиф-контуры) для этого CID останется пустым — глиф "невидим" для
    # текста. None, если у конкретного Bold-шрифта нет /ToUnicode вовсе.
    _xref_bold_ff2: Dict[int, Dict[str, Tuple[int, int, Optional[int]]]] = {}
    for _pn in range(len(doc)):
        _page_contents = doc[_pn].get_contents()
        _pobj = doc.xref_object(doc[_pn].xref)
        _page_bold_f: set = set()
        _page_regular_f: str = "F0"
        _page_digit_w: Dict[str, float] = {}
        _page_space_w: Dict[str, float] = {}
        _page_comma_w: Dict[str, float] = {}
        _page_avail_cids: Dict[str, set] = {}
        _page_bold_ff2: Dict[str, Tuple[int, int, Optional[int]]] = {}  # F-name -> (cid_xref, ff2_xref, tounicode_xref)
        for _fn, _fx in re.findall(r"/F(\d+)\s+(\d+)\s+0\s+R", _pobj):
            try:
                _fobj = doc.xref_object(int(_fx))
                _bm = re.search(r"/BaseFont\s*/(\S+)", _fobj)
                if _bm:
                    _bname = _bm.group(1)
                    if "Bold" in _bname or ",B" in _bname or "bold" in _bname:
                        _page_bold_f.add("F" + _fn)
                    else:
                        _page_regular_f = "F" + _fn

                _desc_m = re.search(r"/DescendantFonts\s*\[\s*(\d+)\s+0\s+R", _fobj)
                if _desc_m:
                    _cidobj = doc.xref_object(int(_desc_m.group(1)))
                    _w_m = re.search(r"/W\b", _cidobj)
                    if _w_m:
                        _bracket_start = _cidobj.find("[", _w_m.end())
                        if _bracket_start >= 0:
                            _w_widths = _parse_cid_widths(_extract_bracketed(_cidobj, _bracket_start))
                            _page_avail_cids["F" + _fn] = {
                                f"{_cid:04X}" for _cid in _w_widths
                            }
                            if _digit_cid is not None:
                                _dw = _w_widths.get(int(_digit_cid, 16))
                                if _dw is not None:
                                    _page_digit_w["F" + _fn] = _dw
                            if _space_cid is not None:
                                _sw = _w_widths.get(int(_space_cid, 16))
                                if _sw is not None:
                                    _page_space_w["F" + _fn] = _sw
                            if _comma_cid is not None:
                                _cw = _w_widths.get(int(_comma_cid, 16))
                                if _cw is not None:
                                    _page_comma_w["F" + _fn] = _cw
                    if "Bold" in _bname or ",B" in _bname or "bold" in _bname:
                        _fd_m2 = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", _cidobj)
                        if _fd_m2:
                            _fdobj2 = doc.xref_object(int(_fd_m2.group(1)))
                            _ff2_m2 = re.search(r"/FontFile2\s+(\d+)\s+0\s+R", _fdobj2)
                            if _ff2_m2:
                                _tu_m2 = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", _fobj)
                                _tu_xref2 = int(_tu_m2.group(1)) if _tu_m2 else None
                                _page_bold_ff2["F" + _fn] = (
                                    int(_desc_m.group(1)), int(_ff2_m2.group(1)), _tu_xref2,
                                )
            except Exception:
                pass
        for _cx in _page_contents:
            _xref_bold_f[_cx] = _page_bold_f
            _xref_regular_f[_cx] = _page_regular_f
            _xref_digit_w[_cx] = _page_digit_w
            _xref_space_w[_cx] = _page_space_w
            _xref_comma_w[_cx] = _page_comma_w
            _xref_avail_cids[_cx] = _page_avail_cids
            _xref_bold_ff2[_cx] = _page_bold_ff2

    # ── Карта page→xref (для порядка обработки) ──────────────────────────
    page_xrefs = [doc[i].get_contents() for i in range(len(doc))]
    # Ширина листа — для контроля переполнения правого края шапки (см.
    # overflow-ужатие шрифта в replace_callback): широкий «Исходящий остаток»
    # в строке «…в валюте: <сумма>» выезжает за кромку листа.
    _page_width = max((doc[i].rect.width for i in range(len(doc))), default=595.276)

    # Множество уникальных пар (cid_font_xref, FontFile2_xref) по всему
    # документу — один и тот же Bold-шрифт обычно встречается на каждой
    # странице под одним и тем же именем ресурса, патчить его нужно только
    # один раз. Собирается ДО doc.close(), т.к. читает doc.xref_object() выше.
    _bold_font_pairs: set = set()
    for _m in _xref_bold_ff2.values():
        _bold_font_pairs.update(_m.values())
    # digit_cids для gate _try_patch_bold_digit_glyphs — тот же CID-маппинг,
    # что уже вычислен для всего документа через FROM_UNICODE выше.
    _digit_cids = {ch: FROM_UNICODE[ch] for ch in "0123456789" if ch in FROM_UNICODE}

    doc.close()


    # Placeholders — updated per stream in the processing loop below
    bold_f_names: set = set()
    regular_f_name: str = "F0"
    digit_w_map: Dict[str, float] = {}
    space_w_map: Dict[str, float] = {}
    comma_w_map: Dict[str, float] = {}
    avail_cids_map: Dict[str, set] = {}

    # ── Очередь замен ─────────────────────────────────────────────────────
    # Ключи:
    #   "IN:<digits>"  — кіріс-суммы (положительные), pop
    #   "OUT:<digits>" — шығыс-суммы (отрицательные, абс. значение), pop
    #   "HDR:<digits>" — заголовочные суммы, peek (не удаляются)
    replacement_queue: Dict[str, _deque] = {}

    def _add(key: str, val: float, typ: str):
        # Страховка второго уровня к инициализации new_* в recalculate_halyk:
        # ни масштабирование (× K), ни FEE-NORM, ни изъятия не могут превратить
        # ненулевую сумму в 0 — нулевое значение здесь означает только одно:
        # new_* остались дефолтом 0.0, т.е. кто-то вернул stmt, не заполнив их.
        # Записать «0,00» в реальную ячейку расхода/прихода хуже, чем не
        # записать ничего (выписка становится арифметически битой), поэтому
        # слот не ставим и шумим в лог.
        if abs(val) < 0.005 and not key.startswith("HDR:"):
            print(f"  [⚠️ ПРОПУСК] {typ}: попытка записать 0,00 в ячейку {key} — "
                  f"похоже, new_* не были заполнены. Замена не выполнена.")
            return
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
        replacement_queue[key].append((val, typ))

    # Зарплаты (кіріс): 2 слота на каждую (op_amount + кіріс колонка) — НО
    # только когда «Сумма операции» реально совпадает с «Приход» (что для
    # категорий дохода верно на всех разобранных образцах: "3 006 798,42 KZT
    # 3 006 798,42..."). Если op_amount_val отсутствует (скрыт как "-") или
    # отличается от kiri_s, в потоке байт под ключом kiri_s_text физически
    # есть только ОДНА ячейка (сам «Приход») — вторая запись была бы лишней и
    # осталась бы «бесхозной» в очереди, рискуя быть ошибочно потреблённой
    # ДРУГОЙ ячейкой с тем же числом где-то ещё в документе.
    for tx in stmt.transactions:
        if tx.is_salary and tx.kiri_s > 0 and abs(tx.new_kiri_s - tx.kiri_s) > 0.005:
            key = "IN:" + _clean_digits(tx.kiri_s_text)
            _add(key, tx.new_kiri_s, "SALARY")
            if tx.op_amount_val is not None and abs(tx.op_amount_val - tx.kiri_s) < 0.005:
                _add(key, tx.new_kiri_s, "SALARY")

    # Изъятия: знак "−" в потоке вынесен в ОТДЕЛЬНЫЙ Tj перед цифрами (не в одном
    # текстовом ране с числом) — replace_callback переносит этот флаг между
    # соседними Tj-вызовами, поэтому такие суммы корректно определяются как
    # отрицательные и должны лежать в очереди OUT, а не IN.
    for tx in stmt.transactions:
        if tx.is_seizure and tx.shyghys < 0 and abs(tx.new_shyghys - tx.shyghys) > 0.005:
            key = "OUT:" + _clean_digits(tx.shyghys_text)
            new_abs = abs(tx.new_shyghys)
            _add(key, new_abs, "SEIZURE")
            _add(key, new_abs, "SEIZURE")
        elif not tx.is_seizure and tx.shyghys < 0 and abs(tx.new_shyghys - tx.shyghys) > 0.005:
            # FEE-NORM (см. recalculate_halyk): «Сумма операции» и «Расход»
            # изначально РАЗНЫЕ числа в потоке (в отличие от SEIZURE, где они
            # совпадают) — цифровая строка «Расход» встречается только один
            # раз, слот в очереди нужен только один.
            key = "OUT:" + _clean_digits(tx.shyghys_text)
            _add(key, abs(tx.new_shyghys), "FEE_NORM")

    # Барлығы: итоговый кіріс и шығыс (peek — по 1 разу каждый).
    # *_text может быть None, если строка "Барлығы:"/"Всего:" не была найдена
    # в шапке (см. parse_halyk_statement) — тогда число посчитано фолбэком по
    # транзакциям (арифметика верна), но у него нет известного места в потоке
    # байт. Пропускаем побайтовую замену вместо угадывания ключа "HDR:000" —
    # запись по неверному ключу могла бы затереть чужую нулевую ячейку.
    if stmt.total_kiri_s_text is not None and abs(stmt.new_total_kiri_s - stmt.total_kiri_s) > 0.005:
        key = "HDR:" + _clean_digits(stmt.total_kiri_s_text)
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_total_kiri_s, "TOTAL_KIRI_S"))

    if stmt.total_shyghys_text is not None and abs(stmt.new_total_shyghys - stmt.total_shyghys) > 0.005:
        key = "HDR:" + _clean_digits(stmt.total_shyghys_text)
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_total_shyghys, "TOTAL_SHYGHYS"))

    # Шығыс қалдығы (closing balance): peek, может встречаться 2 раза на стр.0
    if abs(stmt.new_closing_balance - stmt.closing_balance) > 0.005:
        key = "HDR:" + _clean_digits(stmt.closing_text)
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_closing_balance, "CLOSING"))

    # Кіріс қалдығы / Входящий остаток (opening balance): обычно не меняется,
    # но ПРОВЕРКА 3 в recalculate_halyk может поднять его, если провал running
    # balance случился ДО первой зарплатной проводки периода (бустом зарплаты
    # такой провал не убрать — см. комментарий там).
    if abs(stmt.new_opening_balance - stmt.opening_balance) > 0.005:
        key = "HDR:" + _clean_digits(stmt.opening_text)
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_opening_balance, "OPENING"))

    print(f"\n[Halyk] Подготовлено {sum(len(q) for q in replacement_queue.values())} замен "
          f"в {len(replacement_queue)} ключах")

    # ── Regex для Td/Tj ───────────────────────────────────────────────────
    # Группа 3 — необязательная встроенная смена шрифта/кегля МЕЖДУ Td и <hex>Tj
    # (напр. "12 Td /F0 8 Tf <hex> Tj") — реальная строка "Всего:" этого
    # документа переключает кегль именно так у своих трёх итоговых сумм
    # (89 130 865,71 / -15 531 122,60 / -900,00). Без этой группы паттерн не
    # матчил такие токены вообще (Td\s*<... не допускает ничего, кроме
    # пробелов, между Td и <), из-за чего замена шапки "Всего:" молча не
    # происходила — транзакции пересчитывались, а строка итогов в PDF
    # оставалась старой (несогласованный результат). Группа 4 — сам hex.
    td_pattern = re.compile(
        rb"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Td\s*(/F\d+\s+[\d.]+\s+Tf\s*)?<([0-9A-Fa-f]+)>\s*Tj",
    )

    # ── Обработка raw bytes ───────────────────────────────────────────────
    raw = bytearray(input_bytes)
    total_replaced = 0
    font_substitutions = 0
    cumulative_offset = 0

    # ── Патч Bold-шрифта: вшиваем недостающие глифы цифр, если gate
    # позволяет доверять зашитому эталону (см. halyk_bold_digits.py).
    # Делается ПЕРВЫМ, до поиска позиций content-стримов — все последующие
    # raw.find() увидят уже сдвинутые (после этого патча) позиции сами по
    # себе, отдельного cumulative_offset для этого шага не нужно.
    _newly_available_cids: Dict[int, Dict[str, float]] = {}  # cid_xref -> {cid_hex: width}
    _cid_to_digit_char = {_v: _k for _k, _v in _digit_cids.items()}  # cid_hex -> '0'..'9'
    for _cid_xref, _ff2_xref, _tu_xref in _bold_font_pairs:
        # ── Резолвим и готовим ВСЕ ТРИ правки (FontFile2, /W, ToUnicode) из
        # ОДНОГО снимка `raw` — ни одной записи в `raw` до этого момента.
        # Так гарантируется настоящая, а не «в основном верная», атомарность:
        # либо все три позиции разрешились и патч применяется целиком, либо
        # НИ ОДНА запись не происходит вовсе (см. код-ревью Task 4 — прежняя
        # версия делала свежий raw.find() для ToUnicode ПОСЛЕ того, как
        # FontFile2/W уже были записаны в raw, и на невозможность найти
        # ToUnicode в этот момент реагировала предупреждением, а не отменой
        # уже применённых правок — оставляя ровно то частично пропатченное
        # состояние, которого весь этот блок должен избегать).
        _ff2_pattern = f"{_ff2_xref} 0 obj".encode()
        _ff2_pos = raw.find(_ff2_pattern)
        if _ff2_pos < 0:
            continue
        _stream_kw = raw.find(b"stream", _ff2_pos)
        _data_start = _stream_kw + len(b"stream")
        if raw[_data_start:_data_start + 2] == b"\r\n":
            _data_start += 2
        elif raw[_data_start:_data_start + 1] == b"\n":
            _data_start += 1
        _endstream_pos = raw.find(b"endstream", _data_start)
        _header = bytes(raw[_ff2_pos:_stream_kw])
        _len_m = re.search(rb"/Length\s+(\d+)", _header)
        if not _len_m:
            continue
        _old_length = int(_len_m.group(1))
        _compressed = bytes(raw[_data_start:_data_start + _old_length])
        try:
            _ff2_bytes = zlib.decompress(_compressed)
        except zlib.error:
            continue

        # ── CIDFont-словарь: читаем ЗАРАНЕЕ, до вызова
        # _try_patch_bold_digit_glyphs (который доверяет CID == GID) — нужен
        # и для gate /CIDToGIDMap ниже, и позже для правки /W того же
        # объекта. Один fetch на оба использования, не два.
        _cid_pattern = f"{_cid_xref} 0 obj".encode()
        _cid_pos = raw.find(_cid_pattern)
        if _cid_pos < 0:
            continue
        _endobj_pos = raw.find(b"endobj", _cid_pos)
        _cidobj_bytes = bytes(raw[_cid_pos:_endobj_pos])

        # ── Gate /CIDToGIDMap (design spec — см. docs/superpowers/specs/
        # 2026-08-05-halyk-bold-glyph-embedding-design.md, раздел про
        # _try_patch_bold_digit_glyphs, п.1): если у CIDFont-словаря явно
        # присутствует /CIDToGIDMap И это НЕ /Identity (т.е. ссылка на
        # отдельный поток GID-маппинга), CID == GID доверять нельзя —
        # патчить этот шрифт вообще не пытаемся, откат на старое поведение
        # (подмена шрифта/перебор шума), как и любой другой отказ gate'а.
        # Отсутствие ключа или явный /Identity — CID == GID, как и
        # предполагает весь код ниже (на всех 6 локальных реальных файлах
        # ключа /CIDToGIDMap нет вовсе — эта ветка для них no-op).
        _c2g_m = re.search(rb"/CIDToGIDMap\s*/Identity\b", _cidobj_bytes)
        if b"/CIDToGIDMap" in _cidobj_bytes and _c2g_m is None:
            continue

        _result = _try_patch_bold_digit_glyphs(_ff2_bytes, _digit_cids)
        if _result is None:
            continue
        _patched_ff2, _added_widths = _result

        # Готовим содержимое правки FontFile2 (ничего в raw ещё не пишем).
        _new_compressed = zlib.compress(_patched_ff2)
        _new_length = len(_new_compressed)
        _new_length1 = len(_patched_ff2)
        _new_header = re.sub(rb"/Length\s+\d+", f"/Length {_new_length}".encode(), _header)
        _new_header = re.sub(rb"/Length1\s+\d+", f"/Length1 {_new_length1}".encode(), _new_header)
        # Разделитель между "stream" и телом ("\r\n" или "\n") — берём из
        # оригинала, не хардкодим, тот же приём, что _op_separators для
        # content-стримов.
        _stream_sep = bytes(raw[_stream_kw + len(b"stream"):_data_start])
        _trailing = bytes(raw[_data_start + _old_length:_endstream_pos])
        _ff2_replacement = _new_header + b"stream" + _stream_sep + _new_compressed + _trailing

        # /W-массив CIDFont-словаря (_cidobj_bytes/_endobj_pos уже получены
        # выше, вместе с gate'ом /CIDToGIDMap) — вставляем новые записи В
        # ВОЗРАСТАЮЩЕМ порядке CID, разделителем/внутренним пробелом САМОГО
        # ЭТОГО файла (не хардкодом): h6.pdf пишет "19[500]21[500]..." без
        # единого пробела, а HALYKformat1.pdf — "19[ 500] 20[ 500]..." с
        # пробелом после каждого "[" и между записями. См.
        # _w_array_insert_sorted (тот же принцип, что pdf_service.
        # _op_separators — конвенция читается из соседних записей ЭТОГО
        # файла). Ничего не пишем в raw здесь.
        _w_m = re.search(rb"/W\s*\[", _cidobj_bytes)
        if not _w_m:
            continue
        _bracket_start = _w_m.end() - 1
        _depth = 0
        _close_idx = None
        for _j in range(_bracket_start, len(_cidobj_bytes)):
            _c = _cidobj_bytes[_j:_j + 1]
            if _c == b"[":
                _depth += 1
            elif _c == b"]":
                _depth -= 1
                if _depth == 0:
                    _close_idx = _j
                    break
        if _close_idx is None:
            continue
        _new_cidobj_bytes = _w_array_insert_sorted(
            _cidobj_bytes, _bracket_start, _close_idx, _added_widths
        )
        if _new_cidobj_bytes is None:
            continue

        # ── /ToUnicode CMap Type0-обёртки. Глиф без записи в ToUnicode
        # рисуется корректно ВИЗУАЛЬНО, но для текстового слоя (fitz.get_text
        # — извлечение текста/копипаст/поиск, и КАЖДАЯ проверка этого
        # проекта, читающая PDF именно так, включая её же
        # check_totals_match_rows) этот CID остаётся непривязанным ни к
        # какому символу и извлекается пустым. Найдено смоук-тестом на
        # h6.pdf: без этого блока «859 800,00» реэкстрактился как «89
        # 800,00» — CID новой «5» рисовался, но не читался как текст.
        # Позиция и содержимое правки резолвятся здесь же, из того же
        # снимка `raw`, что и FontFile2/W выше — ничего не пишем в raw.
        _tu_entries = [
            (_cid, _cid_to_digit_char[_cid]) for _cid in _added_widths if _cid in _cid_to_digit_char
        ]
        if _tu_xref is None or not _tu_entries:
            continue
        _tu_pos = raw.find(f"{_tu_xref} 0 obj".encode())
        if _tu_pos < 0:
            continue
        _tu_stream_kw = raw.find(b"stream", _tu_pos)
        _tu_data_start = _tu_stream_kw + len(b"stream")
        if raw[_tu_data_start:_tu_data_start + 2] == b"\r\n":
            _tu_data_start += 2
        elif raw[_tu_data_start:_tu_data_start + 1] == b"\n":
            _tu_data_start += 1
        _tu_endstream_pos = raw.find(b"endstream", _tu_data_start)
        _tu_header = bytes(raw[_tu_pos:_tu_stream_kw])
        _tu_len_m = re.search(rb"/Length\s+(\d+)", _tu_header)
        if not _tu_len_m:
            continue
        _tu_old_length = int(_tu_len_m.group(1))
        _tu_compressed = bytes(raw[_tu_data_start:_tu_data_start + _tu_old_length])
        _tu_is_flate = b"/FlateDecode" in _tu_header
        try:
            _tu_body = zlib.decompress(_tu_compressed) if _tu_is_flate else bytes(_tu_compressed)
        except zlib.error:
            continue
        _endcmap_idx = _tu_body.rfind(b"endcmap")
        if _endcmap_idx < 0:
            continue
        # Отдельный beginbfchar/endbfchar блок перед endcmap — не трогаем
        # уже существующий bfrange-блок (не нужно пересчитывать его счётчик
        # диапазонов), несколько bfchar/bfrange блоков в одном CMap валидны
        # по спецификации (ISO 32000). EOL и разделитель между hex-токенами
        # ЧИТАЮТСЯ из уже существующего bfrange/bfchar-блока этого же потока
        # (см. _cmap_bf_style) — не хардкодятся: h6.pdf/HALYKformat1.pdf оба
        # пишут "beginbfrange\r\n<023C><023C><0412>\r\n..." (CRLF, без
        # пробела между токенами), а прежний код вставлял "\n" и пробел —
        # видимое расхождение почерка ровно того класса, который вся эта
        # ветка призвана убрать (см. CLAUDE.md, критерий 4).
        _bf_style = _cmap_bf_style(_tu_body)
        if _bf_style is None:
            continue
        _entry_eol, _token_sep = _bf_style
        _bfchar_block = (
            f"{len(_tu_entries)} beginbfchar".encode("ascii") + _entry_eol
            + b"".join(
                f"<{_cid}>".encode("ascii") + _token_sep + f"<{ord(_ch):04X}>".encode("ascii") + _entry_eol
                for _cid, _ch in _tu_entries
            )
            + b"endbfchar" + _entry_eol
        )
        _new_tu_body = _tu_body[:_endcmap_idx] + _bfchar_block + _tu_body[_endcmap_idx:]
        _new_tu_compressed = zlib.compress(_new_tu_body) if _tu_is_flate else _new_tu_body
        _new_tu_header = re.sub(
            rb"/Length\s+\d+", f"/Length {len(_new_tu_compressed)}".encode(), _tu_header
        )
        _tu_stream_sep = bytes(raw[_tu_stream_kw + len(b"stream"):_tu_data_start])
        _tu_trailing = bytes(raw[_tu_data_start + _tu_old_length:_tu_endstream_pos])
        _tu_replacement = _new_tu_header + b"stream" + _tu_stream_sep + _new_tu_compressed + _tu_trailing

        # ── Применение: все три позиции уже разрешены из одного снимка
        # `raw` выше — теперь пишем. Три индирект-объекта физически не
        # пересекаются по построению PDF (это отдельные "N 0 obj"..."endobj"
        # блоки), но на всякий случай проверяем это явно перед записью —
        # если бы регионы пересеклись, применение в убывающем порядке start
        # могло бы записать поверх ещё не применённой правки. Затем пишем
        # от КОНЦА файла к НАЧАЛУ: splice в bytearray сдвигает все байты
        # ПОСЛЕ точки правки, поэтому запись в более позднюю (по смещению)
        # область никогда не портит start/end ещё не применённой более
        # ранней области.
        _regions = sorted(
            [
                (_ff2_pos, _endstream_pos, _ff2_replacement),
                (_cid_pos, _endobj_pos, _new_cidobj_bytes),
                (_tu_pos, _tu_endstream_pos, _tu_replacement),
            ],
            key=lambda r: r[0],
            reverse=True,
        )
        if any(_regions[_k][0] < _regions[_k + 1][1] for _k in range(len(_regions) - 1)):
            continue  # региона пересеклись — не должно случаться, но не пишем ничего
        for _start, _end, _new_bytes in _regions:
            raw[_start:_end] = _new_bytes

        print(f"[Halyk] Вшиты недостающие цифры в Bold-шрифт (xref {_ff2_xref}), "
              f"дополнена ToUnicode-карта (xref {_tu_xref}): {sorted(_added_widths.keys())}")

        _newly_available_cids[_cid_xref] = _added_widths

    if _newly_available_cids:
        for _cx, _pair_map in _xref_bold_ff2.items():
            for _fname, (_cx_cid_xref, _cx_ff2_xref, _cx_tu_xref) in _pair_map.items():
                if _cx_cid_xref in _newly_available_cids:
                    _xref_avail_cids.setdefault(_cx, {})
                    _xref_avail_cids[_cx].setdefault(_fname, set())
                    _xref_avail_cids[_cx][_fname] |= set(_newly_available_cids[_cx_cid_xref].keys())

    all_content_xrefs: set = set()
    for xrefs in page_xrefs:
        all_content_xrefs.update(xrefs)

    # Позиции объектов
    obj_positions: Dict[int, int] = {}
    for xref_id in all_content_xrefs:
        pattern = f"{xref_id} 0 obj".encode()
        pos = raw.find(pattern)
        if pos >= 0:
            obj_positions[xref_id] = pos

    sorted_xrefs = sorted(obj_positions.items(), key=lambda x: x[1])

    for xref_id, orig_pos in sorted_xrefs:
        # Update font mapping for this specific content stream's page
        bold_f_names = _xref_bold_f.get(xref_id, set())
        regular_f_name = _xref_regular_f.get(xref_id, "F0")
        digit_w_map = _xref_digit_w.get(xref_id, {})
        space_w_map = _xref_space_w.get(xref_id, {})
        comma_w_map = _xref_comma_w.get(xref_id, {})
        avail_cids_map = _xref_avail_cids.get(xref_id, {})

        pos = orig_pos + cumulative_offset

        endobj_or_stream = raw.find(b"stream", pos)
        if endobj_or_stream < 0:
            continue

        header_region = bytes(raw[pos:endobj_or_stream])
        length_match = re.search(rb"/Length\s+(\d+)", header_region)
        if not length_match:
            continue

        declared_length = int(length_match.group(1))
        length_start = pos + length_match.start(1)
        length_end = pos + length_match.end(1)

        data_start = endobj_or_stream + 6
        if raw[data_start:data_start + 1] == b"\r":
            data_start += 2
        else:
            data_start += 1

        endstream_pos = raw.find(b"endstream", data_start)
        if endstream_pos < 0:
            continue

        raw_stream_data = bytes(raw[data_start:endstream_pos])
        if raw_stream_data.endswith(b"\r\n"):
            raw_stream_data = raw_stream_data[:-2]
        elif raw_stream_data.endswith(b"\n"):
            raw_stream_data = raw_stream_data[:-1]

        try:
            decompressed = zlib.decompress(raw_stream_data)
        except zlib.error:
            continue

        # Знак "−" у отрицательных сумм в этом документе иногда выводится
        # ОТДЕЛЬНЫМ Tj перед цифрами, а не склеен с ними в одном текстовом ране
        # (встречается и в legacy, и в nav-формате — напр. изъятия у Kazakh-формата
        # и почти все отрицательные суммы у мультивалютного). Раз regex matches
        # идут по порядку появления в потоке, переносим этот флаг между вызовами
        # callback, сбрасывая его на каждый content-stream отдельно.
        _prev_lone_minus = [False]

        # Текст двух предшествующих (уже обработанных) токенов — [позапрошлый,
        # предыдущий] — нужен для защиты «Приход»-ячейки зарплаты от кражи
        # слота чужой ячейкой с тем же числом (см. "Защита от «воровства»
        # слота" ниже). В FIFO-очереди замены ключуются по ЗНАЧЕНИЮ, а не по
        # конкретной транзакции: если где-то ещё в документе встречается
        # ЧУЖАЯ ячейка «Приход» с точно той же суммой, что и наша зарплата,
        # прямой защиты от этого раньше не было (только для op_amount-колонки,
        # ниже). Позиционный признак настоящей зарплатной «Приход»-ячейки —
        # ей ВСЕГДА непосредственно предшествует пара [op_amount][код валюты]
        # с ТЕМ ЖЕ числом (см. образцы: "3 006 798,42 KZT 3 006 798,42" —
        # Сумма операции и Приход у поступлений совпадают). Если два токена
        # назад стоит код валюты, а токен перед ним — число, ОТЛИЧНОЕ от
        # искомого, это прямое доказательство, что текущая ячейка принадлежит
        # ДРУГОЙ операции с тем же значением — тогда слот не берём.
        _prev_tok_hist = [None, None]

        # Накопленный "долг" по X для цепочек токенов на одной визуальной строке
        # (Td с dy=0 — не своя колонка таблицы, а продолжение той же строки, как
        # в заголовке "Исходящий остаток: <сумма> KZT; <сумма> USD; ..."). Если
        # заменённое число стало шире/уже, следующий Td с dy=0 рассчитан ещё под
        # СТАРУЮ ширину — переносим разницу на него, иначе он налезет на текст
        # или наоборот. Сбрасывается на каждом Td с dy≠0 (там уже своя, независимая
        # от предыдущего токена позиция — см. компенсацию правого края ниже).
        _pending_x_shift = [0.0]

        # Суммарный горизонтальный сдвиг НАЧАЛА текущей визуальной строки, который
        # наши правки уже "впитали" в матрицу строки (Tlm) этого BT-блока. Td
        # кумулятивны внутри BT-блока: сдвинув вправо токен после расширенного
        # числа (carry выше), мы сдвигаем и начало строки, а Td-перенос на
        # СЛЕДУЮЩУЮ строку («Доступная сумма…», «на дату…») отсчитывается от этого
        # уже сдвинутого начала и наследует сдвиг — из-за чего весь правый блок
        # шапки уезжал вправо с отступом. На токене-переносе (dy≠0) вычитаем
        # накопленный сдвиг, возвращая строку на исходную левую границу.
        # Сбрасывается на границе BT-блока (матрица строки там обнуляется, первый
        # Td блока — абсолютная позиция, чужой сдвиг к ней неприменим).
        _line_shift = [0.0]
        _prev_match_end = [0]

        # Абсолютный X начала текущего токена = кумулятивная сумма tx от начала
        # BT-блока (Td складываются внутри блока; BT сбрасывает матрицу строки).
        # Считаем по ОРИГИНАЛЬНЫМ tx; фактический (emitted) левый край токена =
        # этот X + наши сдвиги (_line_shift + x_adjust). Нужен, чтобы поймать
        # выезд заменённого числа за правую кромку листа и перенести его строкой ниже.
        _abs_x_orig = [0.0]

        # Если предыдущий токен-число перенесён на новую строку (не влез по ширине,
        # см. wrap ниже), здесь X-смещение, с которым СЛЕДУЮЩИЙ токен ("KZT;" из
        # "…в валюте: <сумма> KZT; …") встаёт сразу за перенесённым числом на той же
        # новой строке (dy=0), а не уходит ещё ниже своим исходным Td-переносом.
        _absorb = [None]

        def replace_callback(match):
            nonlocal total_replaced, font_substitutions
            x_str = match.group(1).decode("ascii")
            y_str = match.group(2).decode("ascii")
            _inline_tf_raw = match.group(3)  # b"/F0 8 Tf " или None
            full_hex = match.group(4).decode("ascii").upper()

            # Если у этого токена встроенная смена шрифта — она АВТОРИТЕТНЕЕ
            # обратного скана по потоку (тот ищет последний /F.../Tf ДО начала
            # матча, а инлайн-Tf стоит ВНУТРИ него и переопределяет активный
            # шрифт/кегль именно для этого Tj). Используется ниже вместо
            # обратного скана, когда присутствует.
            _inline_fname, _inline_fsize = None, None
            if _inline_tf_raw:
                _m_inline = re.match(rb"/F(\d+)\s+([\d.]+)\s+Tf", _inline_tf_raw.strip())
                if _m_inline:
                    _inline_fname = "F" + _m_inline.group(1).decode()
                    _inline_fsize = float(_m_inline.group(2))

            try:
                current_x = float(x_str)
            except Exception:
                return match.group(0)

            # Новый BT-блок между предыдущим и этим Tj → матрица строки сброшена,
            # накопленный сдвиг прошлого блока к ней неприменим.
            if b"BT" in decompressed[_prev_match_end[0]:match.start()]:
                _line_shift[0] = 0.0
                _pending_x_shift[0] = 0.0
                _abs_x_orig[0] = 0.0
            _prev_match_end[0] = match.end()

            # Кумулятивный абсолютный X (по оригинальным tx) — origin текущего токена.
            _abs_x_orig[0] += current_x

            # Почерк оригинала: координата без хвостовых нулей и его же
            # разделители вокруг <hex> и Tj — см. _fmt_coord/_op_separators
            # в pdf_service.py и форензик-разбор 02/08/2026. Действует на все
            # ветки возврата ниже.
            _so, _sc = _op_separators(match.group(0))

            # Этот токен идёт сразу за числом, которое мы перенесли на новую строку
            # (см. wrap ниже) — ставим его на ту же новую строку вплотную за числом
            # (dy=0), а не уводим ниже его собственным Td-переносом. _line_shift уже
            # выставлен так, что ближайший следующий перенос вернёт строку на место.
            if _absorb[0] is not None:
                dx = _absorb[0]
                _absorb[0] = None
                _pending_x_shift[0] = 0.0
                _head = f"{_fmt_coord(dx)} 0 Td".encode("ascii")
                if _inline_tf_raw:
                    _head += b" " + _inline_tf_raw.decode("ascii").strip().encode("ascii")
                return _head + _so + f"<{full_hex}>".encode("ascii") + _sc + b"Tj"

            is_same_line = float(y_str) == 0.0
            if is_same_line:
                # Продолжение строки: подхватываем перенесённый сдвиг ширины.
                x_adjust = _pending_x_shift[0]
            else:
                # Перенос на новую строку внутри блока: отматываем накопленный
                # сдвиг начала строки, иначе новая строка уедет вправо вслед за
                # расширенным числом предыдущей.
                x_adjust = -_line_shift[0]
                _line_shift[0] = 0.0
            _pending_x_shift[0] = 0.0

            def _unchanged(hex_override=None):
                # Перенесённый сдвиг применяется к этому токену один раз и тем
                # самым "впитывается" в поток (Td дальше складываются от новой
                # позиции) — дальше нести его незачем, иначе он задвоится на
                # каждом следующем токене строки.
                _pending_x_shift[0] = 0.0
                if x_adjust or hex_override is not None:
                    if is_same_line:
                        _line_shift[0] += x_adjust
                    use_hex = hex_override if hex_override is not None else full_hex
                    _head = f"{_fmt_coord(current_x + x_adjust)} {y_str} Td".encode("ascii")
                    if _inline_tf_raw:
                        _head += b" " + _inline_tf_raw.decode("ascii").strip().encode("ascii")
                    return _head + _so + f"<{use_hex}>".encode("ascii") + _sc + b"Tj"
                return match.group(0)

            # Декодируем hex
            hex_blocks = [full_hex[i:i + 4] for i in range(0, len(full_hex), 4)]
            decoded_blocks = [TO_UNICODE.get(b, "?") for b in hex_blocks]

            # Сдвигаем историю токенов БЕЗУСЛОВНО (до любых return) — она должна
            # отражать РЕАЛЬНЫЙ порядок Tj-вызовов в потоке независимо от того,
            # что мы решим делать с текущим токеном. two_back = токен ДО
            # предыдущего (нужен для проверки "op_amount перед кодом валюты").
            _prev_tok_two_back, _prev_tok_one_back = _prev_tok_hist[0], _prev_tok_hist[1]
            _prev_tok_hist[0] = _prev_tok_hist[1]
            _prev_tok_hist[1] = "".join(decoded_blocks).strip()

            preceded_by_lone_minus = _prev_lone_minus[0]
            _prev_lone_minus[0] = decoded_blocks == ["-"]

            if decoded_blocks == ["-"]:
                # Отдельный токен-минус перед числом (см. _prev_lone_minus).
                # Если следующее число — это OPENING (входящий остаток,
                # единственный тип замены, где знак может смениться с "−" на
                # "+", когда ПРОВЕРКА 3 поднимает его), минус нужно СТЕРЕТЬ —
                # иначе перед новым (уже положительным) числом останется
                # "мёртвый" знак минуса. Для остальных типов (SEIZURE и т.п.)
                # знак всегда сохраняется, поэтому проверяем именно typ.
                _next_m = td_pattern.search(decompressed, match.end())
                if _next_m is not None:
                    _nh = _next_m.group(4).decode("ascii").upper()
                    _nblocks = [_nh[i:i + 4] for i in range(0, len(_nh), 4)]
                    _ntext = "".join(TO_UNICODE.get(b, "?") for b in _nblocks)
                    _nclean = re.sub(r"[^0-9]", "", _ntext)
                    if _nclean:
                        _nq = replacement_queue.get("HDR:" + _nclean)
                        if _nq and _nq[0][1] == "OPENING":
                            _blank_hex = FROM_UNICODE.get("\xa0") or FROM_UNICODE.get(" ")
                            if _blank_hex:
                                return _unchanged(hex_override=_blank_hex)
                return _unchanged()

            # Находим числовую зону
            first_digit = next(
                (i for i, ch in enumerate(decoded_blocks) if ch.isdigit()), None
            )
            last_digit = next(
                (i for i in range(len(decoded_blocks) - 1, -1, -1) if decoded_blocks[i].isdigit()),
                None,
            )
            if first_digit is None or last_digit is None:
                return _unchanged()

            old_num_text = "".join(decoded_blocks[first_digit:last_digit + 1])
            clean_d = re.sub(r"[^0-9]", "", old_num_text)
            if not clean_d:
                return _unchanged()

            prefix_chars = decoded_blocks[:first_digit]
            suffix_chars = decoded_blocks[last_digit + 1:]
            prefix_text = "".join(prefix_chars)
            suffix_text = "".join(suffix_chars)

            has_minus = "-" in prefix_text or preceded_by_lone_minus
            has_digit_prefix = any(ch.isdigit() for ch in prefix_text)

            # Выбираем ключ и очередь
            queue = None
            is_hdr = False

            if has_minus:
                q = replacement_queue.get("OUT:" + clean_d)
                if q:
                    queue = q
                    is_hdr = False
            else:
                q = replacement_queue.get("IN:" + clean_d)
                if q:
                    queue = q
                    is_hdr = False

            # HDR fallback
            if queue is None:
                q = replacement_queue.get("HDR:" + clean_d)
                if q:
                    queue = q
                    is_hdr = True

            if not queue:
                return _unchanged()

            # ── Защита от «воровства» слота столбцом «Сумма операции» ──────
            # Одна и та же сумма встречается в потоке дважды на строке дохода
            # (столбец «Сумма операции» op_amount + столбец «Приход» кіріс) и,
            # что важно, ЕЩЁ РАЗ — как op_amount у чужих операций с тем же
            # числом, но нулевым приходом (напр. «Поступление P2P К2», где
            # приход=0). При сопоставлении строго по значению такой чужой
            # op_amount по FIFO забирал слот реальной зарплаты: чужая ячейка
            # получала масштабированное число, а настоящая зарплата в столбце
            # «Приход» оставалась неизменной (расхождение баланса + визуальный
            # мусор). Отличаем op_amount по коду валюты (KZT/USD/…) сразу за
            # ним: если это op_amount, заменяем его ТОЛЬКО когда «Приход» той
            # же строки равен той же сумме — тогда это доход. Иначе слот не
            # трогаем. Если кода валюты за токеном нет (столбец «Приход» и
            # прочие форматы), поведение прежнее — токен обрабатывается как
            # раньше.
            if queue is not None and not is_hdr and not has_minus:
                _nxt_m = td_pattern.search(decompressed, match.end())
                _is_op_amount_cell = False
                if _nxt_m is not None:
                    _nxt_hex = _nxt_m.group(4).decode("ascii").upper()
                    _nxt_txt = "".join(
                        TO_UNICODE.get(_nxt_hex[i:i + 4], "?")
                        for i in range(0, len(_nxt_hex), 4)
                    ).strip()
                    if _nxt_txt in _CCY_CODES:
                        _is_op_amount_cell = True
                        # Это столбец «Сумма операции» — проверяем «Приход»
                        # той же строки (токен сразу за кодом валюты).
                        _kiris_m = td_pattern.search(decompressed, _nxt_m.end())
                        _kiris_ok = False
                        if _kiris_m is not None:
                            _kh = _kiris_m.group(4).decode("ascii").upper()
                            _kt = "".join(
                                TO_UNICODE.get(_kh[i:i + 4], "?")
                                for i in range(0, len(_kh), 4)
                            )
                            if re.sub(r"[^0-9]", "", _kt) == clean_d:
                                _kiris_ok = True
                        if not _kiris_ok:
                            return _unchanged()

                # Обратная защита «Приход»-ячейки (см. _prev_tok_hist выше):
                # если этот токен НЕ сам op_amount (проверено выше) и ему
                # непосредственно предшествует пара [op_amount][код валюты] с
                # ДРУГИМ числом — это прямое доказательство, что ячейка
                # принадлежит чужой операции с тем же значением, что и наша
                # зарплата (значение случайно совпало). Не трогаем такую
                # ячейку. При отсутствии такой пары (legacy-формат без
                # op_amount+ccy рядом, либо реально наша ячейка) поведение не
                # меняется — это не строгая замена старой защиты, а
                # дополнительный барьер только при явной улике противоречия.
                if not _is_op_amount_cell and _prev_tok_one_back in _CCY_CODES:
                    _prev_num_clean = re.sub(r"[^0-9]", "", _prev_tok_two_back or "")
                    if _prev_num_clean and _prev_num_clean != clean_d:
                        return _unchanged()

            # ── Защита FEE-NORM-слота от «воровства» чужой «Суммой операции» ──
            # FEE-NORM (см. recalculate_halyk) кладёт в очередь РОВНО ОДИН слот
            # — только для ячейки «Расход»; «Сумма операции» той же строки уже
            # содержит верное значение и НЕ должна меняться (в отличие от
            # SEIZURE, которая намеренно кладёт 2 слота на обе ячейки — этот
            # барьер её не касается). Но очередь ключуется по ЗНАЧЕНИЮ, а не по
            # владельцу-транзакции: «Сумма операции» обычно «круглое» число, а
            # «Расход» скрывает комиссию, поэтому ЧУЖАЯ (другой транзакции)
            # отрицательная ячейка «Сумма операции» может случайно совпасть
            # цифрами с нашей «Расход»-ячейкой. Если такая чужая ячейка стоит в
            # потоке РАНЬШЕ настоящей (обе — отдельные dy≠0 ячейки, ищем код
            # валюты сразу за текущим токеном — тот же признак op_amount-ячейки,
            # что и в защите «Приход» выше), она физически как раз является
            # op_amount-ячейкой (сразу после — код валюты) и не должна забирать
            # единственный FEE_NORM слот. Воспроизведено на реальной выписке
            # (Halyk, 2 пары «Басқа картаға аударым»): единственный слот
            # доставался op_amount-ячейке чужой более ранней операции, настоящая
            # «Расход»-ячейка оставалась нетронутой, а «Итого»/баланс в шапке
            # уже пересчитаны так, будто замена прошла — расхождение шапки с
            # фактическим текстом транзакций.
            if queue is not None and not is_hdr and has_minus and queue[0][1] == "FEE_NORM":
                _fee_nxt_m = td_pattern.search(decompressed, match.end())
                if _fee_nxt_m is not None:
                    _fee_nxt_hex = _fee_nxt_m.group(4).decode("ascii").upper()
                    _fee_nxt_txt = "".join(
                        TO_UNICODE.get(_fee_nxt_hex[i:i + 4], "?")
                        for i in range(0, len(_fee_nxt_hex), 4)
                    ).strip()
                    if _fee_nxt_txt in _CCY_CODES:
                        return _unchanged()

            if is_hdr:
                new_val, typ = queue[0]  # peek
            else:
                new_val, typ = queue.popleft()  # pop

            # Форматируем новое число
            formatted_num = _fmt(abs(new_val))
            new_num_hex = text_to_hex(formatted_num)

            if "0000" in new_num_hex and any(
                new_num_hex[i:i + 4] == "0000" for i in range(0, len(new_num_hex), 4)
            ):
                if not is_hdr:
                    queue.appendleft((new_val, typ))
                return _unchanged()

            new_hex = (
                "".join(hex_blocks[:first_digit])
                + new_num_hex
                + "".join(hex_blocks[last_digit + 1:])
            )

            # X-сдвиг: реальные ширины символов из /W активного шрифта на
            # этой позиции (масштабированные на его кегль), с фолбэком на
            # старое приближение "цифра = 4/3 pt, пробел = 4.5 pt" (калибровано
            # под ArialMT в legacy-формате) — у nav-формата (Times New Roman
            # CID) реальные ширины другие, и единая константа даёт неточный
            # сдвиг (см. проверку на реальном документе).
            if _inline_fname is not None:
                _active_fname, _active_fsize = _inline_fname, _inline_fsize
            else:
                _active_fname, _active_fsize = None, None
                for _m in re.finditer(rb"/F(\d+)\s+([\d.]+)\s+Tf", decompressed[: match.start()]):
                    _active_fname = "F" + _m.group(1).decode()
                    _active_fsize = float(_m.group(2))
            _real_digit_w1000 = digit_w_map.get(_active_fname) if _active_fname else None
            _real_space_w1000 = space_w_map.get(_active_fname) if _active_fname else None
            # Запятая раньше вообще не учитывалась в _hw() (0pt) — для ОТНОСИТЕЛЬНЫХ
            # дельт это было безобидно (запятая всегда одна и та же и в
            # old_num_text, и в formatted_num), но ломает АБСОЛЮТНЫЙ расчёт
            # ширины числа, нужный ниже для позиционирования соседнего токена
            # (кода валюты). Фолбэк — ширина пробела (похожая узкая пунктуация).
            _real_comma_w1000 = comma_w_map.get(_active_fname) if _active_fname else None

            def _hw(s: str) -> float:
                if _real_digit_w1000 is not None and _real_space_w1000 is not None and _active_fsize:
                    w = 0.0
                    for ch in s:
                        if ch in (" ", " "):
                            w += _real_space_w1000 / 1000.0 * _active_fsize
                        elif ch == ",":
                            _cw1000 = _real_comma_w1000 if _real_comma_w1000 is not None else _real_space_w1000
                            w += _cw1000 / 1000.0 * _active_fsize
                        elif ch.isdigit() or ch == "-":
                            w += _real_digit_w1000 / 1000.0 * _active_fsize
                    return w
                w = 0.0
                for ch in s:
                    if ch in (" ", " ", ","):
                        w += 4.5
                    elif ch.isdigit() or ch == "-":
                        w += 4.0 / 3.0
                return w
            width_delta = _hw(formatted_num) - _hw(old_num_text)
            # Кегль числа НИКОГДА не меняется — всегда 1.0.
            #
            # Раньше при нехватке места кегль ужимался (до 0.6, а в ветке
            # центрирования до `_MIN_FONT_SCALE`). Замер на всех 6 реальных
            # файлах (2026-08-04): каждый оригинал верстает документ РОВНО
            # одним кеглем 8.0 pt без единого исключения (718/4207/1249/…
            # фрагментов), а в результатах появлялись одиночные 7.425/7.504/
            # 7.866/7.962 — то есть на весь документ несколько чисел набраны
            # заметно мельче соседей. Это самостоятельный признак правки, и
            # человеку он виден лучше, чем небольшой перехлёст числа в
            # соседнюю колонку. Ровно этот же вывод уже зафиксирован в
            # process_kaspi_ip_pdf («Кегль (Tf) здесь больше не трогаем»).
            _font_scale = 1.0
            # Перенос числа на следующую строку (dy≠0): выставляются, если число
            # не влезает по ширине и следующий токен — перенос строки (см. ниже).
            _wrap_dx = None
            _wrap_dy = None
            if is_same_line:
                # Продолжение строки (dy=0): сам токен не двигаем относительно
                # своего предшественника (x_adjust уже учтён), а разницу в ширине
                # переносим на СЛЕДУЮЩИЙ Td той же строки — иначе он останется
                # там, где рассчитан под старую (короче/длиннее) ширину числа,
                # и налезет на нашу замену или оставит разрыв.
                new_x = current_x + x_adjust
                # Сдвиг этого токена «впитан» в начало строки — копим его, чтобы
                # отмотать на ближайшем переносе строки (см. _line_shift).
                _line_shift[0] += x_adjust
                # x_adjust уже учтён в new_x — дальше несём только НОВУЮ разницу
                # ширины, которую породила именно эта замена.
                _pending_x_shift[0] = width_delta

                # Переполнение правого края: фактический левый край токена =
                # оригинальный кумулятивный X + все наши сдвиги на этой строке
                # (= _line_shift после += x_adjust). Второе вхождение остатка в
                # строке «…в валюте: <сумма>» выталкивается вправо шириной первого
                # и у крупных сумм вылезает за кромку листа. Двигать влево некуда
                # (упрётся в «валюте:»). Правильно, как настоящий перенос текста,
                # ПЕРЕНЕСТИ число на следующую строку целиком (тем же кеглем), а
                # идущий за ним "KZT; …" подтянуть к нему (см. _absorb). Перенос
                # возможен, только если СЛЕДУЮЩИЙ токен — сам перенос строки (dy≠0)
                # в том же BT-блоке: тогда его строка и принимает наше число.
                if _active_fsize and _active_fname:
                    emitted_left = _abs_x_orig[0] + _line_shift[0]
                    # Абсолютная ширина числа для контроля выезда и для отступа
                    # подтягиваемого "KZT;": _hw калибрована под сдвиги (относит.
                    # дельты) и занижает абсолют (не считает запятую, узкая цифра),
                    # из-за чего "KZT;" налезал на число. Здесь — прямая оценка под
                    # пропорциональный шрифт шапки: цифра ≈0.5em, разделитель ≈0.27em.
                    # формат числа — только цифры и разделители (пробел/nbsp/запятая)
                    new_num_w = sum(
                        (0.50 if c.isdigit() else 0.27) for c in formatted_num
                    ) * _active_fsize
                    max_right = _page_width - 4.0

                    # Число-продолжение ОТДЕЛЬНОГО minus-токена (preceded_by_lone_minus)
                    # почти всегда — отрицательная сумма в узкой ЯЧЕЙКЕ ТАБЛИЦЫ
                    # ("Сумма операции"/"Расход"), а НЕ в строке шапки: следующий
                    # токен ("Валюта операции"/KZT) сидит на СОБСТВЕННОЙ (dy≠0)
                    # позиции соседней колонки и никуда не подвинется вместе с
                    # нашим числом. Guard по ширине ЛИСТА (max_right выше) такую
                    # ситуацию не ловит — колонка кончается задолго до края листа,
                    # число наезжает на соседнюю ячейку (баг воспроизведён на
                    # реальном документе: «Оплата картотеки BS», выросшее число
                    # перекрывало «KZT»). Если такой сосед есть, его исходный
                    # абсолютный X — жёсткая граница ИМЕННО ЭТОЙ ячейки; "перенос
                    # на следующую строку" тут бессмысленен (сосед — независимая
                    # ячейка, а не продолжение текста), поэтому считаем нужный
                    # font_scale отдельно, используя калиброванную _hw() (реальные
                    # ширины из /W шрифта), а не грубое приближение "0.50em/цифра"
                    # шапки (new_num_w) — оно занижает реальную ширину и оставляет
                    # число впритык к границе колонки даже после «ужатия».
                    #
                    # В ОТЛИЧИЕ от case (B)/wrap-ветки ниже (там "нет BT между
                    # токенами" — признак того же BT-блока шапки), между соседними
                    # ЯЧЕЙКАМИ ТАБЛИЦЫ почти всегда есть "ET ... (отрисовка линий
                    # границ ячейки) ... BT" — каждая ячейка рисуется в СВОЁМ
                    # BT-блоке. Поэтому здесь BT-разрыв не исключается, а наоборот
                    # ожидается: если он есть, X соседа — это его СОБСТВЕННАЯ
                    # (не кумулятивная) координата.
                    _nxt = td_pattern.search(decompressed, match.end())
                    _next_is_row_cell = (
                        preceded_by_lone_minus
                        and _nxt is not None
                        and float(_nxt.group(2)) != 0.0
                    )
                    if _next_is_row_cell:
                        _has_bt_before_next = b"BT" in decompressed[match.end():_nxt.start()]
                        _next_abs_x = (
                            float(_nxt.group(1)) if _has_bt_before_next
                            else _abs_x_orig[0] + float(_nxt.group(1))
                        )
                        # Зазор побольше (2 ширины цифры) — /W-ширины дают только
                        # advance box, реальные чернила глифа (особенно запятой/
                        # последней цифры) визуально съедают часть отступа; без
                        # запаса число упирается прямо в линию колонки.
                        # Раньше здесь кегль ужимался под ширину колонки. Больше
                        # не ужимаем — см. пояснение у `_font_scale = 1.0` выше:
                        # уменьшенный кегль сам по себе признак правки.
                        _col_gap = 2.0 * _active_fsize
                        _col_max_right = _next_abs_x - _col_gap
                        _col_avail = _col_max_right - emitted_left
                        _col_num_w = _hw(formatted_num)

                    if not _next_is_row_cell and emitted_left + new_num_w > max_right and new_num_w > 0:
                        _can_wrap = (
                            _nxt is not None
                            and float(_nxt.group(2)) != 0.0
                            and b"BT" not in decompressed[match.end():_nxt.start()]
                        )
                        if _can_wrap:
                            _nx_dx = float(_nxt.group(1))
                            _nx_dy = float(_nxt.group(2))
                            gap = 0.30 * _active_fsize  # пробел между числом и "KZT;"
                            # Число уходит в начало следующей строки: его новый Td
                            # (относительно предыдущего токена «валюте:») = дойти до
                            # текущего origin + доп. смещение следующего токена к
                            # началу строки, за вычетом уже впитанного сдвига строки.
                            _wrap_dx = current_x + _nx_dx - _line_shift[0]
                            _wrap_dy = _nx_dy
                            new_x = _wrap_dx
                            # На новой строке за числом идёт "KZT; …": ставим его
                            # вплотную (ширина числа + пробел). Этот же сдвиг — новый
                            # _line_shift строки, чтобы её следующий перенос вернулся
                            # на левую границу.
                            _absorb[0] = new_num_w + gap
                            _line_shift[0] = new_num_w + gap
                            _pending_x_shift[0] = 0.0
                        # Перенести некуда (число не в конце визуальной строки) —
                        # раньше здесь ужимался кегль под ширину листа. Больше
                        # не ужимаем: небольшой перехлёст правой кромки менее
                        # заметен, чем единственная строка другим кеглем.
            else:
                # Токен на СОБСТВЕННОЙ (dy≠0) позиции. Тут два разных случая,
                # которые надо различать — иначе широкое число уедет не туда:
                #
                #  (A) ЯЧЕЙКА ТАБЛИЦЫ ("Сумма операции" и т.п.) — ЦЕНТРАЛЬНОЕ
                #      выравнивание (замер bbox по многим строкам оригинала:
                #      центр числа общий для всей колонки, cx std=0, а левый/
                #      правый край «плавают»). Следующий токен — ДРУГАЯ ячейка
                #      (тоже dy≠0), т.е. на этой же строке соседа НЕТ.
                #
                #  (B) СТРОКА ШАПКИ, начинающаяся с числа ("1 312 171 620,09
                #      KZT; 0,00 USD; 0,00 EUR;") — ЛЕВОЕ выравнивание: число
                #      течёт от левого края строки, а за ним на ТОЙ ЖЕ строке
                #      (dy=0) идут соседи "KZT;", "0,00" … Наличие такого
                #      соседа с dy=0 — и есть признак случая (B).
                #
                # Раньше обе ветки центрировались (сдвиг на −½·Δ) — из-за чего
                # у случая (B) широкое число уезжало ВЛЕВО от левого края
                # строки, а идущий за ним "0,00" наследовал сдвиг. x_adjust
                # отматывает накопленный сдвиг начала строки (переносы внутри
                # BT-блока).
                _sib_m = None
                if _active_fsize and _active_fname and _real_digit_w1000 is not None and _real_space_w1000 is not None:
                    _cand_m = td_pattern.search(decompressed, match.end())
                    if (
                        _cand_m is not None
                        and float(_cand_m.group(2)) == 0.0
                        and b"BT" not in decompressed[match.end():_cand_m.start()]
                    ):
                        _sib_m = _cand_m

                _old_half = _hw(old_num_text) / 2.0
                _new_half = _hw(formatted_num) / 2.0

                if _sib_m is not None:
                    # (B) Строка шапки — ЛЕВОЕ выравнивание: левый край числа
                    # остаётся на месте (не центрируем). Сосед ("KZT;" и далее)
                    # пересчитывается АБСОЛЮТНО от реальной ширины нового числа
                    # + фиксированный зазор — самовосстанавливается независимо
                    # от накопленного в истории документа дефицита исходного dx.
                    new_x = current_x + x_adjust
                    _sib_orig_dx = float(_sib_m.group(1))
                    _sib_gap = 0.30 * _active_fsize
                    _pending_x_shift[0] = (_hw(formatted_num) + _sib_gap) - _sib_orig_dx
                else:
                    # (A) Ячейка таблицы — ЦЕНТРИРОВАНИЕ. Раньше широкое число
                    # ужималось по кеглю, чтобы вписаться в узкую колонку
                    # (_COLUMN_SAFETY_RATIO). Больше не ужимаем — центр
                    # сохраняется, а перехлёст в соседнюю колонку принимается
                    # как меньшее зло (см. `_font_scale = 1.0` выше).
                    new_x = current_x + x_adjust + _old_half - _new_half
                    # Правый край числа сдвигается на разницу новой и старой
                    # половины ширины (учитывает и центрирование, и ужатие кегля).
                    _pending_x_shift[0] = _new_half - _old_half

            print(f"  [{typ}] {old_num_text.strip()} → {formatted_num}")
            total_replaced += 1

            # Если контекст Bold и в замене есть CID, которого нет в subset
            # жирного шрифта, — временно переключаем на Regular шрифт (у него
            # есть все глифы). Subset жирного шрифта включает только глифы,
            # встречавшиеся в оригинале жирным; напр. в исходных «Всего»/балансах
            # nav-выписки не было цифры «3» (CID 0016), и без переключения все
            # тройки в новых суммах рисуются пустым «.notdef». Раньше проверялась
            # захардкоженная лишь цифра «4» (CID 0017) — теперь набор недостающих
            # CID определяется по реальному /W-массиву шрифта (avail_cids_map).
            needs_switch = False
            if bold_f_names:
                # Инлайн-Tf (если есть у этого токена) авторитетнее обратного
                # скана — см. комментарий у _inline_fname выше.
                if _inline_fname is not None:
                    _ctx_fname, _ctx_fsize = _inline_fname, f"{_inline_fsize:g}"
                else:
                    _ctx_fname, _ctx_fsize = None, None
                    _last_tf = None
                    for _m in re.finditer(rb"/F(\d+)\s+([\d.]+)\s+Tf", decompressed[: match.start()]):
                        _last_tf = _m
                    if _last_tf is not None:
                        _ctx_fname = "F" + _last_tf.group(1).decode()
                        _ctx_fsize = _last_tf.group(2).decode()
                if _ctx_fname is not None:
                    if _ctx_fname in bold_f_names:
                        _avail = avail_cids_map.get(_ctx_fname)
                        if _avail is not None:
                            _new_blocks = [
                                new_num_hex.upper()[i:i + 4]
                                for i in range(0, len(new_num_hex), 4)
                            ]
                            if any(b not in _avail for b in _new_blocks):
                                needs_switch = True
                        else:
                            # Нет данных /W (не Type0/CID-шрифт) — возвращаемся к
                            # прежней эвристике: переключаем при наличии цифры «4»
                            # (CID 0017), исторически отсутствовавшей в жирном
                            # subset'е legacy-формата. Сохраняет старое поведение
                            # там, где точный набор глифов недоступен.
                            if "0017" in new_num_hex.upper():
                                needs_switch = True

            # При переносе (wrap) у числа своя dy≠0 (уходит на строку ниже);
            # иначе — исходная dy строки/ячейки.
            emit_dy = _fmt_coord(_wrap_dy) if _wrap_dy is not None else y_str

            # Голова токена всегда «X Y Td [+ своя смена шрифта]»; хвост
            # «<hex> Tj» приклеивается разделителями оригинала (_so/_sc), а не
            # жёстким пробелом — иначе Td и Tj склеиваются в одну строку там,
            # где документ их разносит (признак 2 форензик-разбора).
            _head = f"{_fmt_coord(new_x)} {emit_dy} Td".encode("ascii")
            _tail = _so + f"<{new_hex}>".encode("ascii") + _sc + b"Tj"

            if needs_switch:
                font_substitutions += 1
                # Bold-контекст: подменяем шрифт на Regular (у него есть глиф '4')
                # и, если нужно, ужимаем кегль под ширину листа, восстанавливая
                # исходный Bold-кегль после.
                _sw_size = f"{float(_ctx_fsize) * _font_scale:.3f}" if _font_scale < 1.0 else _ctx_fsize
                return (
                    _head + f" /{regular_f_name} {_sw_size} Tf".encode("ascii")
                    + _tail + f" /{_ctx_fname} {_ctx_fsize} Tf".encode("ascii")
                )
            if _font_scale < 1.0 and _active_fsize and _active_fname:
                # Ужимаем кегль числа, чтобы правый край влез в лист, и тут же
                # восстанавливаем окружающий кегль (Tf действует до следующего Tf).
                return (
                    _head
                    + f" /{_active_fname} {_active_fsize * _font_scale:.3f} Tf".encode("ascii")
                    + _tail
                    + f" /{_active_fname} {_active_fsize:g} Tf".encode("ascii")
                )
            if _inline_tf_raw:
                # Сохраняем встроенную смену шрифта/кегля этого токена (см.
                # _inline_fname выше) — иначе после замены он рисовался бы
                # уже АМБИЕНТНЫМ шрифтом, действовавшим ДО этого Td, а не
                # тем, что явно указан прямо в этом Tj-вызове.
                _inline_tf_str = _inline_tf_raw.decode("ascii").strip()
                return _head + b" " + _inline_tf_str.encode("ascii") + _tail
            return _head + _tail

        new_decompressed = td_pattern.sub(replace_callback, decompressed)

        if new_decompressed == decompressed:
            continue

        new_compressed = zlib.compress(new_decompressed)

        old_stream_len = len(raw_stream_data)
        new_stream_len = len(new_compressed)
        delta = new_stream_len - old_stream_len

        old_length_str = str(declared_length).encode()
        new_length_str = str(new_stream_len).encode()
        length_delta = len(new_length_str) - len(old_length_str)

        raw[length_start:length_end] = new_length_str
        data_start += length_delta
        endstream_pos += length_delta

        trailing_start = data_start + old_stream_len
        trailing = bytes(raw[trailing_start:endstream_pos])
        raw[data_start:endstream_pos] = new_compressed + trailing

        cumulative_offset += length_delta + delta

    print(f"\n[Halyk] Произведено замен: {total_replaced}")

    result = bytes(raw)
    # cumulative_offset отслеживает только сдвиги ОТ content-stream Td/Tj
    # замен (см. цикл выше). Патч Bold-глифов (FontFile2/W/ToUnicode) тоже
    # меняет общую длину файла, но делается ДО этого цикла и в этот
    # накопитель не попадает — если сумма length_delta+delta по всем
    # content-стримам случайно даст ровно 0 (растущие и уменьшающиеся замены
    # взаимно погасились), проверка `cumulative_offset != 0` не заметит
    # сдвиг от патча шрифта, xref не перестроится, и почти все offsets
    # окажутся битыми. Поймано на практике (HALYKformat3.pdf x2, редкий
    # розыгрыш шума — не воспроизвелось за ~200 последующих попыток, но
    # причина установлена по коду, а не предположена).
    if cumulative_offset != 0 or _newly_available_cids:
        result = _rebuild_xref_table(result)

    return result, font_substitutions, _newly_available_cids


# Сколько раз перебрать ±3% шум, пытаясь получить итоги без «недостающих» цифр.
# Один проход стоит 0.02–0.14 с на затронутых файлах (замер 2026-08-04:
# h6 0.050, HALYKformat1 0.068, HALYKformat3 0.111, hformat5 0.139 с), то есть
# худший случай перебора — около 3 с и только там, где он реально нужен.
_BOLD_GLYPH_RETRIES = 24

# Диагностика ПОСЛЕДНЕГО вызова process_halyk_pdf в этом процессе. Нужна
# автотестам, чтобы отличить «перебор не справился, потому что задача
# неразрешима» от «перебор сломался»: из одних только байт результата эти два
# случая неразличимы, а разница принципиальная. Заполняется всегда, читается
# только проверками (`verify_halyk_file.check_bold_row_uniform`); прод-логика
# на неё не опирается и опираться не должна — это не потокобезопасное
# состояние, а сведения о последнем прогоне.
LAST_RUN_INFO: Dict[str, object] = {}

# Сколько попыток минимум нужно, чтобы вообще иметь право назвать оставшуюся
# подмену неустранимой. Замер 2026-08-04 (по 100 попыток на связку): там, где
# чистый вариант достижим, он выпадает в пределах первых нескольких попыток;
# там, где нет, — не выпадает ни разу из 100.
_MIN_ATTEMPTS_TO_PROVE = 8


def process_halyk_pdf(input_bytes: bytes, target_monthly_income: float) -> bytes:
    """
    Обрабатывает Halyk PDF напрямую на уровне raw bytes.

    Использует тот же механизм hex-замены (<XXXX>Tj), что и process_pdf_bytes_raw,
    но адаптирован под структуру выписки Halyk Bank.

    **Избегаем подмены шрифта вместо того, чтобы её рисовать.** Строка итогов
    «Барлығы» целиком набрана жирным, но жирный subset содержит только те
    глифы, что печатались жирным в ОРИГИНАЛЕ, — замер на реальных файлах
    (2026-08-04): у `HALYKformat1` и `hformat5` в жирном нет цифры «4», у
    `HALYKformat3` — «3», у `h6` — сразу «1», «5» и «7». Если новая сумма
    содержит такую цифру, писатель вынужден нарисовать её Regular-шрифтом
    (`needs_switch`), и строка итогов становится разнородной: часть жирная,
    часть нет, а минус физически отделяется в свой текстовый прогон (из-за
    чего заодно уезжает центр колонки). В оригиналах таких строк нет ни одной.

    Поэтому ±3% шум пересчёта переразыгрывается до `_BOLD_GLYPH_RETRIES` раз,
    пока не выпадет вариант, где подмена не нужна вовсе. Каждая попытка —
    самостоятельно корректный пересчёт (ничего не «подгоняется», просто из
    нескольких одинаково законных розыгрышей берётся тот, что не требует
    чужого шрифта). Тот же приём и та же оговорка, что у
    `pdf_service._recalc_cert_avoiding_missing_glyphs` (Kaspi Gold): перебор
    выигрывает не всегда — у `h6` недостающих цифр три, и вероятность обойти
    все три в 7-значном итоге мала, — поэтому при исчерпании попыток
    возвращается последний результат С подменой. Отказывать в обработке
    файла из-за этого было бы хуже: всё остальное в нём корректно.
    """
    LAST_RUN_INFO.clear()
    result, subs, glyphs_patched = _process_halyk_pdf_once(input_bytes, target_monthly_income)
    attempts = 1
    if subs == 0:
        LAST_RUN_INFO.update(attempts=1, min_substitutions=0, unavoidable=False,
                              glyphs_patched=glyphs_patched)
        return result
    for attempt in range(2, _BOLD_GLYPH_RETRIES + 1):
        cand, cand_subs, cand_glyphs_patched = _process_halyk_pdf_once(input_bytes, target_monthly_income)
        attempts = attempt
        if cand_subs == 0:
            print(f"[Halyk] Подмена шрифта в строке итогов не понадобилась "
                  f"(попытка {attempt} из {_BOLD_GLYPH_RETRIES})")
            LAST_RUN_INFO.update(attempts=attempt, min_substitutions=0, unavoidable=False,
                                  glyphs_patched=cand_glyphs_patched)
            return cand
        if cand_subs < subs:
            result, subs, glyphs_patched = cand, cand_subs, cand_glyphs_patched
    # Ни одна из попыток не дала чистого варианта — это и есть ДОКАЗАТЕЛЬСТВО
    # неизбежности, полученное измерением, а не рассуждением «в числе есть
    # недостающая цифра» (последнее верно всегда и потому ничего не значит).
    # Замер 2026-08-04 на реальных файлах, по 100 попыток: у h6 итог расхода
    # «859 800,00» выпадал в 100 из 100 (он не масштабируется целью, шум его
    # не двигает), у HALYKformat1 при ×5 недостающая «4» стоит в СТАРШЕМ
    # разряде суммы ≈4,1 млн, зафиксированном порядком величины цели, — тот
    # же случай, что задокументирован для Kaspi Gold («EUR balance is always
    # 8X XXX … cannot be moved by noise»).
    # «Неизбежно» вправе утверждать только достаточно длинный перебор: при
    # искусственно урезанном бюджете (напр. _BOLD_GLYPH_RETRIES=1 в
    # мутационном тесте) одна неудачная попытка ничего не доказывает, и
    # выдавать по ней поблажку — значит снова получить проверку, которая
    # не умеет краснеть.
    LAST_RUN_INFO.update(
        attempts=attempts,
        min_substitutions=subs,
        unavoidable=attempts >= _MIN_ATTEMPTS_TO_PROVE,
        glyphs_patched=glyphs_patched,
    )
    print(f"[Halyk] ⚠️ Не удалось избежать подмены шрифта за {_BOLD_GLYPH_RETRIES} "
          f"попыток: осталось {subs} — в жирном subset'е нет нужных цифр, и "
          f"итоговые суммы этой цели их не обходят ни при одном розыгрыше.")
    return result


# ─── Валидация ────────────────────────────────────────────────────────────────

def validate_halyk(pdf_bytes: bytes) -> dict:
    """
    Проверяет целостность выписки Halyk Bank.
    Возвращает dict совместимый с форматом /verify endpoint.
    """
    import zlib as _zlib

    checks = []
    issues = []
    raw = pdf_bytes

    # 1. Целостность zlib-стримов (/FlateDecode)
    # Слайс ТОЧНО по /Length, а не по эвристике "endswith \r\n / \n" — тот же
    # баг/фикс, что и в process_kaspi_ip_pdf/validate_kaspi_ip (kaspi_ip_pdf_
    # service.py): компрессированные байты сами МОГУТ заканчиваться на 0x0D
    # ('\r'), и тогда suffix-эвристика ошибочно принимает последний байт
    # полезной нагрузки за настоящий CRLF-перевод строки перед "endstream" и
    # отрезает на 1 байт больше, чем нужно — decompress падает на полностью
    # корректном PDF (ложное "N битых стримов" после легитимной пересборки
    # потока в process_halyk_pdf, воспроизведено на реальном образце).
    stream_errors = 0
    for m in re.finditer(rb"(\d+)\s+0\s+obj", raw):
        hdr_end = raw.find(b"stream", m.end(), m.end() + 500)
        if hdr_end < 0:
            continue
        hdr = raw[m.end():hdr_end]
        if b"FlateDecode" not in hdr:
            continue
        length_m = re.search(rb"/Length\s+(\d+)", hdr)
        if not length_m:
            continue
        ds = hdr_end + 6
        if raw[ds:ds+1] == b'\r':
            ds += 2
        else:
            ds += 1
        sd = raw[ds:ds + int(length_m.group(1))]
        try:
            _zlib.decompress(sd)
        except Exception:
            stream_errors += 1

    ok_str = stream_errors == 0
    checks.append({"name": "Целостность стримов", "ok": ok_str,
                   "detail": "OK" if ok_str else f"{stream_errors} битых стримов"})
    if not ok_str:
        issues.append(f"{stream_errors} стримов не декомпрессируются")

    # 2. xref таблица
    xref_bad = 0
    xref_match = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n", raw)
    if xref_match:
        count = int(xref_match.group(2))
        first_entry = xref_match.end()
        for i in range(count):
            entry = raw[first_entry + i * 20: first_entry + (i + 1) * 20]
            if len(entry) < 20:
                break
            offset = int(entry[:10])
            flag = entry[17:18]
            if flag == b'n' and offset > 0:
                obj_id = int(xref_match.group(1)) + i
                expected = f"{obj_id} 0 obj".encode()
                if raw[offset:offset + len(expected)] != expected:
                    xref_bad += 1
    ok_xref = xref_bad == 0
    checks.append({"name": "xref таблица", "ok": ok_xref,
                   "detail": "Все offsets корректны" if ok_xref else f"{xref_bad} битых offsets"})
    if not ok_xref:
        issues.append(f"xref: {xref_bad} битых offsets")

    # 3. Парсинг выписки
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    stmt = parse_halyk_statement(doc)
    doc.close()

    txs = stmt.transactions

    # 4. Зарплатные поступления найдены
    sal_count = len([t for t in txs if t.is_salary])
    ok_sal = sal_count > 0
    checks.append({"name": "Зарплатные поступления", "ok": ok_sal,
                   "detail": f"Найдено: {sal_count} транзакций (Қаражаттың шотқа түсуі)"})
    if not ok_sal:
        issues.append("Зарплатные поступления не найдены")

    # 5. ISI по зарплатным поступлениям
    month_sal: Dict[str, float] = defaultdict(float)
    for t in txs:
        if t.is_salary and t.kiri_s > 0:
            mk = t.op_date[3:]
            month_sal[mk] += t.kiri_s
    vals = list(month_sal.values())
    if len(vals) >= 2:
        mu = sum(vals) / len(vals)
        sigma = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
        isi = max(0.0, 1.0 - sigma / mu) if mu > 0 else 0.0
    else:
        isi = 1.0
    ok_isi = isi >= 0.75
    checks.append({"name": "ISI (стабильность зарплаты)", "ok": ok_isi,
                   "detail": f"ISI = {isi:.4f} (мин. 0.75) | месяцев: {len(month_sal)}"})
    if not ok_isi:
        issues.append(f"ISI = {isi:.4f} < 0.75")

    # 6. Баланс (транзакции): opening + кіріс − шығыс − комиссия = closing
    # (только KZT-операции — суммы в USD/EUR/... не входят в тенговый баланс)
    sum_kiri_s = sum(t.kiri_s for t in txs if t.kiri_s > 0 and t.currency == "KZT")
    sum_shyghys = sum(abs(t.shyghys) for t in txs if t.shyghys < 0 and t.currency == "KZT")
    calc_closing = round(stmt.opening_balance + sum_kiri_s - sum_shyghys - abs(stmt.total_commission), 2)
    delta_bal = round(stmt.closing_balance - calc_closing, 2)
    ok_bal = abs(delta_bal) < 500.0
    checks.append({"name": "Баланс (транзакции)", "ok": ok_bal,
                   "detail": (f"opening={stmt.opening_balance:,.2f} + кіріс={sum_kiri_s:,.2f}"
                              f" − шығыс={sum_shyghys:,.2f} − comm={abs(stmt.total_commission):,.2f}"
                              f" = {calc_closing:,.2f} | closing={stmt.closing_balance:,.2f} | Δ={delta_bal:+,.2f}")})
    if not ok_bal:
        issues.append(f"Баланс: Δ = {delta_bal:+,.2f} ₸")

    # 7. Итого (шапка) — «Барлығы:» строка должна сходиться с суммой транзакций
    delta_kiri_hdr = round(stmt.total_kiri_s - sum_kiri_s, 2)
    delta_shyghys_hdr = round(abs(stmt.total_shyghys) - sum_shyghys, 2)
    ok_hdr = abs(delta_kiri_hdr) < 1.0 and abs(delta_shyghys_hdr) < 1.0
    checks.append({"name": "Итого (шапка)", "ok": ok_hdr,
                   "detail": (f"кіріс: шапка={stmt.total_kiri_s:,.2f} vs транзакции={sum_kiri_s:,.2f} (Δ={delta_kiri_hdr:+,.2f})"
                              f" | шығыс: шапка={abs(stmt.total_shyghys):,.2f} vs транзакции={sum_shyghys:,.2f} (Δ={delta_shyghys_hdr:+,.2f})")})
    if not ok_hdr:
        issues.append(f"Итого (шапка) не сходится с транзакциями: Δкіріс={delta_kiri_hdr:+,.2f}, Δшығыс={delta_shyghys_hdr:+,.2f}")

    # 8. Running balance — цепочка по датам (от старых к новым), проверка на уход в минус
    def _sort_key(t: HalykTransaction):
        try:
            return datetime.strptime(t.op_date, "%d.%m.%Y")
        except ValueError:
            return datetime.max

    sorted_txs = sorted(txs, key=_sort_key)
    rb = stmt.opening_balance
    for t in sorted_txs:
        if t.currency != "KZT":
            continue  # суммы в USD/EUR/... не входят в тенговый баланс
        rb = round(rb + t.kiri_s - abs(t.shyghys), 2)
    # Комиссия — одним списанием в конце (не авансом): она агрегирована только
    # в шапке, отдельных транзакций-комиссий нет, и front-loading создавал
    # фантомный минус в середине периода. См. recalculate_halyk._min_running_balance.
    rb = round(rb - abs(stmt.total_commission), 2)
    delta_rb = round(rb - stmt.closing_balance, 2)
    ok_rb = abs(delta_rb) < 500.0
    checks.append({"name": "Running balance", "ok": ok_rb,
                   "detail": f"Финальный RB = {rb:,.2f} | closing = {stmt.closing_balance:,.2f} | Δ = {delta_rb:+,.2f}"})
    if not ok_rb:
        issues.append(f"Running balance: Δ = {delta_rb:+,.2f} ₸")

    # Минимум — на границах дней (см. _halyk_dayend_min_rb): op_date не содержит
    # времени, порядок нескольких транзакций одной даты в PDF произволен, и
    # проверка после КАЖДОЙ отдельной транзакции ложно ловит внутридневные
    # дипы, которые исчезают к концу того же дня. Этот класс бага реально
    # ловился на Kaspi Gold (немодифицированный gold_statement.pdf уходил в
    # −54,17 ₸ на одной дате из-за порядка пяти дебетов перед двумя
    # покрывающими их кредитами того же дня) — здесь применяем тот же
    # инвариант защитно, хотя на имеющихся 4 реальных Halyk-файлах расхождения
    # с per-transaction найдено не было.
    # _halyk_dayend_min_rb не учитывает комиссию (у неё нет своей даты — см. её
    # докстринг), поэтому берём минимум из (день-минимум по транзакциям, финал
    # ПОСЛЕ комиссии = `rb`, уже посчитан выше) — комиссия одним списанием в
    # самом конце периода, это отдельная граничная точка.
    day_min_rb = min(
        _halyk_dayend_min_rb(sorted_txs, stmt.opening_balance, use_scaled=False),
        rb,
    )
    rb_negative = 1 if day_min_rb < 0 else 0
    ok_rb_neg = rb_negative == 0
    checks.append({"name": "Баланс ≥ 0", "ok": ok_rb_neg,
                   "detail": f"Мин. баланс на границах дней: {day_min_rb:,.2f} ₸"})
    if not ok_rb_neg:
        issues.append(f"Баланс уходит в минус (мин. на границе дня: {day_min_rb:,.2f} ₸)")

    n_months = len(month_sal) or 1
    avg_monthly = sum_kiri_s / n_months

    # suggested_min обязан отражать ОБА floor'а recalculate_halyk, а не только
    # «too_aggressive» (30% от среднего). Второй floor — «below_balance_floor»:
    # доход должен покрыть совокупный расход (шығыс + комиссия) за вычетом
    # стартового остатка плюс запас безопасности. На выписках Halyk именно он, а
    # не 30%-порог, обычно задаёт минимум (расход ≫ 30% дохода) — раньше /verify
    # подсказывал значение в разы НИЖЕ него, и пользователь, введя его в
    # /process, получал IncomeTooLowError(below_balance_floor). Средний доход
    # берём по ЗАРПЛАТНЫМ месяцам (= current_monthly_avg в recalculate_halyk), а
    # не по всему кіріс. Округляем ВВЕРХ (ceil до тенге): round-вниз оставил бы
    # подсказку на доли тенге ниже порога, и при обратной подаче (деление на
    # _INCOME_K) она снова упала бы в IncomeTooLowError.
    salary_monthly_avg = (sum(month_sal.values()) / n_months) if month_sal else 0.0
    total_out = abs(stmt.total_shyghys) + abs(stmt.total_commission)
    required_total_income = total_out - stmt.opening_balance + _SAFETY_MARGIN
    floor_below_balance = required_total_income / n_months if required_total_income > 0 else 0.0
    floor_aggressive = salary_monthly_avg * _MAX_DOWNSCALE_FACTOR
    min_desired = float(math.ceil(max(floor_below_balance, floor_aggressive) * _INCOME_K))

    passed = len(issues) == 0
    return {
        "passed": passed,
        "checks": checks,
        "issues": issues,
        "summary": {
            "balance_start": stmt.opening_balance,
            "balance_end": stmt.closing_balance,
            "total_income": sum_kiri_s,
            "total_expense": sum_shyghys,
            "transactions": len(txs),
            "months": n_months,
            "isi": round(isi, 4),
            "avg_monthly_income": round(avg_monthly, 2),
            "suggested_min": min_desired,
        },
    }

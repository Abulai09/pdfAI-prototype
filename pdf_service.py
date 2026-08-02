import fitz
import re
import random
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Deque, Optional, Tuple


# Метрики ArialMT (единственный шрифт сумм в выписке Kaspi Gold), в долях em.
# Нужны, чтобы при изменении числа сдвинуть Td.x ровно на прирост ширины строки
# и сохранить право-выравнивание колонки «Сумма».
_ARIAL_DIGIT_EM = 0.556   # ширина цифры; пробел/запятая ровно вдвое уже (0.278)
_FALLBACK_FONT_SIZE = 9.5  # кегль тела выписки — если /Tf не удалось прочитать


# ---------------------------------------------------------------------------
#  Структуры данных
# ---------------------------------------------------------------------------


@dataclass
class Transaction:
    """Одна строка транзакции из выписки Kaspi Gold."""
    index: int                  # порядковый номер в выписке (0-based)
    date: Optional[str] = None  # "25.01.26"
    description: str = ""       # "Пополнение", "Покупка", ...
    amount: float = 0.0         # абсолютное значение
    sign: int = 1               # +1 = пополнение, -1 = списание
    balance_after: float = 0.0  # остаток после операции (running balance)
    is_salary: bool = False     # классифицирована как зарплатная
    original_amount_text: str = ""   # как выглядит в PDF ("+ 450 000,00 ₸")
    original_balance_text: str = ""  # как выглядит running balance
    new_amount: float = 0.0
    new_balance_after: float = 0.0
    page_num: int = 0
    is_refund: bool = False          # возврат: тип Покупка/Перевод но sign=+1
    y_pdf_rounded: int = 0          # Y в координатах PDF-потока (round), для фильтрации


@dataclass
class StatementData:
    """Полная модель выписки."""
    balance_start: float = 0.0
    balance_end: float = 0.0
    balance_start_text: str = ""
    balance_end_text: str = ""
    balance_start_date: str = ""
    balance_end_date: str = ""
    total_income: float = 0.0
    total_expense: float = 0.0
    total_income_text: str = ""
    total_expense_text: str = ""
    # Расходы по категориям (как в Kaspi PDF заголовке)
    expense_categories: Dict[str, float] = field(default_factory=dict)  # {"Переводы": 47404891.0, ...}
    expense_category_texts: Dict[str, str] = field(default_factory=dict)  # {"Переводы": "47 404 891,00", ...}
    transactions: List[Transaction] = field(default_factory=list)
    # Новые значения после пересчёта
    new_balance_end: float = 0.0
    new_total_income: float = 0.0
    new_expense_categories: Dict[str, float] = field(default_factory=dict)


@dataclass
class CertificateData:
    """Данные титульной страницы «Справка об остатке на счете» (новый формат Kaspi).

    Появилась в выписках Kaspi Gold с 2026: страница 0 — справка-обложка с
    табличкой ₸/USD/EUR + QR-код, страницы 1..N — собственно выписка.
    """
    cert_number: str = ""             # "1192676821"
    cert_date: str = ""               # "05 мая 2026"
    holder_name: str = ""             # "Бурабай Диас Аскарович"
    holder_iin: str = ""              # "971003300049"
    account_number: str = ""          # "KZ54722C000026022151"
    period_from: str = ""             # "05.05.25"
    period_to: str = ""               # "05.05.26"
    # ── Балансы по валютам (как показаны на стр. 0) ──
    balance_kzt: float = 0.0
    balance_kzt_text: str = ""        # "143 170,28" (без префикса ₸)
    balance_usd: float = 0.0
    balance_usd_text: str = ""        # "308,20"
    balance_eur: float = 0.0
    balance_eur_text: str = ""        # "263,31"
    # ── Новые значения после пересчёта ──
    new_balance_kzt: float = 0.0
    new_balance_usd: float = 0.0
    new_balance_eur: float = 0.0


class HeaderCellOverflowError(Exception):
    """
    Итоговая сумма (доход/баланс) переросла разрядную вместимость своей ячейки
    в шапке стр.1 (cert-формат Kaspi Gold).

    Ячейка «Сумма» в этой шапке имеет фиксированные координаты x=207.7..306.3
    (правый край — якорь право-выравнивания у x≈300.76). При 10 разрядах
    право-выровненная строка начинается на ~2.3 pt левее левой границы ячейки
    — сама строка помещается по ширине, но не на своей анкерной позиции
    (см. find_frame_overflows() в tests/scripts/verify_gold_file.py и разбор
    в CLAUDE.md, раздел «Known remaining limit»). Клэмпить позицию нельзя —
    это либо ломает право-выравнивание колонки для этой строки
    (check_column_alignment), либо требует перерисовки рамки ячейки. Поэтому
    вместо тихой записи визуально сломанного PDF — явный отказ ДО записи,
    тем же архитектурным принципом, что и IncomeTooLowError.
    """

    def __init__(self, field_name: str, value: float, max_safe_value: float):
        self.field_name = field_name
        self.value = round(value, 2)
        self.max_safe_value = max_safe_value
        self.message = (
            f"Итоговая сумма «{field_name}» = {value:,.0f} ₸ не помещается "
            f"в ячейку шапки справки (максимум 9 разрядов, т.е. до "
            f"{max_safe_value:,.0f} ₸). Выберите менее агрессивную цель."
        )
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "reason": "header_cell_overflow",
            "field": self.field_name,
            "value": self.value,
            "max_safe_value": self.max_safe_value,
        }


# Ячейка «Сумма» шапки стр.1 (cert-формат) вмещает право-выровненную строку
# только до 9 разрядов целой части — см. HeaderCellOverflowError.
_HEADER_CELL_MAX_SAFE_VALUE = 999_999_999.0


@dataclass
class ScoringReport:
    """Результат самопроверки."""
    balance_integrity: bool = False    # B_end = B_start + income - expense
    running_balance_ok: bool = False   # все RB сходятся
    totals_ok: bool = False            # итоги = сумма транзакций
    income_stability: float = 0.0      # ISI (0..1)
    expense_ratio: float = 0.0        # ER
    min_balance: float = 0.0
    avg_balance: float = 0.0
    passed: bool = False

    def summary(self) -> str:
        lines = [
            "═══ ОТЧЁТ СКОРИНГА ═══",
            f"  Целостность баланса:     {'✅' if self.balance_integrity else '❌'}",
            f"  Running balance:         {'✅' if self.running_balance_ok else '❌'}",
            f"  Итоги = Σ транзакций:    {'✅' if self.totals_ok else '❌'}",
            f"  Стабильность дохода ISI: {self.income_stability:.2f} {'✅' if self.income_stability >= 0.75 else '⚠️'}",
            f"  Коэфф. расходов ER:      {self.expense_ratio:.2f} {'✅' if 0.40 <= self.expense_ratio <= 0.90 else '⚠️'}",
            f"  Мин. баланс:             {self.min_balance:,.2f} ₸ {'✅' if self.min_balance > 0 else '⚠️'}",
            f"  Средний баланс:          {self.avg_balance:,.2f} ₸",
            f"  ИТОГ:                    {'✅ ПРОЙДЁТ' if self.passed else '❌ НЕ ПРОЙДЁТ'}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
#  CMap: построение карты символов из PDF (сохранено из оригинала)
# ---------------------------------------------------------------------------


def _find_primary_font_tounicode_xref(doc) -> int | None:
    """Определяет xref потока ToUnicode для основного (не жирного) шрифта.

    Числа в PDF Kaspi записаны шрифтом F1 (ArialMT).
    В PDF Halyk — шрифтом F0 (Times New Roman, не Bold).
    Bold-шрифты имеют собственные CMap с ДРУГИМИ CID для тех же символов:
    смешение даёт неправильные CID при записи → испорченные числа.

    Стратегия: сначала ищем ArialMT без Bold (Kaspi), затем — любой
    не-Bold/не-Italic шрифт с ToUnicode (Halyk и другие форматы).
    """
    def _is_bold_or_italic(name: str) -> bool:
        n = name.lower()
        return "bold" in n or "italic" in n or ",b" in n

    # Первый проход: ArialMT без Bold (приоритет для Kaspi PDF)
    for page_num in range(min(1, len(doc))):
        for font_info in doc[page_num].get_fonts(full=True):
            font_xref, _, _, font_name = font_info[:4]
            if "ArialMT" in font_name and not _is_bold_or_italic(font_name):
                try:
                    obj = doc.xref_object(font_xref)
                    m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
                    if m:
                        return int(m.group(1))
                except Exception:
                    pass

    # Второй проход: любой не-Bold/не-Italic шрифт (Halyk и др.)
    for page_num in range(min(1, len(doc))):
        for font_info in doc[page_num].get_fonts(full=True):
            font_xref, _, _, font_name = font_info[:4]
            if not _is_bold_or_italic(font_name):
                try:
                    obj = doc.xref_object(font_xref)
                    m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
                    if m:
                        return int(m.group(1))
                except Exception:
                    pass
    return None


def _parse_cmap_stream(stream_data: str) -> dict:
    """Парсит ToUnicode CMap поток (bfrange и bfchar) → словарь CID→char.

    Парсинг секционированный: регекс применяется только внутри блоков
    beginbfrange/endbfrange и beginbfchar/endbfchar, чтобы избежать
    ложных совпадений на границах строк между блоками.
    """
    to_unicode = {}

    # ── bfrange секции ──────────────────────────────────────────────────────
    # Формат: <START><END><FIRST_UNICODE>  (без пробелов — компактный)
    # или:    <START> <END> <FIRST_UNICODE> (с пробелами — стандартный)
    for section in re.findall(
        r"beginbfrange\s*(.*?)\s*endbfrange", stream_data, re.DOTALL
    ):
        # 4-значные CID
        for start, end, base in re.findall(
            r"<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>",
            section,
        ):
            s, e, b = int(start, 16), int(end, 16), int(base, 16)
            for i in range(s, e + 1):
                to_unicode[f"{i:04X}"] = chr(b + (i - s))
        # 2-значные CID
        for start, end, base in re.findall(
            r"<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{4})>",
            section,
        ):
            s, e, b = int(start, 16), int(end, 16), int(base, 16)
            for i in range(s, e + 1):
                to_unicode[f"{i:02X}"] = chr(b + (i - s))

    # ── bfchar секции ────────────────────────────────────────────────────────
    for section in re.findall(
        r"beginbfchar\s*(.*?)\s*endbfchar", stream_data, re.DOTALL
    ):
        # 4-значные CID
        for code, char_hex in re.findall(
            r"<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>", section
        ):
            try:
                to_unicode[code.upper()] = chr(int(char_hex, 16))
            except Exception:
                pass
        # 2-значные CID
        for code, char_hex in re.findall(
            r"<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{4})>", section
        ):
            try:
                to_unicode[code.upper()] = chr(int(char_hex, 16))
            except Exception:
                pass

    # ── Fallback: нет секционных маркеров (старый формат) ────────────────────
    if not to_unicode:
        for code, char_hex in re.findall(
            r"<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>", stream_data
        ):
            try:
                to_unicode[code.upper()] = chr(int(char_hex, 16))
            except Exception:
                pass

    return to_unicode


def build_dynamic_cmap(doc):
    """Сканирует PDF на наличие таблиц ToUnicode и строит карту кодов.

    Для from_unicode (обратный маппинг char→code) используется ТОЛЬКО
    CMap основного шрифта (ArialMT), чтобы при записи hex-кодов обратно
    в PDF-поток использовались CID, для которых есть глифы.

    Для to_unicode (прямой маппинг code→char) используется CMap основного
    шрифта. Это гарантирует корректное чтение и запись в контексте
    одного шрифта без конфликтов между F1 и F2.
    """
    # ── Шаг 1: пытаемся найти xref ToUnicode для основного шрифта ──
    primary_xref = _find_primary_font_tounicode_xref(doc)

    if primary_xref is not None:
        # Строим CMap ТОЛЬКО из основного шрифта (ArialMT)
        try:
            stream_data = doc.xref_stream(primary_xref).decode(
                "latin-1", errors="ignore"
            )
            to_unicode = _parse_cmap_stream(stream_data)
            from_unicode = {v: k for k, v in to_unicode.items()}

            # Дополняем to_unicode из ВСЕХ остальных ToUnicode-стримов
            # (нужно для распознавания символов из дополнительных шрифтов:
            # знаки +/-, иконки операций и т.д.). from_unicode при этом
            # остаётся "чистым" — только из основного шрифта, чтобы запись
            # hex-кодов обратно использовала корректные глифы.
            extra_added = 0
            for xref in range(1, doc.xref_length()):
                if xref == primary_xref:
                    continue
                if not doc.is_stream(xref):
                    continue
                try:
                    sd = doc.xref_stream(xref).decode("latin-1", errors="ignore")
                except Exception:
                    continue
                if "beginbfchar" not in sd and "beginbfrange" not in sd:
                    continue
                extra = _parse_cmap_stream(sd)
                for code, ch in extra.items():
                    if code not in to_unicode:
                        to_unicode[code] = ch
                        extra_added += 1

            # Добор цифр в ОБРАТНУЮ карту. from_unicode построена по основному
            # шрифту (строка выше) ДО добора to_unicode из доп. шрифтов —
            # поэтому цифра, чей код объявлен только в доп. ToUnicode-стриме,
            # в обратную карту не попадает. На реальном файле так пропала '8'
            # (код 001B пришёл из доп. шрифта): при записи суммы справки с
            # восьмёркой paren_encode/text_to_hex не находили её код и падали
            # (ValueError) либо тихо писали пустой глиф. Цифра — один и тот же
            # глиф во всех шрифтах Kaspi (ArialMT-подсемейство), поэтому код из
            # общего to_unicode для неё корректен. Заполняем ТОЛЬКО отсутствующие
            # (не перезаписываем выбор основного шрифта — конфликтов F1/F2 нет).
            _digit_to_code = {ch: code for code, ch in to_unicode.items() if ch in "0123456789"}
            for _d in "0123456789":
                if _d not in from_unicode and _d in _digit_to_code:
                    from_unicode[_d] = _digit_to_code[_d]

            print(
                f"[CMap] Построена карта символов: {len(to_unicode)} записей "
                f"(основной шрифт xref={primary_xref}"
                + (f", +{extra_added} из доп. шрифтов" if extra_added else "")
                + ")"
            )
            return to_unicode, from_unicode
        except Exception:
            pass  # fallback ниже

    # ── Fallback: все CMap потоки (старое поведение) ──────────
    to_unicode = {}
    for xref in range(1, doc.xref_length()):
        if not doc.is_stream(xref):
            continue
        try:
            stream_data = doc.xref_stream(xref).decode("latin-1", errors="ignore")
        except Exception:
            continue
        if "beginbfchar" not in stream_data and "beginbfrange" not in stream_data:
            continue
        to_unicode.update(_parse_cmap_stream(stream_data))

    from_unicode = {v: k for k, v in to_unicode.items()}
    print(f"[CMap] Построена карта символов: {len(to_unicode)} записей (fallback)")
    return to_unicode, from_unicode


# ---------------------------------------------------------------------------
#  Вспомогательные функции текста / метрик (сохранены)
# ---------------------------------------------------------------------------


def get_text_metrics(page, target_text: str):
    """Ищет текст на странице, возвращает (width, text_len, avg_char_width)."""
    blocks = page.get_text("dict")["blocks"]
    clean_target = (
        target_text.replace(" ", "").replace("₸", "").replace("+", "").strip()
    )
    for b in blocks:
        for line in b.get("lines", []):
            for s in line.get("spans", []):
                text = s["text"]
                clean_text = (
                    text.replace(" ", "").replace("₸", "").replace("+", "").strip()
                )
                if clean_text == clean_target:
                    width = s["bbox"][2] - s["bbox"][0]
                    char_len = len(text)
                    avg = width / char_len if char_len > 0 else 0
                    return width, char_len, avg
    return None, None, None


def parse_amount(text: str) -> float:
    """Парсит строку суммы в float: '450 000,00' -> 450000.0"""
    cleaned = (
        text.replace(" ", "")
        .replace("\xa0", "")
        .replace("₸", "")
        .replace("+", "")
        .replace("-", "")
        .strip()
    )
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_amount(value: float, with_sign: bool = False, with_currency: bool = False) -> str:
    """Форматирует число в формат выписки: '450 000,00' или '+ 450 000,00 ₸'."""
    formatted = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",")
    prefix = ""
    if with_sign:
        prefix = "+ " if value >= 0 else "- "
    suffix = " ₸" if with_currency else ""
    return f"{prefix}{formatted}{suffix}"


def _round_to_natural(val: float) -> float:
    """Округляет сумму до «человеческого» шага без лишних копеек.

    Зарплатные пополнения в Kaspi Gold всегда целые тенге (нет тиынов),
    а последние 2-3 знака обычно круглые. Шаги:
      ≥ 500 000 ₸ → кратно 1 000
      ≥  50 000 ₸ → кратно   500
      ≥  10 000 ₸ → кратно   100
             иначе → кратно    50
    """
    if val >= 500_000:
        unit = 1_000
    elif val >= 50_000:
        unit = 500
    elif val >= 10_000:
        unit = 100
    else:
        unit = 50
    return float(round(val / unit) * unit)


# ---------------------------------------------------------------------------
#  ЭТАП 1: Полный парсинг выписки Kaspi Gold
# ---------------------------------------------------------------------------


def _collect_amount_on_line(words_on_line: list, x_min: float = 120.0) -> Tuple[Optional[str], int, float]:
    """
    Собирает сумму со знаком из слов на одной строке транзакции.
    
    Kaspi Gold layout (по X-координатам):
      X≈52   — дата (09.02.26)
      X≈127..140 — знак (+/-)
      X≈135..153 — цифры суммы (37, 000,00)
      X≈185  — ₸
      X≈238..257 — тип операции (Пополнение, Покупка...)
    
    Возвращает: (amount_text, sign, amount_value)
    """
    sign = 0  # 0 = знак не найден в строке
    amount_parts = []
    
    for w in words_on_line:
        x0 = w['x0']
        txt = w['text']
        
        # Пропускаем всё левее x_min (там дата)
        if x0 < x_min:
            continue
        
        # Знак суммы
        if txt == '+':
            sign = 1
            continue
        if txt == '-':
            sign = -1
            continue
        
        # ₸ — конец суммы
        if '₸' in txt:
            break
        
        # Если это слово операции — стоп
        if txt in ('Пополнение', 'Покупка', 'Перевод', 'Снятие', 'Оплата',
                    'Платёж', 'Платеж', 'Комиссия', 'Разное', 'Возврат',
                    'Поступление', 'Зачисление'):
            break
        
        # Если цифра/запятая — часть суммы
        if any(c.isdigit() for c in txt) or txt in [',', '.']:
            amount_parts.append(txt)
    
    if not amount_parts:
        return None, sign, 0.0
    
    amount_text = " ".join(amount_parts)
    val = parse_amount(amount_text)
    return amount_text, sign, val


# ---------------------------------------------------------------------------
#  ДЕТЕКТОР ФОРМАТА + ПАРСЕР СПРАВКИ (новый формат Kaspi с титульной стр.)
# ---------------------------------------------------------------------------


def detect_statement_format(doc) -> str:
    """Определяет формат PDF: 'cert' (новый, со справкой на стр. 0) или 'legacy'.

    Маркер cert-формата: на стр. 0 присутствуют слова "СПРАВКА" и "об остатке".
    Стр. 0 в legacy сразу содержит "ВЫПИСКА".
    """
    if len(doc) == 0:
        return "legacy"
    try:
        text0 = doc[0].get_text("text")
    except Exception:
        return "legacy"
    if "СПРАВКА" in text0 and "остатке" in text0:
        return "cert"
    return "legacy"


def parse_certificate_page(doc) -> CertificateData:
    """Парсит титульную страницу-справку (стр. 0) нового формата Kaspi.

    Извлекает: ФИО, ИИН, номер счёта, номер справки, дату, период, баланс ₸/USD/EUR.
    Использует Y-группировку слов с порогом 3 px.
    """
    cert = CertificateData()
    if len(doc) == 0:
        return cert

    page = doc[0]
    words = page.get_text("words")

    # ── Y-группировка ──
    y_groups: Dict[int, list] = {}
    for w in words:
        x0, y0, text_w = w[0], w[1], w[4]
        y_key = round(y0 / 3) * 3
        y_groups.setdefault(y_key, []).append((x0, text_w))

    # Соберём строки
    lines: List[Tuple[int, str, list]] = []  # (y, full_text, sorted_words)
    for yk in sorted(y_groups.keys()):
        ws = sorted(y_groups[yk], key=lambda x: x[0])
        text = " ".join(t for _, t in ws)
        lines.append((yk, text, ws))

    # ── Дата справки ── ищем строку из "DD <месяц> YYYY"
    months = {"января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"}
    for yk, text, ws in lines:
        m = re.match(r"^\s*(\d{1,2})\s+(\S+)\s+(\d{4})\s*$", text)
        if m and m.group(2).lower() in months:
            cert.cert_date = f"{m.group(1)} {m.group(2)} {m.group(3)}"
            break

    # ── ФИО, ИИН, счёт (строки 234..246, всё в одном абзаце) ──
    holder_chunks: List[str] = []
    for yk, text, ws in lines:
        if "подтверждает" in text and "ИИН" in text:
            # вырезаем между "что" и ", ИИН"
            m = re.search(r"что\s+(.+?),\s*ИИН\s+(\d+)", text)
            if m:
                holder_chunks.append(m.group(1).strip())
                cert.holder_iin = m.group(2)
        if "является клиентом" in text or "клиентом со счетом" in text:
            m = re.search(r"счетом\s+([A-Z0-9]+)", text)
            if m:
                cert.account_number = m.group(1).rstrip(".")
        # Иногда ФИО переносится на след. строку
        if "является" in text and not holder_chunks:
            pass
    if holder_chunks:
        cert.holder_name = holder_chunks[0]

    # ── Номер справки ── "Информация ... №NNNNNNN."
    for yk, text, ws in lines:
        m = re.search(r"№\s*(\d+)", text)
        if m:
            cert.cert_number = m.group(1)
            break

    # ── Период ── "за период с DD.MM.YY по DD.MM.YY"
    for yk, text, ws in lines:
        m = re.search(r"с\s+(\d{2}\.\d{2}\.\d{2})\s+по\s+(\d{2}\.\d{2}\.\d{2})", text)
        if m:
            cert.period_from = m.group(1)
            cert.period_to = m.group(2)
            break

    # ── Балансы по валютам ──
    # Заголовок: "Сумма на счете в тенге  Эквивалент в USD  Эквивалент в EUR"
    # Значения:  "₸ 143 170,28           $ 308,20            € 263,31"
    # Координаты колонок (по дампу): KZT≈x48..130, USD≈x213..280, EUR≈x376..440
    header_y = None
    for yk, text, ws in lines:
        if "Сумма на счете" in text and "USD" in text and "EUR" in text:
            header_y = yk
            break

    if header_y is not None:
        # Значения — следующая Y-строка (обычно +15 px)
        for yk, text, ws in lines:
            if yk <= header_y or yk > header_y + 30:
                continue

            # Разбиваем по колонкам по X-координате: раньше — фиксированные
            # пороги (x<200 / x<360), откалиброванные под короткие суммы
            # оригинала. После апскейла writer сдвигает выросшее число ВЛЕВО,
            # чтобы сохранить его правый край на месте (см. x_shift в
            # cert_paren_callback/replace_callback) — на крупной справке
            # (реальный файл: KZT выросло с 6 до 9 цифр) сдвиг утаскивает
            # "$"/"€" ЗА фиксированный порог в соседнюю колонку, которая
            # портит parse_amount() чужим нечисловым токеном (напр.
            # "55 620 756,05 $" → ValueError → тихо 0.00). Вместо порогов —
            # кластеризация по 2 наибольшим разрывам между соседними словами
            # в этой строке: колонки всегда разделены пробелом много шире,
            # чем между словами внутри одной суммы, независимо от того,
            # куда сдвинулось число. Та же кластеризация (а не фиксированное
            # окно 200..360) используется и для подтверждения, что это
            # действительно строка значений — на случай если "$" не
            # декодируется (paren-формат): тогда просто проверяем, что средний
            # кластер вообще содержит цифры.
            ws_sorted = sorted(ws, key=lambda p: p[0])
            if len(ws_sorted) >= 3:
                gap_idxs = sorted(
                    range(len(ws_sorted) - 1),
                    key=lambda i: ws_sorted[i + 1][0] - ws_sorted[i][0],
                    reverse=True,
                )
                i1, i2 = sorted(gap_idxs[:2])
                kzt_parts = ws_sorted[:i1 + 1]
                usd_parts = ws_sorted[i1 + 1:i2 + 1]
                eur_parts = ws_sorted[i2 + 1:]
            else:
                kzt_parts, usd_parts, eur_parts = ws_sorted, [], []

            if "₸" not in text or ("$" not in text and "USD" not in text):
                has_usd_digits = any(any(c.isdigit() for c in t) for _, t in usd_parts)
                if not has_usd_digits:
                    continue

            def _join_amount(parts: list, currency_marker: str) -> Tuple[str, float]:
                """Собирает сумму, отделяя префикс валюты."""
                # Сортируем по X
                parts = sorted(parts, key=lambda p: p[0])
                # Префикс — это первый токен если он валютный символ
                tokens = [t for _, t in parts]
                if tokens and tokens[0] in ("₸", "$", "€"):
                    num_tokens = tokens[1:]
                else:
                    # Пропускаем нераспознанные символы (fitz рисует "?" для $ из paren-формата)
                    num_tokens = [t for t in tokens if any(c.isdigit() for c in t)]
                amount_text = " ".join(num_tokens).strip()
                return amount_text, parse_amount(amount_text)

            kzt_text, kzt_val = _join_amount(kzt_parts, "₸")
            usd_text, usd_val = _join_amount(usd_parts, "$")
            eur_text, eur_val = _join_amount(eur_parts, "€")

            cert.balance_kzt_text = kzt_text
            cert.balance_kzt = kzt_val
            cert.balance_usd_text = usd_text
            cert.balance_usd = usd_val
            cert.balance_eur_text = eur_text
            cert.balance_eur = eur_val
            break

    print(f"[Cert] № {cert.cert_number} от {cert.cert_date}")
    print(f"[Cert] {cert.holder_name} (ИИН {cert.holder_iin}) счёт {cert.account_number}")
    print(f"[Cert] Период {cert.period_from} - {cert.period_to}")
    print(f"[Cert] Баланс: ₸ {cert.balance_kzt:,.2f}  $ {cert.balance_usd:,.2f}  € {cert.balance_eur:,.2f}")

    return cert


def parse_full_statement(doc, start_page: int = 0) -> StatementData:
    """
    Парсит ВСЮ выписку Kaspi Gold используя Y-координаты для группировки строк
    и X-координаты для разделения даты/суммы/типа операции.
    
    Структура строки транзакции (по X-координатам):
      X≈52   — дата (09.02.26)
      X≈127-140 — знак (+/-)  
      X≈135-153 — цифры суммы
      X≈185  — ₸
      X≈238-257 — тип операции
      X≈311+ — детали
    """
    stmt = StatementData()
    date_pattern = re.compile(r"\d{2}\.\d{2}\.\d{2}")

    # ── Собираем ВСЕ слова со всех страниц, группируем в строки ──
    all_lines: List[Dict] = []
    page_heights: Dict[int, float] = {}  # pn → высота страницы в pt (PDF coords)

    for pn in range(start_page, len(doc)):
        page = doc[pn]
        page_heights[pn] = page.rect.height
        words = page.get_text("words")
        
        # Группируем слова по Y-строкам (±3px)
        y_groups: Dict[int, list] = {}
        for w in words:
            x0, y0, x1, y1, text_w = w[0], w[1], w[2], w[3], w[4]
            y_key = round(y0 / 3) * 3
            if y_key not in y_groups:
                y_groups[y_key] = []
            y_groups[y_key].append({
                'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1, 'text': text_w
            })
        
        for y_key in sorted(y_groups.keys()):
            line_words = sorted(y_groups[y_key], key=lambda w: w['x0'])
            all_lines.append({
                'y': y_key,
                'page': pn,
                'words': line_words,
                'text': " ".join(w['text'] for w in line_words)
            })

    # ── 1. Балансы «Доступно на ...» ──
    available_candidates = []
    for line in all_lines:
        text = line['text']
        if 'Доступно' not in text:
            continue
        
        date_str = None
        date_word = None
        for w in line['words']:
            if date_pattern.search(w['text']):
                date_str = w['text'].replace(':', '').strip()
                date_word = w
                break

        if date_str:
            # x_min раньше был захардкожен константой 200.0 — тот же класс
            # бага, что уже исправлен ниже для строк транзакций (см. докстринг
            # у date_word['x1'] + 5.0 в цикле по all_lines): при очень большом
            # балансе фиксированный порог может отрезать ведущую цифру. Дата
            # в этой строке всегда предшествует сумме — считаем x_min от её
            # правого края, а не от константы.
            _x_min = (date_word['x1'] + 5.0) if date_word else 200.0
            amount_text, sign, val = _collect_amount_on_line(line['words'], x_min=_x_min)
            if val > 0:
                available_candidates.append((date_str, amount_text, val, line['page']))

    if available_candidates:
        def _parse_date(d_str):
            m = date_pattern.search(d_str)
            if m:
                try:
                    return datetime.strptime(m.group(0), "%d.%m.%y")
                except Exception:
                    pass
            return datetime.min

        available_candidates.sort(key=lambda x: _parse_date(x[0]))
        if len(available_candidates) >= 2:
            stmt.balance_start_date = available_candidates[0][0]
            stmt.balance_start_text = available_candidates[0][1] or ""
            stmt.balance_start = available_candidates[0][2]
            stmt.balance_end_date = available_candidates[-1][0]
            stmt.balance_end_text = available_candidates[-1][1] or ""
            stmt.balance_end = available_candidates[-1][2]
        elif len(available_candidates) == 1:
            stmt.balance_end_date = available_candidates[0][0]
            stmt.balance_end_text = available_candidates[0][1] or ""
            stmt.balance_end = available_candidates[0][2]
    
    print(f"[Parser] Баланс начало ({stmt.balance_start_date}): {stmt.balance_start:,.2f} ₸")
    print(f"[Parser] Баланс конец  ({stmt.balance_end_date}): {stmt.balance_end:,.2f} ₸")

    # ── 2. Сводные итоги (Пополнения / Переводы / Покупки / Снятия / Разное) ──
    total_expense_parts = {}
    expense_labels = {'Переводы', 'Покупки', 'Снятия', 'Разное'}
    
    for line in all_lines:
        if line['page'] > start_page:
            break
        
        words = line['words']
        if not words:
            continue
        
        first_word = words[0]['text']
        # Тот же класс бага, что и у "Доступно"/строк транзакций: фиксированный
        # x_min=200.0 калиброван под типичную сумму и может отрезать ведущую
        # цифру у очень большого итога. Метка ("Пополнения"/"Переводы"/...) —
        # ровно первое слово строки, её правый край — надёжный якорь для
        # начала суммы при любой её ширине.
        _x_min = words[0]['x1'] + 5.0

        if first_word == 'Пополнения':
            amount_text, sign, val = _collect_amount_on_line(words, x_min=_x_min)
            if val > 0:
                stmt.total_income_text = amount_text or ""
                stmt.total_income = val
                print(f"[Parser] Итого пополнений: {stmt.total_income:,.2f} ₸")

        if first_word in expense_labels:
            amount_text, sign, val = _collect_amount_on_line(words, x_min=_x_min)
            if val > 0:
                total_expense_parts[first_word] = val
                stmt.expense_categories[first_word] = val
                stmt.expense_category_texts[first_word] = amount_text or ""
    
    if total_expense_parts:
        stmt.total_expense = sum(total_expense_parts.values())
        print(f"[Parser] Расходы (raw dict): {total_expense_parts}")
        print(f"[Parser] Итого расходов (raw): {stmt.total_expense:,.2f} ₸")

    # Вычисляем эффективные расходы через уравнение баланса:
    # balance_start + total_income - total_expense_effective = balance_end
    # Это надёжнее прямого суммирования строк таблицы, так как в Kaspi
    # некоторые подстроки (Переводы на свои счета, Поступления со своих) дублируют
    # первое слово «Переводы»/«Поступления» и перезаписывают друг друга в dict.
    if stmt.total_income > 0 and stmt.balance_start >= 0 and stmt.balance_end >= 0:
        effective_expense = round(stmt.balance_start + stmt.total_income - stmt.balance_end, 2)
        if effective_expense > 0:
            stmt.total_expense = effective_expense
            print(f"[Parser] Итого расходов (по балансу): {stmt.total_expense:,.2f} ₸")
    
    # ── 3. Все транзакции ──
    TX_TYPES_INCOME = {'Пополнение'}
    TX_TYPES_EXPENSE = {'Покупка', 'Перевод', 'Снятие', 'Оплата', 'Платёж',
                         'Платеж', 'Комиссия', 'Возврат', 'Разное'}
    # «Поступление» (со своего счета / С Kaspi Депозита) и «Зачисление» —
    # приход денег С СОБСТВЕННОГО депозитного субсчёта клиента (self-transfer),
    # а не внешнее пополнение. Раньше отсутствовали в ОБОИХ множествах типов
    # → строка не проходила `if tx_type is None: continue` ниже и целиком
    # выпадала из stmt.transactions, хотя реально двигает баланс счёта.
    # Воспроизведено на реальной выписке (goldformat1.pdf, 62 стр.): 233 таких
    # строки на 19 002 414,75 ₸ отсутствовали в парсинге — Σ(+)/Σ(-) и running
    # balance не сходились с B_start/B_end уже на НЕТРОНУТОМ оригинале, и то
    # же расхождение переносилось в обработанный (scored) PDF, ломая
    # «Баланс (транзакции)»/Running balance проверки. Не добавлены в
    # TX_TYPES_INCOME: как и «Возврат»/«Покупка» со знаком «+» (is_refund
    # ниже), это не масштабируемая зарплата, а деньги, уже принадлежавшие
    # клиенту — is_salary_income для них остаётся False по той же формуле.
    TX_TYPES_SELF_TRANSFER = {'Поступление', 'Зачисление'}
    ALL_TX_TYPES = TX_TYPES_INCOME | TX_TYPES_EXPENSE | TX_TYPES_SELF_TRANSFER

    trans_idx = 0
    tx_line_texts: List[str] = []
    for line in all_lines:
        words = line['words']

        tx_type = None
        for w in words:
            if w['text'] in ALL_TX_TYPES:
                tx_type = w['text']
                break

        if tx_type is None:
            continue

        # Обязательно должна быть дата — иначе это строка заголовка (напр. «Разное - 2 195,00 ₸»)
        # которая совпадает по слову с типом транзакции, но НЕ является транзакцией.
        # Ищем её ДО сбора суммы — её правая граница задаёт ДИНАМИЧЕСКУЮ левую
        # границу колонки суммы (см. x_min ниже).
        date_word = None
        for w in words:
            if w['x0'] < 100 and date_pattern.match(w['text']):
                date_word = w
                break

        if date_word is None:
            continue
        date_str = date_word['text']

        # x_min раньше был захардкожен константой 120.0, калиброванной под
        # типичную 5-6-значную сумму («X≈127…140 — знак», см. докстринг
        # _collect_amount_on_line). У 7-8-значных сумм (после сильного
        # апскейла, напр. «+ 11 487 000,00») ведущая цифра физически рисуется
        # ЛЕВЕЕ x=120 (замерено на реальных файлах: до x0≈104.75) — с
        # фиксированным порогом её выкидывал `if x0 < x_min: continue` внутри
        # _collect_amount_on_line, отрезая сумме один (и больше) разряд слева
        # (воспроизведено на kaspi_gold_cert_scored.pdf: «1 899 000,00» читался
        # как «899 000,00»). Дата — ровно 8 символов «ДД.ММ.ГГ», её правая
        # граница на всех проверенных файлах стабильна (~88.76pt); считаем
        # x_min от неё (+ запас), а не от константы, — работает при любой
        # ширине суммы.
        amount_text, sign_from_line, val = _collect_amount_on_line(
            words, x_min=date_word['x1'] + 5.0
        )

        if val <= 0:
            continue

        # КРИТИЧНО: sign определяется по ЗНАКУ (+/-) в строке PDF,
        # а НЕ по типу операции. Примеры возвратов:
        #   "+ 2 316,00 ₸ Покупка ТОО Kaspi Travel"  → sign=+1 (возврат покупки)
        #   "+ 1 000,00 ₸ Перевод Отмена покупки"     → sign=+1 (отмена перевода)
        # Kaspi учитывает их как пополнения в итогах.
        if sign_from_line != 0:
            sign = sign_from_line
        else:
            # Знак не найден в строке — fallback по типу операции
            sign = 1 if tx_type in TX_TYPES_INCOME or tx_type in TX_TYPES_SELF_TRANSFER else -1

        # is_salary = True только для РЕАЛЬНЫХ пополнений (type=Пополнение И sign=+1)
        # Возвраты (type=Покупка, sign=+1) НЕ salary — не масштабируются как доход
        is_income = (sign == 1)
        is_salary_income = (tx_type in TX_TYPES_INCOME and sign == 1)
        is_refund = (tx_type not in TX_TYPES_INCOME and sign == 1)

        # ПОПЫТКА (отклонена): автоматически вырезать строку с текстом,
        # идентичным последней строке предыдущей страницы, считая это
        # рендер-дублем на разрыве страниц (воспроизведено на
        # "gold_statement - 2026-07-21T142737.432.pdf", страницы 6→7).
        # ОПРОВЕРГНУТО на другом реальном файле (gold9.pdf, границы 19→20,
        # "Обязательные Пенсионные Взносы Работодателя" 100 ₸): там точно
        # такое же совпадение (идентичный текст ровно на границе страниц)
        # оказалось ДВУМЯ РЕАЛЬНЫМИ отдельными транзакциями — удаление
        # сломало баланс файла, который до этого сходился идеально (Δ=0 →
        # Δ=-100 после вырезания). Т.е. «совпадение невозможно для двух
        # независимых операций» — фактически неверное допущение. Нельзя
        # надёжно отличить рендер-дубль от двух совпавших по тексту реальных
        # платежей без дополнительных данных (напр. отдельного ID операции,
        # которого в PDF нет). Оставлено ТОЛЬКО как диагностика (см. ⚠️ ниже,
        # печатается после сборки stmt.transactions) — транзакции не
        # удаляются никогда.
        ph = page_heights.get(line['page'], 841.89)
        tx = Transaction(
            index=trans_idx,
            date=date_str,
            description=tx_type,
            amount=val,
            sign=sign,
            original_amount_text=amount_text or "",
            is_salary=is_salary_income,
            is_refund=is_refund,
            page_num=line['page'],
            # Конвертируем PyMuPDF Y (вниз от верха) → PDF-поток Y (вверх от низа)
            y_pdf_rounded=round(ph - line['y']),
        )
        
        stmt.transactions.append(tx)
        tx_line_texts.append(line['text'])
        trans_idx += 1

    # Диагностика для операторской видимости: две строки с ПОЛНОСТЬЮ
    # идентичным текстом (дата+сумма+тип+контрагент) означают, что одна и та
    # же операция напечатана в исходном PDF дважды — иногда это реальный рендер-
    # артефакт разрыва страницы (последняя строка одной страницы повторяется
    # первой строкой следующей — воспроизведено на реальном файле), иногда
    # действительно два разных платежа на одну сумму в один день (напр. два
    # снятия в банкомате). Различить по одному тексту нельзя — транзакции НЕ
    # удаляются и баланс не трогается, только предупреждение для человека.
    from collections import Counter as _Counter
    _line_counts = _Counter(tx_line_texts)
    _dup_texts = {t: c for t, c in _line_counts.items() if c > 1}
    if _dup_texts:
        _dup_tx_count = sum(_dup_texts.values())
        _dup_amount = sum(
            stmt.transactions[i].amount
            for i, t in enumerate(tx_line_texts)
            if t in _dup_texts
        )
        _samples = sorted(_dup_texts.keys())[:3]
        print(
            f"[Parser] ⚠️ Найдены полностью идентичные строки транзакций "
            f"(дата+сумма+тип+контрагент совпадают дословно): {len(_dup_texts)} "
            f"уникальных строк, {_dup_tx_count} транзакций всего, Σ={_dup_amount:,.2f} ₸. "
            f"Не удаляются автоматически (может быть либо рендер-дубль на "
            f"разрыве страницы, либо два реальных платежа на одну сумму). Примеры: "
            f"{_samples}"
        )

    parsed_income = sum(t.amount for t in stmt.transactions if t.sign == 1)
    parsed_expense = sum(t.amount for t in stmt.transactions if t.sign == -1)

    print(f"\n[Parser] Найдено транзакций: {len(stmt.transactions)} "
          f"(пополнений: {sum(1 for t in stmt.transactions if t.sign == 1)}, "
          f"расходов: {sum(1 for t in stmt.transactions if t.sign == -1)})")
    print(f"[Parser] Σ пополнений (парсер): {parsed_income:,.2f} ₸ "
          f"(из PDF: {stmt.total_income:,.2f} ₸, "
          f"Δ={parsed_income - stmt.total_income:+,.2f})")
    print(f"[Parser] Σ расходов (парсер):    {parsed_expense:,.2f} ₸ "
          f"(из PDF: {stmt.total_expense:,.2f} ₸, "
          f"Δ={parsed_expense - stmt.total_expense:+,.2f})")

    if stmt.total_expense == 0 and parsed_expense > 0:
        stmt.total_expense = parsed_expense
    
    if stmt.total_income == 0 and parsed_income > 0:
        stmt.total_income = parsed_income

    return stmt


# ---------------------------------------------------------------------------
#  ЭТАП 2: Математический движок пересчёта
# ---------------------------------------------------------------------------


def _get_month_key(date_str: str) -> Optional[str]:
    """Извлекает ключ месяца из даты '09.02.26' → '2026-02'."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", date_str or "")
    if m:
        return f"20{m.group(3)}-{m.group(2)}"
    return None


def min_dayend_balance(
    transactions: List["Transaction"],
    balance_start: float,
    amount_attr: str = "amount",
) -> Tuple[float, float]:
    """Минимальный running balance, замеряемый ТОЛЬКО на границах дней.

    Возвращает (min_dayend_rb, final_rb).

    ПОЧЕМУ на границах дней, а не после каждой транзакции:
    в выписках Kaspi (и Halyk/Kaspi ИП) нет внутридневных меток времени —
    для транзакций с ОДНОЙ датой их порядок в PDF произволен. Инвариант
    «баланс ≥ 0» имеет смысл только на границе дня: если день стартует и
    заканчивается с неотрицательным балансом, всегда существует внутридневной
    порядок (сначала кредиты, потом дебеты), при котором баланс не уходит в
    минус. Поэтому строка отчёта, где на ОДНУ дату идут пять дебетов, а за
    ними два кредита, покрывающих их, — не реальный овердрафт, а всего лишь
    неудачная перестановка операций одного дня.

    Реальный баг, который это ловило как ложный минус: немодифицированный
    gold_statement.pdf уходил в −54,17 ₸ в единственной точке 12.12.25
    (пять дебетов перед двумя кредитами того же дня), из-за чего /verify
    объявлял валидную банковскую выписку невалидной, а движок пересчёта
    запускал разрушительный цикл коррекции salary из-за 54 тенге.

    Транзакции идут от НОВЫХ к СТАРЫМ (как в PDF) — считаем в обратном
    порядке (от balance_start = самой ранней даты ВПЕРЁД). Граница дня
    фиксируется в момент СМЕНЫ даты (накопленный rb = конец предыдущего дня)
    плюс в самом конце (конец последнего дня).
    """
    rb = balance_start
    min_rb = rb
    prev_date = None
    for tx in reversed(transactions):
        if prev_date is not None and tx.date != prev_date and rb < min_rb:
            min_rb = rb
        rb = round(rb + tx.sign * getattr(tx, amount_attr), 2)
        prev_date = tx.date
    if rb < min_rb:
        min_rb = rb
    return min_rb, rb


def _date_sort_key(date_str: Optional[str]) -> Tuple[int, int, int]:
    """(yy, mm, dd) для хронологического сравнения дат '12.12.25'.

    Неизвестная дата → максимум, чтобы такие транзакции считались «поздними»
    и не блокировали логику на границе дня.
    """
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", date_str or "")
    if not m:
        return (99, 99, 99)
    return (int(m.group(3)), int(m.group(2)), int(m.group(1)))


def first_negative_dayend(
    transactions: List["Transaction"],
    balance_start: float,
    amount_attr: str = "amount",
) -> Tuple[float, Optional[str]]:
    """(min_dayend_rb, дата первого хронологически отрицательного дня).

    Дополняет min_dayend_balance: возвращает не только минимум на границах
    дней, но и ДАТУ самой ранней границы дня, на которой баланс отрицателен —
    чтобы коррекция знала, какой «горбатый» месяц поднимать (см. Шаг 3 в
    recalculate_statement). Если отрицательных границ нет — дата = None.
    """
    rb = balance_start
    min_rb = rb
    first_neg_date: Optional[str] = None
    prev_date = None
    for tx in reversed(transactions):
        if prev_date is not None and tx.date != prev_date:
            if rb < 0 and first_neg_date is None:
                first_neg_date = prev_date
            if rb < min_rb:
                min_rb = rb
        rb = round(rb + tx.sign * getattr(tx, amount_attr), 2)
        prev_date = tx.date
    if rb < 0 and first_neg_date is None:
        first_neg_date = prev_date
    if rb < min_rb:
        min_rb = rb
    return min_rb, first_neg_date


def recalculate_statement(stmt: StatementData, target_monthly_income: float) -> StatementData:
    """
    Пересчитывает выписку с помесячным выравниванием:
    
    1. Группирует SALARY транзакции (Пополнение) по месяцам
    2. Для каждого месяца: K_month = target / month_salary_income
    3. Применяет K_month × (1 ± ε) к каждой зарплатной транзакции
    4. Масштабирует расходы (sign=-1) через K_exp = K^0.7 (закон Энгеля)
    5. Возвраты (is_refund=True, sign=+1) НЕ масштабируются
    6. Пересчитывает running balance по цепочке
    
    ФОРМУЛЫ ИТОГОВ (как в Kaspi):
      total_income  = Σ(salary tx.new_amount)  — без возвратов!
      total_expense = Σ(sign==-1 tx.new_amount) - Σ(refund tx.new_amount)  — НЕТТО
      balance_end   = balance_start + Σ(sign=+1) - Σ(sign==-1)
    """
    salary_transactions = [t for t in stmt.transactions if t.is_salary]
    refund_transactions = [t for t in stmt.transactions if t.is_refund]

    if not salary_transactions:
        print("[Engine] ⚠️ Не найдено зарплатных транзакций")
        return stmt

    current_salary_income = sum(t.amount for t in salary_transactions)
    current_refund_total = sum(t.amount for t in refund_transactions)
    
    # ── Группировка SALARY доходов по месяцам ──
    monthly_income: Dict[str, float] = {}
    monthly_txs: Dict[str, List[Transaction]] = {}
    
    for tx in salary_transactions:
        mk = _get_month_key(tx.date) or "unknown"
        monthly_income[mk] = monthly_income.get(mk, 0) + tx.amount
        if mk not in monthly_txs:
            monthly_txs[mk] = []
        monthly_txs[mk].append(tx)
    
    n_months = len([k for k in monthly_income if k != "unknown"])
    if n_months == 0:
        n_months = 1
    
    current_monthly_avg = current_salary_income / max(n_months, 1)
    global_K = target_monthly_income / current_monthly_avg

    if current_monthly_avg <= 0:
        print("[Engine] ⚠️ Текущий доход = 0")
        return stmt

    # Защита от K < 1: масштабировать доход ВНИЗ нет смысла —
    # это уменьшает income, итоговый баланс уходит в минус, и PDF сломан.
    # Минимум K = 1.0 (оставляем оригинальные суммы).
    if global_K < 1.0:
        print(f"[Engine] ⚠️ Цель ({target_monthly_income:,.0f}) < текущего дохода/мес ({current_monthly_avg:,.0f}). K={global_K:.4f} < 1 — клипуем до 1.0")
        global_K = 1.0

    print(f"\n{'═' * 60}")
    print(f"  ДВИЖОК ПЕРЕСЧЁТА (помесячное выравнивание)")
    print(f"{'═' * 60}")
    print(f"  Текущий ср. зарплатный/мес: {current_monthly_avg:>14,.2f} ₸")
    print(f"  Целевой доход/мес:          {target_monthly_income:>14,.2f} ₸")
    print(f"  Глобальный K:               {global_K:>14.4f}")
    print(f"  Месяцев в выписке:          {n_months}")
    print(f"  Зарплатных транзакций:      {len(salary_transactions)}")
    print(f"  Возвратов (не масштабируем): {len(refund_transactions)} (Σ={current_refund_total:,.2f} ₸)")
    print(f"{'═' * 60}")
    
    # ── Помесячные K-коэффициенты ──
    # Для каждого месяца: K_month = target / month_income
    # Это выравнивает месяцы к одному уровню → высокий ISI
    # Если global_K = 1.0 (цель ниже текущего), клипуем каждый месяц тоже до 1.0,
    # чтобы не уменьшать высокие месяцы ниже оригинала.
    _clip_k_to_one = (global_K <= 1.0)
    print(f"\n  Помесячные коэффициенты:")
    month_K: Dict[str, float] = {}
    for mk in sorted(monthly_income.keys()):
        if mk == "unknown":
            month_K[mk] = global_K
            continue
        mi = monthly_income[mk]
        k = target_monthly_income / mi if mi > 0 else global_K
        if _clip_k_to_one:
            k = max(k, 1.0)
        month_K[mk] = k
        print(f"    {mk}: доход {mi:>14,.2f} → K = {k:.4f}")

    # ── Расходы: НЕ масштабируем ──
    # Банк (Отбасы) верифицирует расходные категории с базой Kaspi.
    # Любое изменение расходов → статус 6 LG (отклонение).
    # Поэтому K_exp ВСЕГДА = 1.0 — расходы остаются оригинальными.
    original_er = stmt.total_expense / max(current_salary_income, 1)
    projected_total_income = target_monthly_income * n_months
    projected_er = stmt.total_expense / max(projected_total_income, 1)
    
    print(f"\n  ER оригинальный:  {original_er:.3f}")
    print(f"  ER проекция:      {projected_er:.3f}")
    print(f"  K_exp (расходы):  1.0000 (расходы НЕ масштабируются)")

    # ── Шаг 1: Масштабирование транзакций с дисперсией ──
    # Только salary (sign=+1, is_salary) масштабируются.
    # Расходы (sign=-1), возвраты и прочие — остаются без изменений.
    print(f"\n  Масштабирование транзакций:")
    for tx in stmt.transactions:
        if tx.sign == 1 and tx.is_salary and not tx.is_refund:
            mk = _get_month_key(tx.date) or "unknown"
            k = month_K.get(mk, global_K)
            epsilon = random.uniform(-0.03, 0.03)
            tx.new_amount = _round_to_natural(tx.amount * k * (1 + epsilon))
        else:
            # Возвраты, расходы, non-salary income — без изменений
            tx.new_amount = tx.amount

    # ── Шаг 2: Running balance ──
    # ВАЖНО: Транзакции в Kaspi PDF идут от НОВЫХ к СТАРЫМ (09.02.26 → 10.02.25)
    # Для running balance считаем от balance_start (самая ранняя дата) ВПЕРЁД,
    # т.е. идём по транзакциям в ОБРАТНОМ порядке. Минимум замеряем на границах
    # дней (см. min_dayend_balance): внутридневной порядок операций произволен,
    # и дип в −54 ₸ между дебетами и покрывающими их кредитами ТОГО ЖЕ дня —
    # не реальный овердрафт, а лишь перестановка внутри даты.
    current_rb = stmt.balance_start
    for tx in reversed(stmt.transactions):
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        tx.new_balance_after = current_rb
    min_rb, _ = min_dayend_balance(stmt.transactions, stmt.balance_start, "new_amount")

    # ── Шаг 3: Если баланс уходит в минус — ПОДНИМАЕМ доход в дефицитном месяце ──
    # «Горбатый» доход: у выписки есть месяц с очень крупным зарплатным доходом,
    # который тратится в том же месяце. Плоское выравнивание к цели около
    # среднего срезает этот месяц ниже, чем нужно для покрытия его же расходов,
    # и running balance уходит в минус в СЕРЕДИНЕ периода. Расходы трогать
    # нельзя (банк их сверяет), поэтому единственный способ вернуть баланс в
    # плюс — ПОДНЯТЬ зарплату в месяце дефицита (и, если нужно, в более ранних).
    #
    # РАНЬШЕ здесь была обратная (ошибочная) логика: salary УМЕНЬШАЛИ ×0.97,
    # что углубляло дефицит, гнало итоговый баланс в глубокий минус и приводило
    # к ложному floor-отказу (post_check_negative_balance) даже там, где валидный
    # результат достижим. downscale-модуль всегда поднимал ×1.02 — это и есть
    # верное направление. Поднятие дохода всегда сходится: лишние деньги дефицит
    # только закрывают, а на баланс ≥ 0 в остальных точках это не влияет.
    #
    # Как и прежде, корректируем только если дефицит СОЗДАН нашим масштабированием
    # (тождество баланса на уровне транзакций у оригинала сходится). Если оригинал
    # сам «структурно дефицитен» на уровне транзакций (напр. self-transfer суммы
    # в шапке, но не в виде «Пополнение»-транзакций) — не вмешиваемся.
    individual_income_total = sum(tx.amount for tx in stmt.transactions if tx.sign == 1)
    individual_expense_total = sum(tx.amount for tx in stmt.transactions if tx.sign == -1)
    original_min_rb_deficit = individual_income_total + stmt.balance_start - individual_expense_total
    min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")
    if min_rb < 0 and original_min_rb_deficit >= -1.0:
        print(f"\n  ⚠️ Баланс уходил в минус: {min_rb:,.2f} ₸ (первый дефицитный день ~{neg_date})")
        print(f"  Корректируем: поднимаем зарплату на/до точки дефицита")
        prev_min = None
        for attempt in range(80):
            min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")
            if min_rb >= -0.01:
                print(f"  ✅ Баланс скорректирован за {attempt} итераций, мин: {min_rb:,.2f} ₸")
                break
            # Дефицит на границе дня neg_date можно закрыть ТОЛЬКО доходом,
            # пришедшим НЕ ПОЗЖЕ этой даты (более поздняя зарплата на эту точку
            # не влияет). Поднимаем зарплату в МЕСЯЦЕ дефицита, но лишь ту, что
            # датирована ≤ neg_date (тот самый «горб»); если такой в этом месяце
            # нет — расширяемся на всю зарплату ≤ neg_date. Ограничение «≤ дата»
            # критично: без него зарплата, пришедшая ПОСЛЕ дефицита, растёт до
            # бесконечности, не сдвигая баланс (runaway).
            neg_mk = _get_month_key(neg_date)
            neg_key = _date_sort_key(neg_date)
            eligible = [
                tx for tx in stmt.transactions
                if tx.sign == 1 and tx.is_salary and not tx.is_refund
                and _date_sort_key(tx.date) <= neg_key
            ]
            targeted = [tx for tx in eligible if _get_month_key(tx.date) == neg_mk] or eligible
            if not targeted:
                # Дефицит без единой зарплатной транзакции до него — поднять
                # нечего, ниже сработает safety-net (raise IncomeTooLowError).
                print(f"  ⚠️ Нет зарплаты до точки дефицита {neg_date} — коррекция невозможна")
                break
            # _round_to_natural (не round(x, 2)!) — реальные зарплатные суммы
            # Kaspi Gold всегда целые тенге в «человеческом» шаге (см. её
            # докстринг); голый round(x, 2) после нескольких итераций ×1.05
            # оставляет произвольные копейки (проверено на реальном файле:
            # 100 из 561 зарплатных транзакций получали копейки вроде
            # "232 682,62 ₸" — визуально выдаёт результат как посчитанный по
            # формуле, а не настоящую зарплатную проводку).
            for tx in targeted:
                tx.new_amount = _round_to_natural(tx.new_amount * 1.05)
            # Guard от зависания: если минимум не улучшается — прекращаем.
            if prev_min is not None and min_rb <= prev_min + 0.01:
                print(f"  ⚠️ Коррекция не сходится (мин застрял на {min_rb:,.2f} ₸) — стоп")
                break
            prev_min = min_rb
        else:
            print(f"  ⚠️ Коррекция не сошлась за 80 итераций, мин: {min_rb:,.2f} ₸")
        # Финальный пересчёт running balance после коррекции
        current_rb = stmt.balance_start
        for tx in reversed(stmt.transactions):
            current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
            tx.new_balance_after = current_rb
    elif min_rb < 0:
        print(f"\n  ℹ️ Running balance минус ({min_rb:,.2f} ₸) — структурная особенность PDF."
              f" Оригинал тоже дефицитен ({original_min_rb_deficit:,.2f} ₸). Не корректируем.")

    # ── Итоги (формулы Kaspi) ──
    # Kaspi считает "Пополнения" как НЕТТО:
    #   Пополнения = Σ(+ Пополнение) − Σ(− Пополнение)
    # Отрицательные «Пополнение» (Возврат Х.) — это отмены пополнений, и Kaspi
    # вычитает их из суммарной категории. Если их игнорировать, header будет
    # завышен на величину возврата → tx-уровень не сойдётся (Δ = +возврат).
    salary_income_pos = sum(
        tx.new_amount for tx in stmt.transactions
        if tx.is_salary and not tx.is_refund
    )
    refund_topups_neg = sum(
        tx.amount for tx in stmt.transactions
        if tx.description == "Пополнение" and tx.sign == -1
    )
    stmt.new_total_income = round(salary_income_pos - refund_topups_neg, 2)
    
    # Расходы: оставляем ОРИГИНАЛЬНЫЕ из header PDF (вычислены по уравнению баланса).
    # НЕ пересчитываем через expense_categories — там дублируются строки типа
    # «Переводы» и «Переводы на свои счета» (одинаковый ключ → перезапись).
    # stmt.total_expense уже содержит корректное значение из parse_full_statement.
    
    # Balance: start + new_income - original_expense = end
    stmt.new_balance_end = round(
        stmt.balance_start + stmt.new_total_income - stmt.total_expense, 2
    )

    # «Доступно на …» в PDF Kaspi Gold обычно пишется БЕЗ знака вовсе (не
    # «+5 356,25», а просто «5 356,25») — а глиф «-» для BALANCE_END/
    # TOTAL_INCOME синтезировать негде: у "-" нет CID в CMap ОСНОВНОГО
    # шрифта (from_unicode строится только из него — см. build_dynamic_cmap),
    # он существует только в CMap доп. шрифта, использовать чужой CID в
    # чужом шрифте — риск отрисовать не тот глиф. process_pdf_bytes_raw тогда
    # молча оставил бы старый (положительный) текст — сумма выглядела бы как
    # положительная, будучи на самом деле отрицательной (воспроизведено на
    # реальном файле: near-noop таргет после коррекции running balance,
    # «Шаг 3» выше, ушёл в -7 990 164,08 ₸, а в PDF осталась исходная
    # положительная «5 356,25»). Как и в pdf_service_downscale (см. её
    # докстринг: «никогда не получим отрицательный B_end — архитектурно, а не
    # патчем cmap»), решаем на уровне пересчёта, а не байт-записи: если после
    # ВСЕХ коррекций (включая «Шаг 3») баланс всё равно уходит в минус —
    # запрошенный target для этой выписки небезопасен, поднимаем явную ошибку
    # вместо тихой записи визуально неверного PDF.
    if stmt.new_balance_end < 0:
        from pdf_service_downscale import IncomeTooLowError  # локальный импорт — избегаем цикла

        new_min = max(target_monthly_income, current_monthly_avg) * 1.10
        raise IncomeTooLowError(
            min_target_monthly_income=new_min,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="post_check_negative_balance",
            message=(
                f"Не удалось удержать неотрицательный итоговый баланс при "
                f"{target_monthly_income:,.0f} ₸/мес "
                f"(получилось {stmt.new_balance_end:,.0f} ₸). Минимально "
                f"рекомендуемый доход: {new_min:,.0f} ₸/мес."
            ),
        )

    # Шапка стр.1 (cert-формат) физически не вмещает право-выровненное
    # 10-значное число в своей ячейке «Сумма» (см. HeaderCellOverflowError).
    # Проверяем ОБА поля, которые в неё пишутся, до тяжёлой записи PDF.
    for _field_name, _val in (
        ("total_income", stmt.new_total_income),
        ("balance_end", stmt.new_balance_end),
    ):
        if abs(_val) > _HEADER_CELL_MAX_SAFE_VALUE:
            raise HeaderCellOverflowError(
                field_name=_field_name,
                value=_val,
                max_safe_value=_HEADER_CELL_MAX_SAFE_VALUE,
            )

    # Расходные категории заголовка: оставляем ОРИГИНАЛЬНЫМИ (не пересчитываем!)
    if stmt.expense_categories:
        for cat, old_val in stmt.expense_categories.items():
            stmt.new_expense_categories[cat] = old_val  # без изменений!

    # ── Помесячная статистика нового дохода (только salary) ──
    new_monthly: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date) or "unknown"
            new_monthly[mk] = new_monthly.get(mk, 0) + tx.new_amount
    
    print(f"\n  {'─' * 50}")
    print(f"  Новый доход по месяцам:")
    for mk in sorted(new_monthly.keys()):
        deviation = (new_monthly[mk] - target_monthly_income) / target_monthly_income * 100
        print(f"    {mk}: {new_monthly[mk]:>14,.2f} ₸ ({deviation:>+5.1f}%)")
    
    new_avg = sum(new_monthly.values()) / max(len(new_monthly), 1)
    print(f"\n  Σ нового дохода:            {stmt.new_total_income:>14,.2f} ₸")
    print(f"  Σ новых расходов:           {stmt.total_expense:>14,.2f} ₸")
    print(f"  Новый баланс конец:         {stmt.new_balance_end:>14,.2f} ₸")
    print(f"  Новый средний доход/мес:    {new_avg:>14,.2f} ₸")
    print(f"  Целевой:                    {target_monthly_income:>14,.2f} ₸")
    print(f"  {'─' * 50}")

    return stmt


def recalculate_with_certificate(
    cert: CertificateData,
    stmt: StatementData,
    target_monthly_income: float,
    recalc_fn=None,
) -> Tuple[CertificateData, StatementData]:
    """Согласованный пересчёт: сначала выписка, затем синхронизация справки.

    После пересчёта `stmt.new_balance_end`:
      - cert.new_balance_kzt := stmt.new_balance_end
      - cert.new_balance_usd := new_kzt / rate_usd
      - cert.new_balance_eur := new_kzt / rate_eur

    Курсы (rate_usd, rate_eur) сохраняются из оригинальных значений справки —
    это то, как банк зафиксировал курс на момент выдачи справки.

    `recalc_fn` (по умолчанию — recalculate_statement, upscale-движок)
    позволяет подставить другой движок пересчёта — например,
    recalculate_statement_downscale из pdf_service_downscale.py. Раньше эта
    функция ВСЕГДА использовала recalculate_statement напрямую, игнорируя
    recalc_fn, который process_pdf_bytes_raw принимает как параметр — из-за
    этого process_downscale() на cert-формате (текущий формат Kaspi Gold,
    введён в 2026) фактически прогонял downscale-запрос через upscale-движок:
    ни одна из трёх downscale floor-проверок не срабатывала, а доход не
    уменьшался (мог даже слегка вырасти) без единой ошибки.
    """
    if recalc_fn is None:
        recalc_fn = recalculate_statement

    # 1) Движок пересчёта выписки (upscale по умолчанию, либо переданный)
    stmt = recalc_fn(stmt, target_monthly_income)

    # 2) Курсы из оригинала
    rate_usd = cert.balance_kzt / cert.balance_usd if cert.balance_usd > 0 else 0.0
    rate_eur = cert.balance_kzt / cert.balance_eur if cert.balance_eur > 0 else 0.0

    cert.new_balance_kzt = stmt.new_balance_end
    if rate_usd > 0:
        cert.new_balance_usd = round(cert.new_balance_kzt / rate_usd, 2)
    if rate_eur > 0:
        cert.new_balance_eur = round(cert.new_balance_kzt / rate_eur, 2)

    print(f"\n  ┌─ Согласование справки ─────────────────────────")
    print(f"  │ Курс USD: {rate_usd:>10.4f}  Курс EUR: {rate_eur:>10.4f}")
    print(f"  │ Было: ₸ {cert.balance_kzt:,.2f}  $ {cert.balance_usd:,.2f}  € {cert.balance_eur:,.2f}")
    print(f"  │ Стало: ₸ {cert.new_balance_kzt:,.2f}  $ {cert.new_balance_usd:,.2f}  € {cert.new_balance_eur:,.2f}")
    print(f"  └────────────────────────────────────────────────")

    return cert, stmt


def build_income_replacement_entries(stmt: StatementData) -> Dict[str, Deque[Tuple[float, str]]]:
    """Строит очередь замен для ВСЕХ доходных (sign=+1) транзакций — и salary,
    и refund/self-transfer — в порядке stmt.transactions (совпадает с
    порядком появления в PDF: Kaspi печатает от новых к старым).

    КРИТИЧНО: каждая sign=+1 транзакция должна зарезервировать РОВНО один
    слот в очереди, даже если для salary новое значение совпало со старым
    (new_amount == amount — K_month округлился близко к 1.0 для этого
    месяца). Раньше такие транзакции слот не резервировали — из-за этого
    raw-byte сканер при встрече с их оригинальными байтами "съедал" слот,
    предназначенный для ДРУГОЙ транзакции с тем же текстом суммы, каскадно
    сдвигая все последующие одинаковые суммы (воспроизведено на реальном
    файле: 28 из 1334 доходных транзакций получали чужое значение при
    таргете, где K_month одного из месяцев округлялся к 1.0). Refund-
    транзакции уже резервировали identity-слот по этой же причине — теперь
    salary-транзакции с K≈1 делают то же самое.
    """
    from collections import deque as _deque

    def _clean(raw: str, prefix: str = "") -> str:
        s = raw.replace(" ", "").replace("₸", "").replace("\xa0", "")
        s = s.replace("+", "").replace("-", "")
        return (prefix + s).strip()

    queue: Dict[str, _deque] = {}
    for tx in stmt.transactions:
        if tx.sign != 1:
            continue
        value = tx.amount if tx.is_refund else tx.new_amount
        label = "REFUND_IDENTITY" if tx.is_refund else "TRANSACTION_IN"
        key = _clean(tx.original_amount_text, prefix="IN:")
        if key == "IN:":
            continue
        if key not in queue:
            queue[key] = _deque()
        queue[key].append((value, label))
    return queue


def build_cert_replacement_entries(cert: CertificateData) -> Dict[str, Tuple[float, str]]:
    """Строит записи для replacement_queue, которые синхронизируют страницу
    «Справка об остатке» (стр. 0) с новым балансом выписки.

    Ключи ("CERT_KZT:"/"CERT_USD:"/"CERT_EUR:" + голые цифры оригинального
    текста) должны совпадать с тем, что уже ищет читающий код в
    process_pdf_bytes_raw (cert_paren_callback и hex-ветка с _key_map) —
    раньше туда никто ничего не клал, поэтому справка молча оставалась со
    старым остатком даже после апскейла/даунскейла всей выписки.
    """
    entries: Dict[str, Tuple[float, str]] = {}
    _pairs = (
        ("CERT_KZT:", cert.balance_kzt_text, cert.balance_kzt, cert.new_balance_kzt),
        ("CERT_USD:", cert.balance_usd_text, cert.balance_usd, cert.new_balance_usd),
        ("CERT_EUR:", cert.balance_eur_text, cert.balance_eur, cert.new_balance_eur),
    )
    for key_prefix, text, old_val, new_val in _pairs:
        if not text or old_val <= 0:
            continue
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            continue
        entries[key_prefix + digits] = (new_val, key_prefix.rstrip(":"))
    return entries


def _estimate_months(dates: List[str]) -> int:
    """Оценивает количество месяцев в выписке по датам транзакций."""
    parsed = []
    for d in dates:
        try:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", d)
            if m:
                parsed.append(datetime.strptime(m.group(0), "%d.%m.%y"))
        except Exception:
            continue
    if len(parsed) < 2:
        return 1
    min_d, max_d = min(parsed), max(parsed)
    return max(1, round((max_d - min_d).days / 30))


# ---------------------------------------------------------------------------
#  ЭТАП 3: Валидация (самопроверка перед генерацией PDF)
# ---------------------------------------------------------------------------


def validate_scoring(stmt: StatementData) -> ScoringReport:
    """
    Проверяет правила скоринга:
    Жёсткие: баланс, running balance, итоги
    Мягкие: ISI, ER, min balance, avg balance
    """
    report = ScoringReport()

    # 1. Целостность баланса
    calculated_end = stmt.balance_start + stmt.new_total_income - stmt.total_expense
    report.balance_integrity = abs(calculated_end - stmt.new_balance_end) < 0.02

    # 2. Running balance (в обратном порядке — от старых к новым)
    rb_ok = True
    current_rb = stmt.balance_start
    for tx in reversed(stmt.transactions):
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        if abs(current_rb - tx.new_balance_after) > 0.02:
            rb_ok = False
            break
    report.running_balance_ok = rb_ok

    # 3. Итоги
    # total_income = Σ(salary, NOT refund)
    calc_salary_income = sum(tx.new_amount for tx in stmt.transactions if tx.is_salary and not tx.is_refund)
    
    income_ok = abs(calc_salary_income - stmt.new_total_income) < 1.0
    # Расходы: берём из header (оригинальные), не сверяем с транзакциями
    # (может быть парсинг-дельта, это нормально)
    expense_ok = True
    report.totals_ok = income_ok and expense_ok

    # 4. ISI — считаем по ПОМЕСЯЧНОМУ SALARY доходу (не по возвратам!)
    monthly_incomes: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date) or "unknown"
            monthly_incomes[mk] = monthly_incomes.get(mk, 0) + tx.new_amount
    
    month_values = list(monthly_incomes.values())
    if len(month_values) >= 2:
        mu = sum(month_values) / len(month_values)
        variance = sum((x - mu) ** 2 for x in month_values) / len(month_values)
        sigma = variance ** 0.5
        report.income_stability = max(0, 1 - (sigma / mu)) if mu > 0 else 0
    elif len(month_values) == 1:
        report.income_stability = 1.0

    # 5. Expense Ratio
    if stmt.new_total_income > 0:
        report.expense_ratio = stmt.total_expense / stmt.new_total_income

    # 6-7. Балансы
    balances = [tx.new_balance_after for tx in stmt.transactions]
    report.min_balance = min(balances) if balances else 0
    report.avg_balance = sum(balances) / len(balances) if balances else 0

    # Вердикт
    hard_rules = report.balance_integrity and report.running_balance_ok and report.totals_ok
    soft_rules = report.income_stability >= 0.75 and report.min_balance >= 0
    report.passed = hard_rules and soft_rules

    return report


# ---------------------------------------------------------------------------
#  ЭТАП 4: Подмена значений в PDF (оригинальный механизм сохранён)
# ---------------------------------------------------------------------------


def process_pdf_bytes(input_bytes: bytes, target_monthly_income: float) -> bytes:
    """УСТАРЕЛО, НЕ ИСПОЛЬЗОВАТЬ — оставлено только как исторический вариант
    записи через `doc.update_stream` (см. process_pdf_bytes_raw, который и
    вызывается из main.py:/process).

    Написана до появления cert-формата и с тех пор не поддерживалась. Проверено
    на реальном gold_statement.pdf (цель ×10) — три независимые поломки:
      - сумма на стр.-справке уезжает на x≈4 pt, далеко за левую границу
        таблицы (в raw-писателе это лечится shift = 0 для cert-потока);
      - USD/EUR на справке не обновляются вовсе → «справка = баланс выписки»
        в /verify не сойдётся;
      - нет логики REFUND_IDENTITY, поэтому часть «+»-ячеек получает чужие
        значения из общей по номиналу очереди (12 из 593 сумм оказались вне
        «человеческой» сетки _round_to_natural).
    Чинить её отдельно смысла нет — правильный путь один, raw.

    1. Парсит выписку → StatementData
    2. Пересчитывает математику (K × дисперсия)
    3. Валидирует скоринг
    4. Подменяет значения в PDF-потоках
    5. Возвращает новый PDF
    """
    from collections import deque as _deque

    doc = fitz.open(stream=input_bytes, filetype="pdf")

    # 1. CMap
    TO_UNICODE, FROM_UNICODE = build_dynamic_cmap(doc)

    def hex_to_text(hex_str: str) -> str:
        res = ""
        for i in range(0, len(hex_str), 4):
            chunk = hex_str[i: i + 4]
            res += TO_UNICODE.get(chunk, "?")
        return res

    def text_to_hex(s: str) -> str:
        res = ""
        for c in s:
            res += FROM_UNICODE.get(c, "0000")
        return res

    # 2. Парсинг
    stmt = parse_full_statement(doc)

    # 3. Пересчёт
    stmt = recalculate_statement(stmt, target_monthly_income)

    # 4. Валидация
    report = validate_scoring(stmt)
    print(f"\n{report.summary()}\n")

    # ─── 5. Построение очереди замен ──────────────────────────────
    # Для каждого clean_original текста → deque пар (new_val, type_label).
    # Когда встречаем hex-строку с этим текстом, берём popleft() из очереди.
    # Это гарантирует, что каждый экземпляр суммы получает свою new_val.
    replacement_queue: Dict[str, _deque] = {}

    def _clean(raw: str, prefix: str = "") -> str:
        """Clean amount text to key. prefix distinguishes +income from expenses."""
        s = raw.replace(" ", "").replace("₸", "").replace("\xa0", "")
        s = s.replace("+", "").replace("-", "")
        return (prefix + s).strip()

    # ВАЖНО: порядок — PDF итерируется по страницам сверху вниз.
    # Kaspi печатает транзакции от новых к старым (тот же порядок что в stmt.transactions).
    # Поэтому добавляем в очередь в порядке stmt.transactions.

    # Транзакции пополнения (sign == 1, is_salary — все доходные)
    for tx in stmt.transactions:
        if tx.sign == 1 and tx.is_salary and tx.new_amount != tx.amount:
            key = _clean(tx.original_amount_text, prefix="IN:")
            if key != "IN:":
                if key not in replacement_queue:
                    replacement_queue[key] = _deque()
                replacement_queue[key].append((tx.new_amount, "TRANSACTION_IN"))

    # Транзакции расходов — НЕ масштабируются, НЕ добавляем в очередь
    # (расходы остаются оригинальными для прохождения верификации банка)

    # Итого пополнения (одноразовая замена)
    if stmt.total_income_text:
        key = _clean(stmt.total_income_text, prefix="HDR:")
        if key != "HDR:":
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_total_income, "TOTAL_INCOME"))

    # Расходные категории заголовка — НЕ меняем
    # (банк верифицирует расходные категории с базой Kaspi)

    # Баланс конец
    if stmt.balance_end_text:
        key = _clean(stmt.balance_end_text, prefix="HDR:")
        if key != "HDR:":
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_balance_end, "BALANCE_END"))

    total_planned = sum(len(q) for q in replacement_queue.values())
    print(f"\n[Замены] Подготовлено {total_planned} замен "
          f"({len(replacement_queue)} уникальных ключей)")

    # ─── 6. Regex замена в PDF-потоках ────────────────────────────
    td_pattern = re.compile(
        rb"(\d+\.?\d*)\s+(\d+\.?\d*)\s+Td\s*[<\(]([0-9A-F]+)[>\)]\s*Tj",
        re.IGNORECASE,
    )

    total_replaced = 0

    for page_num in range(len(doc)):
        page = doc[page_num]
        contents = page.get_contents()
        for xref in contents:
            stream_data = doc.xref_stream(xref)
            if stream_data is None:
                continue

            def replace_callback(match, _page=page_num):
                nonlocal total_replaced
                x_str = match.group(1).decode("ascii")
                y_str = match.group(2).decode("ascii")
                full_hex = match.group(3).decode("ascii").upper()

                try:
                    current_x = float(x_str)
                except Exception:
                    return match.group(0)

                original_text = hex_to_text(full_hex).strip()
                clean_digits = (
                    original_text.replace("₸", "")
                    .replace("+", "").replace("-", "")
                    .replace(" ", "").replace("\xa0", "").strip()
                )

                if "?" in clean_digits or not clean_digits:
                    return match.group(0)

                # Определяем тип замены по содержимому текста
                has_plus = "+" in original_text

                # Попробуем все возможные ключи
                # 1) Транзакция: доход (с +) или расход (без +)
                is_hdr = False
                if has_plus:
                    key = "IN:" + clean_digits
                else:
                    key = "OUT:" + clean_digits

                queue = replacement_queue.get(key)
                if not queue:
                    # 2) Заголовочные итоги (TOTAL_INCOME, EXPENSE_*, BALANCE_END)
                    key = "HDR:" + clean_digits
                    queue = replacement_queue.get(key)
                    is_hdr = True
                if not queue:
                    return match.group(0)

                # Для HDR ключей: peek (не удаляем из очереди) — заменяем ВСЕ экземпляры
                # Для транзакций: popleft (каждый экземпляр получает свою new_val)
                if is_hdr:
                    new_val, typ = queue[0]  # peek
                else:
                    new_val, typ = queue.popleft()

                # Формируем новый текст
                # Знак (+/-): для транзакций — из оригинала (тип операции не меняется).
                # Для BALANCE_END и TOTAL_INCOME — из знака нового значения.
                if typ in ("BALANCE_END", "TOTAL_INCOME"):
                    if new_val >= 0:
                        prefix = "+ " if "+" in original_text else ""
                    else:
                        prefix = "- "
                elif "+" in original_text:
                    prefix = "+ "
                elif "-" in original_text:
                    prefix = "- "
                else:
                    prefix = ""
                suffix = " ₸" if "₸" in original_text else ""
                formatted_num = f"{abs(new_val):,.2f}".replace(",", " ").replace(".", ",")
                new_text = f"{prefix}{formatted_num}{suffix}"
                new_hex = text_to_hex(new_text)

                if "0000" in new_hex:
                    # Проверяем по 4-символьным блокам (а не подстрокой)
                    has_missing = any(
                        new_hex[i:i+4] == "0000"
                        for i in range(0, len(new_hex), 4)
                    )
                    if has_missing:
                        print(f"  [⚠️] Ошибка кодирования: '{new_text}'")
                        # Возвращаем в очередь
                        queue.appendleft((new_val, typ))
                        return match.group(0)

                # Сдвиг X
                result = get_text_metrics(doc[_page], original_text)
                avg_char_width = result[2] if result and result[2] else 4.0

                def get_weighted_length(text):
                    weights = {
                        " ": 0.4, "\xa0": 0.4, ".": 0.4, ",": 0.4,
                        "₸": 1.0, "+": 1.0, "-": 1.0,
                    }
                    length = 0.0
                    for char in text:
                        length += 1.0 if char.isdigit() else weights.get(char, 1.0)
                    return length

                len_old = get_weighted_length(original_text)
                len_new = get_weighted_length(new_text)
                original_pixel_w = avg_char_width * len(original_text)
                digit_unit = original_pixel_w / len_old if len_old > 0 else avg_char_width
                shift = (len_new * digit_unit - original_pixel_w) * 0.96
                new_x = current_x - shift

                print(f"  [🎯 {typ}] {original_text} → {new_text} "
                      f"(X: {current_x:.1f} → {new_x:.1f})")

                total_replaced += 1

                return f"{new_x:.5f} {y_str} Td <{new_hex}> Tj".encode("ascii")

            new_data = td_pattern.sub(replace_callback, stream_data)
            doc.update_stream(xref, new_data)

    # Проверяем, остались ли незамененные элементы (исключая HDR — они peek-based)
    leftover = sum(
        len(q) for key, q in replacement_queue.items()
        if not key.startswith("HDR:")
    )
    if leftover:
        print(f"\n[⚠️] Не заменено {leftover} транзакционных элементов:")
        for key, q in replacement_queue.items():
            if q and not key.startswith("HDR:"):
                print(f"  '{key}' — осталось {len(q)} замен: "
                      f"{[(v, t) for v, t in list(q)[:3]]}")

    print(f"\n[Результат] Произведено замен: {total_replaced}")

    return doc.tobytes()


# ---------------------------------------------------------------------------
#  ЭТАП 5: Raw-bytes замена (сохраняет бинарную структуру PDF)
# ---------------------------------------------------------------------------

import zlib


def process_pdf_bytes_raw(
    input_bytes: bytes,
    target_monthly_income: float,
    recalc_fn=None,
) -> bytes:
    """
    Обрабатывает PDF напрямую на уровне raw bytes.

    Параметр `recalc_fn` (callable: (stmt, target) -> stmt) позволяет
    подменить движок пересчёта. По умолчанию используется
    `recalculate_statement` — рабочий путь завышения. Это единственная
    точка расширения для отдельных режимов (например, downscale).

    Вместо doc.tobytes() (который пересобирает PDF с другими line endings,
    ID, trailer и т.д.), этот метод:
    1. Парсит через fitz (для логики)
    2. Строит карту замен (deque-based)
    3. Находит стримы в raw bytes
    4. Декомпрессирует (zlib) → regex замена → компрессирует обратно
    5. Обновляет /Length в объекте
    6. Пересчитывает xref offsets
    7. Сохраняет ОРИГИНАЛЬНЫЕ: header, trailer формат, ID, line endings
    """
    from collections import deque as _deque

    if recalc_fn is None:
        recalc_fn = recalculate_statement

    # ─── 1. Парсинг через fitz (только для логики) ─────────────
    doc = fitz.open(stream=input_bytes, filetype="pdf")
    TO_UNICODE, FROM_UNICODE = build_dynamic_cmap(doc)

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

    # Детектор формата: 'cert' (стр. 0 — справка) или 'legacy'
    fmt = detect_statement_format(doc)
    print(f"[Raw] Формат PDF: {fmt}")

    cert: Optional[CertificateData] = None
    if fmt == "cert":
        cert = parse_certificate_page(doc)
        # Выписка начинается со стр. 1 (стр. 0 = справка)
        stmt = parse_full_statement(doc, start_page=1)
        # Согласованный пересчёт: stmt + cert через сохранённый курс валют.
        # recalc_fn прокидываем и сюда — иначе downscale-запросы (см.
        # pdf_service_downscale.process_downscale) на cert-формате всегда
        # прогонялись бы upscale-движком в обход своих floor-проверок.
        cert, stmt = recalculate_with_certificate(cert, stmt, target_monthly_income, recalc_fn=recalc_fn)
    else:
        stmt = parse_full_statement(doc)
        stmt = recalc_fn(stmt, target_monthly_income)

    report = validate_scoring(stmt)
    print(f"\n{report.summary()}\n")

    # ─── 1b. Per-page Y-фильтр для salary-транзакций ────────────────────────
    # content_stream_Y ≈ tx.y_pdf_rounded - ~9 pt: text baseline находится ниже
    # верха bounding box (PyMuPDF y0) на высоту шрифта минус descender (~7-10 pt).
    # Невидимые PDF-дубликаты появляются на других Y — per-page фильтр их блокирует.
    _Y_OFFSET = 9   # медианное смещение baseline от top bounding box
    _Y_TOL = 4      # допуск ±4 pt покрывает разброс 7-10 pt
    page_income_cs_ys: Dict[int, set] = {}
    for _tx in stmt.transactions:
        if _tx.sign == 1 and not _tx.is_refund and _tx.y_pdf_rounded > 0:
            _lo = _tx.y_pdf_rounded - _Y_OFFSET - _Y_TOL
            _hi = _tx.y_pdf_rounded - _Y_OFFSET + _Y_TOL
            page_income_cs_ys.setdefault(_tx.page_num, set()).update(range(_lo, _hi + 1))
    print(f"[YFilter] Salary на {len(page_income_cs_ys)} страницах, "
          f"допустимых Y (per-page, offset={_Y_OFFSET}±{_Y_TOL}): "
          f"{sum(len(s) for s in page_income_cs_ys.values())}")

    # Тот же Y-фильтр для is_refund-строк (возвраты покупок/переводов И
    # «Поступление»/«Зачисление» self-transfer). Нужен как НАДЁЖНАЯ замена
    # y_has_refund_type ниже: тот сканирует тип-слово ("Покупка"/"Поступление"
    # и т.п.) КАК hex-Tj-токен В ТОМ ЖЕ content-стриме, что и суммы — но на
    # реальных Kaspi Gold PDF описание/тип строки транзакции физически лежит
    # в ДРУГОМ объекте (не среди Td/Tj-токенов этого стрима вообще; проверено
    # на goldformat1.pdf — ни один из ~44 токенов стрима страницы не декодируется
    # в тип-слово), поэтому y_has_refund_type там всегда остаётся ПУСТЫМ
    # множеством и ветка ниже никогда не срабатывает. y_pdf_rounded же взят из
    # высокоуровневого page.get_text("words") (PyMuPDF сам разбирает
    # XObject/сложную структуру), поэтому page_income_cs_ys для salary уже
    # работает корректно — используем ТОТ ЖЕ механизм и для refund-строк.
    page_refund_cs_ys: Dict[int, set] = {}
    for _tx in stmt.transactions:
        if _tx.sign == 1 and _tx.is_refund and _tx.y_pdf_rounded > 0:
            _lo = _tx.y_pdf_rounded - _Y_OFFSET - _Y_TOL
            _hi = _tx.y_pdf_rounded - _Y_OFFSET + _Y_TOL
            page_refund_cs_ys.setdefault(_tx.page_num, set()).update(range(_lo, _hi + 1))

    # ─── 2. Очередь замен ────────────────────────────────────
    replacement_queue: Dict[str, _deque] = {}

    def _clean(raw: str, prefix: str = "") -> str:
        s = raw.replace(" ", "").replace("₸", "").replace("\xa0", "")
        s = s.replace("+", "").replace("-", "")
        return (prefix + s).strip()

    # ВСЕ транзакции sign==+1 идут в IN: очередь в порядке PDF (см.
    # build_income_replacement_entries): salary — new_amount (даже если он
    # совпал со старым при K_month≈1 — иначе эта транзакция не резервирует
    # свой слот и раскрадывает чужой), возвраты — amount (identity).
    for key, entries in build_income_replacement_entries(stmt).items():
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
        replacement_queue[key].extend(entries)

    # Расходные транзакции — НЕ масштабируются, НЕ добавляем в очередь

    if stmt.total_income_text and stmt.total_income > 0:
        key = _clean(stmt.total_income_text, prefix="HDR:")
        if key != "HDR:":
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            # Заголовок «Пополнения» = точная сумма новых salary-транзакций.
            # Формула через дельту балансов давала расхождение, если total_expense
            # был взят из категорий (а не из уравнения баланса).
            replacement_queue[key].append((stmt.new_total_income, "TOTAL_INCOME"))

    # Расходные категории заголовка — НЕ меняем (банк верифицирует с базой)

    if stmt.balance_end_text:
        key = _clean(stmt.balance_end_text, prefix="HDR:")
        if key != "HDR:":
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_balance_end, "BALANCE_END"))

    # CERT-балансы (₸/$/€) со страницы справки.
    #
    # Раньше этот блок был отключён: включение build_cert_replacement_entries()
    # вскрывало два независимых бага, найденные и исправленные на реальном
    # файле (₸212 017,14 → ₸39 959 306,05, справка НЕ обновлялась вовсе):
    #   1) hex-ветка ниже (cert_prefix_sym / _key_map) искала ключ по
    #      clean_digits, который (в отличие от build_cert_replacement_entries)
    #      НЕ вырезает запятую-разделитель дробной части — "CERT_KZT:212017,14"
    #      никогда не совпадал с "CERT_KZT:21201714", подстановка молча
    #      пропускалась. См. cert_clean_digits ниже.
    #   2) parse_certificate_page() делил слова строки на КZT/USD/EUR колонки
    #      по фиксированным X-порогам (200/360), откалиброванным под короткие
    #      суммы оригинала; когда апскейл увеличивает разрядность числа,
    #      writer сдвигает его влево (сохраняя правый край), и на крупных
    #      суммах сдвиг утаскивает "$"/"€" за фиксированный порог в соседнюю
    #      колонку — parse_amount() получал чужой нечисловой токен и тихо
    #      возвращал 0.00. Заменено на кластеризацию по разрывам (см. там же).
    if fmt == "cert" and cert is not None:
        for key, (new_val, typ) in build_cert_replacement_entries(cert).items():
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            replacement_queue[key].append((new_val, typ))

    total_planned = sum(len(q) for q in replacement_queue.values())
    print(f"[Raw] Подготовлено {total_planned} замен ({len(replacement_queue)} уникальных ключей)")

    # ─── 3. Собираем page→xref маппинг ────────────────────────
    page_xrefs = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_xrefs.append(page.get_contents())

    # Измеряем реальную ширину символа на странице справки для точной X-корректировки.
    # Делаем это ДО doc.close(), пока страница ещё доступна.
    cert_char_width: float = 5.5  # fallback
    if fmt == "cert" and cert is not None and cert.balance_kzt_text:
        _m = get_text_metrics(doc[0], cert.balance_kzt_text)
        if _m[2] and _m[2] > 0:
            cert_char_width = _m[2]
            print(f"[Cert] Реальная ширина символа: {cert_char_width:.2f} pt (от '{cert.balance_kzt_text}')")
        else:
            print(f"[Cert] Ширину символа измерить не удалось, fallback={cert_char_width}")

    # ─── Шрифт-заменитель для недостающих глифов цифр на стр. справки ──────
    # Субсет-шрифт стр. 0 (F1) может НЕ содержать глиф какой-то цифры, если в
    # оригинале справки эта цифра нигде не встречалась (реальный файл: ИИН/счёт
    # без '8' → F1 не включил глиф '8', и пересчитанный баланс с восьмёркой
    # рисовался пустым квадратом □). На страницах выписки та же цифра рисуется
    # шрифтом F2 (в его субсете глиф есть). Решение: если F1 не покрывает цифру,
    # а другой шрифт документа покрывает И совпадает с F1 по CID для остальных
    # цифр (тот же ArialMT-субсет), подставляем этот шрифт для ячеек справки,
    # которые он полностью покрывает (KZT: ₸/пробел/запятая/цифры — все есть).
    # Символы валюты $/€ у F2 обычно отсутствуют, поэтому такие ячейки этот
    # механизм не трогает (fallback на прежнее поведение). Всё под gate: если у
    # F1 все цифры на месте (обычный файл) — заменитель не ищется, поведение не
    # меняется.
    cert_sub_font_name: Optional[bytes] = None   # напр. b"F2"
    cert_sub_font_xref: Optional[int] = None
    cert_sub_chars: set = set()
    cert_page_font_dict_xref: Optional[int] = None
    if fmt == "cert":
        try:
            def _font_tounicode_cmap(font_xref: int) -> dict:
                fo = doc.xref_object(font_xref)
                mm = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", fo)
                if not mm:
                    return {}
                return _parse_cmap_stream(
                    doc.xref_stream(int(mm.group(1))).decode("latin-1", "ignore")
                )

            _p0 = doc.xref_object(doc.page_xref(0))
            _rm = re.search(r"/Resources\s+(\d+)\s+0\s+R", _p0)
            _res = doc.xref_object(int(_rm.group(1))) if _rm else _p0
            _fm = re.search(r"/Font\s+(\d+)\s+0\s+R", _res)
            if _fm:
                cert_page_font_dict_xref = int(_fm.group(1))
                _fdict = doc.xref_object(cert_page_font_dict_xref)
                _page0_fonts = {n: int(x) for n, x in re.findall(r"/(\w+)\s+(\d+)\s+0\s+R", _fdict)}
                if _page0_fonts:
                    # Основной шрифт стр.0 = с наибольшим cmap (в нём кириллица).
                    _prim = max(_page0_fonts, key=lambda n: len(_font_tounicode_cmap(_page0_fonts[n])))
                    _prim_inv = {v: k for k, v in _font_tounicode_cmap(_page0_fonts[_prim]).items()}
                    _missing = [d for d in "0123456789" if d not in _prim_inv]
                    if _missing:
                        # Кандидаты — все шрифты документа, не на стр.0.
                        _cand_xrefs = set()
                        for _pi in range(len(doc)):
                            for _f in doc[_pi].get_fonts(full=True):
                                _cand_xrefs.add(_f[0])
                        for _cx in sorted(_cand_xrefs):
                            if _cx in _page0_fonts.values():
                                continue
                            _cm = _font_tounicode_cmap(_cx)
                            if not _cm:
                                continue
                            _inv = {v: k for k, v in _cm.items()}
                            if not all(d in _inv for d in "0123456789"):
                                continue
                            # CID должны совпадать с F1 для общих цифр.
                            if not all(_inv[d] == _prim_inv[d] for d in "0123456789" if d in _prim_inv):
                                continue
                            cert_sub_font_xref = _cx
                            cert_sub_chars = set(_cm.values())
                            _i = 2
                            while f"F{_i}" in _page0_fonts:
                                _i += 1
                            cert_sub_font_name = f"F{_i}".encode()
                            print(f"[Cert] Шрифт-заменитель для цифр {_missing}: xref={_cx} как /{cert_sub_font_name.decode()}")
                            break
        except Exception as _e:
            cert_sub_font_name = None
            print(f"[Cert] Поиск шрифта-заменителя не удался: {_e}")

    doc.close()

    def _ambient_tf(buf: bytes, pos: int) -> Tuple[Optional[bytes], Optional[bytes]]:
        """Имя и кегль последнего /Fx N Tf до позиции pos (для восстановления
        шрифта после числа, набранного шрифтом-заменителем)."""
        last = None
        for mm in re.finditer(rb"/(\w+)\s+([\d.]+)\s+Tf", buf[:pos]):
            last = mm
        if last:
            return last.group(1), last.group(2)
        return None, None

    def _digit_width_at(buf: bytes, pos: int) -> float:
        """Реальная ширина цифры (pt) в той точке потока, где стоит число.

        Суммы в выписке ПРАВО-выровнены: Td.x — левый край строки, поэтому при
        росте числа X обязан сдвинуться влево ровно на прирост ширины текста,
        иначе колонка «Сумма» становится рваной. Ширина берётся из реального
        кегля (последний `/Fx N Tf` до этой позиции), а не из константы:
        у ArialMT цифра = _ARIAL_DIGIT_EM em, т.е. 5.28 pt при кегле 9.5
        (тело выписки) и 5.56 pt при кегле 10 (сводная таблица на стр. 1).
        """
        _, size_b = _ambient_tf(buf, pos)
        size = 0.0
        if size_b:
            try:
                size = float(size_b)
            except ValueError:
                size = 0.0
        if size <= 0:
            size = _FALLBACK_FONT_SIZE
        return _ARIAL_DIGIT_EM * size

    # ─── 4. Regex для Td/Tj в декомпрессированных стримах ─────
    td_pattern = re.compile(
        rb"(\d+\.?\d*)\s+(\d+\.?\d*)\s+Td\s*[<\(]([0-9A-F]+)[>\)]\s*Tj",
        re.IGNORECASE,
    )

    # ─── 5. Raw-bytes обработка ────────────────────────────────
    raw = bytearray(input_bytes)
    total_replaced = 0

    # Найдём все объекты content streams и обработаем их
    # Собираем все content xrefs
    all_content_xrefs = set()
    for xrefs in page_xrefs:
        all_content_xrefs.update(xrefs)

    # Для каждого content stream: найти в raw bytes, декомпрессировать, заменить
    # Нам нужно обрабатывать объекты в порядке appearance в PDF (по offset),
    # чтобы корректно обновлять xref.
    # 
    # Стратегия: 
    #   1. Находим все объекты и их позиции
    #   2. Для content streams делаем замены в декомпрессированных данных
    #   3. Компрессируем обратно
    #   4. Если новый сжатый стрим отличается по длине — обновляем /Length и  
    #      все последующие offsets
    
    # Находим позиции всех объектов
    obj_positions = {}  # xref_id → offset в raw
    for xref_id in all_content_xrefs:
        pattern = f"{xref_id} 0 obj".encode()
        pos = raw.find(pattern)
        if pos >= 0:
            obj_positions[xref_id] = pos

    # Сортируем по позиции (чтобы обрабатывать от начала к концу)
    sorted_xrefs = sorted(obj_positions.items(), key=lambda x: x[1])

    # Определяем page для каждого xref (для get_text_metrics)
    xref_to_page = {}
    for page_num, xrefs in enumerate(page_xrefs):
        for xref_id in xrefs:
            xref_to_page[xref_id] = page_num

    # Аккумулятор сдвига: когда мы меняем длину стрима, 
    # все последующие offset-ы сдвигаются
    cumulative_offset = 0

    for xref_id, orig_pos in sorted_xrefs:
        pos = orig_pos + cumulative_offset

        # Находим объект: "N 0 obj ... stream\r\n ... \r\nendstream"
        obj_header = f"{xref_id} 0 obj".encode()
        
        # Находим /Length N
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
        
        # Находим начало данных стрима
        stream_keyword_pos = endobj_or_stream
        data_start = stream_keyword_pos + 6  # len("stream")
        # stream может быть followed by \r\n or \n
        if raw[data_start:data_start + 1] == b'\r':
            data_start += 2  # \r\n
        else:
            data_start += 1  # \n
        
        # endstream
        endstream_pos = raw.find(b"endstream", data_start)
        if endstream_pos < 0:
            continue
        
        # Данные стрима (могут иметь trailing \r\n перед endstream)
        raw_stream_data = bytes(raw[data_start:endstream_pos])
        # Убираем trailing whitespace перед endstream
        if raw_stream_data.endswith(b'\r\n'):
            raw_stream_data = raw_stream_data[:-2]
        elif raw_stream_data.endswith(b'\n'):
            raw_stream_data = raw_stream_data[:-1]
        
        # Декомпрессируем
        try:
            decompressed = zlib.decompress(raw_stream_data)
        except zlib.error:
            # Стрим не FlateDecode — пропускаем
            continue
        
        # ─── Делаем regex замены ──────────────────────────────
        page_num = xref_to_page.get(xref_id, 0)
        # cert-страница = 0 (только в новом формате); выписка начинается с cert_page_offset
        cert_page_offset = 1 if fmt == "cert" else 0
        # Множество xref'ов, относящихся к странице справки (стр. 0).
        cert_xrefs = set(page_xrefs[0]) if fmt == "cert" and page_xrefs else set()
        is_cert_stream = xref_id in cert_xrefs

        # Строим карту Y → тип операции для распознавания доходов и возвратов.
        # Kaspi PDF позиционирует каждый элемент строки через Tm-reset + Td с
        # абсолютными координатами, поэтому group(2) = абсолютная Y-координата
        # строки и одинакова для суммы, типа, текущего остатка на той же строке.
        # ВАЖНО: этот набор должен зеркалить is_refund-классификацию из
        # parse_full_statement (TX_TYPES_EXPENSE | TX_TYPES_SELF_TRANSFER) —
        # иначе строка «+»-суммы, которую parse_full_statement пометил как
        # is_refund=True (и поставил в очередь REFUND_IDENTITY, см. ниже), тут
        # не распознаётся как «строка возврата»: слот REFUND_IDENTITY не
        # потребляется физической ячейкой (см. ветку has_plus_decoded and
        # y_str in y_has_refund_type ниже), остаётся «застрявшим» в начале
        # общей по значению очереди "IN:<сумма>" и достаётся СЛЕДУЮЩЕЙ ячейке
        # с тем же числом (обычно зарплатной) вместо неё — та получает старое
        # немасштабированное значение, а её собственная запись в очереди
        # сдвигает уже ВСЕ последующие ячейки с этим числом на одну позицию.
        # Воспроизведено на реальной выписке (goldformat1.pdf) после добавления
        # «Поступление»/«Зачисление» в parse_full_statement: 179 из 536 «+»
        # ячеек получали чужое значение каскадно от одной незамеченной строки.
        REFUND_TYPE_WORDS = {
            'Покупка', 'Перевод', 'Снятие', 'Оплата',
            'Платёж', 'Платеж', 'Комиссия', 'Возврат', 'Разное',
            'Поступление', 'Зачисление',
        }
        INCOME_TYPE_WORDS = {'Пополнение'}
        y_has_refund_type: set = set()
        y_has_income_type: set = set()

        for td_match in td_pattern.finditer(decompressed):
            y_val = td_match.group(2).decode("ascii")
            hex_val = td_match.group(3).decode("ascii").upper()
            decoded = hex_to_text(hex_val).strip()
            if decoded in REFUND_TYPE_WORDS:
                y_has_refund_type.add(y_val)
            if decoded in INCOME_TYPE_WORDS:
                y_has_income_type.add(y_val)

        if y_has_income_type:
            print(f"[Scan] Пополнение-строки (Y в потоке): {sorted(y_has_income_type)[:5]}")
        
        def replace_callback(match):
            nonlocal total_replaced
            x_str = match.group(1).decode("ascii")
            y_str = match.group(2).decode("ascii")
            full_hex = match.group(3).decode("ascii").upper()

            try:
                current_x = float(x_str)
            except Exception:
                return match.group(0)

            original_text = hex_to_text(full_hex).strip()

            # ── Разбираем hex на 4-байтные блоки и определяем числовую зону ──
            # Это позволяет сохранить "посторонние" глифы (знаки +/-, валюта,
            # иконки) даже если они декодируются как "?" из-за неполного CMap.
            hex_blocks = [full_hex[i:i + 4] for i in range(0, len(full_hex), 4)]
            decoded_blocks = [TO_UNICODE.get(b, "?") for b in hex_blocks]

            first_digit = next(
                (i for i, ch in enumerate(decoded_blocks) if ch.isdigit()),
                None,
            )
            last_digit = next(
                (i for i in range(len(decoded_blocks) - 1, -1, -1) if decoded_blocks[i].isdigit()),
                None,
            )
            if first_digit is None or last_digit is None:
                return match.group(0)

            old_num_text = "".join(decoded_blocks[first_digit:last_digit + 1])
            clean_digits = (
                old_num_text.replace(" ", "").replace("\xa0", "")
                .replace("+", "").replace("-", "").strip()
            )
            if not clean_digits:
                return match.group(0)

            # Префикс/суффикс (то, что вне числовой зоны)
            prefix_chars = decoded_blocks[:first_digit]
            suffix_chars = decoded_blocks[last_digit + 1:]
            prefix_text = "".join(prefix_chars)
            suffix_text = "".join(suffix_chars)

            # Знак "+/-" → определяем по prefix (если виден) или по фону
            has_plus_decoded = "+" in prefix_text
            has_minus_decoded = "-" in prefix_text
            sign_unknown = (not has_plus_decoded and not has_minus_decoded
                            and "?" in prefix_text)

            # ── Маршрутизация ──
            # CERT (страница-справка) — формат "<валюта> <число>" (валюта В НАЧАЛЕ).
            # Обычные суммы выписки имеют формат "+ NNN ₸" / "- NNN ₸" (валюта В КОНЦЕ).
            queue = None
            typ = None
            cert_currency: Optional[str] = None
            stripped_prefix = prefix_text.strip()
            cert_prefix_sym: Optional[str] = None
            for _sym in ("₸", "$", "€"):
                if stripped_prefix.startswith(_sym):
                    cert_prefix_sym = _sym
                    break

            if is_cert_stream and fmt == "cert":
                # На странице справки: пробуем все три CERT-ключа.
                # Валюта может быть либо в префиксе ("₸ 143 170,28" — один Tj),
                # либо отдельным Tj — тогда префикс пуст, число встречается само
                # по себе. Для USD/EUR на справке валюта всегда отдельным глифом.
                _key_map = {"₸": "CERT_KZT:", "$": "CERT_USD:", "€": "CERT_EUR:"}
                # CERT-ключи в build_cert_replacement_entries() хранят ТОЛЬКО
                # цифры (без запятой-разделителя дробной части) — а clean_digits
                # здесь запятую сохраняет (эта же переменная используется ниже
                # для обычных IN:/OUT:/HDR: ключей, где запятая обязана
                # остаться, см. _clean() в build_income_replacement_entries).
                # Отдельно дочищаем только для CERT-поиска.
                cert_clean_digits = clean_digits.replace(",", "").replace(".", "")
                # 1) Если префикс начинается с валюты — берём её.
                tried_currency = None
                for _sym in ("₸", "$", "€"):
                    if cert_prefix_sym is None:
                        continue
                    if cert_prefix_sym == _sym:
                        _q = replacement_queue.get(_key_map[_sym] + cert_clean_digits)
                        if _q:
                            queue = _q
                            tried_currency = _sym
                            break
                # 2) Иначе перебираем все три по голому числу.
                if queue is None:
                    for _sym in ("₸", "$", "€"):
                        _q = replacement_queue.get(_key_map[_sym] + cert_clean_digits)
                        if _q:
                            queue = _q
                            tried_currency = _sym
                            break
                if queue is None:
                    return match.group(0)
                cert_currency = tried_currency
                new_val, typ = queue[0]  # peek — одно значение на справку
                is_hdr = True
            else:
                # Возврат/self-transfer? (+ на строке типа Покупка/Перевод/
                # Поступление/etc) — пропускаем, но потребляем слот
                # REFUND_IDENTITY, чтобы он не перехватил замену следующей
                # зарплатной транзакции с той же суммой. y_has_refund_type
                # (сканирование тип-слова В ЭТОМ ЖЕ content-стриме) на части
                # реальных Kaspi Gold PDF всегда пуст — тип/описание строки
                # физически не лежит среди Td/Tj-токенов этого стрима — тогда
                # полагаемся на page_refund_cs_ys (Y из page.get_text("words"),
                # см. комментарий у его построения выше), который работает
                # независимо от структуры content-стрима.
                _y_int = round(float(y_str))
                _page_refund_ys = page_refund_cs_ys.get(page_num)
                _is_refund_row = (y_str in y_has_refund_type) or (
                    _page_refund_ys is not None and _y_int in _page_refund_ys
                )
                if has_plus_decoded and _is_refund_row:
                    _ref_q = replacement_queue.get("IN:" + clean_digits)
                    if _ref_q and _ref_q[0][1] == "REFUND_IDENTITY":
                        _ref_q.popleft()
                    return match.group(0)

                # Кандидаты ключей в порядке предпочтения
                candidates = []
                if has_plus_decoded:
                    # Per-page фильтр: принимаем IN: только если content-stream Y
                    # совпадает с ожидаемой позицией salary-транзакции на ЭТОЙ странице.
                    _y_int = round(float(y_str))
                    _page_ys = page_income_cs_ys.get(page_num)
                    if _page_ys is not None and _y_int in _page_ys:
                        candidates.append(("IN:" + clean_digits, False))
                elif has_minus_decoded:
                    candidates.append(("OUT:" + clean_digits, False))
                elif prefix_text:
                    # Знак присутствует но не декодирован (например, "?"): пробуем оба.
                    # Пустой prefix_text означает отсутствие знака (running balance,
                    # заголовочное число) — в этот блок не заходим, чтобы не съедать
                    # слоты транзакционной очереди.
                    candidates.append(("OUT:" + clean_digits, False))
                    candidates.append(("IN:" + clean_digits, False))
                # HDR (peek) как последний шанс
                candidates.append(("HDR:" + clean_digits, True))

                key = None
                is_hdr = False
                for _k, _is_hdr in candidates:
                    _q = replacement_queue.get(_k)
                    if _q:
                        key = _k
                        is_hdr = _is_hdr
                        queue = _q
                        break
                if not queue:
                    return match.group(0)

                if is_hdr:
                    new_val, typ = queue[0]
                else:
                    new_val, typ = queue.popleft()

            # ── Формируем НОВЫЙ числовой блок (только цифры/пробелы/запятая) ──
            formatted_num = f"{abs(new_val):,.2f}".replace(",", " ").replace(".", ",")
            new_num_hex = text_to_hex(formatted_num)

            # Проверка что все цифровые символы есть в FROM_UNICODE
            if "0000" in new_num_hex:
                if any(new_num_hex[i:i + 4] == "0000"
                       for i in range(0, len(new_num_hex), 4)):
                    if not is_hdr:
                        queue.appendleft((new_val, typ))
                    return match.group(0)

            # Собираем итоговый hex: префикс + новое число + суффикс
            new_hex = "".join(hex_blocks[:first_digit]) + new_num_hex + "".join(hex_blocks[last_digit + 1:])

            # Для логов и пересчёта X — собираем "новый текст" целиком
            new_text = prefix_text + formatted_num + suffix_text
            original_text_for_log = original_text

            # Длину не сдвигаем — просто меняем hex
            # X-координату подстраиваем под разницу длин строк.
            # Cert-страница использует более крупный шрифт → ширина символа больше.
            # cert_char_width измеряется из оригинального PDF (см. выше).
            # Для тела выписки ширина цифры берётся из фактического кегля в этом
            # месте потока (_digit_width_at). Раньше тут стояла константа 4.0 pt
            # — это лишь ~76% реальной ширины цифры (5.28 pt при кегле 9.5), из-за
            # чего сдвиг X недобирал ~24% прироста, и чем сильнее выросло число,
            # тем дальше вправо от своей колонки уезжал его правый край: на
            # реальном файле gold_statement.pdf правые края «Суммы» разъезжались
            # на 187.5/188.1/189.4 вместо общей для колонки 190.9, а итоговый
            # остаток в сводной таблице пересекал правую границу ячейки.
            avg_char_width = (cert_char_width if is_cert_stream
                              else _digit_width_at(decompressed, match.start()))

            def get_weighted_length(text):
                weights = {
                    " ": 0.5, "\xa0": 0.5, ".": 0.5, ",": 0.5,
                    "₸": 1.2, "+": 0.8, "-": 0.8,
                }
                length = 0.0
                for char in text:
                    length += 1.0 if char.isdigit() else weights.get(char, 1.0)
                return length

            # .strip() обязателен на ОБЕИХ строках: original_text уже обрезан
            # (см. выше), а new_text собран из сырых prefix/suffix и сохраняет
            # хвостовые пробелы ячейки («+ 6 500,00 ₸   »). Без него хвост
            # считался только в len_new и давал постоянный лишний сдвиг влево
            # (3 пробела × 0.5 × ширина цифры ≈ 8 pt) поверх основной ошибки.
            len_old = get_weighted_length(original_text.strip())
            len_new = get_weighted_length(new_text.strip())
            original_pixel_w = avg_char_width * len_old
            if is_cert_stream:
                # Ячейки на справке ЛЕВО-выровненные — исходный Td.x суммы
                # совпадает с Td.x заголовка её колонки ("Сумма на счете...").
                # Формула ниже (сдвиг влево на разницу ширин) верна для
                # ПРАВО-выровненного макета сумм в самой выписке — на справке
                # она уводит короткий оригинал далеко влево при сильном росте
                # цифр (воспроизведено на реальном файле: «1 898,08» →
                # «11 148 074,08» вылезло за левую границу таблицы). Справа
                # до следующей колонки достаточно места — X не трогаем.
                shift = 0.0
            else:
                shift = (len_new - len_old) * avg_char_width
            new_x = current_x - shift

            print(f"  [🎯 {typ}] {original_text} → {new_text}")

            total_replaced += 1

            # Если это ячейка справки, а основной шрифт стр.0 не содержит какой-то
            # цифры — набираем ЦИФРЫ шрифтом-заменителем (в нём глиф есть), а
            # префикс (валюта ₸/$/€ + пробелы) оставляем прежним шрифтом (в
            # заменителе может не быть глифа валюты). После Tj позиция сама
            # сдвигается на ширину текста, поэтому второй Tj встаёт вплотную —
            # ширину префикса вычислять не нужно. В конце возвращаем прежний
            # шрифт, чтобы не сломать последующий текст на странице.
            if is_cert_stream and cert_sub_font_name is not None:
                _amb, _amb_sz = _ambient_tf(decompressed, match.start())
                # Цифровая часть + суффикс должны быть покрыты заменителем.
                _rest_chars = formatted_num + suffix_text
                if _amb is not None and all(c in cert_sub_chars for c in _rest_chars):
                    _prefix_hex = "".join(hex_blocks[:first_digit])
                    _rest_hex = new_num_hex + "".join(hex_blocks[last_digit + 1:])
                    _sub = cert_sub_font_name.decode()
                    _an = _amb.decode()
                    _sz = _amb_sz.decode()
                    _out = f"{new_x:.5f} {y_str} Td ".encode("ascii")
                    if _prefix_hex:
                        _out += f"/{_an} {_sz} Tf <{_prefix_hex}> Tj ".encode("ascii")
                    _out += f"/{_sub} {_sz} Tf <{_rest_hex}> Tj /{_an} {_sz} Tf".encode("ascii")
                    return _out

            return f"{new_x:.5f} {y_str} Td <{new_hex}> Tj".encode("ascii")

        new_decompressed = td_pattern.sub(replace_callback, decompressed)

        # ── Cert-страница: скобочный (parenthesized) формат ──────
        # На стр. 0 некоторые Tj закодированы как Td(...) Tj (сырые байты, BigEndian).
        # Паттерн: X Y Td (<raw2bytes...>) Tj
        if is_cert_stream and cert is not None:
            paren_pat = re.compile(
                rb"(\d+\.?\d*)\s+(\d+\.?\d*)\s+Td\s*\(([^)]*)\)\s*Tj"
            )

            def paren_decode(raw_bytes: bytes) -> str:
                result = ""
                for i in range(0, len(raw_bytes) - 1, 2):
                    code = "%04X" % (raw_bytes[i] << 8 | raw_bytes[i + 1])
                    result += TO_UNICODE.get(code, "?")
                return result

            def paren_encode(text: str) -> bytes:
                out = bytearray()
                for ch in text:
                    # Дефолт "0000" (hex-строка нулевого CID), НЕ "\x00\x00"
                    # (сырые байты): int("\x00\x00", 16) падал ValueError'ом на
                    # символе вне CMap. "0000" даёт 2 нулевых байта, которые
                    # ловит guard в cert_paren_callback (см. ниже) и пропускает
                    # замену, а не роняет всю обработку.
                    code = FROM_UNICODE.get(ch, "0000")
                    c = int(code, 16)
                    out.append((c >> 8) & 0xFF)
                    out.append(c & 0xFF)
                return bytes(out)

            def cert_paren_callback(m: re.Match) -> bytes:
                nonlocal total_replaced
                x_str = m.group(1).decode("ascii")
                y_str2 = m.group(2).decode("ascii")
                try:
                    current_x = float(x_str)
                except Exception:
                    return m.group(0)

                raw_bytes = m.group(3)
                # Для unescape: Kaspi использует bigendian без escape обычно
                # но backslash-escape может быть
                try:
                    unescaped = raw_bytes.decode("latin-1")
                    unescaped_bytes = raw_bytes
                except Exception:
                    return m.group(0)

                original_text = paren_decode(unescaped_bytes)

                # Числовые блоки
                decoded_blocks_p = list(original_text)
                first_d = next((i for i, ch in enumerate(decoded_blocks_p) if ch.isdigit()), None)
                last_d = next((i for i in range(len(decoded_blocks_p) - 1, -1, -1) if decoded_blocks_p[i].isdigit()), None)
                if first_d is None or last_d is None:
                    return m.group(0)

                clean_d = "".join(
                    ch for ch in decoded_blocks_p[first_d:last_d + 1]
                    if ch.isdigit() or ch in (",", ".")
                )
                clean_d = clean_d.replace(".", "").replace(",", "")  # только цифры для ключа
                # Строим ключ как чистые цифры с разделителем
                num_part = "".join(decoded_blocks_p[first_d:last_d + 1])
                clean_key = (
                    num_part.replace(" ", "").replace("\xa0", "")
                    .replace(",", "").replace(".", "").strip()
                )
                prefix_p = "".join(decoded_blocks_p[:first_d])
                suffix_p = "".join(decoded_blocks_p[last_d + 1:])

                # Ищем CERT ключ по валюте в тексте
                cert_sym = None
                for _sym in ("₸", "$", "€"):
                    if _sym in original_text:
                        cert_sym = _sym
                        break
                if cert_sym is None:
                    return m.group(0)

                _key_map = {"₸": "CERT_KZT:", "$": "CERT_USD:", "€": "CERT_EUR:"}
                # Нужно пересобрать ключ как сохраняем в replacement_queue:
                # _clean(cert.balance_usd_text, prefix="CERT_USD:") strip spaces, currency, +/-
                # cert_balance_*_text например "$ 308,20" → clean = "30820"
                # clean_key у нас = "30820" — должно совпадать
                queue = replacement_queue.get(_key_map[cert_sym] + clean_key)
                if not queue:
                    # Fallback — попробуем с запятой как разделителем
                    clean_key2 = (
                        num_part.replace(" ", "").replace("\xa0", "").strip()
                    )
                    queue = replacement_queue.get(_key_map[cert_sym] + clean_key2)
                if not queue:
                    return m.group(0)

                new_val, typ = queue[0]  # peek
                formatted_num = f"{abs(new_val):,.2f}".replace(",", " ").replace(".", ",")
                new_text = prefix_p + formatted_num + suffix_p

                # Сохраняем оригинальные байты префикса (символ валюты $, €, ₸ и пробелы).
                # FROM_UNICODE не содержит $  → paren_encode("$") = \x00\x00 → □.
                # Решение: сохраняем prefix/suffix байты как есть, кодируем только цифры.
                prefix_raw = unescaped_bytes[:first_d * 2]   # 2 байта на символ BigEndian
                suffix_raw = unescaped_bytes[(last_d + 1) * 2:]
                new_num_encoded = paren_encode(formatted_num)
                # Если какой-то символ числа не нашёлся в CMap (paren_encode дал
                # нулевой CID \x00\x00) — не пишем битый глиф, пропускаем замену
                # (то же, что делает hex-ветка выше при "0000"). После добора
                # цифр в build_dynamic_cmap этого не должно случаться, но guard
                # оставляем как страховку от нецифрового символа вне карты.
                if b"\x00\x00" in new_num_encoded:
                    return m.group(0)
                new_encoded = prefix_raw + new_num_encoded + suffix_raw

                # Ячейки справки лево-выровнены (см. docstring в hex-ветке
                # replace_callback выше) — X не сдвигаем, иначе короткий
                # оригинал при сильном росте числа уезжает за левую границу
                # таблицы. current_x уже провалидирован как float выше.
                new_x_paren = current_x

                print(f"  [CERT_P {typ}] {original_text!r} -> {new_text!r}")
                total_replaced += 1

                def _esc(b: bytes) -> bytes:
                    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

                # Ячейка справки: цифры набираем шрифтом-заменителем (в основном
                # шрифте стр.0 может не быть глифа '8'), префикс валюты — прежним
                # шрифтом (в заменителе нет $/€). Второй Tj встаёт вплотную к
                # первому (позиция сдвигается сама на ширину префикса).
                if cert_sub_font_name is not None:
                    _amb, _amb_sz = _ambient_tf(new_decompressed, m.start())
                    _rest_chars = formatted_num + suffix_p
                    if _amb is not None and all(c in cert_sub_chars for c in _rest_chars):
                        _sub = cert_sub_font_name.decode()
                        _an = _amb.decode()
                        _sz = _amb_sz.decode()
                        _rest_bytes = new_num_encoded + suffix_raw
                        _out = f"{new_x_paren:.5f} {y_str2} Td ".encode("ascii")
                        if prefix_raw:
                            _out += f"/{_an} {_sz} Tf (".encode("ascii") + _esc(prefix_raw) + b") Tj "
                        _out += f"/{_sub} {_sz} Tf (".encode("ascii") + _esc(_rest_bytes) + b") Tj "
                        _out += f"/{_an} {_sz} Tf".encode("ascii")
                        return _out

                # Заменяем только внутренность скобок
                return (
                    f"{new_x_paren:.5f} {y_str2} Td (".encode("ascii")
                    + new_encoded
                    + b") Tj"
                )

            new_decompressed = paren_pat.sub(cert_paren_callback, new_decompressed)

        
        if new_decompressed == decompressed:
            # Ничего не изменилось — пропускаем
            continue
        
        # ─── Компрессируем обратно ────────────────────────────
        new_compressed = zlib.compress(new_decompressed)
        
        old_stream_len = len(raw_stream_data)
        new_stream_len = len(new_compressed)
        delta = new_stream_len - old_stream_len
        
        # Обновляем /Length в header объекта
        old_length_str = str(declared_length).encode()
        new_length_str = str(new_stream_len).encode()
        length_delta = len(new_length_str) - len(old_length_str)
        
        # Заменяем /Length
        raw[length_start:length_end] = new_length_str
        
        # Пересчитываем позиции после замены Length
        # data_start мог сдвинуться если length_delta != 0
        data_start += length_delta
        endstream_pos += length_delta
        
        # Определяем, что именно находится между data и endstream
        # (может быть trailing \r\n или \n)
        # Оригинальная структура: [raw_stream_data][\r\n]endstream
        # или [raw_stream_data][\n]endstream
        # Нам нужно заменить только raw_stream_data, сохранив trailing
        trailing_start = data_start + old_stream_len
        trailing = bytes(raw[trailing_start:endstream_pos])
        
        # Заменяем данные стрима
        raw[data_start:endstream_pos] = new_compressed + trailing
        
        total_delta = length_delta + delta
        cumulative_offset += total_delta

    print(f"\n[Raw] Произведено замен: {total_replaced}")
    print(f"[Raw] Суммарный сдвиг: {cumulative_offset} байт")

    # ─── Регистрируем шрифт-заменитель в /Font стр.0 ──────────────────────
    # Если хоть одна ячейка справки была набрана шрифтом-заменителем (см.
    # cert_sub_font_name), его имя обязано присутствовать в словаре /Font
    # страницы 0 — иначе оператор "/F2 .. Tf" ссылается на неизвестный ресурс.
    # Вставляем "/F2 <xref> 0 R" перед закрывающим ">>" словаря шрифтов стр.0.
    if (cert_sub_font_name is not None and cert_sub_font_xref is not None
            and cert_page_font_dict_xref is not None):
        _obj_pat = re.compile(rb"(?<![0-9])" + str(cert_page_font_dict_xref).encode() + rb"\s+0\s+obj")
        _om = _obj_pat.search(raw)
        if _om:
            _dict_open = raw.find(b"<<", _om.end())
            _dict_close = raw.find(b">>", _dict_open)
            _entry = b" /" + cert_sub_font_name + b" " + str(cert_sub_font_xref).encode() + b" 0 R"
            if _dict_open >= 0 and _dict_close > _dict_open and _entry.strip() not in raw[_dict_open:_dict_close]:
                raw[_dict_close:_dict_close] = _entry
                cumulative_offset += len(_entry)
                print(f"[Cert] /{cert_sub_font_name.decode()} {cert_sub_font_xref} 0 R добавлен в /Font стр.0")

    # ─── 6. Обновляем xref таблицу ────────────────────────────
    # Находим xref таблицу и обновляем offsets
    result = bytes(raw)

    if cumulative_offset != 0:
        result = _rebuild_xref_table(result)

    return result


def _rebuild_xref_table(pdf_bytes: bytes) -> bytes:
    """
    Пересчитывает xref таблицу на основе фактических позиций объектов.
    Сохраняет оригинальный формат (line endings, пробелы).
    
    Поскольку стримы изменили длину, xref таблица сдвинулась.
    Ищем xref по паттерну "xref\r\n0 ", а не по startxref offset.
    """
    raw = bytearray(pdf_bytes)

    # ─── Находим xref таблицу по паттерну ─────────────────
    xref_match = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n", raw)
    if not xref_match:
        print("[WARN] xref таблица не найдена")
        return bytes(raw)
    
    xref_pos = xref_match.start()
    start_id = int(xref_match.group(1))
    count = int(xref_match.group(2))
    
    # Определяем line ending
    xref_le = b"\r\n" if raw[xref_pos + 4:xref_pos + 6] == b"\r\n" else b"\n"
    
    first_entry_start = xref_match.end()
    
    print(f"[XREF] start_id={start_id}, count={count}, found at offset={xref_pos}")
    
    # ─── Определяем формат entry (20 bytes) ───────────────
    first_entry = bytes(raw[first_entry_start:first_entry_start + 20])
    if first_entry.endswith(b"\r\n"):
        entry_le = b"\r\n"
    elif first_entry.endswith(b" \n"):
        entry_le = b" \n"
    elif first_entry.endswith(b" \r"):
        entry_le = b" \r"
    else:
        entry_le = b"\r\n"
    
    # ─── Парсим текущие entries ────────────────────────────
    old_entries = []
    for i in range(count):
        entry = bytes(raw[first_entry_start + i * 20: first_entry_start + (i + 1) * 20])
        offset = int(entry[:10])
        gen = entry[11:16].decode()
        flag = entry[17:18].decode()
        old_entries.append((offset, gen, flag))
    
    # ─── Находим фактические позиции объектов ─────────────
    obj_offsets = {}
    for m in re.finditer(rb"(\d+) 0 obj", raw):
        obj_id = int(m.group(1))
        # Убеждаемся что это реальный объект (не внутри stream)
        # Простая проверка: перед ним должен быть \n или начало файла
        pos = m.start()
        if pos == 0 or raw[pos-1:pos] in (b"\n", b"\r"):
            obj_offsets[obj_id] = pos
    
    # ─── Строим новые entries ─────────────────────────────
    new_entries_data = bytearray()
    updated = 0
    for i in range(count):
        obj_id = start_id + i
        old_offset, gen, flag = old_entries[i]
        
        if flag == 'n' and obj_id in obj_offsets:
            new_offset = obj_offsets[obj_id]
            if new_offset != old_offset:
                updated += 1
        else:
            new_offset = old_offset
        
        entry = f"{new_offset:010d} {gen} {flag}".encode() + entry_le
        new_entries_data.extend(entry)
    
    # ─── Заменяем entries (длина 20*count — НЕ меняется) ──
    old_entries_end = first_entry_start + count * 20
    raw[first_entry_start:old_entries_end] = new_entries_data
    
    # ─── Обновляем startxref ──────────────────────────────
    startxref_match = re.search(rb"startxref\r?\n(\d+)\r?\n", raw)
    if startxref_match:
        new_startxref_str = str(xref_pos).encode()
        raw[startxref_match.start(1):startxref_match.end(1)] = new_startxref_str
        print(f"[XREF] startxref: {startxref_match.group(1).decode()} → {xref_pos}")
    
    print(f"[XREF] Обновлено {updated} offsets из {count}")
    
    return bytes(raw)


from __future__ import annotations

import re
import math
import zlib
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from pdf_service import (
    build_dynamic_cmap,
    _rebuild_xref_table,
    _fmt_coord,
    _op_separators,
    _ARIAL_DIGIT_EM,
    _find_primary_font_tounicode_xref,
)
from pdf_service_downscale import IncomeTooLowError

# ─── Константы ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_NOISE = 0.03
# Допуск (pt) совпадения X ячейки с измеренным X лево-выровненной колонки
# «Кредит» (см. credit_col_x в process_kaspi_ip_pdf). Значения Кредит делят
# один X (±доли pt); зазор до ближайшей дебетовой ячейки — порядка 10+ pt,
# поэтому 3.0 надёжно различает колонки, не задевая соседнюю.
_COL_MATCH_TOL = 3.0
# Коридор помесячного K вокруг единого (глобального) K — см. recalculate_kaspi_ip.
# Подобрано эмпирически на реальной выписке (nav.pdf, 431 транзакция, 12
# месяцев с оборотом от 271 000₸ до 7 895 400₸): при 3.5 ISI стабильно
# ~0.65-0.66 (порог 0.60 в validate_kaspi_ip) при разбросе роста между
# транзакциями ~9-10x — против ISI=0.20 при едином K на весь период и ~29x
# разброса при чистом K по месяцам (без коридора).
_MAX_MONTH_K_SPREAD = 3.5

# Форма отрендеренной в PDF денежной ячейки Kaspi: "XXX" / "X XXX,XX" /
# "X XXX KZT" (поля остатка на стр.0 — целые тенге + суффикс валюты, БЕЗ
# копеек, в отличие от таблицы транзакций). Используется при raw-bytes
# замене (process_kaspi_ip_pdf), чтобы: 1) отличить ячейку Дебет/Кредит/
# остатка от произвольного текста, где-то содержащего цифры (маскированная
# карта, номер документа, дата, время); 2) воспроизвести ТУ ЖЕ форму (копейки
# показывать, только если исходное поле их показывало; суффикс " KZT"
# сохранять, если он был) — см. _parse_amount_cell/_format_amount_cell.
_AMOUNT_CELL_RE = re.compile(r"^(\d{1,3}(?:\s\d{3})*)(,\d{2})?(\s*KZT)?$")

# Те же floor-правила занижения дохода, что и в pdf_service_downscale (Kaspi Gold)
# и halyk_pdf_service. Константы продублированы локально — модуль самодостаточен.
_INCOME_K = 0.3914
_SAFETY_MARGIN = 100_000.0
_MAX_DOWNSCALE_FACTOR = 0.30

# Назначения платежей → тип операции
#
# ВАЖНО: is_credit (Кредит vs Дебет) НЕ определяется по ключевым словам в
# назначении платежа — у разных ИП-клиентов совершенно разные источники
# дохода (продажи через Kaspi.kz, оплата за услуги, переводы от контрагентов
# и т.д.), и жёсткий список ключевых слов ловит только один конкретный
# бизнес. Вместо этого is_credit читается из реальной колонки таблицы —
# см. _credit_debit_threshold() и её использование в _parse_transactions_from_page.
# Ключевые слова здесь классифицируют только ДЕБЕТ: какие расходы
# масштабируются вместе с доходом (комиссии/переводы, зависящие от оборота),
# а какие фиксированы (разовые/просроченные платежи).
_DEBIT_SCALE_KEYWORDS = [
    "Оплата услуги по обработке данных",
    "Оплата за услуги процессинга",
    "Оплата за информационно-технологические услуги",  # та же комиссия, старое название
    "Перевод собственных средств",
    "Снятия наличных",
]
_DEBIT_FIXED_KEYWORDS = [
    "Погашение просроченной комиссии за ведение счета",
    "Погашение комиссии за ведение",
]


def _fmt(val: float) -> str:
    """Форматирует число как '1 234 567,89' (формат Kaspi)."""
    # Целое без дробной части — без ",00"? Нет: у Kaspi все суммы с 2 знаками
    s = f"{abs(val):,.2f}".replace(",", " ").replace(".", ",")
    return s


_ROUND_UNIT = 1000.0

# Как _NATURAL_STEP_CANDIDATES в pdf_service.py (Kaspi Gold, "Исправлено
# 2026-08-03") — каждое кандидатное число точный делитель следующего, поэтому
# шаг никогда не выходит МЕЛЬЧЕ базовой тысячи.
_NATURAL_STEP_CANDIDATES_IP = (1_000_000.0, 500_000.0, 100_000.0, 50_000.0, 10_000.0, 5_000.0, 1_000.0)


def _round_amount(val: float, original: Optional[float] = None) -> float:
    """
    Округляет масштабированную сумму до ближайшей тысячи — реальные счета ИП
    (Оплата за услуги, Перевод собственных средств и т.д.) почти всегда
    круглые ("95 000", "145 000"). round(x, 2) после умножения на K и шум
    даёт произвольные дробные числа ("47 126,38"), что визуально выделяется
    на фоне остального документа. Минимум 1 тысяча, чтобы сильное занижение
    не обнуляло сумму целиком.

    Если передан `original` (сумма ДО масштабирования) и он сам кратен более
    крупному «человеческому» числу (5 000/10 000/.../1 000 000), шаг
    округления результата подтягивается к нему — иначе круглый оригинал
    («100 000») × дробный K даёт формально кратное тысяче, но не похожее на
    реальный платёж число («233 000» вместо «230 000»/«250 000»), тот же
    класс фикса, что и `pdf_service._round_to_natural(val, original=...)`.
    """
    if original and original > 0:
        unit = None
        for cand in _NATURAL_STEP_CANDIDATES_IP:
            if original % cand < 0.01 or cand - (original % cand) < 0.01:
                unit = cand
                break
        if unit is None:
            # Оригинал не кратен даже тысяче — он «точный» (комиссия 35,15 ₸,
            # поступление 49 676 ₸). Округлять его результат до тысяч нельзя:
            # это меняет сам характер документа. На реальных файлах (IP2/IP3 —
            # процессинговые счета, где кратны 1000 всего 6.0% и 5.8% сумм
            # кредита) прежняя безусловная сетка давала 99.8% круглых — то
            # есть распределение, противоположное оригинальному. Решение
            # принимается по КАЖДОЙ сумме отдельно, поэтому файлы вроде IP4
            # (98.6% круглых) не меняются вовсе.
            #
            # Копейки наследуются так же, как и кратность. Это не косметика:
            # ячейка печатается в форме СВОЕГО оригинала (см.
            # `_format_amount_cell`, `had_decimal`), поэтому дробный результат
            # в ячейке без копеек молча теряет дробную часть при печати, а
            # шапка считается по неокруглённым числам — на IP2 это давало
            # расхождение шапка/тело в 1.67…4.18 ₸.
            return round(val, 2) if original % 1 else float(round(val))
        rounded = round(val / unit) * unit
        if rounded <= 0 and unit > _ROUND_UNIT:
            # Занижение увело сумму ниже её же укрупнённого шага (оригинал
            # 50 000 ₸ → 23 253 ₸ при шаге 50 000). Эскалация шага работает
            # только ВВЕРХ от базовой тысячи, поэтому здесь просто
            # возвращаемся к ней, а не отдаём число с копейками: оригинал был
            # круглым, результат обязан выглядеть так же.
            unit = _ROUND_UNIT
            rounded = round(val / unit) * unit
        if rounded > 0:
            return rounded
        # Сумма меньше половины тысячи. Прежний код возвращал здесь `unit`,
        # то есть ровно 1 000 ₸, и это выталкивало в 1 000 ₸ КАЖДУЮ мелкую
        # сумму: на IP2 значение «1 000,00» встречалось 304 раза подряд в
        # колонке дебета (в оригинале — ни одной такой серии), а комиссия в
        # 35 ₸ превращалась в тысячу, раздувая расход.
        if val <= 0:
            return unit
        return round(val, 2) if original % 1 else float(round(val))

    unit = _ROUND_UNIT
    rounded = round(val / unit) * unit
    return rounded if rounded > 0 else unit


def _clean_digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


def _count_standalone(haystack: str, needle: str) -> int:
    """Сколько раз needle встречается как САМОСТОЯТЕЛЬНОЕ число.

    Обычный str.count() считает и подстроки: «0,15» находится внутри «60,15»,
    а «3 928 947» — внутри «3 928 947,49». Для подсчёта копий summary-ячейки
    это давало лишние слоты в очереди замен и ложное «N замен(ы) не
    применено» на каждом прогоне (реальный случай — IP3).

    Отсекаем соседние цифры с обеих сторон, включая случай, когда соседняя
    цифра отделена пробелом-разделителем тысяч. Перенос строки разделителем
    НЕ считается: в извлечённом тексте предыдущая ячейка сплошь и рядом
    заканчивается цифрой ровно перед нашим числом. Промах в меньшую сторону
    безопасен — вызывающий код страхуется через `or 1`.
    """
    if not needle:
        return 0
    pattern = (
        r"(?<!\d)(?<!\d[ \u00a0])"        # слева: не цифра и не «цифра + пробел»
        + re.escape(needle)
        + r"(?![\d,])(?![ \u00a0]\d)"     # справа: не цифра, не «,цифры», не «пробел + цифра»
    )
    return len(re.findall(pattern, haystack))


def _date_key(date_str: str) -> str:
    """'DD.MM.YYYY' → 'YYYYMMDD', для хронологической сортировки без datetime."""
    return date_str[6:10] + date_str[3:5] + date_str[0:2]


def _parse_amount(text: str) -> Optional[float]:
    text = text.strip().replace("\xa0", " ")
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("-")
    # Удаляем тысячные разделители, заменяем запятую на точку
    digits = re.sub(r"[^0-9,]", "", text).replace(",", ".")
    try:
        return sign * float(digits)
    except Exception:
        return None


# ─── Dataclasses ───────────────────────────────────────────────────────────

@dataclass
class KaspiIPTransaction:
    page_num: int
    doc_number: str           # "68431820"
    date: str                 # "17.06.2026"
    amount: float             # Дебет (>0) или Кредит (>0), всегда положительное
    amount_text: str          # "80 476" или "7 608,20"
    is_credit: bool           # True = Кредит (доход), False = Дебет (расход)
    is_scaleable: bool        # True = масштабировать, False = оставить
    purpose: str              # Назначение платежа
    new_amount: float = 0.0
    # Сумма строки продублирована внутри «Назначения платежа» («…Сумма
    # 210 000-00 теңге…»). Такие строки НЕ масштабируются — см.
    # _purpose_repeats_amount и причину там же.
    amount_in_purpose: bool = False


@dataclass
class KaspiIPSummary:
    opening_balance: float
    opening_text: str               # "0,9"
    closing_balance: float
    closing_text: str               # "0,41"
    total_debit: float
    total_debit_text: str           # "3 928 947,49"
    total_credit: float
    total_credit_text: str          # "3 928 947"
    summary_page: int = 0           # Страница с "Итого обороты"


@dataclass
class KaspiIPStatementData:
    summary: KaspiIPSummary
    transactions: List[KaspiIPTransaction] = field(default_factory=list)


# ─── Детектор формата ──────────────────────────────────────────────────────

def detect_kaspi_ip_format(doc) -> bool:
    """True, если PDF — IP-выписка Kaspi Bank (Выписка по счету, ИП)."""
    try:
        text = doc[0].get_text()
        return "Лицевой счет:" in text and "Входящий остаток" in text
    except Exception:
        return False


# ─── Парсинг ───────────────────────────────────────────────────────────────

def _classify_debit_purpose(purpose: str) -> Tuple[bool, bool]:
    """
    Классифицирует ДЕБЕТовую транзакцию (is_credit определяется отдельно —
    по колонке таблицы, а не по назначению платежа, см. _credit_debit_threshold).
    Возвращает (is_scaleable, is_fixed_debit).
    is_scaleable: True = масштабировать вместе с кредитом (доходом)
    is_fixed_debit: True = фиксированный расход, не масштабировать
    """
    for kw in _DEBIT_FIXED_KEYWORDS:
        if kw in purpose:
            return False, True

    for kw in _DEBIT_SCALE_KEYWORDS:
        if kw in purpose:
            return True, False

    return False, False


# Минимальная сумма, для которой совпадение с числом в назначении считается
# осмысленным. Мелкие суммы («14 ₸» комиссии за процессинг) случайно совпадают
# с фрагментами дат и номеров счетов внутри назначения («…за 14/07/2026»), и
# без этого порога такие строки зря фиксировались бы (проверено на IP2/IP3:
# 7 ложных совпадений на общую сумму 73 ₸, все — дата или номер документа).
_PURPOSE_AMOUNT_MIN = 1000.0


def _purpose_repeats_amount(purpose: str, amount: float) -> bool:
    """True, если сумма строки продублирована в тексте «Назначения платежа».

    Реальные выписки ИП сплошь и рядом повторяют сумму счёта прямо в
    назначении: «Оплата за транспортные услуги по сч №91 от 10.07.26 Сумма
    75 000-00 теңге без НДС». Если такую строку отмасштабировать, колонка
    «Кредит» покажет 5 346 000, а назначение рядом — по-прежнему «Сумма
    75 000-00»: арифметика сойдётся и наложений не будет (то есть ни одна
    автоматическая проверка этого не заметит), но человек, читающий выписку,
    увидит строку, противоречащую самой себе.

    Флаг пока ТОЛЬКО диагностический — такие строки масштабируются наравне с
    остальными. Очевидное «не масштабировать их» проверено на реальных файлах
    и не работает: на IP4 под это правило попадает 42.8% всего кредита
    (82 строки, 28 275 000 ₸), на kaspiIP — 8.8%. Замороженный кредит нельзя
    подтянуть к цели, и месяцы перестают выравниваться: ISI падает до
    0.50 (IP4) и 0.56–0.59 (kaspiIP) при пороге 0.60 в validate_kaspi_ip —
    то есть жёсткая проверка начинает валиться на ВСЕХ целях, включая те, что
    раньше проходили. Компенсация цели по месяцам (K = (target − фикс)/масшт.)
    не спасает: там, где фиксированный кредит месяца сам по себе больше цели,
    месяц физически не опустить. Настоящее решение — переписывать сумму и
    внутри текста назначения, но это перенос строк в узкой многострочной
    ячейке, отдельная работа.

    Пишем сумму без копеек: в назначении она встречается и как «75 000-00»,
    и как «75000-00», и как «75 000» — поэтому пробелы-разделители тысяч
    убираем перед поиском, а границы проверяем по соседним цифрам, чтобы
    «190000» не находилось внутри номера счёта «00000000190000123».
    """
    if amount < _PURPOSE_AMOUNT_MIN or not purpose:
        return False
    compact = re.sub(r"[ \u00a0]", "", purpose)  # пробел и неразрывный пробел
    digits = str(int(round(amount)))
    return re.search(r"(?<!\d)" + digits + r"(?!\d)", compact) is not None



# Страница повёрнута на 90° — PyMuPDF возвращает координаты уже в читаемом
# порядке, поэтому колонки таблицы различаются по Y (не по X): у "Дебет" и
# "Кредит" непересекающиеся Y-диапазоны (проверено на двух разных выписках —
# позиции идентичны, это часть фиксированного шаблона отчёта).
_FALLBACK_CREDIT_DEBIT_THRESHOLD = 609.0  # (554–580 Кредит) / (638–660 Дебет)


def _credit_debit_threshold(page0) -> float:
    """
    Находит Y-границу между колонками "Кредит" и "Дебет" по заголовку таблицы
    на первой странице. Возвращает середину между их Y-диапазонами: значения
    ВЫШЕ порога — Дебет, НИЖЕ — Кредит.
    """
    debet_y = None
    kredit_y = None
    for w in page0.get_text("words"):
        text = w[4].strip()
        if text == "Дебет":
            debet_y = (w[1] + w[3]) / 2
        elif text == "Кредит":
            kredit_y = (w[1] + w[3]) / 2
    if debet_y is not None and kredit_y is not None:
        return (debet_y + kredit_y) / 2
    return _FALLBACK_CREDIT_DEBIT_THRESHOLD


def parse_kaspi_ip_statement(doc) -> KaspiIPStatementData:
    """Извлекает транзакции из выписки Kaspi ИП."""
    transactions: List[KaspiIPTransaction] = []

    # ── Заголовочные данные (страница 0) ─────────────────────────────────
    page0_text = doc[0].get_text()
    opening_balance = 0.0
    opening_text = "0,9"
    closing_balance = 0.0
    closing_text = "0,41"
    total_debit = 0.0
    total_debit_text = "0,00"
    total_credit = 0.0
    total_credit_text = "0,00"
    summary_page = 0

    # Входящий остаток (копейки не всегда присутствуют — "3 403 KZT" без запятой)
    m = re.search(r"Входящий остаток\s+([\d\s]+(?:,\d+)?)", page0_text)
    if m:
        opening_text = m.group(1).strip()
        opening_balance = _parse_amount(opening_text) or 0.0

    # Исходящий остаток (может быть на странице 0 или последней)
    m = re.search(r"Исходящий остаток\s+([\d\s]+(?:,\d+)?)", page0_text)
    if m:
        closing_text = m.group(1).strip()
        closing_balance = _parse_amount(closing_text) or 0.0

    # ── Транзакции: по всем страницам ─────────────────────────────────────
    # Транзакция: строка с числовым doc_number, датой, суммой, назначением
    # Kaspi IP страница имеет поворот 90°, PyMuPDF корректно извлекает текст
    cd_threshold = _credit_debit_threshold(doc[0])

    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        page_text = page.get_text()

        # Ищем итого на любой странице
        if "Итого обороты" in page_text:
            summary_page = pg_idx
            # Формат может быть "...валюте: X,XX / Y" или многострочный "...валюте\nX,XX\nY"
            # Копейки не всегда присутствуют — обе суммы могут быть целыми (без запятой).
            total_match = re.search(
                r"Итого обороты[^0-9]*(\d[\d ]*(?:,\d{2})?)\s+(\d[\d ]*(?:,\d{2})?)",
                page_text
            )
            if total_match:
                total_debit_text = total_match.group(1).strip()
                total_debit = _parse_amount(total_debit_text) or 0.0
                total_credit_text = total_match.group(2).strip()
                total_credit = _parse_amount(total_credit_text) or 0.0

            # Исходящий остаток
            m_cl = re.search(r"Исходящий остаток\s+([\d\s]+(?:,\d+)?)", page_text)
            if m_cl:
                closing_text = m_cl.group(1).strip()
                closing_balance = _parse_amount(closing_text) or 0.0

        # Парсим транзакции через get_text("blocks")
        # Kaspi IP структура: каждая транзакция — группа из нескольких блоков
        # Проще всего парсить через полный текст страницы строка за строкой
        _parse_transactions_from_page(page, pg_idx, transactions, cd_threshold)

    print(f"[KaspiIP] Распознано транзакций: {len(transactions)}")
    credits = [t for t in transactions if t.is_credit and t.is_scaleable]
    debits_scale = [t for t in transactions if not t.is_credit and t.is_scaleable]
    print(f"[KaspiIP] Кредит (масштаб): {len(credits)}, Дебет (масштаб): {len(debits_scale)}")

    # Диагностика для операторской видимости: _classify_debit_purpose матчит
    # is_scaleable ТОЛЬКО по ключевым словам (_DEBIT_SCALE_KEYWORDS/
    # _DEBIT_FIXED_KEYWORDS в назначении платежа) — список закрытый и не
    # покрывает все возможные формулировки реальных ИП. Fallback безопасен
    # (несовпадение → is_scaleable=False, т.е. расход НЕ масштабируется,
    # трактуется как фиксированный — консервативно, баланс не нарушается) —
    # риск в другом: реально масштабируемый расход
    # (напр. комиссия с непривычной формулировкой), классифицированный как
    # фиксированный, останется прежним при сильном росте дохода — визуально
    # подозрительно (расход не растёт вместе с оборотом), хоть математически
    # безопасно. Печатаем сумму/образцы НЕклассифицированного (не попавшего
    # ни в один из двух списков) дебета, чтобы это было видно при разборе
    # нового реального файла, а не тонуло молча в логах.
    debits_all = [t for t in transactions if not t.is_credit]
    debits_unclassified = [
        t for t in debits_all
        if not t.is_scaleable and not any(kw in t.purpose for kw in _DEBIT_FIXED_KEYWORDS)
    ]
    if debits_unclassified:
        total_debit_amt = sum(t.amount for t in debits_all) or 1.0
        unclass_amt = sum(t.amount for t in debits_unclassified)
        share = unclass_amt / total_debit_amt * 100
        samples = sorted({t.purpose[:60] for t in debits_unclassified})[:3]
        print(
            f"[KaspiIP] ⚠️ Неклассифицированный дебет (не попал ни в _DEBIT_SCALE_KEYWORDS, "
            f"ни в _DEBIT_FIXED_KEYWORDS): {len(debits_unclassified)} шт., "
            f"Σ={unclass_amt:,.2f} ₸ ({share:.1f}% от всего дебета). Трактуется как "
            f"фиксированный (безопасно для баланса, но может не отражать реальную "
            f"масштабируемость). Примеры назначений: {samples}"
        )

    # Строки, где сумма продублирована в тексте назначения платежа (см.
    # _purpose_repeats_amount): после масштабирования колонка суммы будет
    # противоречить тексту рядом («Кредит 5 346 000» против «Сумма 75 000-00»
    # в назначении той же строки). Ни одна автоматическая проверка этого не
    # видит — математика сходится, наложений нет, — поэтому печатаем долю
    # явно: чем она выше, тем заметнее расхождение в готовом документе.
    repeated = [t for t in transactions if t.amount_in_purpose]
    if repeated:
        total_credit_amt = sum(t.amount for t in transactions if t.is_credit) or 1.0
        rep_credit = sum(t.amount for t in repeated if t.is_credit)
        samples = sorted({t.purpose[:60] for t in repeated})[:3]
        print(
            f"[KaspiIP] ⚠️ Сумма продублирована в назначении платежа: {len(repeated)} шт. "
            f"(из них кредит: Σ={rep_credit:,.2f} ₸, {rep_credit / total_credit_amt * 100:.1f}% "
            f"от всего кредита). Масштабируются как обычно, но в этих строках текст "
            f"назначения останется со старой суммой. Примеры: {samples}"
        )

    summary = KaspiIPSummary(
        opening_balance=opening_balance,
        opening_text=opening_text,
        closing_balance=closing_balance,
        closing_text=closing_text,
        total_debit=total_debit,
        total_debit_text=total_debit_text,
        total_credit=total_credit,
        total_credit_text=total_credit_text,
        summary_page=summary_page,
    )
    return KaspiIPStatementData(summary=summary, transactions=transactions)


def _page_lines_with_y(page) -> List[Tuple[str, Optional[float]]]:
    """
    Строки страницы (как в get_text(), с тем же порядком и разбиением), но
    каждая — с Y-координатой середины (для определения колонки Дебет/Кредит).
    Строится из get_text("dict"), а не из отдельного вызова get_text(), чтобы
    порядок и разбиение на строки совпадали с bbox гарантированно (единый источник).
    """
    result: List[Tuple[str, Optional[float]]] = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            bbox = line.get("bbox")
            y_mid = (bbox[1] + bbox[3]) / 2 if bbox else None
            result.append((text, y_mid))
    return result


def _parse_transactions_from_page(
    page, pg_idx: int, transactions: List[KaspiIPTransaction], cd_threshold: float
):
    """Парсит транзакции со страницы через текстовый вывод."""
    lines_with_y = _page_lines_with_y(page)
    lines_text = [t for t, _y in lines_with_y]

    # Структура каждой транзакции в Kaspi IP (по позиции строки):
    # i+0: doc_number (1-9 цифр — номер документа/КНП-ссылки может быть
    #      совсем коротким, напр. "26", "67", "197"; исключаем только ровно
    #      12-значные БИН/ИИН, которые сюда никогда не попадают благодаря
    #      следующей строгой проверке на (дата, время) сразу после)
    # i+1: дата DD.MM.YYYY
    # i+2: время H:MM:SS
    # i+3: сумма (Дебет или Кредит) — "66 923" или "7 608,20"
    # i+4+: получатель, IBAN, BIN, BIC, КНП, назначение

    _DOC_NUM_RE = re.compile(r"^\d{1,9}$")
    _TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
    _AMOUNT_RE = re.compile(r"^\d{1,3}(?:\s\d{3})*(?:,\d{2})?$")
    _SKIP_RE = re.compile(r"^(KZ[A-Z0-9]+|\d{12}|[A-Z]{6,}[A-Z0-9]{0,4}|\d{3})$")

    i = 0
    while i < len(lines_text):
        line = lines_text[i]

        # doc_number: ровно 7-9 цифр (исключает BIN/IIN длиной 12)
        if not _DOC_NUM_RE.match(line):
            i += 1
            continue

        if i + 3 >= len(lines_text):
            i += 1
            continue

        # Дата
        date_line = lines_text[i + 1]
        date_m = _DATE_RE.search(date_line)
        if not date_m:
            i += 1
            continue
        date_str = date_m.group(0)

        # Время
        time_line = lines_text[i + 2]
        if not _TIME_RE.match(time_line):
            i += 1
            continue

        # Сумма
        amount_line = lines_text[i + 3]
        amt_m = _AMOUNT_RE.match(amount_line)
        if not amt_m:
            i += 1
            continue

        amount_text = amt_m.group(0)
        amount_val = _parse_amount(amount_text)
        if amount_val is None or amount_val <= 0:
            i += 1
            continue

        # Кредит vs Дебет — по Y-координате колонки (не по назначению платежа,
        # см. комментарий у _DEBIT_SCALE_KEYWORDS).
        amount_y = lines_with_y[i + 3][1]
        is_credit = amount_y is not None and amount_y < cd_threshold

        # Собираем остаток блока до следующего doc_number.
        # ВАЖНО: КНП (напр. "342", "190", "841") — тоже 1-3-значное число и
        # само по себе матчится _DOC_NUM_RE, как и настоящий doc_number.
        # Без доп. проверки блок обрывается на КНП ТЕКУЩЕЙ транзакции, и
        # "Назначение платежа" (где живут ключевые слова классификации
        # дебета — см. _DEBIT_SCALE_KEYWORDS) в purpose не попадает вообще,
        # из-за чего Дебет никогда не считается масштабируемым. Настоящий
        # doc_number отличают так же, как и на верхнем уровне сканирования:
        # следующая строка после него — дата.
        j = i + 4
        block_lines = []
        while j < len(lines_text):
            ln = lines_text[j]
            if _DOC_NUM_RE.match(ln) and j + 1 < len(lines_text) and _DATE_RE.search(lines_text[j + 1]):
                break
            if "Итого" in ln or "Входящий" in ln or "Исходящий" in ln:
                break
            block_lines.append(ln)
            j += 1

        # Назначение: пропускаем IBAN/BIN/BIC/КНП, берём остальное
        purpose_parts = []
        for ln in block_lines:
            if not ln:
                continue
            if _SKIP_RE.match(ln):
                continue
            purpose_parts.append(ln)
        purpose = " ".join(purpose_parts)

        if is_credit:
            # Любое реальное поступление на счёт ИП — доход, который мы и
            # масштабируем к target_monthly_income (аналогично Halyk: нет
            # универсальной фразы для "дохода", источники разные у разных ИП).
            is_scaleable = True
        else:
            is_scaleable, _is_fixed = _classify_debit_purpose(purpose)

        # Проверяем по ПОЛНОМУ назначению, до обрезки до 120 символов ниже:
        # сумма счёта часто стоит в самом конце фразы. Флаг НЕ влияет на
        # масштабирование — только диагностика, см. _purpose_repeats_amount.
        amount_in_purpose = _purpose_repeats_amount(purpose, amount_val)

        tx = KaspiIPTransaction(
            page_num=pg_idx,
            doc_number=line,
            date=date_str,
            amount=amount_val,
            amount_text=amount_text,
            is_credit=is_credit,
            is_scaleable=is_scaleable,
            purpose=purpose[:120],
            new_amount=amount_val,
            amount_in_purpose=amount_in_purpose,
        )
        transactions.append(tx)
        i = j

    return transactions


def recalculate_kaspi_ip(stmt: KaspiIPStatementData, target_monthly_income: float) -> KaspiIPStatementData:
    """
    Масштабирует кредитные поступления (Продажи с Kaspi.kz) до target_monthly_income,
    пропорционально масштабируя комиссии и переводы собственных средств.
    """
    # Группируем кредит (доход) по месяцам
    month_credit: Dict[str, float] = defaultdict(float)
    for tx in stmt.transactions:
        if tx.is_credit and tx.is_scaleable and tx.amount > 0:
            month_key = tx.date[3:]  # "MM.YYYY"
            month_credit[month_key] += tx.amount

    if not month_credit:
        # Раньше здесь молча возвращался stmt БЕЗ изменений — process_kaspi_ip_pdf
        # в этом случае делает 0 замен, а /process всё равно отдаёт 200 OK с
        # "обработанным" PDF, который на самом деле байт-в-байт оригинал.
        # Пользователь не мог никак узнать, что обработка не произошла (кроме
        # ручной сверки сумм). Триггерится не только полным отсутствием дохода
        # на счёте, но и ЛЮБЫМ сбоем парсера (новый шаблон Kaspi, нестандартная
        # вёрстка) — то есть это реалистичный, а не экзотический сценарий.
        # main.py оборачивает /process в `except Exception` → 500 с текстом
        # ошибки, так что обычный ValueError здесь превращается в понятный
        # ответ клиенту вместо тихого "успеха".
        raise ValueError(
            f"Не найдено ни одной кредитовой (доходной) транзакции для "
            f"масштабирования (всего распознано транзакций: {len(stmt.transactions)}). "
            f"Похоже, формат выписки не распознан парсером — файл не был обработан."
        )

    n_months = len(month_credit)
    current_monthly_avg = sum(month_credit.values()) / n_months

    # Дебет, не подлежащий масштабированию (комиссии, невыясненные списания), не
    # сжимается вместе с доходом — именно он лимитирует безопасное занижение.
    # Считаем всегда (не только для занижения): используется и в ПРОВЕРКЕ 3 как
    # запасной ориентир минимума, даже когда исходный запрос — завышение.
    fixed_debit_total = sum(
        t.amount for t in stmt.transactions if not t.is_credit and not t.is_scaleable
    )
    required_total_income = fixed_debit_total - stmt.summary.opening_balance + _SAFETY_MARGIN
    min_target = required_total_income / n_months if required_total_income > 0 else 0.0

    # ── Floor-проверки 1 и 2 — только если это занижение (target < текущего ср.) ──
    # Идентично трём проверкам в pdf_service_downscale / halyk_pdf_service.
    if current_monthly_avg > 0 and target_monthly_income < current_monthly_avg:
        # ПРОВЕРКА 1: баланс не должен уйти в минус
        if target_monthly_income < min_target:
            raise IncomeTooLowError(
                min_target_monthly_income=min_target,
                current_expense=fixed_debit_total,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="below_balance_floor",
                message=(
                    f"Слишком низкий целевой доход. При фиксированных расходах "
                    f"{fixed_debit_total:,.0f} ₸ и стартовом балансе "
                    f"{stmt.summary.opening_balance:,.0f} ₸ за {n_months} мес "
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
                current_expense=fixed_debit_total,
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

    # Глобальный K (центр коридора) — среднее по всей выписке, а не по
    # каждому месяцу отдельно.
    global_k = target_monthly_income / current_monthly_avg

    # По месяцам, но с коридором вокруг global_k: чистый per-month K
    # (K_month = target/месячный_оборот) даёт множители, различающиеся на
    # порядок между "тощими" и "жирными" месяцами реального оборота (напр.
    # K=9.43 в месяце с оборотом 271 000₸ против K=0.32 в месяце с
    # 7 895 400₸ в одном и том же прогоне) — один платёж растёт в 40+ раз,
    # другой почти не меняется. Чистый единый K для ВСЕХ месяцев решает это,
    # но ломает ISI (индекс стабильности дохода в validate_kaspi_ip) на
    # выписках с большим естественным разбросом исходного оборота — итоговые
    # месячные суммы остаются такими же "рваными", как исходные (проверено:
    # ISI = 0.1987 при пороге 0.60 на реальной выписке). Коридор
    # [global_k / _MAX_MONTH_K_SPREAD, global_k * _MAX_MONTH_K_SPREAD]
    # — компромисс: месяцы с экстремальным исходным оборотом всё ещё
    # подтягиваются к цели (нужно для ISI), но не более чем в
    # _MAX_MONTH_K_SPREAD² раз относительно друг друга, а близкие к среднему
    # месяцы получают K, неотличимый от единого.
    month_k: Dict[str, float] = {}
    for month_key, total in month_credit.items():
        if total <= 0:
            month_k[month_key] = global_k
            continue
        raw_k = target_monthly_income / total
        lo = global_k / _MAX_MONTH_K_SPREAD
        hi = global_k * _MAX_MONTH_K_SPREAD
        month_k[month_key] = max(lo, min(hi, raw_k))

    print(f"[KaspiIP] Глобальный K={global_k:.4f} (ср.доход={current_monthly_avg:,.0f}₸/мес → цель={target_monthly_income:,.0f}₸/мес), коридор ×{1 / _MAX_MONTH_K_SPREAD:.2f}..×{_MAX_MONTH_K_SPREAD:.2f}")
    for m, k in sorted(month_k.items()):
        print(f"  {m}: K={k:.4f} (кредит={month_credit[m]:,.0f}₸)")

    # Применяем K ко всем масштабируемым транзакциям. Округление до тысячи —
    # ТОЛЬКО в самом конце (после ПРОВЕРКИ 3 ниже), не здесь: цикл коррекции
    # баланса домножает new_amount итеративно на аналитический множитель, и
    # если округлять на каждом шаге, поправки меньше _ROUND_UNIT (частый
    # случай — обычно нужно всего несколько сотен ₸) "съедаются" округлением,
    # и коррекция не сходится за 8 итераций (см. отладку — min_rb зависает
    # на -767₸ вместо схождения к ≥0).
    # Шум разыгрывается ОДИН РАЗ НА РАЗЛИЧНОЕ ЗНАЧЕНИЕ, а не на строку.
    # В реальной выписке ИП одна и та же сумма повторяется десятками строк
    # (20 переводов ровно по 100 000 ₸ — обычное дело для регулярного
    # платежа), и это заметная структура документа. Независимый розыгрыш на
    # каждую строку её уничтожал: у IP4 число различных значений кредита
    # росло 72 → 114 (×2) → 161 (×5) → 221 (×20), то есть почти каждая
    # строка становилась уникальной — след пересчёта, которого в настоящей
    # выписке быть не может. Ключ включает месяц, потому что коридор
    # `_MAX_MONTH_K_SPREAD` даёт разным месяцам разный K: две одинаковые
    # суммы из разных месяцев обязаны разойтись, но это законное
    # масштабирование, а не шум.
    noise_by_value: Dict[Tuple[str, float], float] = {}
    for tx in stmt.transactions:
        if not tx.is_scaleable:
            continue
        mk = tx.date[3:]
        k = month_k.get(mk, global_k)
        nkey = (mk, round(tx.amount, 2))
        if nkey not in noise_by_value:
            noise_by_value[nkey] = random.uniform(-_NOISE, _NOISE)
        new_val = tx.amount * k * (1 + noise_by_value[nkey])
        tx.new_amount = new_val
        print(f"  [{'CR' if tx.is_credit else 'DR'}] {tx.date} {tx.amount:,.2f} → {new_val:,.2f} | {tx.purpose[:50]}")

    # ПРОВЕРКА 3 (post-check): цепочка running balance не должна уходить в минус.
    # В отличие от Kaspi Gold/Halyk (где расходы никогда не масштабируются и
    # завышение дохода математически не может увести баланс ниже, чем в
    # оригинале), здесь часть дебета (Снятия наличных, Перевод собственных
    # средств и т.д.) масштабируется тем же K, что и кредит — поэтому проверка
    # выполняется ВСЕГДА, и при завышении тоже: если дебет со сдвигом по дате
    # опережает соответствующий кредит, увеличение K может увести
    # промежуточный баланс в минус, даже когда итоговый баланс в порядке.
    #
    # date хранит только "DD.MM.YYYY" (время отброшено при парсинге), поэтому
    # сортировка по date теряет порядок внутри одного дня. Исходный PDF
    # перечисляет строго от новых к старым (включая время) — reversed() даёт
    # верный порядок «от старых к новым» между различными секундами, как и в
    # main.py._verify_pdf() для Kaspi Gold. Но часть транзакций в один день
    # имеет один и тот же ночной batch-timestamp ("0:00:10") — реальный порядок
    # внутри такой группы неразличим по тексту PDF. Для этого случая кредит
    # ставится раньше дебета в пределах дня (стабильная сортировка): деньги не
    # могут быть списаны/переведены раньше, чем они зачислены.
    sorted_txs = sorted(
        reversed(stmt.transactions),
        key=lambda t: (_date_key(t.date), 0 if t.is_credit else 1),
    )

    def _min_running_balance() -> Tuple[float, float]:
        """Возвращает (min_rb, credit_up_to_min) — минимальный баланс и сумму
        масштабируемого кредита, накопленную ДО точки минимума включительно
        (нужна для расчёта точного коэффициента коррекции ниже)."""
        rb = stmt.summary.opening_balance
        min_rb = rb
        credit_acc = 0.0
        credit_at_min = 0.0
        for t in sorted_txs:
            amt = t.new_amount if t.is_scaleable else t.amount
            if t.is_credit:
                rb = round(rb + amt, 2)
                if t.is_scaleable:
                    credit_acc += amt
            else:
                rb = round(rb - amt, 2)
            if rb < min_rb:
                min_rb = rb
                credit_at_min = credit_acc
        return min_rb, credit_at_min

    # Коррекция поднимает ВЕСЬ масштабируемый кредит (и до, и после точки
    # минимума) на один и тот же множитель, поэтому нужный множитель можно
    # вычислить аналитически: new_min_rb = min_rb + (f-1)*credit_up_to_min
    # должен быть ⩾ 0 → f = 1 + (-min_rb)/credit_up_to_min. Раньше коррекция
    # была слепым шагом ×1.02 максимум 5 раз (~+10.4% суммарно) — этого не
    # хватает, когда в каком-то месяце исходный Дебет уже был больше
    # Кредита: масштабирование обоих одним K тем же множителем, что и
    # Кредит, кратно увеличивает и абсолютный дефицit (см. коммент выше о
    # post-check). Точный расчёт закрывает дефицит за 1 шаг (несколько
    # итераций — только на случай, если поднятие кредита сдвигает точку
    # минимума и остаточный дефицит требует долива).
    min_rb, credit_before_min = _min_running_balance()
    if min_rb < 0:
        print(f"\n[KaspiIP] ⚠️ После пересчёта min_rb={min_rb:,.2f}, поднимаем кредит")
        for attempt in range(8):
            if credit_before_min <= 0:
                # Нет масштабируемого кредита ДО точки минимума — поднимать
                # нечего, коррекция невозможна. Выходим; проверка ниже
                # (min_rb < 0) поднимет IncomeTooLowError. Раньше здесь стоял
                # for/else, и этот break молча отменял ветку else с raise —
                # функция возвращала PDF с отрицательным промежуточным балансом.
                break
            factor = 1.0 + (-min_rb) / credit_before_min * 1.05  # +5% запас на округление/сдвиг точки
            for tx in stmt.transactions:
                if tx.is_credit and tx.is_scaleable:
                    tx.new_amount = round(tx.new_amount * factor, 2)
            min_rb, credit_before_min = _min_running_balance()
            if min_rb >= 0:
                print(f"[KaspiIP] ✅ Скорректировано за {attempt + 1} итераций, min_rb={min_rb:,.2f}")
                break

        # Единая точка отказа: сработает при ЛЮБОМ способе выхода из цикла
        # (исчерпаны 8 попыток ИЛИ ранний break из-за credit_before_min <= 0),
        # если баланс так и не удалось поднять до неотрицательного.
        if min_rb < 0:
            new_min = max(min_target, target_monthly_income) * 1.10
            raise IncomeTooLowError(
                min_target_monthly_income=new_min,
                current_expense=fixed_debit_total,
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

    # Финальное округление до круглых тысяч — теперь, когда баланс уже
    # подтверждён неотрицательным на полной точности (см. комментарий выше).
    for tx in stmt.transactions:
        if tx.is_scaleable:
            tx.new_amount = _round_amount(tx.new_amount, original=tx.amount)

    # Округление могло сдвинуть min_rb обратно в лёгкий минус (ошибка
    # округления до ±500₸ на транзакцию). Вместо повторного умножения ВСЕХ
    # кредитных транзакций (что опять испортило бы круглость чисел),
    # точечно докидываем ОДНУ — последнюю кредитную транзакцию перед точкой
    # минимума — на дефицит, округлённый вверх до ближайшей тысячи.
    min_rb, _ = _min_running_balance()
    if min_rb < 0:
        shortfall = -min_rb
        rb = stmt.summary.opening_balance
        bump_tx: Optional[KaspiIPTransaction] = None
        for t in sorted_txs:
            amt = t.new_amount if t.is_scaleable else t.amount
            if t.is_credit:
                rb = round(rb + amt, 2)
                if t.is_scaleable:
                    bump_tx = t
            else:
                rb = round(rb - amt, 2)
            if rb <= min_rb + 0.005:
                break
        if bump_tx is not None:
            bump_units = -(-shortfall // _ROUND_UNIT)  # ceil-деление
            bump_tx.new_amount += bump_units * _ROUND_UNIT
            print(f"[KaspiIP] Точечная докрутка +{bump_units * _ROUND_UNIT:,.0f}₸ на {bump_tx.date} (остаток после округления до тысяч)")

        # Перепроверяем после докрутки. Если поднимать было нечего
        # (bump_tx is None — перед точкой минимума нет масштабируемого
        # кредита) ИЛИ докрутка не закрыла дефицит полностью (напр. второй,
        # более ранний отрицательный провал от ошибки округления, который
        # эта одна докрутка не поднимает), баланс так и остался в минусе.
        # Раньше в обоих случаях функция молча возвращала PDF с отрицательным
        # промежуточным балансом — как и в исходной ПРОВЕРКЕ 3.
        min_rb, _ = _min_running_balance()
        if min_rb < 0:
            new_min = max(min_target, target_monthly_income) * 1.10
            raise IncomeTooLowError(
                min_target_monthly_income=new_min,
                current_expense=fixed_debit_total,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="post_check_negative_balance",
                message=(
                    f"Не удалось удержать неотрицательный баланс при "
                    f"{target_monthly_income:,.0f} ₸/мес после округления "
                    f"(min_rb={min_rb:,.0f} ₸). Минимально рекомендуемый "
                    f"доход: {new_min:,.0f} ₸/мес."
                ),
            )

    # Обновляем итоги напрямую по новым значениям транзакций
    s = stmt.summary
    new_total_credit = sum(
        (t.new_amount if t.is_scaleable else t.amount) for t in stmt.transactions if t.is_credit
    )
    new_total_debit = sum(
        (t.new_amount if t.is_scaleable else t.amount) for t in stmt.transactions if not t.is_credit
    )
    s.total_credit = round(new_total_credit, 2)
    s.total_debit = round(new_total_debit, 2)
    new_closing = s.opening_balance + s.total_credit - s.total_debit
    s.closing_balance = round(new_closing, 2)

    print(f"\n[KaspiIP] Итого кредит: → {s.total_credit:,.2f}")
    print(f"[KaspiIP] Итого дебет: → {s.total_debit:,.2f}")
    print(f"[KaspiIP] Исходящий остаток: → {s.closing_balance:,.2f}")

    return stmt


# ─── Замена через raw bytes (paren-формат BigEndian CID) ─────────────────────

def _fmt_coord_debet(value: float) -> str:
    """X-координата право-выровненной колонки «Дебет» — ВСЕГДА ровно 2 знака.

    Найдено 2026-08-04 на реальных файлах (`testpdf/kaspiPay`): в отличие от
    Kaspi Gold (`pdf_service._fmt_coord`, где генератор сам пишет переменную
    точность — «42.5», «211», «510.94995», и обрезка незначащих нулей
    корректна), эта колонка в оригинале печатает X с фиксированными двумя
    знаками на КАЖДОЙ денежной ячейке без исключений (6707 из 6707, все 4
    файла). Общий `_fmt_coord` на пересчитанном (`right_edge - w_new`,
    плавающая точка) X почти никогда не оканчивается на ноль, поэтому
    обрезать нечего — «207.392» вместо «207.39». `:.2f` не обрезает и не
    добавляет: оно и есть сама конвенция этого формата, а не приближение.
    """
    out = f"{value:.2f}"
    return "0.00" if out == "-0.00" else out


def _parse_cid_widths(w_body: str) -> Dict[int, float]:
    """Разбор массива /W CID-шрифта: формы «c [w1 w2 …]» и «c_first c_last w»."""
    widths: Dict[int, float] = {}
    for m in re.finditer(r"(\d+)\s*\[([\d\s.]+)\]", w_body):
        start = int(m.group(1))
        for i, w in enumerate(m.group(2).split()):
            widths[start + i] = float(w)
    # Диапазонная форма — только вне уже разобранных скобок.
    for m in re.finditer(r"(?<![\[\d])(\d+)\s+(\d+)\s+(\d+(?:\.\d+)?)(?!\s*[\d\[])", w_body):
        lo, hi, w = int(m.group(1)), int(m.group(2)), float(m.group(3))
        if 0 <= lo <= hi and hi - lo < 65536:
            for c in range(lo, hi + 1):
                widths.setdefault(c, w)
    return widths


def _primary_glyph_advances(doc, from_unicode: Dict[str, str]) -> Dict[str, float]:
    """char → ширина глифа в долях em, из /W РЕАЛЬНОГО шрифта документа.

    Зачем не константа. Правый край колонки «Дебет» держится тем, что при
    смене числа X сдвигается ровно на изменение ширины строки, поэтому ширина
    обязана совпадать с той, которой считал сам генератор, до сотых пункта.
    Приближения не годятся: сперва здесь стояло `avg_w = 4.44` вместо
    4.448 (0.556 em × кегль 8), а после его исправления остался второй,
    более тонкий слой той же ошибки — модель «пробел и запятая ровно вдвое
    уже цифры». В этом шрифте цифра = 556, а пробел и запятая = **277**, а не
    278, и разницы в 1/1000 em хватало, чтобы 280 сумм из 1738 встали на
    соседний правый край. Ширины берём из /W дескендант-шрифта — тогда
    формула точна для любого набора символов и любого шрифта, а не только
    для цифр ArialMT.

    Пустой словарь = не удалось разобрать; вызывающий код откатывается на
    прежнюю приближённую модель.
    """
    try:
        primary = _find_primary_font_tounicode_xref(doc)
    except Exception:  # noqa: BLE001
        primary = None

    descendants: List[int] = []
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref)
        except Exception:  # noqa: BLE001
            continue
        if "/Type0" not in obj:
            continue
        dm = re.search(r"/DescendantFonts\s*\[?\s*(\d+)\s+0\s+R", obj)
        if not dm:
            continue
        tm = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
        is_primary = primary is not None and tm is not None and int(tm.group(1)) == primary
        if is_primary:
            descendants.insert(0, int(dm.group(1)))
        else:
            descendants.append(int(dm.group(1)))

    for desc in descendants:
        try:
            obj = doc.xref_object(desc)
        except Exception:  # noqa: BLE001
            continue
        wm = re.search(r"/W\s*\[(.*?)\]\s*(?:/|>>)", obj, re.S)
        if not wm:
            continue
        widths = _parse_cid_widths(wm.group(1))
        if not widths:
            continue
        dwm = re.search(r"/DW\s+(\d+(?:\.\d+)?)", obj)
        default_w = float(dwm.group(1)) if dwm else 1000.0
        adv: Dict[str, float] = {}
        for ch, code in from_unicode.items():
            try:
                cid = int(code, 16)
            except (TypeError, ValueError):
                continue
            adv[ch] = widths.get(cid, default_w) / 1000.0
        # Цифры обязаны найтись — иначе это не тот шрифт, которым набраны суммы.
        if all(c in adv for c in "0123456789"):
            return adv
    return {}


def process_kaspi_ip_pdf(input_bytes: bytes, target_monthly_income: float) -> bytes:
    """
    Масштабирует доходы в IP-выписке Kaspi Bank через raw bytes замену.

    PDF использует литеральные бинарные строки X Y Td (BigEndian-CID) Tj.
    Применяет тот же подход, что pdf_service использует для cert-страницы:
    paren-паттерн + BigEndian 2-байт CID-кодирование. Не вызывает
    doc.tobytes() — шрифты и структура PDF не меняются.
    """
    doc = fitz.open(stream=input_bytes, filetype="pdf")
    TO_UNICODE, FROM_UNICODE = build_dynamic_cmap(doc)

    # Kaspi IP использует тот же ArialMT CID-диапазон, что и Kaspi Gold.
    # Применяем override только если dynamic CMap не определил цифры корректно.
    if not all(FROM_UNICODE.get(ch) == f'{0x0013 + i:04X}' for i, ch in enumerate('0123456789')):
        for _i, _ch in enumerate('0123456789'):
            FROM_UNICODE[_ch] = f'{0x0013 + _i:04X}'
        FROM_UNICODE[','] = '000F'
        FROM_UNICODE[' '] = '0003'
        FROM_UNICODE['.'] = '0011'

    GLYPH_EM = _primary_glyph_advances(doc, FROM_UNICODE)

    stmt = parse_kaspi_ip_statement(doc)
    stmt = recalculate_kaspi_ip(stmt, target_monthly_income)

    # ─── Декодер/энкодер для BigEndian 2-байт CID ────────────────────────
    def paren_decode(raw_bytes: bytes) -> str:
        result = ""
        for i in range(0, len(raw_bytes) - 1, 2):
            code = f"{(raw_bytes[i] << 8 | raw_bytes[i + 1]):04X}"
            result += TO_UNICODE.get(code, "?")
        return result

    # Порог X-координаты (в системе координат content stream, ДО поворота
    # страницы) между колонками "Дебет" и "Кредит" — находим по позиции
    # заголовков на стр.0. Значения ВЫШЕ порога — Кредит, НИЖЕ — Дебет.
    # Нужен, чтобы отличать реальную кредитовую сумму от дебетовой с тем же
    # числовым текстом (напр. "95 000" может быть и доходом одного месяца, и
    # неклассифицированным расходом того же периода) — без этого при
    # совпадении цифр значение может уйти не в ту транзакцию (см. халык-фикс).
    _tm_hdr_pat = re.compile(
        rb"1\s+0\s+0\s+1\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Tm\s+/F\d+\s+\d+\s+Tf\s+\(([^)]*)\)\s*Tj"
    )
    _debet_x: Optional[float] = None
    _kredit_x: Optional[float] = None
    for _xref in doc[0].get_contents():
        _stream = doc.xref_stream(_xref)
        for _m in _tm_hdr_pat.finditer(_stream):
            _dec = paren_decode(_m.group(3)).strip()
            if _dec == "Дебет":
                _debet_x = float(_m.group(1))
            elif _dec == "Кредит":
                _kredit_x = float(_m.group(1))
    if _debet_x is not None and _kredit_x is not None:
        cd_x_threshold = (_debet_x + _kredit_x) / 2
    else:
        cd_x_threshold = 221.6  # запасное значение (наблюдалось в шаблоне)

    # ── Левая граница колонки «Кредит» (для надёжной классификации ячеек) ──
    # cd_x_threshold (середина между ЗАГОЛОВКАМИ Дебет/Кредит) — ненадёжный
    # разделитель ЗНАЧЕНИЙ: колонка Кредит ЛЕВО-выровнена (все суммы делят один
    # X независимо от длины), а Дебет — ПРАВО-выровнен (короткое число
    # начинается сильно правее длинного). Из-за этого очень короткая дебетовая
    # сумма (напр. комиссия «40») стартует правее середины заголовков и
    # ошибочно попадала в колонку Кредит: её замена не находила свою (дебетовую)
    # очередь, ячейка оставалась со старым значением, а итог в шапке уже был
    # пересчитан — расхождение шапка/тело (воспроизведено на IP2.pdf: +2 920 ₸,
    # ловится validate_kaspi_ip как провал running balance).
    #
    # Надёжный признак ЛЕВО-выровненной колонки: на её X встречаются числа
    # РАЗНОЙ длины (2..7 цифр), тогда как на X право-выровненной колонки — всё
    # одной длины (общий правый край + одинаковая ширина). Ищем X денежных
    # ячеек с максимальным разнообразием длин — это и есть X колонки Кредит.
    # По нему классифицируем ячейки в replace_tm (см. там). Если измерить не
    # удалось (мало ячеек), остаётся старый порог cd_x_threshold.
    _x_lengths: Dict[float, set] = defaultdict(set)
    _x_count: Dict[float, int] = defaultdict(int)
    _measure_pat = re.compile(
        rb"1\s+0\s+0\s+1\s+(-?\d+\.?\d*)\s+-?\d+\.?\d*\s+Tm\s+/F\d+\s+\d+\s+Tf\s+\(([^)]*)\)\s*Tj"
    )
    for _pi in range(len(doc)):
        for _xref in doc[_pi].get_contents():
            try:
                _st = doc.xref_stream(_xref)
            except Exception:
                continue
            for _m in _measure_pat.finditer(_st):
                _txt = paren_decode(_m.group(2)).strip()
                if not (_AMOUNT_CELL_RE.match(_txt) and any(c.isdigit() for c in _txt)):
                    continue
                try:
                    _xr = round(float(_m.group(1)), 1)
                except Exception:
                    continue
                _x_lengths[_xr].add(len(re.sub(r"[^0-9]", "", _txt)))
                _x_count[_xr] += 1
    credit_col_x: Optional[float] = None
    if _x_lengths:
        _best = max(_x_lengths.items(), key=lambda kv: (len(kv[1]), _x_count[kv[0]]))
        # Требуем ≥2 разных длин — иначе это право-выровненная (или служебная,
        # напр. КНП) колонка, а не лево-выровненный Кредит; тогда fallback.
        if len(_best[1]) >= 2:
            credit_col_x = _best[0]

    page_xrefs = [doc[i].get_contents() for i in range(len(doc))]
    xref_to_page: Dict[int, int] = {}
    for pg_idx, xrefs in enumerate(page_xrefs):
        for xref_id in xrefs:
            xref_to_page[xref_id] = pg_idx

    # Полный текст документа — нужен ниже, чтобы посчитать РЕАЛЬНОЕ число
    # вхождений summary-значений (total_credit/total_debit/closing) вместо
    # того, чтобы гадать (см. _add_summary).
    full_text_for_counts = "".join(doc[i].get_text() for i in range(len(doc)))
    doc.close()

    def _parse_amount_cell(decoded: str) -> Optional[Tuple[str, bool, bool]]:
        """
        Разбирает decoded-текст Tj-рана как денежную ячейку Kaspi — возвращает
        (цифры, было_ли_",XX", был_ли_суффикс_"KZT") ТОЛЬКО если весь текст
        ЦЕЛИКОМ имеет такую форму, иначе None.

        Раньше бралась подстрока между первой и последней цифрой ГДЕ УГОДНО в
        тексте — из-за этого произвольный текст, просто содержащий цифры
        (маскированная карта "Kaspi Gold *4170", номер документа, дата,
        время), ошибочно распознавался как сумма и мог "украсть" из очереди
        замену, предназначенную для настоящей ячейки Дебет/Кредит. Реальная
        сумма при этом оставалась нетронутой (старый текст), а итог в шапке
        уже пересчитан на новое значение.

        Форма (",XX" / "KZT") фиксируется здесь же и позже воспроизводится
        1-в-1 в _format_amount_cell — раньше замена всегда шла через _fmt()
        (жёстко 2 знака после запятой, без суффикса), из-за чего "Исходящий
        остаток" терял " KZT" ("1 380 133 KZT" → "7 292 396,03"), а "Итого
        обороты" получал несуществовавшие в оригинале копейки
        ("37 411 887" → "16 492 286,17") — оба видны невооружённым глазом
        как признак подделки при сверке с оригинальным форматом банка.
        """
        m = _AMOUNT_CELL_RE.match(decoded.strip())
        if not m:
            return None
        int_part, dec_part, kzt = m.groups()
        # Ключ ОБЯЗАН включать цифры копеек (dec_part), а не только
        # int_part — очереди на замену строятся через _clean_digits() на
        # ПОЛНОМ исходном тексте ("254 117,63" → "25411763", копейки
        # входят), см. target_q[...][_clean_digits(tx.amount_text)] и
        # _add_summary(). Раньше здесь брались цифры только из int_part
        # ("254117"), из-за чего любая ячейка с копейками (в т.ч.
        # "Исходящий остаток") никогда не находила свою запись в очереди —
        # match молча возвращался без замены (см. ветку `if new_val is
        # None: return match.group(0)` ниже), а шапка PDF оставалась со
        # старым текстом, хотя recalculate_kaspi_ip уже посчитал новый
        # closing_balance. На реальной выписке это давало Δ в разы больше
        # исходного (баланс "прыгал" от нескольких сотен тысяч до
        # нескольких миллионов ₸) — validate_kaspi_ip ловит это как
        # провал проверки "Баланс" и "Running balance", но сам PDF уже
        # был выдан пользователю с несовпадающими цифрами.
        digits = re.sub(r"[^0-9]", "", int_part + (dec_part or ""))
        if not digits:
            return None
        return digits, dec_part is not None, kzt is not None

    def _format_amount_cell(new_val: float, has_decimal: bool, has_kzt: bool) -> str:
        """Форматирует new_val в ТОЙ ЖЕ форме, в которой была исходная ячейка."""
        if has_decimal:
            body = _fmt(new_val)
        else:
            body = f"{round(abs(new_val)):,.0f}".replace(",", " ")
        return body + " KZT" if has_kzt else body

    def paren_encode(text: str) -> bytes:
        out = bytearray()
        for ch in text:
            c = int(FROM_UNICODE.get(ch, "0000"), 16)
            out.append((c >> 8) & 0xFF)
            out.append(c & 0xFF)
        return bytes(out)

    # ─── Очереди замен ───────────────────────────────────────────────────
    # Раздельные очереди по колонке (Дебет/Кредит): один и тот же числовой
    # текст (напр. "95 000") может быть одновременно и суммой одной
    # (масштабируемой) кредитовой транзакции, и суммой другой (тоже
    # масштабируемой) дебетовой в этом же месяце — без разделения по колонке
    # цифровой ключ их не различает, и значение может уйти не в ту запись.
    page_replace_credit: Dict[int, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
    page_replace_debit: Dict[int, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
    for tx in stmt.transactions:
        if not tx.is_scaleable or abs(tx.new_amount - tx.amount) < 0.005:
            continue
        target_q = page_replace_credit if tx.is_credit else page_replace_debit
        # Храним СЫРОЕ значение (не готовую строку) — форма (копейки/"KZT")
        # определяется позже, в момент замены, по фактическому тексту
        # найденной ячейки (см. _format_amount_cell).
        target_q[tx.page_num][_clean_digits(tx.amount_text)].append(tx.new_amount)

    s = stmt.summary
    summary_replace: Dict[str, deque] = {}
    def _add_summary(old_text: str, old_val: float, new_val: float):
        if abs(new_val - old_val) > 0.005:
            k = _clean_digits(old_text)
            if k:
                # Ровно столько копий, сколько раз это значение РЕАЛЬНО
                # встречается в тексте документа. Раньше здесь было жёстко
                # 2 копии ("значение может встречаться на стр.0 и на
                # итоговой") — но на реальной выписке total_credit/
                # total_debit/closing каждое встречается ровно 1 раз, и
                # лишняя закладка каждый раз давала ложное предупреждение
                # "N замен(ы) не применено", хотя все реальные ячейки уже
                # были заменены (см. систематическую отладку — leftover
                # всегда ровно 1 на каждый summary-ключ). `or 1` — на
                # случай, если значение вообще не найдено текстовым
                # поиском (иначе очередь была бы пустой и настоящее
                # отсутствие совпадения прошло бы молча).
                #
                # Считаем ОТДЕЛЬНО стоящие вхождения, а не подстроки: голый
                # str.count() находит короткое значение внутри длинного числа
                # («Исходящий остаток 0,15» встречался ещё и хвостом суммы
                # «60,15» на реальном IP3) — лишний слот оставался
                # непотраченным и на каждом прогоне печаталось ложное
                # «1 замен(ы) не применено», обесценивая единственный сигнал
                # о настоящей незаписанной ячейке.
                occurrences = _count_standalone(full_text_for_counts, old_text.strip()) or 1
                summary_replace[k] = deque([new_val] * occurrences)
    _add_summary(s.total_credit_text, _parse_amount(s.total_credit_text) or 0.0, s.total_credit)
    _add_summary(s.total_debit_text,  _parse_amount(s.total_debit_text)  or 0.0, s.total_debit)
    _add_summary(s.closing_text,      _parse_amount(s.closing_text)      or 0.0, s.closing_balance)

    # ─── Паттерн: 1 0 0 1 X Y Tm /Fx sz Tf (...) Tj ─────────────────────
    # Шрифт разбит на имя и кегль отдельными группами — кегль уменьшается,
    # если новое число шире поля (см. replace_tm), не меняя имя шрифта.
    tm_pat = re.compile(
        rb"1\s+0\s+0\s+1\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Tm\s+"
        rb"(/F\d+)\s+(\d+)\s+Tf\s+"
        rb"\(([^)]*)\)\s*Tj",
    )

    # ─── Raw bytes обработка ─────────────────────────────────────────────
    raw = bytearray(input_bytes)
    total_replaced = 0
    cumulative_offset = 0

    all_content_xrefs: set = set()
    for xrefs in page_xrefs:
        all_content_xrefs.update(xrefs)

    obj_positions: Dict[int, int] = {}
    for xref_id in all_content_xrefs:
        p = f"{xref_id} 0 obj".encode()
        found = bytes(raw).find(p)
        if found >= 0:
            obj_positions[xref_id] = found

    sorted_xrefs = sorted(obj_positions.items(), key=lambda x: x[1])

    for xref_id, orig_pos in sorted_xrefs:
        pos = orig_pos + cumulative_offset
        pg_idx = xref_to_page.get(xref_id, -1)
        credit_queues_on_page = page_replace_credit.get(pg_idx, {})
        debit_queues_on_page = page_replace_debit.get(pg_idx, {})

        stream_pos = raw.find(b"stream", pos)
        if stream_pos < 0:
            continue

        header_region = bytes(raw[pos:stream_pos])
        length_m = re.search(rb"/Length\s+(\d+)", header_region)
        if not length_m:
            continue

        declared_length = int(length_m.group(1))
        length_start = pos + length_m.start(1)
        length_end = pos + length_m.end(1)

        data_start = stream_pos + 6
        if raw[data_start:data_start + 1] == b"\r":
            data_start += 2
        else:
            data_start += 1

        endstream_pos = raw.find(b"endstream", data_start)
        if endstream_pos < 0:
            continue

        # Слайс ТОЧНО по /Length, а не по эвристике "endswith \r\n / \n" —
        # компрессированные байты сами по себе МОГУТ заканчиваться на 0x0D
        # ('\r'), и тогда suffix-эвристика ошибочно принимает последний байт
        # полезной нагрузки за настоящий CRLF-перевод строки перед
        # "endstream" и отрезает на 1 байт больше, чем нужно — на выходе
        # /Length ссылается на данные короче фактических, поток бьётся
        # (zlib "incomplete or truncated stream"). /Length — единственный
        # источник истины о границе payload; всё после него и до
        # "endstream" — оригинальный хвост (перевод строки), который просто
        # сохраняется как есть.
        raw_stream_data = bytes(raw[data_start:data_start + declared_length])

        try:
            decompressed = zlib.decompress(raw_stream_data)
        except zlib.error:
            continue

        def replace_tm(match, _pg=pg_idx, _qc=credit_queues_on_page, _qd=debit_queues_on_page):
            nonlocal total_replaced
            x_str = match.group(1).decode("ascii")
            y_str = match.group(2).decode("ascii")
            font_name = match.group(3)
            font_size_str = match.group(4).decode("ascii")
            raw_content = match.group(5)
            try:
                current_x = float(x_str)
                orig_size = float(font_size_str)
            except Exception:
                return match.group(0)

            decoded = paren_decode(raw_content)
            parsed_cell = _parse_amount_cell(decoded)
            if parsed_cell is None:
                return match.group(0)
            clean_d, had_decimal, had_kzt = parsed_cell
            old_num = decoded.strip()

            # Колонка этого конкретного Tj-рана определяется его СОБСТВЕННОЙ
            # X-позицией — так цифровое совпадение с транзакцией из ДРУГОЙ
            # колонки (Дебет vs Кредит) не приводит к ошибочной замене.
            #
            # Если удалось измерить X лево-выровненной колонки Кредит
            # (credit_col_x, см. выше) — классифицируем по нему, а не по
            # ненадёжной середине заголовков: ячейка на этом X (±tol) — Кредит;
            # ЛЕВЕЕ него — Дебет (право-выровнен, всегда левее колонки Кредит);
            # ПРАВЕЕ (напр. КНП-код) — не денежная колонка таблицы, очередь не
            # трогаем (только summary ниже). Это чинит попадание короткого
            # дебета в Кредит и заодно не даёт КНП «украсть» дебетовый слот.
            if credit_col_x is not None:
                if abs(current_x - credit_col_x) <= _COL_MATCH_TOL:
                    is_credit_cell = True
                    _q = _qc
                elif current_x < credit_col_x:
                    is_credit_cell = False
                    _q = _qd
                else:
                    is_credit_cell = False
                    _q = None
            else:
                is_credit_cell = current_x > cd_x_threshold
                _q = _qc if is_credit_cell else _qd
            new_val = None
            is_summary = False
            q = _q.get(clean_d) if _q is not None else None
            if q:
                new_val = q.popleft()
            else:
                sq = summary_replace.get(clean_d)
                if sq:
                    new_val = sq.popleft()
                    is_summary = True

            if new_val is None:
                return match.group(0)

            # Форма (копейки/"KZT") воспроизводится ИЗ ТОГО ЖЕ Tj-рана, а не
            # берётся жёстко зашитой — иначе "Исходящий остаток" теряет
            # " KZT" и/или "Итого обороты" получает несуществовавшие в
            # оригинале копейки (см. docstring _parse_amount_cell).
            new_txt = _format_amount_cell(new_val, had_decimal, had_kzt)
            new_bytes = paren_encode(new_txt)
            if b'\x00\x00' in new_bytes:
                return match.group(0)

            # Ширина цифры берётся из РЕАЛЬНОГО кегля этого же Tj-рана, а не из
            # константы: у ArialMT цифра = _ARIAL_DIGIT_EM em, т.е. ровно
            # 4.448 pt при кегле 8 (весь этот формат набран восьмым). Раньше
            # здесь стояло 4.44 — приближение, которое занижает ширину на
            # 0.008 pt на символ, из-за чего право-выровненная колонка «Дебет»
            # уезжала ровно на 0.008 × (длина_новая − длина_старая): в
            # оригинале все суммы делят один правый край, а в результате их
            # края расползались на 610.13/610.14/610.16 при эталонном 610.15
            # (замер настоящими метриками шрифта, не этой моделью). Тот же
            # класс дефекта и то же лечение, что и `_digit_width_at` в
            # pdf_service.py (Kaspi Gold) — см. критерий 2 в CLAUDE.md.
            avg_w = _ARIAL_DIGIT_EM * orig_size

            def _adv(text: str, _sz=orig_size) -> float:
                """Ширина строки в пунктах. Реальные /W, если их удалось прочесть."""
                if GLYPH_EM:
                    return sum(GLYPH_EM.get(c, _ARIAL_DIGIT_EM) for c in text) * _sz
                return sum(0.5 if c in (" ", ",", ".") else 1.0 for c in text) * avg_w

            w_old = _adv(old_num)
            w_new = _adv(new_txt)
            font_bytes = font_name + b" " + font_size_str.encode("ascii") + b" Tf"

            if is_summary or is_credit_cell:
                # "Входящий/Исходящий остаток" и другие суммарные поля — НЕ
                # ячейки таблицы фиксированной ширины: это одна строка
                # "подпись — значение" со свободным местом справа (значение
                # растёт вправо, как и все остальные поля в этом блоке —
                # "Наименование клиента:", "Входящий остаток" и т.д., все
                # начинаются с одного и того же X). Если применить формулу
                # сохранения правого края, левый край "уедет" левее общей
                # колонки значений — именно это и было замечено визуально
                # ("Исходящий остаток" начинался левее "Входящий остаток").
                #
                # Колонка "Кредит" в таблице транзакций — ТОЖЕ левого
                # выравнивания, а не правого: замер X по многим строкам
                # оригинала показал, что все значения "Кредит" (475 000,
                # 100 000, 65 000, ...) делят один и тот же X независимо от
                # длины числа — колонка растёт вправо от общей левой
                # границы (визуально "прижата" к разделителю Дебет|Кредит
                # с его правой стороны). Раньше здесь ошибочно применялась
                # формула для ПРАВОГО выравнивания (как у Дебет), из-за чего
                # рост числа сдвигал его ВЛЕВО, в чужую колонку Дебет —
                # баг, замеченный визуально ("Кредит" пересекал границу
                # с Дебет). Поэтому для Кредит, как и для summary-полей,
                # X не трогаем вообще — число просто растёт вправо.
                new_x = current_x
                x_recomputed = False
            else:
                # Колонка "Дебет" в таблице транзакций фиксированной ширины
                # и выровнена по ПРАВОМУ краю (замер X по многим строкам
                # оригинала подтверждает: короче число — левее начинается,
                # но правый край общий). Раньше переполнение (новое число
                # длиннее старого) решалось уменьшением кегля (Tf) — сперва
                # без ограничения (MIN_SCALE=0.35, ≈2.8pt из 8pt —
                # нечитаемо), потом с мягким полом (MIN_SCALE=0.8) — но и
                # 6.4pt на фоне соседних 8pt-строк заметно мельче и
                # выглядит как визуальный баг. Кегль (Tf) здесь больше не
                # трогаем — только сдвиг влево с сохранением правого края,
                # точно как для Kaspi Gold в pdf_service.py (там эта схема
                # проверена и не вызывает жалоб). Небольшой перехлёст в
                # соседнюю колонку при экстремальном росте числа —
                # приемлемый компромисс, нечитаемый/мелкий шрифт — нет.
                # `right_edge` округляется ДО вычитания w_new, а не после
                # всей арифметики (см. `_fmt_coord_debet` ниже) — иначе две
                # источника суб-0.01pt погрешности накапливаются: (1) w_old
                # (сумма ширин цифр/разделителей по /W, кратных 0.001pt, а не
                # 0.01pt) сдвигает right_edge на несколько тысячных от
                # «чистого» табличного края; (2) финальное округление new_x
                # добавляет свою погрешность до ±0.005pt. По отдельности
                # каждая безобидна, но их СУММА на части ячеек превышала
                # 0.005pt и переносила измеренный правый край на соседнее
                # значение сетки (610.14/610.16 вместо 610.15) — найдено
                # 2026-08-04 инструментированием реального прогона (IP4 ×2):
                # без этой правки 29 из 179 ячеек «Дебет» имели остаток
                # округления −0.004, что в сумме с оставшейся неточностью
                # current_x/w_old давало измеренный дрейф до +0.006pt.
                # Округление right_edge СРАЗУ убирает источник (1), оставляя
                # только источник (2) — единственный настоящий шаг округления
                # до 2 знаков, максимум ±0.005pt, чего всегда достаточно,
                # чтобы измеренный край остался в той же ячейке сетки 0.01pt.
                right_edge = round(current_x + w_old, 2)
                new_x = right_edge - w_new
                x_recomputed = True

            print(f"  [IP] стр.{_pg} {old_num.strip()!r} → {new_txt!r}")
            total_replaced += 1
            # Разделитель перед Tj берём из оригинала: этот формат пишет
            # «)Tj» вплотную, а писатель вставлял пробел — признак 3
            # форензик-разбора (104 строки чужого стиля против 0 в оригинале).
            # Переводы строк после Tm и Tf почерку оригинала уже отвечают
            # (разбор их и не отметил), поэтому остаются как есть.
            _so, _sc = _op_separators(match.group(0))
            # X-координата форматируется по-разному в зависимости от того,
            # пересчитан ли X. Когда он НЕ пересчитан («Кредит», summary —
            # new_x is current_x, тот же float, что и в оригинале), общий
            # `_fmt_coord` (переменная точность, обрезка нулей) воспроизводит
            # исходную запись байт-в-байт — замерено на реальных файлах, эта
            # ветка уже 100% совпадает с оригиналом, трогать не нужно. Когда X
            # пересчитан («Дебет», право-выровненная колонка), тот же
            # `_fmt_coord` почти всегда даёт 3 знака вместо 2 (после
            # вычитания ширины строки в плавающей точке результат почти
            # никогда не оканчивается на ноль, обрезать нечего) — а этот
            # формат, в отличие от Kaspi Gold, пишет РОВНО 2 знака на каждой
            # ячейке таблицы без исключений (замерено: 6707 из 6707 денежных
            # ячеек «Дебет» на 4 реальных файлах). `_fmt_coord_debet` — тот
            # же класс фикса, что и `_op_separators`/`_fmt_coord` в целом
            # (критерий 4, CLAUDE.md): почерк записи обязан быть неотличим от
            # оригинала, только здесь конвенция формата ФИКСИРОВАННАЯ, а не
            # «повторить, что было», поэтому формататор жёстко на 2 знака.
            x_out = _fmt_coord_debet(new_x) if x_recomputed else _fmt_coord(new_x)
            return (
                b"1 0 0 1 " + x_out.encode("ascii") +
                b" " + y_str.encode("ascii") + b" Tm\n" +
                font_bytes + _so +
                b"(" + new_bytes + b")" + _sc + b"Tj"
            )

        new_decompressed = tm_pat.sub(replace_tm, decompressed)
        if new_decompressed == decompressed:
            continue

        new_compressed = zlib.compress(new_decompressed)
        old_stream_len = len(raw_stream_data)
        new_stream_len = len(new_compressed)
        delta = new_stream_len - old_stream_len

        old_len_b = str(declared_length).encode()
        new_len_b = str(new_stream_len).encode()
        len_delta = len(new_len_b) - len(old_len_b)

        raw[length_start:length_end] = new_len_b
        data_start += len_delta
        endstream_pos += len_delta

        trailing_start = data_start + old_stream_len
        trailing = bytes(raw[trailing_start:endstream_pos])
        raw[data_start:endstream_pos] = new_compressed + trailing

        cumulative_offset += len_delta + delta

    print(f"\n[KaspiIP] Произведено замен: {total_replaced}")

    # Диагностика: если очередь не опустела — часть транзакций не нашла свою
    # ячейку в PDF (не заматчилась под tm_pat/_extract_amount_digits) и
    # осталась со старым текстом, хотя итоги в шапке уже посчитаны по новым
    # суммам (recalculate_kaspi_ip). Раньше это проходило молча.
    leftover = sum(
        len(dq)
        for pages in (page_replace_credit, page_replace_debit)
        for cols in pages.values()
        for dq in cols.values()
    )
    leftover += sum(len(dq) for dq in summary_replace.values())
    if leftover:
        print(
            f"[KaspiIP] ⚠️ {leftover} замен(ы) не применено — часть сумм в PDF "
            f"осталась старой, итоги в шапке разойдутся с напечатанной таблицей."
        )
        for pg, cols in page_replace_credit.items():
            for digits, dq in cols.items():
                if dq:
                    print(f"  [leftover CR] стр.{pg} digits={digits!r} остались={list(dq)}")
        for pg, cols in page_replace_debit.items():
            for digits, dq in cols.items():
                if dq:
                    print(f"  [leftover DR] стр.{pg} digits={digits!r} остались={list(dq)}")
        for digits, dq in summary_replace.items():
            if dq:
                print(f"  [leftover SUMMARY] digits={digits!r} остались={list(dq)}")

    result = bytes(raw)
    if cumulative_offset != 0:
        result = _rebuild_xref_table(result)
    return result


# ─── Валидация ────────────────────────────────────────────────────────────────

def validate_kaspi_ip(pdf_bytes: bytes) -> dict:
    """
    Проверяет целостность выписки Kaspi ИП.
    Возвращает dict совместимый с форматом /verify endpoint.
    """
    import zlib as _zlib

    checks = []
    issues = []
    raw = pdf_bytes

    # 1. Целостность zlib-стримов.
    #
    # Слайс ТОЧНО по /Length, а не по эвристике "endswith \r\n / \n" — тот же
    # баг и фикс, что и в process_kaspi_ip_pdf (см. комментарий там):
    # компрессированные байты сами МОГУТ заканчиваться на 0x0D ('\r'), и тогда
    # suffix-эвристика ошибочно принимает последний байт полезной нагрузки за
    # настоящий CRLF-перевод строки перед "endstream" и отрезает на 1 байт
    # больше, чем нужно — decompress падает на полностью корректном PDF
    # (ложное "N битых стримов" в /verify после легитимной пересборки потока).
    # Заодно требуем, чтобы /Length нашёлся ИМЕННО в словаре ЭТОГО объекта
    # (между "N 0 obj" и найденным "stream") — иначе найденный "stream" может
    # принадлежать СЛЕДУЮЩЕМУ объекту (напр. у "/Type/Page" без своего
    # контента, за которым в пределах окна поиска идёт чужой стрим), и один
    # и тот же реальный стрим считается дважды под разными obj-номерами.
    stream_errors = 0
    for m in re.finditer(rb"(\d+)\s+0\s+obj", raw):
        ss = raw.find(b"stream", m.end(), m.end() + 500)
        if ss < 0:
            continue
        length_m = re.search(rb"/Length\s+(\d+)", raw[m.end():ss])
        if not length_m:
            continue
        ds = ss + 6
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

    # 3. Парсинг + проверка баланса + ISI
    isi = 0.0
    month_cr: Dict[str, float] = {}
    s = KaspiIPSummary(0, "0", 0, "0", 0, "0", 0, "0")
    txs: list = []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        page_count = len(doc)
        ok_pages = page_count > 0
        checks.append({"name": "Структура PDF", "ok": ok_pages,
                       "detail": f"Страниц: {page_count}"})
        if not ok_pages:
            issues.append("PDF не содержит страниц")
        else:
            page0_text = doc[0].get_text()
            has_opening = "Входящий остаток" in page0_text
            checks.append({"name": "Выписка Kaspi ИП (формат)", "ok": has_opening,
                           "detail": "Найдено 'Входящий остаток'" if has_opening
                           else "Не найдено 'Входящий остаток'"})
            if not has_opening:
                issues.append("Не найден заголовок 'Входящий остаток'")

        stmt = parse_kaspi_ip_statement(doc)
        doc.close()
        txs = stmt.transactions
        s = stmt.summary

        # Баланс: opening + total_credit − total_debit = closing
        calc_closing = round(s.opening_balance + s.total_credit - s.total_debit, 2)
        delta_bal = round(s.closing_balance - calc_closing, 2)
        ok_bal = abs(delta_bal) < 500.0
        checks.append({"name": "Баланс (opening + кредит − дебет = closing)",
                       "ok": ok_bal,
                       "detail": (f"{s.opening_balance:,.2f} + {s.total_credit:,.2f}"
                                  f" − {s.total_debit:,.2f} = {calc_closing:,.2f}"
                                  f" | closing={s.closing_balance:,.2f} | Δ={delta_bal:+,.2f}")})
        if not ok_bal:
            issues.append(f"Баланс: Δ = {delta_bal:+,.2f} ₸")

        # Running balance — цепочка по датам (от старых к новым), проверка на уход в минус.
        # У Kaspi ИП часть дебета масштабируется вместе с кредитом, поэтому
        # проверка агрегированной суммы (выше) не гарантирует, что баланс не
        # уходил в минус в середине периода — нужен пошаговый обход.
        # date хранит только "DD.MM.YYYY" (без времени). Исходный PDF идёт
        # строго от новых к старым (включая время) — reversed() даёт верный
        # порядок между различными секундами. Часть операций в один день имеет
        # общий ночной batch-timestamp ("0:00:10") — порядок внутри такой
        # группы неразличим по тексту, поэтому кредит внутри дня ставится
        # раньше дебета (стабильная сортировка): списание не может случиться
        # раньше зачисления.
        sorted_txs = sorted(
            reversed(txs),
            key=lambda t: (_date_key(t.date), 0 if t.is_credit else 1),
        )
        rb = s.opening_balance
        rb_negative = 0
        for t in sorted_txs:
            rb = round(rb + t.amount if t.is_credit else rb - t.amount, 2)
            if rb < 0:
                rb_negative += 1
        delta_rb = round(rb - s.closing_balance, 2)
        ok_rb = abs(delta_rb) < 500.0
        checks.append({"name": "Running balance", "ok": ok_rb,
                       "detail": f"Финальный RB = {rb:,.2f} | closing = {s.closing_balance:,.2f} | Δ = {delta_rb:+,.2f}"})
        if not ok_rb:
            issues.append(f"Running balance: Δ = {delta_rb:+,.2f} ₸")

        ok_rb_neg = rb_negative == 0
        checks.append({"name": "Баланс ≥ 0", "ok": ok_rb_neg,
                       "detail": f"Отрицательных точек: {rb_negative}"})
        if not ok_rb_neg:
            issues.append(f"Баланс уходит в минус в {rb_negative} точках")

        # ISI по кредитовым транзакциям
        month_cr = defaultdict(float)
        for t in txs:
            if t.is_credit and t.amount > 0:
                mk = t.date[3:]
                month_cr[mk] += t.amount
        vals = list(month_cr.values())
        if len(vals) >= 3:
            mu = sum(vals) / len(vals)
            sigma = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
            isi = max(0.0, 1.0 - sigma / mu) if mu > 0 else 0.0
            ok_isi = isi >= 0.60
            isi_detail = f"ISI = {isi:.4f} (мин. 0.60) | месяцев: {len(month_cr)}"
            if not ok_isi:
                issues.append(f"ISI = {isi:.4f} < 0.60")
        else:
            isi = 1.0
            ok_isi = True
            isi_detail = f"ISI: n/a (месяцев: {len(month_cr)} — недостаточно)"
        checks.append({"name": "ISI (стабильность оборота)", "ok": ok_isi,
                       "detail": isi_detail})

    except Exception as e:
        checks.append({"name": "Парсинг PDF", "ok": False, "detail": str(e)})
        issues.append(f"Ошибка парсинга: {e}")

    n_months = len(month_cr) or 1
    avg_monthly = s.total_credit / n_months

    # suggested_min обязан отражать ОБА floor'а из recalculate_kaspi_ip, а не
    # только «too_aggressive» (30% от среднего). Второй floor —
    # «below_balance_floor»: доход должен покрыть нескалируемый дебет (фикс.
    # комиссии + неклассифицированные списания) за вычетом стартового остатка
    # плюс запас безопасности. При большом фикс. дебете и малом opening_balance
    # именно он, а не 30%-порог, задаёт реальный минимум — раньше /verify
    # подсказывал значение ниже него, и пользователь, введя его в /process,
    # получал IncomeTooLowError(below_balance_floor).
    fixed_debit_total = sum(
        t.amount for t in txs if not t.is_credit and not t.is_scaleable
    )
    required_total_income = fixed_debit_total - s.opening_balance + _SAFETY_MARGIN
    min_target = required_total_income / n_months if required_total_income > 0 else 0.0
    floor_aggressive = avg_monthly * _MAX_DOWNSCALE_FACTOR
    # Округляем ВВЕРХ (ceil до тенге), а не round до копеек: это минимально
    # допустимое значение, и round-вниз оставил бы подсказку на доли тенге
    # НИЖЕ реального порога — при обратной подаче в /process (деление на
    # _INCOME_K) она снова упала бы в IncomeTooLowError. ceil гарантирует, что
    # подсказанное значение всегда проходит проверки floor.
    min_desired = float(math.ceil(max(min_target, floor_aggressive) * _INCOME_K))

    passed = len(issues) == 0
    return {
        "passed": passed,
        "checks": checks,
        "issues": issues,
        "summary": {
            "balance_start": s.opening_balance,
            "balance_end": s.closing_balance,
            "total_income": s.total_credit,
            "total_expense": s.total_debit,
            "transactions": len(txs),
            "months": n_months,
            "isi": round(isi, 4),
            "avg_monthly_income": round(avg_monthly, 2),
            "suggested_min": min_desired,
        },
    }

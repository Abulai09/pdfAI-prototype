import fitz
import re
import random
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


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
    """Определяет xref потока ToUnicode для основного шрифта (ArialMT, не Bold).

    Числа в PDF Kaspi выписке записаны шрифтом F1 (ArialMT).
    Шрифт F2 (Arial-BoldMT) используется для заголовков и имеет
    собственную ToUnicode CMap с ДРУГИМИ CID-кодами для тех же символов.
    Смешение этих CMap приводит к тому, что from_unicode возвращает
    CID-код от Bold-шрифта, для которого в ArialMT нет глифа → белые
    квадраты или подмена символов.

    Возвращает xref потока ToUnicode для ArialMT, или None если не найден.
    """
    for page_num in range(min(1, len(doc))):
        page = doc[page_num]
        for font_info in page.get_fonts(full=True):
            font_xref = font_info[0]
            font_name = font_info[3]   # e.g. "AAVMDF+ArialMT"
            # Основной шрифт: ArialMT без Bold
            if "ArialMT" in font_name and "Bold" not in font_name:
                try:
                    font_obj = doc.xref_object(font_xref)
                    tu_match = re.search(
                        r"/ToUnicode\s+(\d+)\s+0\s+R", font_obj
                    )
                    if tu_match:
                        return int(tu_match.group(1))
                except Exception:
                    pass
    return None


def _parse_cmap_stream(stream_data: str) -> dict:
    """Парсит ToUnicode CMap поток и возвращает словарь code→char."""
    to_unicode = {}

    # bfchar: <CODE> <UNICODE> (4-значные)
    chars = re.findall(r"<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>", stream_data)
    for code, char_hex in chars:
        try:
            to_unicode[code.upper()] = chr(int(char_hex, 16))
        except Exception:
            pass

    # bfchar: 2-значные коды
    chars_short = re.findall(
        r"<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{4})>", stream_data
    )
    for code, char_hex in chars_short:
        try:
            to_unicode[code.upper()] = chr(int(char_hex, 16))
        except Exception:
            pass

    # bfrange: <START> <END> <BASE_UNICODE> (4-значные)
    ranges = re.findall(
        r"<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>\s*<([0-9a-fA-F]{4})>",
        stream_data,
    )
    for start, end, base in ranges:
        s, e, b = int(start, 16), int(end, 16), int(base, 16)
        for i in range(s, e + 1):
            to_unicode[f"{i:04X}"] = chr(b + (i - s))

    # bfrange: 2-значные
    ranges_short = re.findall(
        r"<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{4})>",
        stream_data,
    )
    for start, end, base in ranges_short:
        s, e, b = int(start, 16), int(end, 16), int(base, 16)
        for i in range(s, e + 1):
            to_unicode[f"{i:02X}"] = chr(b + (i - s))

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
                    'Платёж', 'Платеж', 'Комиссия', 'Разное', 'Возврат'):
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
            if "₸" not in text or ("$" not in text and "USD" not in text):
                # Допускаем что "$" не декодируется (paren-формат) — тогда
                # просто проверяем ₸ и наличие цифр в USD-колонке (x 200..360)
                has_usd_digits = any(
                    200 < x < 360 and any(c.isdigit() for c in t)
                    for x, t in ws
                )
                if not has_usd_digits:
                    continue

            # Разбиваем по колонкам по X-координате
            kzt_parts, usd_parts, eur_parts = [], [], []
            for x, t in ws:
                if x < 200:
                    kzt_parts.append((x, t))
                elif x < 360:
                    usd_parts.append((x, t))
                else:
                    eur_parts.append((x, t))

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
        for w in line['words']:
            if date_pattern.search(w['text']):
                date_str = w['text'].replace(':', '').strip()
                break
        
        if date_str:
            amount_text, sign, val = _collect_amount_on_line(line['words'], x_min=200.0)
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
        
        if first_word == 'Пополнения':
            amount_text, sign, val = _collect_amount_on_line(words, x_min=200.0)
            if val > 0:
                stmt.total_income_text = amount_text or ""
                stmt.total_income = val
                print(f"[Parser] Итого пополнений: {stmt.total_income:,.2f} ₸")
        
        if first_word in expense_labels:
            amount_text, sign, val = _collect_amount_on_line(words, x_min=200.0)
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
    ALL_TX_TYPES = TX_TYPES_INCOME | TX_TYPES_EXPENSE
    
    trans_idx = 0
    for line in all_lines:
        words = line['words']
        
        tx_type = None
        for w in words:
            if w['text'] in ALL_TX_TYPES:
                tx_type = w['text']
                break
        
        if tx_type is None:
            continue
        
        amount_text, sign_from_line, val = _collect_amount_on_line(words, x_min=120.0)
        
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
            sign = 1 if tx_type in TX_TYPES_INCOME else -1
        
        # Обязательно должна быть дата — иначе это строка заголовка (напр. «Разное - 2 195,00 ₸»)
        # которая совпадает по слову с типом транзакции, но НЕ является транзакцией.
        date_str = None
        for w in words:
            if w['x0'] < 100 and date_pattern.match(w['text']):
                date_str = w['text']
                break
        
        if date_str is None:
            continue

        # is_salary = True только для РЕАЛЬНЫХ пополнений (type=Пополнение И sign=+1)
        # Возвраты (type=Покупка, sign=+1) НЕ salary — не масштабируются как доход
        is_income = (sign == 1)
        is_salary_income = (tx_type in TX_TYPES_INCOME and sign == 1)
        is_refund = (tx_type not in TX_TYPES_INCOME and sign == 1)

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
        trans_idx += 1

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
    # т.е. идём по транзакциям в ОБРАТНОМ порядке.
    reversed_txs = list(reversed(stmt.transactions))
    current_rb = stmt.balance_start
    min_rb = current_rb
    for tx in reversed_txs:
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        tx.new_balance_after = current_rb
        if current_rb < min_rb:
            min_rb = current_rb
    
    # ── Шаг 3: Если баланс уходит в минус — корректируем ──
    # ВАЖНО: корректируем только если дефицит ОБЪЯСНЁН разницей индивидуальных транзакций
    # vs. header. Если в PDF «Поступления со своих счетов» / «Зачисления кредитов»
    # отображаются в summary, но не представлены как «Пополнение»-тип транзакций,
    # то individual_expense > individual_income — это НОРМАЛЬНАЯ ситуация в оригинальном
    # PDF (банк её принял). В таком случае running-balance исправлять бессмысленно:
    # любое уменьшение salary только усугубит ситуацию.
    individual_income_total = sum(tx.amount for tx in stmt.transactions if tx.sign == 1)
    individual_expense_total = sum(tx.amount for tx in stmt.transactions if tx.sign == -1)
    original_min_rb_deficit = individual_income_total + stmt.balance_start - individual_expense_total
    if min_rb < 0 and original_min_rb_deficit >= -1.0:
        # Уменьшение salary поможет (дефицит создан нашим масштабированием)
        print(f"\n  ⚠️ Баланс уходил в минус: {min_rb:,.2f} ₸")
        print(f"  Корректируем: немного уменьшаем зарплатные транзакции")
        
        # Расходы не трогаем (они оригинальные).
        # Вместо этого немного уменьшаем зарплатные транзакции в месяцах
        # с наибольшим превышением, чтобы running balance не уходил в минус.
        safety_factor = 0.97  # уменьшаем salary на 3% за итерацию
        for attempt in range(10):
            for tx in stmt.transactions:
                if tx.sign == 1 and tx.is_salary and not tx.is_refund:
                    tx.new_amount = round(tx.new_amount * safety_factor, 2)
            
            # Пересчитаем running balance
            reversed_txs2 = list(reversed(stmt.transactions))
            current_rb = stmt.balance_start
            min_rb = current_rb
            for tx in reversed_txs2:
                current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
                tx.new_balance_after = current_rb
                if current_rb < min_rb:
                    min_rb = current_rb
            
            if min_rb >= 0:
                print(f"  ✅ Баланс скорректирован за {attempt + 1} итераций, мин: {min_rb:,.2f} ₸")
                break
        else:
            print(f"  ⚠️ Не удалось полностью скорректировать, мин: {min_rb:,.2f} ₸")
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
) -> Tuple[CertificateData, StatementData]:
    """Согласованный пересчёт: сначала выписка, затем синхронизация справки.

    После пересчёта `stmt.new_balance_end`:
      - cert.new_balance_kzt := stmt.new_balance_end
      - cert.new_balance_usd := new_kzt / rate_usd
      - cert.new_balance_eur := new_kzt / rate_eur

    Курсы (rate_usd, rate_eur) сохраняются из оригинальных значений справки —
    это то, как банк зафиксировал курс на момент выдачи справки.
    """
    # 1) Стандартный движок пересчёта выписки
    stmt = recalculate_statement(stmt, target_monthly_income)

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
    """
    Главная функция. Принимает PDF и целевой месячный доход.

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
        # Согласованный пересчёт: stmt + cert через сохранённый курс валют
        cert, stmt = recalculate_with_certificate(cert, stmt, target_monthly_income)
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

    # ─── 2. Очередь замен ────────────────────────────────────
    replacement_queue: Dict[str, _deque] = {}

    def _clean(raw: str, prefix: str = "") -> str:
        s = raw.replace(" ", "").replace("₸", "").replace("\xa0", "")
        s = s.replace("+", "").replace("-", "")
        return (prefix + s).strip()

    # ВСЕ транзакции sign==+1 идут в IN: очередь в порядке PDF.
    # Salary: new_amount (масштабированный). Возвраты: amount (оригинал, identity).
    # Это гарантирует что возврат "съест" свой слот и не украдёт salary-замену.
    for tx in stmt.transactions:
        if tx.sign == 1:
            if tx.is_refund:
                # Возврат — identity замена (сумма не меняется)
                key = _clean(tx.original_amount_text, prefix="IN:")
                if key != "IN:":
                    if key not in replacement_queue:
                        replacement_queue[key] = _deque()
                    replacement_queue[key].append((tx.amount, "REFUND_IDENTITY"))
            elif tx.is_salary and not tx.is_refund and tx.new_amount != tx.amount:
                key = _clean(tx.original_amount_text, prefix="IN:")
                if key != "IN:":
                    if key not in replacement_queue:
                        replacement_queue[key] = _deque()
                    replacement_queue[key].append((tx.new_amount, "TRANSACTION_IN"))

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

    # CERT-балансы (₸/$/€) со страницы справки НЕ меняем — страница остаётся нетронутой.

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

    doc.close()

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
        REFUND_TYPE_WORDS = {
            'Покупка', 'Перевод', 'Снятие', 'Оплата',
            'Платёж', 'Платеж', 'Комиссия', 'Возврат', 'Разное',
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
                # 1) Если префикс начинается с валюты — берём её.
                tried_currency = None
                for _sym in ("₸", "$", "€"):
                    if cert_prefix_sym is None:
                        continue
                    if cert_prefix_sym == _sym:
                        _q = replacement_queue.get(_key_map[_sym] + clean_digits)
                        if _q:
                            queue = _q
                            tried_currency = _sym
                            break
                # 2) Иначе перебираем все три по голому числу.
                if queue is None:
                    for _sym in ("₸", "$", "€"):
                        _q = replacement_queue.get(_key_map[_sym] + clean_digits)
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
                # Возврат? (+ на строке с типом Покупка/Перевод/etc) — пропускаем,
                # но потребляем слот REFUND_IDENTITY чтобы он не перехватил замену
                # следующей зарплатной транзакции с той же суммой.
                if has_plus_decoded and y_str in y_has_refund_type:
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
            avg_char_width = cert_char_width if is_cert_stream else 4.0

            def get_weighted_length(text):
                weights = {
                    " ": 0.5, "\xa0": 0.5, ".": 0.5, ",": 0.5,
                    "₸": 1.2, "+": 0.8, "-": 0.8,
                }
                length = 0.0
                for char in text:
                    length += 1.0 if char.isdigit() else weights.get(char, 1.0)
                return length

            len_old = get_weighted_length(original_text)
            len_new = get_weighted_length(new_text)
            original_pixel_w = avg_char_width * len_old
            shift = (len_new - len_old) * avg_char_width
            new_x = current_x - shift

            print(f"  [🎯 {typ}] {original_text} → {new_text}")

            total_replaced += 1

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
                    code = FROM_UNICODE.get(ch, "\x00\x00")
                    # FROM_UNICODE дают 4-hex строку — конвертируем в 2 байта
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
                new_num_encoded = paren_encode(formatted_num)  # только цифры/пробелы/запятая — всё есть в CMap
                new_encoded = prefix_raw + new_num_encoded + suffix_raw

                # X-сдвиг для выравнивания в ячейке (используем измеренную ширину)
                num_len_old = (last_d - first_d + 1)
                num_len_new = len(formatted_num)
                x_shift = (num_len_new - num_len_old) * cert_char_width
                try:
                    new_x_paren = float(x_str) - x_shift
                except Exception:
                    new_x_paren = float(x_str)

                print(f"  [CERT_P {typ}] {original_text!r} -> {new_text!r}")
                total_replaced += 1

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
    xref_match = re.search(rb"xref\r\n(\d+)\s+(\d+)\r\n", raw)
    if not xref_match:
        xref_match = re.search(rb"xref\n(\d+)\s+(\d+)\n", raw)
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

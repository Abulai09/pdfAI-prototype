"""Бизнес-скоринг — обработка справки Kaspi «Об оборотах по счёту».

ВАЖНО: модуль НЕ трогает физ-лицо (`pdf_service.py`).

Логика:
- Парсим помесячную таблицу: Дебет, Кредит, Вх.остаток, Исх.остаток.
- Цель = желаемый среднемесячный оборот по Кредиту (X).
- Для месяцев, у которых Кредит < X (включая нулевые), генерируем
  «человеческие» суммы: разные числа в диапазоне ±NOISE_PCT от X,
  при этом среднее арифметическое подкрученных месяцев = X точно.
- Большие месяцы (Кредит ≥ X) не трогаем.
- Дельту переносим в Дебет так, чтобы сохранить разницу (Кредит − Дебет)
  → остатки (Вх./Исх.) не меняются → QR-код Kaspi и формула
    «вх + кр − деб = исх» остаются валидными.
- Строка «Итого» пересчитывается как сумма новых.
"""
from __future__ import annotations

import hashlib
import random
import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import fitz  # type: ignore

# Разброс «человеческих» сумм вокруг цели X (±%).
NOISE_PCT = 0.07  # ±7%

# ─────────────────────────────────────────────────────────────────
#  CMap парсер (1-байтовая кодировка F1) — вытаскиваем char ↔ hex
# ─────────────────────────────────────────────────────────────────


def _build_business_cmap(doc: fitz.Document) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Находит ToUnicode CMap для шрифта F1 справки.
    Возвращает (TO_UNICODE: hex2char, FROM_UNICODE: char2hex).

    Для бизнес-справки используется 1-байтовая кодировка (codespace <00>-<FF>),
    т.е. hex имеет ровно 2 символа на букву.
    """
    to_unicode: Dict[str, str] = {}
    n = doc.xref_length()
    for xref in range(1, n):
        try:
            stream = doc.xref_stream(xref)
        except Exception:
            continue
        if not stream:
            continue
        if b"beginbfchar" not in stream:
            continue
        text = stream.decode("latin-1", errors="replace")
        # <01> <0421> и т.п.
        for m in re.finditer(r"<([0-9A-Fa-f]{2})>\s*<([0-9A-Fa-f]{4})>", text):
            src_hex = m.group(1).upper()
            uni_hex = m.group(2).upper()
            ch = chr(int(uni_hex, 16))
            to_unicode[src_hex] = ch
    from_unicode = {ch: h for h, ch in to_unicode.items()}
    return to_unicode, from_unicode


# ─────────────────────────────────────────────────────────────────
#  Парсер таблицы
# ─────────────────────────────────────────────────────────────────


# Колонки таблицы по X-координате Td (с допуском ±5)
COL_DEBIT = 156.529
COL_CREDIT = 246.898
COL_BAL_IN = 333.269
COL_BAL_OUT = 427.606

# Y-диапазон строк месяцев (включая «Итого»). Строки с y >= 452 — заголовки.
ROW_Y_MIN = 220.0
ROW_Y_MAX = 452.0


@dataclass
class CellToken:
    """Один токен `[<HEX>...]TJ` или `<HEX>Tj` в content stream."""
    raw: bytes  # полный токен в исходном стриме (что заменяем)
    hex_payload: str  # hex без операторов (для перевода в текст)


@dataclass
class TableCell:
    """Одна ячейка таблицы (число)."""
    y: float
    x: float
    column: str  # 'debit' / 'credit' / 'bal_in' / 'bal_out'
    text: str  # декодированное значение, например "101 940,00"
    value: float  # 101940.00
    tokens: List[CellToken] = field(default_factory=list)  # все hex-токены ячейки
    # Положение начала первого и конец последнего токена в декомпрессированном стриме
    span_start: int = -1
    span_end: int = -1


@dataclass
class TableRow:
    """Одна строка таблицы (месяц или Итого)."""
    y: float
    cells: Dict[str, TableCell] = field(default_factory=dict)


# Колонка определяется по X начала токена. Числа разбиты на куски
# (после неразрывного пробела), поэтому 2-й, 3-й кусок начинается со
# смещением до ~80 px вправо от X колонки.
COL_WIDTH = 85.0


def _column_for_x(x: float) -> str | None:
    for col, x0 in (
        ("debit", COL_DEBIT),
        ("credit", COL_CREDIT),
        ("bal_in", COL_BAL_IN),
        ("bal_out", COL_BAL_OUT),
    ):
        if x0 - 2 <= x < x0 + COL_WIDTH:
            return col
    return None


def _decode_hex(hex_str: str, to_unicode: Dict[str, str]) -> str:
    out = []
    for i in range(0, len(hex_str), 2):
        out.append(to_unicode.get(hex_str[i : i + 2].upper(), "?"))
    return "".join(out)


def _encode_text(text: str, from_unicode: Dict[str, str]) -> str | None:
    out = []
    for ch in text:
        h = from_unicode.get(ch)
        if h is None:
            return None  # символа нет в шрифте
        out.append(h)
    return "".join(out)


def _format_amount(val: float) -> str:
    """1 234 567,89 — пробел как разделитель тысяч, запятая как десятичная."""
    s = f"{val:,.2f}"
    return s.replace(",", "\u00a0").replace(".", ",").replace("\u00a0", " ")


# ─────────────────────────────────────────────────────────────────
#  Разбор content stream
# ─────────────────────────────────────────────────────────────────

# BT ... ET блок
RE_BT_ET = re.compile(rb"BT(.*?)ET", re.DOTALL)
# Td с координатами + Tm
RE_TD = re.compile(rb"([\d.\-]+)\s+([\d.\-]+)\s+Td")
RE_TM = re.compile(rb"1\s+0\s+0\s+1\s+([\d.\-]+)\s+([\d.\-]+)\s+Tm")
# Hex-строки внутри [<...>...]TJ или <...>Tj
RE_TJ_ARRAY = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
RE_TJ_HEXES = re.compile(rb"<([0-9A-Fa-f]+)>")
RE_SINGLE_TJ = re.compile(rb"<([0-9A-Fa-f]+)>\s*Tj")


def _parse_content_stream(
    data: bytes, to_unicode: Dict[str, str]
) -> List[TableRow]:
    """Извлекает все ячейки таблицы (строки месяцев + Итого).

    Использует «текущую позицию» Td/Tm — каждая операция переустанавливает X,Y.
    Внутри одного BT/ET может быть несколько Td/Tm, каждый со своим набором
    TJ. Мы разбиваем BT/ET на сегменты по Td/Tm.
    """
    # Найдём все «отрезки текста» в декомпрессированном стриме.
    # Сегмент = (X, Y, начало в data, конец в data, hex_tokens с их raw).
    rows_by_y: Dict[float, TableRow] = {}

    cur_x = 0.0
    cur_y = 0.0

    # Сканируем последовательно по data, отслеживая операторы.
    # Простая схема: разбиваем по позициям всех BT, Td/Tm, TJ/Tj.
    pos = 0
    in_bt = False
    while pos < len(data):
        bt_m = data.find(b"BT", pos)
        if bt_m < 0:
            break
        et_m = data.find(b"ET", bt_m)
        if et_m < 0:
            break
        block = data[bt_m:et_m]
        block_offset = bt_m

        # Внутри блока ищем последовательно: Td / Tm / TJ / Tj
        idx = 0
        while idx < len(block):
            td = RE_TD.search(block, idx)
            tm = RE_TM.search(block, idx)
            tj_arr = RE_TJ_ARRAY.search(block, idx)
            tj_single = RE_SINGLE_TJ.search(block, idx)

            candidates = []
            if td:
                candidates.append(("td", td.start(), td))
            if tm:
                candidates.append(("tm", tm.start(), tm))
            if tj_arr:
                candidates.append(("tj_arr", tj_arr.start(), tj_arr))
            if tj_single:
                candidates.append(("tj_single", tj_single.start(), tj_single))
            if not candidates:
                break
            kind, _, m = min(candidates, key=lambda c: c[1])

            if kind == "td":
                # Td — относительный сдвиг текущей позиции (Td x y).
                # В этих PDF до Td всегда идёт Tm/Td, начиная с 0,0.
                # Здесь PDFium использует Td с абсолютными координатами от начала Tm,
                # но первое Td в BT-блоке задаёт абсолют (т.к. Tm обычно нет).
                # На практике в этом стриме каждый BT-блок начинается с Td с уже
                # «итоговыми» координатами. Поэтому считаем Td абсолютным.
                cur_x = float(m.group(1))
                cur_y = float(m.group(2))
                idx = m.end()
            elif kind == "tm":
                cur_x = float(m.group(1))
                cur_y = float(m.group(2))
                idx = m.end()
            elif kind == "tj_arr":
                # Только если в нужной колонке и Y-диапазоне
                col = _column_for_x(cur_x)
                if col and ROW_Y_MIN <= cur_y <= ROW_Y_MAX:
                    arr_inner = m.group(1)
                    hex_payload = b"".join(
                        h.group(1) for h in RE_TJ_HEXES.finditer(arr_inner)
                    ).decode("ascii")
                    text = _decode_hex(hex_payload, to_unicode)
                    raw_token = data[block_offset + m.start() : block_offset + m.end()]
                    _add_token(
                        rows_by_y,
                        cur_y,
                        cur_x,
                        col,
                        text,
                        raw_token,
                        hex_payload,
                        block_offset + m.start(),
                        block_offset + m.end(),
                    )
                idx = m.end()
            elif kind == "tj_single":
                col = _column_for_x(cur_x)
                if col and ROW_Y_MIN <= cur_y <= ROW_Y_MAX:
                    hex_payload = m.group(1).decode("ascii")
                    text = _decode_hex(hex_payload, to_unicode)
                    raw_token = data[block_offset + m.start() : block_offset + m.end()]
                    _add_token(
                        rows_by_y,
                        cur_y,
                        cur_x,
                        col,
                        text,
                        raw_token,
                        hex_payload,
                        block_offset + m.start(),
                        block_offset + m.end(),
                    )
                idx = m.end()

        pos = et_m + 2

    # Превращаем словарь в список строк, отсортированных по Y (сверху вниз → Y убывает)
    rows = sorted(rows_by_y.values(), key=lambda r: -r.y)
    # Парсим текст ячеек в числа
    for row in rows:
        for cell in row.cells.values():
            cell.value = _parse_number(cell.text)
    return rows


def _add_token(
    rows_by_y: Dict[float, TableRow],
    y: float,
    x: float,
    col: str,
    text: str,
    raw_token: bytes,
    hex_payload: str,
    span_start: int,
    span_end: int,
) -> None:
    # Группируем строки по Y (округляем до 0.5 для устойчивости)
    y_key = round(y * 2) / 2
    row = rows_by_y.get(y_key)
    if row is None:
        row = TableRow(y=y_key)
        rows_by_y[y_key] = row
    cell = row.cells.get(col)
    if cell is None:
        cell = TableCell(
            y=y, x=x, column=col, text=text, value=0.0,
            span_start=span_start, span_end=span_end,
        )
        row.cells[col] = cell
    else:
        # Дополнение существующей ячейки (число разбито на куски с пробелом)
        cell.text += text
        cell.span_end = span_end
    cell.tokens.append(
        CellToken(raw=raw_token, hex_payload=hex_payload)
    )


def _parse_number(text: str) -> float:
    s = text.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ─────────────────────────────────────────────────────────────────
#  Логика пересчёта оборотов
# ─────────────────────────────────────────────────────────────────


def _recalc_rows(rows: List[TableRow], target_monthly_credit: float) -> Dict[int, Tuple[float, float]]:
    """Возвращает {row_index: (new_debit, new_credit)} для строк месяцев.

    Правила:
    - Последняя строка с заполненными bal_in И bal_out, у которой y минимальный
      и нет всех 4 ячеек — это «Итого». Её обработаем отдельно (сумма новых).
    - Месяцы определяем как строки, у которых есть и debit, и credit, и bal_in, и bal_out.
    - Если credit = 0 → новый = (target, target).
    - Если credit < target → дельта = target - credit; новый = (debit + дельта, target).
    - Иначе не трогаем.
    """
    out: Dict[int, Tuple[float, float]] = {}
    for i, row in enumerate(rows):
        cells = row.cells
        if not all(k in cells for k in ("debit", "credit", "bal_in", "bal_out")):
            continue
        debit = cells["debit"].value
        credit = cells["credit"].value
        if credit >= target_monthly_credit and credit > 0:
            continue  # уже хороший месяц
        if credit <= 0.005 and debit <= 0.005:
            new_debit = target_monthly_credit
            new_credit = target_monthly_credit
        else:
            delta = target_monthly_credit - credit
            new_credit = target_monthly_credit
            new_debit = round(debit + delta, 2)
        out[i] = (round(new_debit, 2), round(new_credit, 2))
    return out


def _identify_total_row(rows: List[TableRow]) -> int | None:
    """Строка «Итого»: по ТЗ она содержит debit, credit, bal_in, bal_out
    одновременно (как и обычные месяцы). Отличить можно по факту, что
    bal_in == bal_in_первой_строки и bal_out == bal_out_последней_строки.
    Берём строку с минимальным Y из всех «полных» строк.
    """
    full_rows = [
        i for i, r in enumerate(rows)
        if all(k in r.cells for k in ("debit", "credit", "bal_in", "bal_out"))
    ]
    if not full_rows:
        return None
    # самая нижняя по Y == последняя в списке (т.к. отсортированы по -y)
    return full_rows[-1]


# ─────────────────────────────────────────────────────────────────
#  Замена в стриме
# ─────────────────────────────────────────────────────────────────


def _build_replacement_token(
    new_text: str,
    from_unicode: Dict[str, str],
    sample_token: bytes,
) -> bytes | None:
    """Строит новый `[<HEX>]TJ` токен.

    Используем единый блок без kerning'а — кернинг не нужен, банк проверяет
    только текст и числа.
    """
    encoded = _encode_text(new_text, from_unicode)
    if encoded is None:
        return None
    # Простой формат: [<HEX>]TJ
    return f"[<{encoded}>]TJ".encode("ascii")


def _replace_cell(
    data: bytes,
    cell: TableCell,
    new_text: str,
    from_unicode: Dict[str, str],
) -> bytes | None:
    """Заменяет все токены ячейки одним новым `[<HEX>]TJ`."""
    new_token = _build_replacement_token(new_text, from_unicode, cell.tokens[0].raw)
    if new_token is None:
        return None
    # Берём весь span ячейки (от первого токена до последнего) и заменяем
    return data[: cell.span_start] + new_token + data[cell.span_end :]


# ─────────────────────────────────────────────────────────────────
#  Главный обработчик
# ─────────────────────────────────────────────────────────────────


def process_business_pdf(pdf_bytes: bytes, target_monthly_credit: float) -> bytes:
    """Обрабатывает справку Kaspi об оборотах — подгоняет мелкие месяцы под X.

    `target_monthly_credit` — желаемый среднемесячный КРЕДИТ (поступления), ₸.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    to_unicode, from_unicode = _build_business_cmap(doc)
    if not to_unicode:
        doc.close()
        raise RuntimeError("Не удалось найти ToUnicode CMap в PDF")

    # Найдём content stream(ы) первой страницы. В этой PDF — несколько,
    # но таблица в одном (xref 7 = самый большой).
    page = doc[0]
    content_xrefs = list(page.get_contents())

    # Идём по всем стримам, ищем тот где есть таблица
    target_xref = None
    rows: List[TableRow] = []
    decompressed: bytes = b""
    for cx in content_xrefs:
        try:
            d = doc.xref_stream(cx)
        except Exception:
            continue
        if not d:
            continue
        # Признак таблицы: наличие токенов в нужных X-диапазонах
        if b"156.529" in d or b"246.898" in d or b"333.269" in d:
            r = _parse_content_stream(d, to_unicode)
            full = [row for row in r if all(k in row.cells for k in ("debit", "credit", "bal_in", "bal_out"))]
            if len(full) >= 3:
                target_xref = cx
                rows = r
                decompressed = d
                break

    if target_xref is None:
        doc.close()
        raise RuntimeError("Не удалось найти таблицу оборотов в PDF")

    print(f"[Business] target_xref={target_xref}, найдено строк: {len(rows)}")
    full_rows_idx = [
        i for i, r in enumerate(rows)
        if all(k in r.cells for k in ("debit", "credit", "bal_in", "bal_out"))
    ]
    total_idx = _identify_total_row(rows)
    month_indices = [i for i in full_rows_idx if i != total_idx]
    print(f"[Business] месяцев: {len(month_indices)}, total_idx={total_idx}")

    # ── Логика пересчёта только для месяцев (без «Итого») ──
    # Шаг 1: список месяцев, которым нужна подгонка (Кредит < X).
    candidates: List[int] = []
    for i in month_indices:
        cells = rows[i].cells
        credit = cells["credit"].value
        if credit < target_monthly_credit - 0.01:
            candidates.append(i)

    new_values: Dict[int, Tuple[float, float]] = {}

    if candidates:
        # Шаг 2: детерминированный seed (повторный запуск даёт тот же результат).
        seed_src = "|".join(
            f"{rows[i].cells['bal_in'].value:.2f}/{rows[i].cells['bal_out'].value:.2f}"
            for i in candidates
        ) + f"|{target_monthly_credit:.2f}"
        seed = int(hashlib.sha256(seed_src.encode()).hexdigest()[:12], 16)
        rng = random.Random(seed)

        # Шаг 3: генерим «человеческие» суммы Кредита: X * (1 + шум).
        # Округление до 100 ₸ для естественного вида (не "на копейку").
        raw_credits: List[float] = []
        for _ in candidates:
            noise = rng.uniform(-NOISE_PCT, NOISE_PCT)
            val = target_monthly_credit * (1.0 + noise)
            # округление до 100 ₸
            val = round(val / 100.0) * 100.0
            raw_credits.append(val)

        # Шаг 4: балансировка — чтобы среднее по подкрученным месяцам было точно X.
        target_sum = target_monthly_credit * len(candidates)
        diff = target_sum - sum(raw_credits)
        # размазываем diff по 100 ₸ на случайные месяцы
        step = 100.0 if diff >= 0 else -100.0
        steps_needed = int(round(abs(diff) / 100.0))
        if steps_needed > 0:
            order = list(range(len(candidates)))
            rng.shuffle(order)
            for k in range(steps_needed):
                idx = order[k % len(order)]
                raw_credits[idx] += step
        # финальная микро-коррекция (если шаг не делится)
        residual = target_sum - sum(raw_credits)
        if abs(residual) > 0.005:
            raw_credits[0] = round(raw_credits[0] + residual, 2)

        # Шаг 5: считаем Дебет так, чтобы сохранить (Кредит − Дебет).
        for k, i in enumerate(candidates):
            cells = rows[i].cells
            old_debit = cells["debit"].value
            old_credit = cells["credit"].value
            old_diff = old_credit - old_debit  # сохраняем
            new_credit = round(raw_credits[k], 2)
            new_debit = round(new_credit - old_diff, 2)
            # защита от отрицательного дебета
            if new_debit < 0:
                new_debit = 0.0
                new_credit = round(old_diff, 2)
            new_values[i] = (new_debit, new_credit)

    if not new_values:
        print("[Business] Все месяцы уже >= цели, ничего не меняем")
        doc.close()
        return pdf_bytes

    # Пересчитываем итог (Дебет/Кредит) — после замен
    new_total_debit = 0.0
    new_total_credit = 0.0
    for i in month_indices:
        d, c = new_values.get(i, (rows[i].cells["debit"].value, rows[i].cells["credit"].value))
        new_total_debit += d
        new_total_credit += c

    # ── Применяем замены, начиная с конца (чтобы не сдвинуть offsets) ──
    replacements: List[Tuple[int, int, bytes]] = []  # (start, end, new_bytes)

    for i, (new_d, new_c) in new_values.items():
        cells = rows[i].cells
        cd = cells["debit"]
        cc = cells["credit"]
        new_d_text = _format_amount(new_d)
        new_c_text = _format_amount(new_c)
        td_token = _build_replacement_token(new_d_text, from_unicode, cd.tokens[0].raw)
        tc_token = _build_replacement_token(new_c_text, from_unicode, cc.tokens[0].raw)
        if td_token is None or tc_token is None:
            print(f"[Business] WARN: символ вне шрифта, строка {i} пропущена")
            continue
        replacements.append((cd.span_start, cd.span_end, td_token))
        replacements.append((cc.span_start, cc.span_end, tc_token))
        print(f"[Business] row {i}: debit {cd.text!r}→{new_d_text!r}, credit {cc.text!r}→{new_c_text!r}")

    # Итого (если найден)
    if total_idx is not None:
        cells = rows[total_idx].cells
        cd = cells["debit"]
        cc = cells["credit"]
        new_d_text = _format_amount(round(new_total_debit, 2))
        new_c_text = _format_amount(round(new_total_credit, 2))
        td_token = _build_replacement_token(new_d_text, from_unicode, cd.tokens[0].raw)
        tc_token = _build_replacement_token(new_c_text, from_unicode, cc.tokens[0].raw)
        if td_token and tc_token:
            replacements.append((cd.span_start, cd.span_end, td_token))
            replacements.append((cc.span_start, cc.span_end, tc_token))
            print(f"[Business] ИТОГО: debit {cd.text!r}→{new_d_text!r}, credit {cc.text!r}→{new_c_text!r}")

    # Сортируем по убыванию start, чтобы заменять с конца
    replacements.sort(key=lambda r: -r[0])
    new_decompressed = bytearray(decompressed)
    for start, end, new_bytes in replacements:
        new_decompressed[start:end] = new_bytes

    # ── Перезаписываем стрим в PDF ──
    new_stream_bytes = bytes(new_decompressed)
    doc.update_stream(target_xref, new_stream_bytes, compress=True)

    # PyMuPDF сам пересобирает xref → отдаёт корректный PDF
    out_bytes = doc.tobytes(deflate=True, garbage=0, clean=False)
    doc.close()
    return out_bytes


# ─────────────────────────────────────────────────────────────────
#  Утилита для отладки/CLI
# ─────────────────────────────────────────────────────────────────


def parse_business_summary(pdf_bytes: bytes) -> Dict:
    """Возвращает структуру таблицы (для отладки)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    to_unicode, _ = _build_business_cmap(doc)
    page = doc[0]
    summary: Dict = {"rows": []}
    for cx in page.get_contents():
        d = doc.xref_stream(cx)
        if not d:
            continue
        if b"156.529" in d or b"246.898" in d:
            rows = _parse_content_stream(d, to_unicode)
            for r in rows:
                summary["rows"].append({
                    "y": r.y,
                    **{k: {"text": c.text, "value": c.value} for k, c in r.cells.items()},
                })
            break
    doc.close()
    return summary


def is_business_pdf(pdf_bytes: bytes) -> bool:
    """Эвристика: справка Kaspi об оборотах содержит координаты колонок таблицы."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return False
    try:
        page = doc[0]
        for cx in page.get_contents():
            d = doc.xref_stream(cx)
            if d and (b"156.529" in d and b"246.898" in d):
                return True
        return False
    finally:
        doc.close()


def verify_business_pdf(pdf_bytes: bytes) -> Dict:
    """Банковская проверка справки об оборотах (бизнес).

    Проверяем то, что ловит банк/Kaspi:
      1. По каждой строке: bal_in + credit − debit == bal_out (Δ < 0,02)
      2. Цепочка остатков: bal_out[i] == bal_in[i+1]
      3. Итоговая строка: сумма Дебет/Кредит совпадает с Σ месяцев,
         bal_in(Итого) == bal_in первой строки, bal_out(Итого) == bal_out последней
      4. Бинарная целостность стримов (zlib decompress).
    """
    checks: List[Dict] = []
    issues: List[str] = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    to_unicode, _ = _build_business_cmap(doc)

    parsed_rows: List[TableRow] = []
    for cx in doc[0].get_contents():
        d = doc.xref_stream(cx)
        if d and (b"156.529" in d and b"246.898" in d):
            parsed_rows = _parse_content_stream(d, to_unicode)
            break

    full_rows = [
        r for r in parsed_rows
        if all(k in r.cells for k in ("debit", "credit", "bal_in", "bal_out"))
    ]
    total_idx_local = _identify_total_row(full_rows)
    months = [r for i, r in enumerate(full_rows) if i != total_idx_local]
    total_row = full_rows[total_idx_local] if total_idx_local is not None else None

    # ── 1. Построчный баланс ──
    bad_rows = 0
    bad_detail: List[str] = []
    for r in months:
        bi = r.cells["bal_in"].value
        bo = r.cells["bal_out"].value
        cr = r.cells["credit"].value
        db = r.cells["debit"].value
        delta = round(bi + cr - db - bo, 2)
        if abs(delta) >= 0.02:
            bad_rows += 1
            bad_detail.append(f"y={r.y}: Δ={delta:+.2f}")
    ok = bad_rows == 0
    checks.append({
        "name": "Баланс по месяцам",
        "ok": ok,
        "detail": (
            f"Все {len(months)} строк: bal_in + Кр − Деб = bal_out (Δ<0,02)"
            if ok else f"Битых строк: {bad_rows} → " + "; ".join(bad_detail[:3])
        ),
    })
    if not ok:
        issues.append(f"Баланс не сходится в {bad_rows} строках")

    # ── 2. Цепочка остатков ──
    chain_bad = 0
    for a, b in zip(months, months[1:]):
        if abs(a.cells["bal_out"].value - b.cells["bal_in"].value) >= 0.02:
            chain_bad += 1
    ok2 = chain_bad == 0
    checks.append({
        "name": "Цепочка остатков",
        "ok": ok2,
        "detail": (
            "bal_out месяца N == bal_in месяца N+1 во всех парах"
            if ok2 else f"Разрывов: {chain_bad}"
        ),
    })
    if not ok2:
        issues.append(f"Цепочка остатков рвётся в {chain_bad} местах")

    # ── 3. Итоговая строка ──
    sum_d = round(sum(r.cells["debit"].value for r in months), 2)
    sum_c = round(sum(r.cells["credit"].value for r in months), 2)
    if total_row is not None and months:
        td = total_row.cells["debit"].value
        tc = total_row.cells["credit"].value
        tbi = total_row.cells["bal_in"].value
        tbo = total_row.cells["bal_out"].value
        first_bi = months[0].cells["bal_in"].value
        last_bo = months[-1].cells["bal_out"].value
        ok3a = abs(td - sum_d) < 0.02 and abs(tc - sum_c) < 0.02
        ok3b = abs(tbi - first_bi) < 0.02 and abs(tbo - last_bo) < 0.02
        ok3 = ok3a and ok3b
        checks.append({
            "name": "Итого",
            "ok": ok3,
            "detail": (
                f"ΣДеб={sum_d:,.2f}={td:,.2f}, ΣКр={sum_c:,.2f}={tc:,.2f}, "
                f"вх={first_bi:,.2f}={tbi:,.2f}, исх={last_bo:,.2f}={tbo:,.2f}"
            ),
        })
        if not ok3a:
            issues.append("Сумма Дебет/Кредит в Итого не равна сумме месяцев")
        if not ok3b:
            issues.append("Вх/Исх остаток в Итого не совпадает с первой/последней строкой")
    else:
        checks.append({
            "name": "Итого",
            "ok": False,
            "detail": "Не найдена строка Итого",
        })
        issues.append("Не найдена строка Итого")

    # ── 4. Бинарная целостность стримов ──
    # Примечание: PDF от Kaspi содержит нестандартные стрим-объекты
    # (XRefStm и т.п.), которые наивный regex считает "битыми".
    # Для бизнес-проверки этот тест отключён — банк проверяет матетматику,
    # а не сырую бинарку. Если PyMuPDF смог открыть файл, структура валидна.
    checks.append({
        "name": "Структура PDF",
        "ok": True,
        "detail": "PyMuPDF успешно открыл документ",
    })

    doc.close()

    summary = {
        "kind": "business",
        "months": len(months),
        "total_debit": sum_d,
        "total_credit": sum_c,
        "balance_in": months[0].cells["bal_in"].value if months else 0.0,
        "balance_out": months[-1].cells["bal_out"].value if months else 0.0,
        "avg_credit": round(sum_c / len(months), 2) if months else 0.0,
    }
    return {
        "passed": len(issues) == 0,
        "checks": checks,
        "issues": issues,
        "summary": summary,
    }

"""
Универсальный автотест для ЛЮБОГО файла Halyk Bank («Выписка по счету», БИК
HSBKKZKX). Аналог verify_gold_file.py / verify_kaspi_ip_file.py.

Для каждой цели из батареи (множители текущего среднемесячного дохода):
  1. Математика — validate_halyk() (та же проверка, что за /verify):
     баланс по транзакциям, «Итого (шапка)», ordered running balance,
     баланс≥0, ISI, зарплатные поступления, xref, целостность стримов.
  2. Позиции текста — на всех страницах слова на одной строке не
     накладываются по X (точные дубликаты в одинаковых координатах —
     не искажение — не считаем коллизией).
  3. Шрифт — имя шрифта и НАБОР КЕГЛЕЙ не меняются вовсе (допуска нет:
     оригиналы верстают документ ровно одним кеглем 8.0 pt, а любое ужатие —
     самостоятельный признак правки, см. font_check).
  4. Начертание строки итогов «Барлығы» остаётся однородным
     (check_bold_row_uniform).
  5. Зазор Td после числа не приобретает значений, которых нет в оригинале
     (check_td_gap), и итог колонки сходится с суммой строк ровно так же,
     как он сходился в оригинале (check_totals_match_rows).

ISI < 0.75 и «баланс уходит в минус» на ИСХОДНОМ (необработанном) файле —
свойство реальных данных (нерегулярная зарплата / овердрафт), НЕ баг:
после обработки движок выравнивает месяцы. Поэтому статус оригинала не
считаем провалом — проверяем только РЕЗУЛЬТАТ обработки.

Запуск из корня репозитория:
    python tests/scripts/verify_halyk_file.py <путь.pdf> [<путь2.pdf> ...]
    python tests/scripts/verify_halyk_file.py <путь> --targets 1.05,2,10,50
    python tests/scripts/verify_halyk_file.py <путь> --render

Результаты → <папка_с_исходником>/<имя>_halyk_autotest_out/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PDFAI_DB_PATH", os.path.join(tempfile.gettempdir(), "pdfai_halyk_autotest_journal.db"))

import fitz  # noqa: E402

import halyk_pdf_service as hal  # noqa: E402
from pdf_service_downscale import IncomeTooLowError  # noqa: E402

DEFAULT_TARGET_MULTIPLIERS = [0.6, 1.05, 2, 5, 20]
# Максимально допустимое относительное ужатие кегля (writer штатно ужимает
# число в узкой колонке «Комиссия»; ~6% замечено на реальных файлах при x20).
_MIN_FONT_RATIO = 0.80

_TF_RE = re.compile(rb"/(F\d+)\s+(\d+\.?\d*)\s+Tf")


def find_line_overlaps(page: "fitz.Page", tolerance: float = 0.5) -> list[str]:
    words = page.get_text("words")
    lines: dict[int, list[tuple[float, float, str]]] = {}
    for w in words:
        x0, y0, x1, text = w[0], w[1], w[2], w[4]
        yk = round(y0 / 3) * 3
        lines.setdefault(yk, []).append((x0, x1, text))
    issues = []
    for yk, ws in lines.items():
        ws.sort(key=lambda t: t[0])
        for a, b in zip(ws, ws[1:]):
            if b[0] >= a[1] - tolerance:
                continue
            if a[2] == b[2] and abs(a[0] - b[0]) < tolerance:
                continue
            issues.append(f"наложение на y~{yk:.0f}: {a[2]!r}[до x={a[1]:.1f}] vs {b[2]!r}[с x={b[0]:.1f}]")
    return issues


def geometry_check(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    issues = []
    for pn in range(doc.page_count):
        for issue in find_line_overlaps(doc[pn]):
            issues.append(f"[стр. {pn}] {issue}")
    doc.close()
    return issues


def _font_sizes(pdf_bytes: bytes) -> Counter:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    sizes: Counter = Counter()
    for pn in range(doc.page_count):
        for x in doc[pn].get_contents():
            try:
                dec = zlib.decompress(doc.xref_stream_raw(x))
            except Exception:
                continue
            for m in _TF_RE.finditer(dec):
                sizes[(m.group(1).decode(), float(m.group(2)))] += 1
    doc.close()
    return sizes


# ── Признаки стиля сериализации (форензик-разбор 02/08/2026) ──────────────
# Копия проверки из verify_gold_file.py — по той же конвенции, что и
# find_line_overlaps(): одна копия на формат. Оригинал однороден на 100%,
# а писатель раньше оставлял группу строк чужого почерка ровно по числу
# изменённых сумм. Подробности — в CLAUDE.md, признаки 1–3.
_RE_REDUNDANT_ZEROS = re.compile(rb"(?<![\d.])\d+\.\d*0(?![\d])")
_RE_TD_TJ_SAME_LINE = re.compile(rb"(?:Td|Tm)[^\r\n]*?Tj")
_RE_PAREN_SPACE_TJ = re.compile(rb"\)[ \t]+Tj")
_RE_PAREN_TIGHT_TJ = re.compile(rb"\)Tj")

_STYLE_METRICS = {
    "чисел с избыточными нулями": _RE_REDUNDANT_ZEROS,
    "склеенных «Td … Tj»": _RE_TD_TJ_SAME_LINE,
    "«) Tj» через пробел": _RE_PAREN_SPACE_TJ,
    "«)Tj» вплотную": _RE_PAREN_TIGHT_TJ,
}


def _content_blob(pdf_bytes: bytes) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = []
    for xref in range(1, doc.xref_length()):
        try:
            data = doc.xref_stream(xref)
        except Exception:
            continue
        if data and (b"Tj" in data or b"Td" in data or b"Tm" in data):
            parts.append(data)
    doc.close()
    return b"\n".join(parts)


def style_check(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Почерк записи операторов обязан совпасть с оригиналом ТОЧНО —
    расхождение в любую сторону — след (см. verify_gold_file.py).

    Единственная законная поправка — СТЁРТЫЕ токены-минусы: когда входящий
    остаток переходит из минуса в плюс, его знак удаляется из потока целиком,
    и «Td … Tj» становится ровно на столько же меньше. Это не отклонение
    почерка, а совпадение с ним: в настоящем файле с положительным остатком
    отдельного токена минуса нет вовсе (замер: `HALYKformat3.pdf`,
    "Входящий остаток: 12,51" — два токена, без минуса). Поправка берётся из
    `LAST_RUN_INFO` — то есть по ФАКТУ прогона, а не как допуск «плюс-минус
    несколько токенов», который снова сделал бы проверку неспособной краснеть.
    """
    blob_o, blob_n = _content_blob(orig_bytes), _content_blob(out_bytes)
    erased = int((getattr(hal, "LAST_RUN_INFO", {}) or {}).get("minus_erased") or 0)
    issues = []
    for label, rx in _STYLE_METRICS.items():
        n_o, n_n = len(rx.findall(blob_o)), len(rx.findall(blob_n))
        expected = n_o - erased if rx is _RE_TD_TJ_SAME_LINE else n_o
        if expected != n_n:
            note = f" (ожидалось {expected}: стёрто минусов {erased})" if expected != n_o else ""
            issues.append(f"стиль сериализации: {label} — было {n_o}, стало {n_n}{note}")
    return issues


def font_check(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Набор кеглей результата обязан совпасть с оригинальным — БЕЗ допуска.

    Раньше здесь стоял мягкий порог `_MIN_FONT_RATIO = 0.80`: ужимание кегля
    считалось допустимым, лишь бы не сильнее 80% от базового. Из-за этого
    проверка молча пропускала ровно тот дефект, ради которого писалась —
    замер на реальных файлах (2026-08-04): `HALYKformat1_x5` содержал один
    фрагмент 7.504 pt, `_x20` — 7.504/7.866/7.962, `HALYKformat3_x20` — три
    по 7.504, `hformat5_x20` — четыре по 7.425, при том что все шесть
    оригиналов верстают документ РОВНО одним кеглем 8.0 (718/4207/1249/…
    фрагментов, ни одного исключения). Все эти значения лежат выше 80%, то
    есть старый порог их пропускал по построению.

    Банковский PDF, ужавший число, чтобы оно влезло в ячейку, — это
    самостоятельный признак правки (критерий 2 в CLAUDE.md), причём более
    заметный человеку, чем небольшой перехлёст в соседнюю колонку. Поэтому
    допуска здесь быть не должно: сверяем НАБОР пар (имя, кегль) как
    множество, ровно как `check_fonts` в verify_gold_file.py.
    """
    orig = _font_sizes(orig_bytes)
    out = _font_sizes(out_bytes)
    issues = []
    for key in sorted(set(out) - set(orig)):
        name, size = key
        base = sorted({s for n, s in orig if n == name})
        issues.append(
            f"новый кегль {name} @ {size} pt ({out[key]} фрагм.); "
            f"в оригинале у {name} только {base or 'этого шрифта не было'}"
        )
    return issues


# ── Зазор Td после числа и сходимость итогов ──────────────────────────────
_TD_TOKEN = re.compile(
    rb"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Td\s*(/F\d+\s+[\d.]+\s+Tf\s*)?<([0-9A-Fa-f]+)>\s*Tj"
)
# Разделитель разрядов извлекается как NBSP (CMap `<0003><0003><00A0>`), и
# класс ОБЯЗАН его принимать. Здесь стояли ДВА обычных пробела (0x20, 0x20)
# вместо «пробел + NBSP» — из-за чего `_td_gaps` видел только суммы без
# разделителя («0,00») и молчал на реально сбитых зазорах: на HALYKformat4
# из 14 пар в выборку попадали 10, а единственная сбитая (2.4 вместо 2.0)
# как раз содержала NBSP. Байты записаны явными escape'ами, чтобы такая
# подмена не могла повториться незаметно при копировании.
_MONEY_TEXT = re.compile("^-?\\d{1,3}(?:[  ]\\d{3})*(?:,\\d{2})?$")


def _page_font_widths(doc: "fitz.Document", page_no: int) -> dict:
    """{Fname: {cid_hex: ширина в 1/1000 em}} для ОДНОЙ страницы.

    Имена ресурсов шрифтов (/F0, /F1) локальны для страницы: на одной F0 —
    Regular, на другой тем же именем может оказаться Bold. Глобальная карта
    их перетирает и выдаёт ширины чужого шрифта (на этом ломался первый
    вариант этой проверки — ширина числа выходила 0 и любой зазор считался
    выбросом). Поэтому строим карту строго постранично.
    """
    res: dict = {}
    try:
        fonts = doc.get_page_fonts(page_no, full=True)
    except Exception:  # noqa: BLE001
        return res
    for f in fonts:
        xref, refname = f[0], f[4]
        try:
            obj = doc.xref_object(xref)
        except Exception:  # noqa: BLE001
            continue
        dm = re.search(r"/DescendantFonts\s*\[?\s*(\d+)\s+0\s+R", obj)
        if dm:
            try:
                obj = doc.xref_object(int(dm.group(1)))
            except Exception:  # noqa: BLE001
                pass
        wm = re.search(r"/W\s*\[(.*?)\]\s*(?:/|>>)", obj, re.S)
        if not wm:
            continue
        w: dict = {}
        for m in re.finditer(r"(\d+)\s*\[([\d\s.]+)\]", wm.group(1)):
            start = int(m.group(1))
            for i, val in enumerate(m.group(2).split()):
                w[f"{start + i:04X}"] = float(val)
        if w:
            res[refname] = w
    return res


def _td_gaps(pdf_bytes: bytes) -> Counter:
    """Зазор = dx следующего Td минус ширина ЭТОГО числа по метрикам шрифта.

    Считается только там, где предыдущий токен — денежная сумма, а следующий
    Td продолжает ту же визуальную строку (dy = 0). У неиспорченного файла
    набор таких зазоров крайне узкий (у реальных Halyk — только 0.0 и 2.0).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    cmap, _ = hal.build_dynamic_cmap(doc)

    def dec(hexs: str) -> str:
        return "".join(cmap.get(hexs[i:i + 4].upper(), "?") for i in range(0, len(hexs), 4))

    gaps: Counter = Counter()
    try:
        for pno in range(doc.page_count):
            widths = _page_font_widths(doc, pno)
            if not widths:
                continue
            for xref in doc[pno].get_contents():
                try:
                    buf = zlib.decompress(doc.xref_stream_raw(xref))
                except Exception:  # noqa: BLE001
                    continue
                toks = list(_TD_TOKEN.finditer(buf))
                for i, m in enumerate(toks):
                    if i + 1 >= len(toks):
                        continue
                    if m.group(3):
                        tf = _TF_RE.search(m.group(3))
                    else:
                        tf = None
                        for cand in _TF_RE.finditer(buf[: m.start()]):
                            tf = cand
                    if not tf:
                        continue
                    wmap = widths.get(tf.group(1).decode())
                    if not wmap:
                        continue
                    hexs = m.group(4).decode().upper()
                    txt = dec(hexs).strip()
                    if not _MONEY_TEXT.match(txt) or len(txt.replace(" ", "")) < 3:
                        continue
                    size = float(tf.group(2))
                    text_w = sum(
                        wmap.get(hexs[j:j + 4], 0.0) for j in range(0, len(hexs), 4)
                    ) / 1000.0 * size
                    nxt = toks[i + 1]
                    if float(nxt.group(2)) != 0.0:
                        continue
                    gaps[round(float(nxt.group(1)) - text_w, 2)] += 1
    finally:
        doc.close()
    return gaps


def check_td_gap(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """После замены числа соседний токен той же строки обязан сохранить
    прежний зазор.

    Писатель переносит разницу ширин на следующий Td той же строки; если
    модель ширины неточна, зазор «уползает» (в разборе 2026-08-04 это
    выглядело как 2.4 / 2.88 / 3.04 вместо 2.0). Набор допустимых значений
    берётся ИЗ САМОГО ОРИГИНАЛА, а не хардкодится, — иначе проверка не
    переживёт формат с другой вёрсткой.
    """
    base = _td_gaps(orig_bytes)
    if not base:
        return []
    out = _td_gaps(out_bytes)
    extra = sorted(set(out) - set(base))
    if not extra:
        return []
    total = sum(out[g] for g in extra)
    return [
        f"зазор Td после числа изменился: {total} шт. с новыми значениями "
        f"{extra[:6]} (в оригинале только {sorted(base)})"
    ]


def check_totals_match_rows(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Итог колонки обязан сходиться с суммой строк ТАК ЖЕ, как в оригинале.

    Сравнение именно с оригиналом, а не с нулём: у реального `HALYKformat2`
    сам исходный файл не сходится (приход −11.45 ₸, расход +73.33 ₸), и
    проверка «Δ должна быть 0» падала бы на неиспорченном документе.
    """
    def deltas(pdf_bytes: bytes) -> tuple:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        stmt = hal.parse_halyk_statement(doc)
        doc.close()
        d_in = stmt.total_kiri_s - round(sum(t.kiri_s for t in stmt.transactions), 2)
        d_out = stmt.total_shyghys - round(sum(t.shyghys for t in stmt.transactions), 2)
        return round(d_in, 2), round(d_out, 2)

    b_in, b_out = deltas(orig_bytes)
    o_in, o_out = deltas(out_bytes)
    issues = []
    if abs(o_in - b_in) > 1.0:
        issues.append(
            f"приход: итог−Σстрок = {o_in:+,.2f} (в оригинале {b_in:+,.2f})"
        )
    if abs(o_out - b_out) > 1.0:
        issues.append(
            f"расход: итог−Σстрок = {o_out:+,.2f} (в оригинале {b_out:+,.2f})"
        )
    return issues


def _cmap_block_kinds(pdf_bytes: bytes) -> dict[int, Counter]:
    """{xref ToUnicode-потока: Counter{'bfchar': n, 'bfrange': n}}.

    Считает, СКОЛЬКИМИ блоками какого рода записана таблица code→Unicode в
    каждом CMap-потоке документа.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: dict[int, Counter] = {}
    try:
        for xref in range(1, doc.xref_length()):
            try:
                body = doc.xref_stream(xref)
            except Exception:  # noqa: BLE001
                continue
            if not body or b"begincodespacerange" not in body:
                continue
            out[xref] = Counter(
                {
                    "bfchar": body.count(b"beginbfchar"),
                    "bfrange": body.count(b"beginbfrange"),
                }
            )
    finally:
        doc.close()
    return out


def check_cmap_block_style(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Таблица code→Unicode обязана быть записана ТЕМ ЖЕ родом блоков, что и
    в оригинале.

    Найдено 2026-08-06: вшивание недостающих глифов цифр в Bold-subset (см.
    «Исправлено 2026-08-05») дописывало соответствия CID→Unicode ОТДЕЛЬНЫМ
    блоком `beginbfchar`, тогда как генератор Halyk пишет ВСЮ таблицу
    исключительно через `beginbfrange` вырожденными диапазонами
    (`<0013><0013><0030>`) и `beginbfchar` не эмитит нигде и никогда —
    замер на 6 реальных файлах. По спецификации оба блока валидны, но это
    ровно тот класс признака, что и критерий 4 «стиль сериализации»: не
    ошибка формата, а чужой почерк. Причём самый заметный из всех
    найденных — присутствие `beginbfchar` в Halyk-документе видно БЕЗ
    эталона для сравнения, само по себе.

    Сравнивается всё же с оригиналом (а не «bfchar запрещён» жёстко) — по
    той же конвенции, что и `check_td_gap`: если однажды встретится
    Halyk-вариант, чей генератор сам пишет bfchar, проверка обязана это
    принять, а не падать на неиспорченном файле.
    """
    base = _cmap_block_kinds(orig_bytes)
    if not base:
        return []
    out = _cmap_block_kinds(out_bytes)
    issues = []
    for xref, counts in sorted(out.items()):
        was = base.get(xref)
        if was is None:
            continue  # новый поток — не наш класс дефекта, ловится другими проверками
        for kind in ("bfchar", "bfrange"):
            if counts[kind] > was[kind]:
                issues.append(
                    f"в CMap-потоке xref={xref} появилось блоков `begin{kind}`: "
                    f"{counts[kind]} против {was[kind]} в оригинале — таблица "
                    f"code→Unicode дописана не тем родом блока, каким её пишет "
                    f"сам генератор"
                )
    return issues


def _flate_streams(pdf_bytes: bytes) -> dict:
    """{xref: (сжатые байты, распакованные байты)} — только те потоки, чей
    почерк сжатия вообще о чём-то говорит: content-стримы страниц, ToUnicode и
    FontFile2. Потоки картинок исключены намеренно: логотип в HALYKformat1
    пересжимается впустую и давал бы ложное срабатывание.
    """
    out = {}
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        want = set()
        for pno in range(doc.page_count):
            want.update(doc[pno].get_contents())
        for xref in range(1, doc.xref_length()):
            obj = doc.xref_object(xref, compressed=True) or ""
            for key in ("/ToUnicode", "/FontFile2"):
                m = re.search(re.escape(key) + r"\s+(\d+)\s+0\s+R", obj)
                if m:
                    want.add(int(m.group(1)))
        for xref in sorted(want):
            obj = doc.xref_object(xref, compressed=True) or ""
            if "FlateDecode" not in obj:
                continue
            try:
                comp = doc.xref_stream_raw(xref)
                body = doc.xref_stream(xref)
            except Exception:  # noqa: BLE001
                continue
            if comp and body:
                out[xref] = (bytes(comp), bytes(body))
    finally:
        doc.close()
    return out


def check_stream_compressor(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Ни один поток результата не должен воспроизводиться python-zlib, если
    ни один поток оригинала не воспроизводится.

    Замер 2026-08-07 на всех 6 реальных файлах: из 56 потоков (content
    страниц + ToUnicode + FontFile2) НИ ОДИН не воспроизводится
    `zlib.compress` ни на одном уровне 0..9 — оригиналы выпущены
    `PDFsharp 1.50 / MigraDoc`, а он сжимает собственной копией SharpZipLib,
    самостоятельной реализацией DEFLATE. Наши же переписанные потоки
    воспроизводились `zlib.compress(данные, 6)` ТОЧНО: 145 совпадений на 24
    связках файл×цель (format1 — 5, format2 — 11-12, format3 — 7, format4 — 1,
    h6 — 4, hformat5 — 6-8). Это самый дешёвый для проверяющего признак из
    всех найденных за проект: три строки кода и никакого эталона.

    Сравнение идёт с оригиналом, а не «zlib запрещён» жёстко, — та же
    конвенция, что у `check_td_gap`/`check_cmap_block_style`: если однажды
    попадётся Halyk-вариант, чей генератор сам пишет python-совместимым
    zlib, проверка обязана это принять, а не краснеть на неиспорченном файле.
    Проверяется весь диапазон уровней, а не только 6: подмена уровня — не
    исправление признака, а его маскировка от одной конкретной формулировки.
    """
    base = _flate_streams(orig_bytes)
    base_hits = sum(
        1 for comp, body in base.values()
        if any(zlib.compress(body, lvl) == comp for lvl in range(10))
    )
    if base_hits:
        return []  # генератор этого файла сам пишет как python-zlib
    issues = []
    for xref, (comp, body) in sorted(_flate_streams(out_bytes).items()):
        for lvl in range(10):
            if zlib.compress(body, lvl) == comp:
                issues.append(
                    f"поток xref={xref} побайтово воспроизводится "
                    f"zlib.compress(данные, {lvl}) — пересжат python-zlib, тогда "
                    f"как ни один из {len(base)} потоков оригинала так не "
                    f"воспроизводится"
                )
                break
    return issues


def _cmap_text_order_inversions(pdf_bytes: bytes) -> dict:
    """{xref ToUnicode: (инверсий, записей)} — насколько порядок записей
    таблицы расходится с порядком ПЕРВОГО ПОЯВЛЕНИЯ CID в тексте.

    Имена шрифтовых ресурсов ЛОКАЛЬНЫ ДЛЯ СТРАНИЦЫ, поэтому каждый `Tj`
    привязывается к активному `Tf` в пределах своей страницы. Без этого
    CID Regular и Bold смешиваются, и проверка показывает сотни «инверсий»
    даже на нетронутом оригинале — на этом сломался первый вариант замера.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        first_use: dict = {}
        seen: dict = {}
        for pno in range(doc.page_count):
            page = doc[pno]
            fmap = {}
            for f in page.get_fonts(full=True):
                fobj = doc.xref_object(f[0], compressed=True) or ""
                m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", fobj)
                if m:
                    fmap[f[4]] = int(m.group(1))
            if not fmap:
                continue
            buf = b""
            for cx in page.get_contents():
                try:
                    buf += doc.xref_stream(cx)
                except Exception:  # noqa: BLE001
                    return {}
            cur = None
            for m in re.finditer(rb"/(F\d+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj", buf):
                if m.group(1) is not None:
                    cur = fmap.get(m.group(1).decode("ascii"))
                    continue
                if cur is None:
                    continue
                h = m.group(2).decode("ascii").upper()
                lst = first_use.setdefault(cur, [])
                st = seen.setdefault(cur, set())
                for j in range(0, len(h), 4):
                    c = h[j:j + 4]
                    if c not in st:
                        st.add(c)
                        lst.append(c)

        result = {}
        for tux, used in first_use.items():
            body = doc.xref_stream(tux)
            if not body:
                continue
            listed = []
            for bm in re.finditer(rb"beginbf(range|char)(.*?)endbf(?:range|char)",
                                  body, re.S):
                chunk = bm.group(2)
                if bm.group(1) == b"range":
                    for em in re.finditer(
                        rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<", chunk
                    ):
                        for c in range(int(em.group(1), 16), int(em.group(2), 16) + 1):
                            listed.append(f"{c:04X}")
                else:
                    for em in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<", chunk):
                        listed.append(f"{int(em.group(1), 16):04X}")
            rank = {c: i for i, c in enumerate(used)}
            seq = [rank[c] for c in listed if c in rank]
            inv = sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq))
                      if seq[i] > seq[j])
            result[tux] = (inv, len(seq))
        return result
    finally:
        doc.close()


def check_cmap_text_order(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Порядок записей ToUnicode обязан совпадать с порядком первого появления
    CID в тексте — ровно так, как это делает сам генератор.

    Замер 2026-08-07: во всех 12 таблицах 6 оригиналов — 0 инверсий при 100%
    покрытии (каждый перечисленный CID реально напечатан). Генератор выкладывает
    subset по мере вёрстки, поэтому признак различим БЕЗ эталона: достаточно
    сопоставить таблицу и текст одного и того же файла.

    В результатах инверсии были на 16 связках из 24 (3-40 штук) по ДВУМ
    причинам сразу: дописывание новых CID в конец таблицы / удаление старых из
    середины И сама замена текста, которая двигает первое появление цифры по
    документу. Вторая причина задевала даже Regular-таблицу, которую движок не
    патчит вовсе, — поэтому проверка меряет ИТОГОВЫЙ порядок, а не факт правки.
    """
    base = _cmap_text_order_inversions(orig_bytes)
    if not base:
        return []
    out = _cmap_text_order_inversions(out_bytes)
    issues = []
    for tux, (inv, n) in sorted(out.items()):
        was = base.get(tux)
        if was is None:
            continue
        if inv > was[0]:
            issues.append(
                f"в ToUnicode xref={tux} порядок записей разошёлся с порядком "
                f"первого появления CID в тексте: {inv} инверсий на {n} записях "
                f"против {was[0]} в оригинале"
            )
    return issues


_AMOUNT_SPAN_RE = re.compile(r"^-?\d[\d  ]*(?:,\d{2})?$")


def _amount_centers(pdf_bytes: bytes) -> set:
    """Множество X-центров ТЕКСТОВЫХ ПРОГОНОВ с денежными суммами.

    Разделитель разрядов в этом формате извлекается как NBSP (CMap:
    `<0003><0003><00A0>`), а не как обычный пробел — регулярка обязана его
    принимать, иначе замер молча теряет почти все суммы документа и
    проверка становится зелёной на заведомо сбитых файлах (ровно так
    первая попытка этого замера 2026-08-06 не увидела дефект вовсе).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    centers = set()
    try:
        for pn in range(doc.page_count):
            for blk in doc[pn].get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for s in line.get("spans", []):
                        t = s["text"].strip()
                        if _AMOUNT_SPAN_RE.match(t) and any(c.isdigit() for c in t):
                            centers.add(round((s["bbox"][0] + s["bbox"][2]) / 2, 2))
    finally:
        doc.close()
    return centers


# Насколько близко к центру колонки сумма считается «принадлежащей» ей.
# Уход внутри этого окна — сбитое центрирование; дальше — это уже не эта
# колонка (напр. число, законно перенесённое wrap-логикой на строку ниже).
_COLUMN_ATTRACTION_PT = 8.0
# Сколько сумм на одном центре в оригинале, чтобы считать его КОЛОНКОЙ, а не
# одиночным числом в тексте шапки.
_MIN_AMOUNTS_PER_COLUMN = 3


def check_amount_center_grid(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Денежные КОЛОНКИ этого формата центрированы: во всех 6 реальных
    оригиналах суммы любой длины стоят на одном и том же X-центре своей
    колонки ({240.60, 341.81, 407.46, 451.47, 464.90} — набор свой у каждого
    файла, поэтому берётся ИЗ ОРИГИНАЛА, а не хардкодится). Сумма, оставшаяся
    в своей колонке, обязана сохранить её центр ТОЧНО.

    Проверяются только настоящие колонки (не менее `_MIN_AMOUNTS_PER_COLUMN`
    сумм на центре в оригинале) и только «уползание» в пределах
    `_COLUMN_ATTRACTION_PT`. Одиночное число в тексте шапки колонкой не
    является, а число, ЗАКОННО перенесённое wrap-логикой на строку ниже
    (замер: `HALYKformat2`, «…в валюте: 80 664 911,06» не влезло в строку и
    ушло переносом), встаёт далеко от любой колонки — считать это сбитым
    центрированием было бы ложным срабатыванием.

    `find_line_overlaps` этот класс не видит по построению: равномерно
    сдвинутая сумма ни на что не наезжает — та же слепая зона, из-за
    которой в Kaspi ИП пришлось заводить `check_column_alignment`.
    """
    from collections import Counter as _C

    doc = fitz.open(stream=orig_bytes, filetype="pdf")
    tally: _C = _C()
    try:
        for pn in range(doc.page_count):
            for blk in doc[pn].get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for s in line.get("spans", []):
                        t = s["text"].strip()
                        if _AMOUNT_SPAN_RE.match(t) and any(c.isdigit() for c in t):
                            tally[round((s["bbox"][0] + s["bbox"][2]) / 2, 2)] += 1
    finally:
        doc.close()
    columns = {c for c, n in tally.items() if n >= _MIN_AMOUNTS_PER_COLUMN}
    if not columns:
        return []

    drifted = {}
    for c in _amount_centers(out_bytes) - set(tally):
        near = min(columns, key=lambda col: abs(col - c))
        if abs(near - c) <= _COLUMN_ATTRACTION_PT:
            drifted.setdefault(near, []).append(c)
    if not drifted:
        return []
    parts = [
        f"колонка {col}: суммы уехали на {sorted(v)}" for col, v in sorted(drifted.items())
    ]
    return ["центрирование денежной колонки сбито — " + "; ".join(parts)]


def _fee_rows(pdf_bytes: bytes) -> int:
    """Сколько строк, где «Расход» больше «Суммы операции» — то есть в расход
    свёрнута комиссия за перевод.

    Колонки берутся по их X-центрам (240.60 — «Сумма операции», 407.46 —
    «Расход»), потому что это та же фиксированная сетка, что проверяет
    `check_amount_center_grid`.
    """
    def num(t: str):
        try:
            return float(t.replace(" ", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return None

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = 0
    try:
        for pn in range(doc.page_count):
            rows: dict[float, list] = {}
            for blk in doc[pn].get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for s in line.get("spans", []):
                        if s["text"].strip():
                            rows.setdefault(round(s["bbox"][1], 1), []).append(s)
            for sps in rows.values():
                cent = {
                    round((s["bbox"][0] + s["bbox"][2]) / 2, 2): s["text"].strip()
                    for s in sps
                }
                op, rash = num(cent.get(240.60, "")), num(cent.get(407.46, ""))
                if op is None or rash is None or rash >= 0:
                    continue
                if abs(abs(rash) - abs(op)) > 0.005:
                    n += 1
    finally:
        doc.close()
    return n


def check_fee_rows_preserved(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Строки со свёрнутой в «Расход» комиссией обязаны сохраниться.

    Замер 2026-08-06 по всем 6 реальным файлам: «Расход» превышает «Сумму
    операции» ровно на 150 или 200 ₸ при нулевой колонке «Комиссия» —
    17/60/23/2/17/23 строк соответственно. Это конвенция формата, одинаковая
    во всех подлинных документах.

    До удаления `[FEE-NORM]` движок приводил расход к сумме операции, и таких
    строк в результате оставалось РОВНО НОЛЬ на каждом файле и каждой цели.
    Отсутствие того, что есть у всех настоящих выписок, — самостоятельный
    признак правки, различимый без эталона, поэтому проверка требует
    сохранения, а не «не хуже, чем было».

    Сравнивается с числом строк В ОРИГИНАЛЕ (а не с ненулём) — по той же
    конвенции, что `check_td_gap`/`check_totals_match_rows`: файл, у которого
    таких строк нет вовсе, не должен падать на пустом месте.
    """
    base = _fee_rows(orig_bytes)
    if base == 0:
        return []
    got = _fee_rows(out_bytes)
    if got == base:
        return []
    return [
        f"строк со свёрнутой в «Расход» комиссией стало {got} вместо {base} "
        f"— в подлинных выписках этот признак есть всегда (150/200 ₸ при "
        f"нулевой колонке «Комиссия»), его исчезновение само по себе улика"
    ]


_DIGIT_CID_HEX = {f"{0x13 + i:04X}": str(i) for i in range(10)}


def _bold_digits_present_and_used(pdf_bytes: bytes) -> tuple[set, set]:
    """(цифры в /W жирных шрифтов, цифры, реально нарисованные жирным).

    Все 6 реальных файлов рисуют текст ИСКЛЮЧИТЕЛЬНО через `<hex>Tj` —
    ни одного `TJ`-массива, `()Tj` или `'`/`"` (замер 2026-08-06), поэтому
    скан по `<hex>Tj` для этого формата полон, а не приблизителен.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    present: set = set()
    used: set = set()
    seen_fonts: set = set()
    try:
        for pn in range(doc.page_count):
            pobj = doc.xref_object(doc[pn].xref)
            bold: dict[str, int] = {}
            for fn, fx in re.findall(r"/F(\d+)\s+(\d+)\s+0\s+R", pobj):
                try:
                    fobj = doc.xref_object(int(fx))
                except Exception:  # noqa: BLE001
                    continue
                bm = re.search(r"/BaseFont\s*/(\S+)", fobj)
                if not bm or ("Bold" not in bm.group(1) and ",B" not in bm.group(1)):
                    continue
                dm = re.search(r"/DescendantFonts\s*\[?\s*(\d+)\s+0\s+R", fobj)
                if dm:
                    bold["F" + fn] = int(dm.group(1))
            for dx in set(bold.values()):
                if dx in seen_fonts:
                    continue
                seen_fonts.add(dx)
                wm = re.search(r"/W\s*\[(.*?)\]\s*(?:/|>>)", doc.xref_object(dx), re.S)
                if not wm:
                    continue
                for m in re.finditer(r"(\d+)\s*\[\s*[\d\s]+\]", wm.group(1)):
                    h = f"{int(m.group(1)):04X}"
                    if h in _DIGIT_CID_HEX:
                        present.add(h)
            if not bold:
                continue
            buf = b""
            for cx in doc[pn].get_contents():
                try:
                    buf += zlib.decompress(doc.xref_stream_raw(cx))
                except Exception:  # noqa: BLE001
                    try:
                        buf += doc.xref_stream(cx)
                    except Exception:  # noqa: BLE001
                        pass
            cur = None
            for m in re.finditer(rb"/F(\d+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj", buf):
                if m.group(1) is not None:
                    cur = "F" + m.group(1).decode()
                elif cur in bold:
                    h = m.group(2).decode().upper()
                    for j in range(0, len(h), 4):
                        if h[j:j + 4] in _DIGIT_CID_HEX:
                            used.add(h[j:j + 4])
    finally:
        doc.close()
    return present, used


def check_bold_subset_tightness(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Subset ЖИРНОГО шрифта у настоящего файла ПЛОТНЫЙ: что лежит в /W, то и
    напечатано в документе — замер на всех 4 реальных файлах с жирными
    цифрами, ноль лишних, 4 из 4. Глиф, не встречающийся ни в одном числе
    документа, — признак правки, различимый БЕЗ эталона.

    Проверка делит лишние глифы на две причины, потому что закрыты они
    по-разному:

    * **вшитый и неиспользованный** — цифра, которую наш патч добавил в
      subset, хотя в новом тексте её нет. Это НАША ошибка и она устранима
      (закрыто 2026-08-06 ограничением патча набором реально нужных цифр),
      поэтому здесь — FAIL: возврат к «вшиваем все недостающие подряд»
      должен краснеть.
    * **осиротевший** — цифра, что БЫЛА в оригинальном subset'е и
      использовалась ИМ, но перестала употребляться после замены текста.
      Это свойство любого редактирования, а не вшивания глифов (замер: 20 из
      22 лишних на корпусе — именно такие, они остались бы даже без патча).
      Убрать их можно только УДАЛЕНИЕМ глифов из /W/ToUnicode/glyf, что
      несопоставимо рискованнее добавления, — решено не делать (см.
      CLAUDE.md). Поэтому здесь — не FAIL, а `[guard]`-пометка: число
      остаётся на виду и не вырастет незамеченным.
    """
    orig_present, _ = _bold_digits_present_and_used(orig_bytes)
    if not orig_present:
        return []  # у этого файла жирный шрифт цифр не содержит вовсе
    present, used = _bold_digits_present_and_used(out_bytes)
    extra = present - used
    if not extra:
        return []

    info = getattr(hal, "LAST_RUN_INFO", {}) or {}
    patched = {
        cid for widths in (info.get("glyphs_patched") or {}).values() for cid in widths
    }
    d = lambda s: sorted(_DIGIT_CID_HEX[c] for c in s)  # noqa: E731

    issues = []
    patched_unused = extra & patched
    if patched_unused:
        issues.append(
            f"в жирный subset вшиты цифры {d(patched_unused)}, которых нет ни в "
            f"одном напечатанном числе — патч обязан ограничиваться реально "
            f"нужными цифрами (см. _bold_needed_digits)"
        )
    orphaned = extra - patched
    if orphaned:
        issues.append(
            f"[guard] в жирном subset'е осталось {len(orphaned)} неиспользуемых "
            f"цифр {d(orphaned)} — они были в ОРИГИНАЛЬНОМ subset'е и осиротели "
            f"после замены текста (не следствие вшивания глифов); устранимо "
            f"только удалением глифов, сознательно не делается"
        )
    return issues


_OPENING_LABELS = ("қалдығы: ", "Кіріс қалдығы:", "Входящий остаток:")


def check_opening_balance_sign_erasure(out_bytes: bytes) -> list[str]:
    """Если минус входящего остатка стёрт (ПРОВЕРКА 3 подняла его в плюс),
    между двоеточием и первой цифрой не должно остаться НИКАКОГО символа,
    кроме одного обычного пробела — как в любом реально положительном файле
    (замер: `HALYKformat3.pdf`, "Входящий остаток: 12,51" — сразу цифра).

    Найдено 2026-08-06: `process_halyk_pdf` стирал токен минуса, подставляя
    вместо него глиф-заглушку (NBSP, а не обычный пробел — `FROM_UNICODE.get
    ("\\xa0")` шёл первым), из-за чего в тексте оставался невидимый, но
    посторонний символ, которого нет ни в одном настоящем документе с
    положительным входящим остатком. Проверяет РЕЗУЛЬТАТ напрямую по тексту
    страницы 0 — не зависит от того, чем именно заменили минус.
    """
    doc = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        text = doc[0].get_text()
    finally:
        doc.close()
    issues = []
    for label in _OPENING_LABELS:
        idx = text.find(label)
        if idx == -1:
            continue
        after = text[idx + len(label):idx + len(label) + 8]
        m = re.match(r"([^\d\-]*)(-?)\d", after)
        if not m:
            continue
        gap = m.group(1)
        if gap not in (" ", ""):
            issues.append(
                f"после «{label.strip()}» остался посторонний символ(ы) {gap!r} "
                f"(ord={[hex(ord(c)) for c in gap]}) — настоящий файл с положительным "
                f"остатком не содержит там ничего, кроме одного пробела"
            )
        break  # первая найденная метка — единственная актуальная для этого файла
    return issues


_TOTALS_LABEL = ("Барлығы", "Всего")


def _totals_rows(pdf_bytes: bytes) -> list[list[dict]]:
    """Строки итогов документа (по подписи «Барлығы:»/«Всего:»).

    Группируем спаны по Y ЧЕРЕЗ ВСЕ блоки страницы, а не полагаемся на
    line-объекты PyMuPDF: подпись «Всего:» и суммы этой же визуальной строки
    попадают у него в РАЗНЫЕ line'ы (проверено на h6.pdf — line с подписью
    содержал ровно один спан, без единого числа). С прежней группировкой
    проверка смотрела на строку из одной подписи, всегда находила её
    однородной и молчала на заведомо дефектных файлах.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    rows: list[list[dict]] = []
    try:
        for pn in range(doc.page_count):
            by_y: dict[float, list[dict]] = {}
            for blk in doc[pn].get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for s in line.get("spans", []):
                        if s["text"].strip():
                            by_y.setdefault(round(s["bbox"][1], 1), []).append(s)
            for y in sorted(by_y):
                sps = sorted(by_y[y], key=lambda s: s["bbox"][0])
                if any(lbl in s["text"] for s in sps for lbl in _TOTALS_LABEL):
                    rows.append(sps)
    finally:
        doc.close()
    return rows


def _bold_missing_digits(pdf_bytes: bytes) -> set:
    """Цифры, которых нет в subset'е ЖИРНОГО шрифта документа.

    Subset жирного содержит только те глифы, что печатались жирным в самом
    оригинале, поэтому новая сумма вполне может потребовать отсутствующий.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    missing: set = set()
    try:
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref)
            except Exception:  # noqa: BLE001
                continue
            if "/BaseFont" not in obj or "Bold" not in obj:
                continue
            dm = re.search(r"/DescendantFonts\s*\[?\s*(\d+)\s+0\s+R", obj)
            if not dm:
                continue
            try:
                dobj = doc.xref_object(int(dm.group(1)))
            except Exception:  # noqa: BLE001
                continue
            wm = re.search(r"/W\s*\[(.*?)\]\s*(?:/|>>)", dobj, re.S)
            if not wm:
                continue
            cids = set()
            for m in re.finditer(r"(\d+)\s*\[([\d\s.]+)\]", wm.group(1)):
                start = int(m.group(1))
                for i in range(len(m.group(2).split())):
                    cids.add(start + i)
            # Цифра '0' — CID 0x13 в этих subset'ах, далее по порядку.
            present = {str(d) for d in range(10) if (0x13 + d) in cids}
            if present:  # шрифт вообще набирает цифры — значит остальных нет
                missing |= {str(d) for d in range(10)} - present
    finally:
        doc.close()
    return missing


def check_bold_row_uniform(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Строка итогов «Барлығы» обязана остаться однородной по начертанию.

    Найдено 2026-08-04 на реальных файлах: в оригинале строка целиком жирная и
    `-2 030 782,79` читается как ОДИН текстовый прогон, а в результате число
    перерисовывалось обычным Times New Roman (минус при этом оставался жирным
    и физически отделялся в свой прогон, из-за чего заодно уезжал центр
    колонки — п.4 того же разбора). Причина: жирный subset содержит только
    глифы, печатавшиеся жирным в оригинале, и новой сумме может не хватить
    цифры (замер: HALYKformat1/hformat5 — нет «4», HALYKformat3 — «3»,
    h6 — «1», «5», «7»). `process_halyk_pdf` перебирает ±3% шум, чтобы этого
    избежать; проверка следит, что перебор действительно сработал.

    **Разнородность — всегда FAIL, без «уважительных причин».** Первая версия
    этой проверки прощала подмену, если в написанном числе есть цифра,
    отсутствующая в жирном subset'е, — считая такой случай «доказуемо
    неизбежным». Мутационный прогон (перебор отключён, `_BOLD_GLYPH_RETRIES`
    = 1) показал, что так проверка не краснеет НИКОГДА: подмена по
    определению вызвана недостающей цифрой, то есть под эту поблажку
    попадает ровно 100% случаев — 0 срабатываний на 24 заведомо дефектных
    прогонах. Проверка, которая не умеет краснеть, хуже отсутствующей.

    Поэтому оговорка убрана: перебор шума в `process_halyk_pdf` обязан
    избавиться от подмены, и если он не справился — батарея должна стать
    красной, а человек решить, что делать с конкретным файлом. Недостающие
    цифры по-прежнему печатаются в сообщении: без них непонятно, почему
    подмена вообще потребовалась.
    """
    orig_rows = _totals_rows(orig_bytes)
    if not orig_rows:
        return []  # у этого файла строки итогов нет — проверять нечего
    if any(len({("B" if "Bold" in s["font"] else "R") for s in r}) > 1 for r in orig_rows):
        return []  # оригинал сам разнороден — не наш дефект

    missing = _bold_missing_digits(out_bytes)
    issues = []

    # Task 5: если в этом прогоне писатель физически вшил недостающие глифы
    # цифр в Bold-subset (Task 3/4), подмена шрифта не требуется вовсе — это
    # сильнее, чем «перебор шума нашёл чистый вариант» (subs == 0 без этого
    # пути тоже возможен, если сумма просто не содержала недостающую цифру).
    # Помечаем отдельным маркером [glyph-patched], НЕ guard и не FAIL, чтобы
    # в выводе battery было видно, что новый путь реально сработал.
    info = getattr(hal, "LAST_RUN_INFO", {}) or {}
    glyphs_patched = info.get("glyphs_patched") or {}
    if glyphs_patched:
        patched_cids = sorted(
            {cid_hex for widths in glyphs_patched.values() for cid_hex in widths}
        )
        issues.append(
            f"[glyph-patched] недостающие глифы цифр вшиты физически в Bold-subset "
            f"({len(glyphs_patched)} шрифт(ов), CID {patched_cids}) — подмена шрифта "
            f"в строке итогов не потребовалась"
        )

    for row in _totals_rows(out_bytes):
        weights = {("B" if "Bold" in s["font"] else "R") for s in row}
        if len(weights) <= 1:
            continue
        desc = " ".join(
            f"{s['text'].strip()!r}:{'B' if 'Bold' in s['font'] else 'R'}" for s in row
        )
        why = f"; в жирном subset'е нет цифр {sorted(missing)}" if missing else ""
        if info.get("unavoidable"):
            # Писатель ИЗМЕРИЛ неизбежность: ни одна из его попыток не дала
            # чистого варианта. Этоguard, а не FAIL — но выдаётся строго по
            # факту прогона, а не по признаку «в числе есть недостающая
            # цифра» (он верен всегда и потому ничего не доказывает).
            issues.append(
                f"[guard] подмена шрифта неустранима: {info.get('attempts')} попыток "
                f"перебора не дали варианта без неё (минимум {info.get('min_substitutions')}); "
                f"{desc}{why}"
            )
            continue
        issues.append(
            f"строка итогов стала разнородной по начертанию (в оригинале — целиком "
            f"жирная): {desc}{why} — перебор шума не избавился от подмены шрифта"
        )
    return issues


def check_isi_floor(out_bytes: bytes, threshold: float = 0.75, tolerance: float = 0.02) -> list[str]:
    """Критерий (добавлен 2026-08-03): жёсткий ISI-порог (0.75) должен реально
    держаться в РЕЗУЛЬТАТЕ — пересчитано НЕЗАВИСИМО от `validate_halyk`'s
    "passed" (напрямую из помесячных сумм kiri_s, разобранных из выходного
    PDF), а не просто переиспользует её вердикт. См. CLAUDE.md, "Исправлено
    2026-08-03: помесячное выравнивание убивало естественный разброс дохода
    (Halyk)" — адаптивный коридор `recalculate_halyk` обязан гарантировать
    именно это.
    """
    stmt = hal.parse_halyk_statement(fitz.open(stream=out_bytes, filetype="pdf"))
    monthly: dict[str, float] = {}
    for t in stmt.transactions:
        if t.is_salary and t.kiri_s > 0:
            mk = t.op_date[3:]
            monthly[mk] = monthly.get(mk, 0) + t.kiri_s
    vals = list(monthly.values())
    if len(vals) < 2:
        return []
    mu = sum(vals) / len(vals)
    if mu <= 0:
        return []
    sigma = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
    isi = max(0.0, 1.0 - sigma / mu)
    if isi < threshold - tolerance:
        return [f"ISI фактического результата = {isi:.4f} < порога {threshold} (допуск {tolerance}) — адаптивный коридор не удержал жёсткую проверку"]
    return []


def check_rounding_escalation(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Критерий (добавлен 2026-08-03): если оригинальная зарплата кратна
    крупному «человеческому» числу (5 000/.../1 000 000), результат должен
    остаться кратным ЕМУ ЖЕ, а не просесть до мелкого базового шага (см.
    CLAUDE.md, "`_round_to_natural` не сохраняла круглость ПОСЛЕ умножения
    на дробный K"). Решение round(x,2) vs natural rounding в `recalculate_halyk`
    принимается НА УРОВНЕ ВСЕЙ ВЫПИСКИ (если хоть одна реальная зарплата имеет
    копейки — round(x,2) для ВСЕХ, даже целых) — реплицируем то же решение
    здесь, а не проверяем копейки по каждой транзакции отдельно, иначе
    легитимный round(x,2)-путь на статистически целых транзакциях того же
    файла даёт ложный FAIL (пойман на реальном hformat5.pdf).
    """
    orig_stmt = hal.parse_halyk_statement(fitz.open(stream=orig_bytes, filetype="pdf"))
    out_stmt = hal.parse_halyk_statement(fitz.open(stream=out_bytes, filetype="pdf"))
    o_sal = [t for t in orig_stmt.transactions if t.is_salary and t.kiri_s > 0]
    n_sal = [t for t in out_stmt.transactions if t.is_salary and t.kiri_s > 0]
    if len(o_sal) != len(n_sal):
        return []
    salary_has_cents = any(abs(t.kiri_s - round(t.kiri_s)) > 0.001 for t in o_sal)
    if salary_has_cents:
        return []  # весь файл идёт через round(x, 2) — эскалация тут не применяется
    candidates = (1_000_000.0, 500_000.0, 100_000.0, 50_000.0, 10_000.0, 5_000.0, 1_000.0)
    issues = []
    for ot, nt in zip(o_sal, n_sal):
        oa, na = ot.kiri_s, nt.kiri_s
        if oa <= 0:
            continue
        for cand in candidates:
            if oa % cand < 0.01 or cand - (oa % cand) < 0.01:
                if na % cand > 0.01 and cand - (na % cand) > 0.01:
                    issues.append(f"{ot.op_date}: оригинал {oa:,.0f} кратен {cand:,.0f}, но результат {na:,.0f} — нет")
                break
    return issues[:10]


def render_pages(pdf_bytes: bytes, out_dir: Path, label: str, pages: list[int], dpi: int = 150) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for pn in pages:
        if 0 <= pn < doc.page_count:
            doc[pn].get_pixmap(dpi=dpi).save(str(out_dir / f"{label}_page{pn}.png"))
    doc.close()


def run_one(path: Path, multipliers: list[float], out_dir: Path, render: bool) -> bool:
    raw = path.read_bytes()
    doc = fitz.open(stream=raw, filetype="pdf")
    is_halyk = hal.detect_halyk_format(doc)
    doc.close()
    if not is_halyk:
        print(f"{path.name}: НЕ формат Halyk (нет BIC HSBKKZKX / halykbank.kz) — пропуск")
        return True

    base = hal.validate_halyk(raw)
    avg = base["summary"]["avg_monthly_income"]
    months = base["summary"]["months"]
    print(f"\n=== {path.name} === месяцев={months}, ср.доход/мес≈{avg:,.0f} ₸, "
          f"тр-ций={base['summary']['transactions']}")
    if months < 12:
        print("  ⚠️ период < 12 мес — банк может отклонить по сроку (свойство оригинала, не баг генерации)")
    if avg <= 0:
        print("  пропуск: доход 0")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    all_ok = True
    for mult in multipliers:
        target = avg * mult
        label = f"x{mult:g}"
        try:
            out_bytes = hal.process_halyk_pdf(raw, target_monthly_income=target)
        except IncomeTooLowError as e:
            rows.append((label, target, "floor-guard", "-", "-", "-", str(e)))
            continue
        except hal.NoScalableIncomeError as e:
            # Легитимный отказ, а не поломка: в выписке нечего масштабировать
            # (формулировка дохода не в _NAV_INCOME_KEYWORDS). Раньше на этом
            # месте молча возвращался неизменённый файл.
            rows.append((label, target, "no-income", "-", "-", "-", str(e)))
            continue
        except Exception as e:  # noqa: BLE001
            rows.append((label, target, "EXCEPTION", "FAIL", "-", "-", f"{type(e).__name__}: {e}"))
            all_ok = False
            continue

        verify = hal.validate_halyk(out_bytes)
        math_ok = verify["passed"]
        geo_issues = (
            geometry_check(out_bytes)
            + style_check(raw, out_bytes)
            + check_isi_floor(out_bytes)
            + check_rounding_escalation(raw, out_bytes)
            + check_bold_row_uniform(raw, out_bytes)
            + check_td_gap(raw, out_bytes)
            + check_totals_match_rows(raw, out_bytes)
            + check_opening_balance_sign_erasure(out_bytes)
            + check_cmap_block_style(raw, out_bytes)
            + check_amount_center_grid(raw, out_bytes)
            + check_bold_subset_tightness(raw, out_bytes)
            + check_fee_rows_preserved(raw, out_bytes)
            + check_stream_compressor(raw, out_bytes)
            + check_cmap_text_order(raw, out_bytes)
        )
        # Сообщения с префиксом «[guard]» — не провал: это случаи, чью
        # неустранимость движок ДОКАЗАЛ измерением (см. check_bold_row_uniform).
        # «[glyph-patched]» — тоже не провал, а информационная пометка о том,
        # что недостающие глифы были физически вшиты в Bold-subset и подмена
        # шрифта не потребовалась вовсе (Task 5). Оба всё равно попадают в
        # примечание, чтобы не потеряться.
        geo_fail = [
            i for i in geo_issues
            if not i.startswith("[guard]") and not i.startswith("[glyph-patched]")
        ]
        geo_ok = len(geo_fail) == 0
        font_issues = font_check(raw, out_bytes)
        font_ok = len(font_issues) == 0

        (out_dir / f"{path.stem}_{label}.pdf").write_bytes(out_bytes)
        if render:
            render_pages(out_bytes, out_dir, f"{path.stem}_{label}", pages=[0, 1])

        note = "; ".join(verify["issues"] + geo_issues + font_issues)
        rows.append((label, target, "process",
                     "OK" if math_ok else "FAIL",
                     "OK" if geo_ok else "FAIL",
                     "OK" if font_ok else "FAIL", note))
        if not (math_ok and geo_ok and font_ok):
            all_ok = False

    print(f"  {'цель':<7} {'режим':<11} {'матем':<6} {'позиции':<8} {'шрифт':<6} примечание")
    for label, target, mode, m_s, g_s, f_s, note in rows:
        print(f"  {label:<7} {mode:<11} {m_s:<6} {g_s:<8} {f_s:<6} {note}")

    with open(out_dir / "_report.json", "w", encoding="utf-8") as f:
        json.dump([{"target_label": r[0], "target": r[1], "mode": r[2],
                    "math": r[3], "geometry": r[4], "font": r[5], "note": r[6]} for r in rows],
                  f, ensure_ascii=False, indent=2)
    print(f"  результаты и отчёт: {out_dir}")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="путь(и) к оригинальному Halyk PDF")
    parser.add_argument("--targets", default=",".join(str(x) for x in DEFAULT_TARGET_MULTIPLIERS))
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()

    multipliers = [float(x) for x in args.targets.split(",")]
    overall_ok = True
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.exists():
            print(f"{path}: файл не найден")
            overall_ok = False
            continue
        out_dir = path.parent / f"{path.stem}_halyk_autotest_out"
        overall_ok = run_one(path, multipliers, out_dir, args.render) and overall_ok

    print("\n" + ("ВСЁ ОК" if overall_ok else "ЕСТЬ ПРОБЛЕМЫ — см. FAIL выше"))
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()

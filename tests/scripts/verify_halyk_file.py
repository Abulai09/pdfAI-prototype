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
    расхождение в любую сторону — след (см. verify_gold_file.py)."""
    blob_o, blob_n = _content_blob(orig_bytes), _content_blob(out_bytes)
    issues = []
    for label, rx in _STYLE_METRICS.items():
        n_o, n_n = len(rx.findall(blob_o)), len(rx.findall(blob_n))
        if n_o != n_n:
            issues.append(f"стиль сериализации: {label} — было {n_o}, стало {n_n}")
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
_MONEY_TEXT = re.compile(r"^-?\d{1,3}(?:[  ]\d{3})*(?:,\d{2})?$")


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

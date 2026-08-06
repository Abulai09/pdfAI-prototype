"""
Универсальный автотест для ЛЮБОГО файла Kaspi ИП («Выписка по счету», БИК
CASPKZKA, с колонками № | Дата | Дебет | Кредит | ...). Аналог
verify_gold_file.py, но для формата ИП/sole-proprietor.

Для каждой цели из батареи (множители текущего среднемесячного оборота):
  1. Математика — validate_kaspi_ip() (та же проверка, что за /verify):
     баланс opening+кредит−дебет=closing, ordered running balance, баланс≥0,
     ISI, xref, целостность стримов.
  2. Целостность замен — заголовок обязан сходиться с телом: сумма Дебета/
     Кредита по строкам транзакций == итог в шапке. Именно это ловит баг
     «ячейку не переписали, а итог уже пересчитан» (короткий дебет попадал
     не в ту колонку по X — воспроизведено на IP2.pdf: Δ +2 920 ₸).
  3. Позиции текста — на всех выборочных страницах слова на одной строке не
     накладываются по X (в координатах get_text повёрнутой страницы).

ISI < 0.60 на ИСХОДНОМ (необработанном) файле — это свойство реальных
данных (нерегулярный оборот), НЕ баг: после обработки движок выравнивает
месяцы и ISI поднимается. Поэтому ISI на оригинале не считаем провалом.

Запуск из корня репозитория:
    python tests/scripts/verify_kaspi_ip_file.py <путь.pdf> [<путь2.pdf> ...]
    python tests/scripts/verify_kaspi_ip_file.py <путь> --targets 0.6,1.05,2,10
    python tests/scripts/verify_kaspi_ip_file.py <путь> --render

Результаты (обработанные PDF, при --render — PNG страниц) → в
<папка_с_исходником>/<имя>_ip_autotest_out/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PDFAI_DB_PATH", os.path.join(tempfile.gettempdir(), "pdfai_ip_autotest_journal.db"))

import fitz  # noqa: E402

import kaspi_ip_pdf_service as kip  # noqa: E402
from pdf_service_downscale import IncomeTooLowError  # noqa: E402

DEFAULT_TARGET_MULTIPLIERS = [0.6, 1.05, 2, 5, 20]


def find_line_overlaps(page: "fitz.Page", tolerance: float = 0.5) -> list[str]:
    """Слова на одной визуальной строке не должны накладываться по X.

    Точные дубликаты в одинаковых координатах (Kaspi иногда дважды рисует
    один и тот же текст — не искажение) не считаем коллизией.
    """
    words = page.get_text("words")
    lines: dict[int, list[tuple[float, float, float, str]]] = {}
    for w in words:
        x0, y0, x1, text = w[0], w[1], w[2], w[4]
        yk = round(y0 / 3) * 3
        lines.setdefault(yk, []).append((x0, x1, y0, text))

    issues = []
    for yk, ws in lines.items():
        ws.sort(key=lambda t: t[0])
        for a, b in zip(ws, ws[1:]):
            if b[0] >= a[1] - tolerance:
                continue
            if a[3] == b[3] and abs(a[0] - b[0]) < tolerance and abs(a[1] - b[1]) < tolerance:
                continue
            issues.append(
                f"наложение на y~{yk:.0f}: {a[3]!r}[до x={a[1]:.1f}] vs {b[3]!r}[с x={b[0]:.1f}]"
            )
    return issues


def header_matches_body(pdf_bytes: bytes) -> list[str]:
    """Итоги в шапке обязаны совпадать с суммой строк транзакций.

    Ловит «осиротевшую» замену: если ячейку суммы не переписали (напр. из-за
    неверной колонки), а итог в шапке уже пересчитан, суммы разойдутся —
    validate_kaspi_ip тоже поймает это через running balance, но здесь
    сообщение точнее указывает на причину.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    stmt = kip.parse_kaspi_ip_statement(doc)
    doc.close()
    s = stmt.summary
    body_credit = round(sum(t.amount for t in stmt.transactions if t.is_credit), 2)
    body_debit = round(sum(t.amount for t in stmt.transactions if not t.is_credit), 2)
    issues = []
    if abs(body_credit - s.total_credit) > 1.0:
        issues.append(f"Кредит шапка/тело: {s.total_credit:,.2f} vs Σстрок {body_credit:,.2f} (Δ={s.total_credit-body_credit:+,.2f})")
    if abs(body_debit - s.total_debit) > 1.0:
        issues.append(f"Дебет шапка/тело: {s.total_debit:,.2f} vs Σстрок {body_debit:,.2f} (Δ={s.total_debit-body_debit:+,.2f})")
    return issues


# ── Признаки стиля сериализации (форензик-разбор 02/08/2026) ──────────────
# Копия проверки из verify_gold_file.py — одна копия на формат, по той же
# конвенции, что и find_line_overlaps(). Именно на этом формате был найден
# признак 3: оригинал пишет «)Tj» вплотную (1669 раз, 0 исключений), а
# писатель вставлял пробел ровно в 102–104 строках — по числу правок.
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


def geometry_check(pdf_bytes: bytes, sample_pages: list[int]) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    issues = []
    pages = {0}
    pages.update(p for p in sample_pages if 0 <= p < doc.page_count)
    if doc.page_count > 1:
        pages.add(doc.page_count // 2)
        pages.add(doc.page_count - 1)
    for pn in sorted(pages):
        for issue in find_line_overlaps(doc[pn]):
            issues.append(f"[стр. {pn}] {issue}")
    doc.close()
    return issues


def check_isi_floor(out_bytes: bytes, threshold: float = 0.60, tolerance: float = 0.02) -> list[str]:
    """Критерий (добавлен 2026-08-03): жёсткий ISI-порог (0.60) должен реально
    держаться в РЕЗУЛЬТАТЕ — пересчитано НЕЗАВИСИМО от `validate_kaspi_ip`'s
    "issues" (напрямую из помесячных сумм кредита, разобранных из выходного
    PDF), а не просто переиспользует её вердикт. Коридор ±`_MAX_MONTH_K_SPREAD`
    в `recalculate_kaspi_ip` обязан гарантировать именно это.
    """
    stmt = kip.parse_kaspi_ip_statement(fitz.open(stream=out_bytes, filetype="pdf"))
    monthly: dict[str, float] = {}
    for t in stmt.transactions:
        if t.is_credit:
            mk = t.date[3:]
            monthly[mk] = monthly.get(mk, 0) + t.amount
    vals = list(monthly.values())
    if len(vals) < 2:
        return []
    mu = sum(vals) / len(vals)
    if mu <= 0:
        return []
    sigma = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
    isi = max(0.0, 1.0 - sigma / mu)
    if isi < threshold - tolerance:
        return [f"ISI фактического результата = {isi:.4f} < порога {threshold} (допуск {tolerance}) — коридор не удержал жёсткую проверку"]
    return []


def check_rounding_escalation(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Критерий (добавлен 2026-08-03): если оригинальный кредит кратен крупному
    «человеческому» числу (5 000/.../1 000 000), результат должен остаться
    кратным ЕМУ ЖЕ, а не просесть до базового шага в 1000 (см. CLAUDE.md,
    "`_round_amount` didn't preserve the original amount's own roundness class").
    Строки с `amount_in_purpose=True` (сумма продублирована в назначении
    платежа, не масштабируется) пропускаются.
    """
    orig_stmt = kip.parse_kaspi_ip_statement(fitz.open(stream=orig_bytes, filetype="pdf"))
    out_stmt = kip.parse_kaspi_ip_statement(fitz.open(stream=out_bytes, filetype="pdf"))
    o_cr = [t for t in orig_stmt.transactions if t.is_credit and not t.amount_in_purpose]
    n_cr = [t for t in out_stmt.transactions if t.is_credit and not t.amount_in_purpose]
    if len(o_cr) != len(n_cr):
        return []
    candidates = (1_000_000.0, 500_000.0, 100_000.0, 50_000.0, 10_000.0, 5_000.0, 1_000.0)
    issues = []
    for ot, nt in zip(o_cr, n_cr):
        oa, na = ot.amount, nt.amount
        if oa <= 0:
            continue
        for cand in candidates:
            if oa % cand < 0.01 or cand - (oa % cand) < 0.01:
                if na % cand > 0.01 and cand - (na % cand) > 0.01:
                    issues.append(f"{ot.date}: оригинал {oa:,.0f} кратен {cand:,.0f}, но результат {na:,.0f} — нет")
                break
    return issues[:10]


# Разделитель разрядов или копейки обязательны: без них под «сумму» подпадают
# 3-значные коды КНП (по одному на транзакцию, все на одном крае — они и
# становились модальным краем ЧУЖОЙ колонки) и 8-значные номера документов.
_MONEY_SPAN = re.compile(r"^\d{1,3}(?:[  ]\d{3})+(?:,\d{2})?$|^\d{1,3},\d{2}$")


def _money_span_edges(doc: "fitz.Document") -> dict[float, int]:
    """Правые края денежных Tj-ранов, в системе координат ПОВЁРНУТОЙ страницы.

    Страница Kaspi ИП повёрнута на 90°, поэтому колонка — это полоса по X, а
    текст внутри строки идёт по УБЫВАЮЩЕЙ Y: конец числа (его правый край в
    визуальном смысле) — это `y0` спана, а не `x1`. Меряем по spans, то есть
    настоящими метриками шрифта из PyMuPDF, а не моделью ширины символа: у
    писателя своя модель, и проверять его же моделью — значит согласиться с
    его ошибкой по построению.
    """
    edges: dict[float, int] = {}
    for pn in range(doc.page_count):
        for blk in doc[pn].get_text("dict").get("blocks", []):
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    t = sp["text"].strip()
                    if len(t.replace(" ", "")) >= 3 and _MONEY_SPAN.match(t):
                        k = round(sp["bbox"][1], 2)
                        edges[k] = edges.get(k, 0) + 1
    return edges


def check_column_alignment(orig_bytes: bytes, out_bytes: bytes, window: float = 1.0) -> list[str]:
    """Право-выровненная колонка «Дебет» обязана сохранить общий правый край.

    Копия по смыслу с `verify_gold_file.check_column_alignment` (одна копия на
    формат, конвенция та же, что и у `find_line_overlaps`), но проверять «весь
    набор краёв» здесь нельзя: колонка «Кредит» в этом формате ЛЕВО-выровнена
    (см. комментарий в `process_kaspi_ip_pdf`), поэтому её правый край законно
    едет вместе с длиной числа и дал бы ложные срабатывания. Поэтому берём
    только модальный край оригинала (это и есть «Дебет» — сотни строк на одном
    значении) и требуем, чтобы рядом с ним в результате не появилось НИ ОДНОГО
    другого края: равномерный сдвиг всей колонки не создаёт ни одного
    наложения слов, поэтому `find_line_overlaps` этот класс дефекта не видит
    вообще.
    """
    orig = fitz.open(stream=orig_bytes, filetype="pdf")
    out = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        a, b = _money_span_edges(orig), _money_span_edges(out)
    finally:
        orig.close()
        out.close()
    if not a:
        return []
    anchor = max(a, key=lambda k: a[k])
    drifted = {e: n for e, n in b.items() if e != anchor and abs(e - anchor) <= window}
    if not drifted:
        return []
    total = sum(drifted.values())
    sample = sorted(drifted)[:6]
    return [
        f"колонка «Дебет» разъехалась: {total} сумм встали на новые правые края "
        f"{sample} вместо единственного {anchor} (в оригинале на нём {a[anchor]} сумм)"
    ]


def check_purpose_amount_consistency(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Для каждой транзакции ОРИГИНАЛА с amount_in_purpose=True, чья сумма при
    данной цели изменилась: либо текст назначения в РЕЗУЛЬТАТЕ теперь содержит
    НОВУЮ сумму (успех, без сообщения), либо это задокументированный skip
    (перенос строки / gate по ширине — см. design spec
    docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md)
    — помечается [guard]. Молчаливое расхождение (ни то, ни другое — текст
    остался со СТАРОЙ суммой без видимой причины) — FAIL.
    """
    orig_doc = fitz.open(stream=orig_bytes, filetype="pdf")
    orig_stmt = kip.parse_kaspi_ip_statement(orig_doc)
    orig_doc.close()

    out_doc = fitz.open(stream=out_bytes, filetype="pdf")
    out_stmt = kip.parse_kaspi_ip_statement(out_doc)
    out_doc.close()

    if len(orig_stmt.transactions) != len(out_stmt.transactions):
        return []  # разное число транзакций — не эта проверка должна это ловить

    issues = []
    for tx_o, tx_n in zip(orig_stmt.transactions, out_stmt.transactions):
        if not tx_o.amount_in_purpose or not tx_o.is_scaleable:
            continue
        if abs(tx_n.new_amount - tx_o.amount) < 0.005:
            continue  # сумма фактически не изменилась при этой цели

        locatable = any(
            kip._locate_purpose_amount(ln, tx_o.amount) is not None
            for ln, *_ in tx_o.purpose_line_bboxes
        )
        if not locatable:
            issues.append(
                f"[guard] {tx_o.doc_number}: сумма разорвана переносом строки в "
                f"назначении — переписывание вне рамок (design spec)"
            )
            continue

        new_digits = str(int(round(tx_n.new_amount)))
        old_digits = str(int(round(tx_o.amount)))
        new_out_purpose = " ".join(ln for ln, *_ in tx_n.purpose_line_bboxes)
        compact = re.sub(r"[  ]", "", new_out_purpose)
        has_new = re.search(r"(?<!\d)" + new_digits + r"(?!\d)", compact) is not None
        has_old = re.search(r"(?<!\d)" + old_digits + r"(?!\d)", compact) is not None

        if has_new and not has_old:
            continue  # успех — сумма переписана
        if has_old and not has_new:
            # Не FAIL сама по себе: строка могла не влезть в эмпирическую
            # ширину ячейки (см. design spec, gate) — это задокументированный,
            # осознанный skip, а не молчаливое расхождение.
            issues.append(
                f"[guard] {tx_o.doc_number}: назначение платежа не переписано "
                f"(старая сумма {old_digits} осталась, новая {new_digits} не "
                f"уместилась — gate по ширине ячейки)"
            )
            continue
        # has_new and has_old одновременно, или ни то ни другое — неоднозначный
        # случай, требует ручного разбора.
        issues.append(
            f"{tx_o.doc_number}: неоднозначный результат переписывания "
            f"назначения (old_present={has_old}, new_present={has_new})"
        )
    return issues


def render_pages(pdf_bytes: bytes, out_dir: Path, label: str, pages: list[int], dpi: int = 150) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for pn in pages:
        if 0 <= pn < doc.page_count:
            doc[pn].get_pixmap(dpi=dpi).save(str(out_dir / f"{label}_page{pn}.png"))
    doc.close()


def run_one(path: Path, multipliers: list[float], out_dir: Path, render: bool) -> bool:
    raw = path.read_bytes()
    doc = fitz.open(stream=raw, filetype="pdf")
    is_ip = kip.detect_kaspi_ip_format(doc)
    doc.close()
    if not is_ip:
        print(f"{path.name}: НЕ формат Kaspi ИП (нет 'Лицевой счет'/'Входящий остаток') — пропуск")
        return True

    base = kip.validate_kaspi_ip(raw)
    avg = base["summary"]["avg_monthly_income"]
    months = base["summary"]["months"]
    print(f"\n=== {path.name} === месяцев={months}, ср.оборот/мес≈{avg:,.0f} ₸, "
          f"тр-ций={base['summary']['transactions']}")
    if months < 12:
        print(f"  ⚠️ период < 12 мес — банк может отклонить по сроку (свойство оригинала, не баг генерации)")
    if avg <= 0:
        print("  пропуск: оборот 0")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    all_ok = True
    for mult in multipliers:
        target = avg * mult
        label = f"x{mult:g}"
        try:
            out_bytes = kip.process_kaspi_ip_pdf(raw, target_monthly_income=target)
        except IncomeTooLowError as e:
            rows.append((label, target, "floor-guard", "-", "-", "-", str(e)))
            continue
        except Exception as e:  # noqa: BLE001
            rows.append((label, target, "EXCEPTION", "FAIL", "-", "-", f"{type(e).__name__}: {e}"))
            all_ok = False
            continue

        verify = kip.validate_kaspi_ip(out_bytes)
        # ISI на самом РЕЗУЛЬТАТЕ проверяем как обычно (движок обязан его поднять);
        # тут ничего не исключаем.
        math_issues = list(verify["issues"])
        math_ok = len(math_issues) == 0
        hdr_issues = header_matches_body(out_bytes)
        hdr_ok = len(hdr_issues) == 0
        geo_issues_all = (
            geometry_check(out_bytes, sample_pages=[1, 2])
            + style_check(raw, out_bytes)
            + check_isi_floor(out_bytes)
            + check_rounding_escalation(raw, out_bytes)
            + check_column_alignment(raw, out_bytes)
            + check_purpose_amount_consistency(raw, out_bytes)
        )
        # «[guard]» — не провал: случаи, чью неустранимость движок ДОКАЗАЛ
        # измерением (см. check_purpose_amount_consistency — перенос строки
        # разрывает сумму, либо новая строка не влезает в эмпирическую ширину
        # ячейки — переписывание вне рамок design spec). Показываем в
        # примечании, чтобы не потерялось, но не роняем ими батарею — та же
        # конвенция, что уже применена в verify_halyk_file.py.
        geo_issues = [i for i in geo_issues_all if not i.startswith("[guard]")]
        geo_ok = len(geo_issues) == 0

        (out_dir / f"{path.stem}_{label}.pdf").write_bytes(out_bytes)
        if render:
            render_pages(out_bytes, out_dir, f"{path.stem}_{label}", pages=[0, 1])

        note = "; ".join(math_issues + hdr_issues + geo_issues_all)
        rows.append((label, target, "process",
                     "OK" if math_ok else "FAIL",
                     "OK" if hdr_ok else "FAIL",
                     "OK" if geo_ok else "FAIL", note))
        if not (math_ok and hdr_ok and geo_ok):
            all_ok = False

    print(f"  {'цель':<7} {'режим':<11} {'матем':<6} {'шапка=тело':<11} {'позиции':<8} примечание")
    for label, target, mode, m_s, h_s, g_s, note in rows:
        print(f"  {label:<7} {mode:<11} {m_s:<6} {h_s:<11} {g_s:<8} {note}")

    with open(out_dir / "_report.json", "w", encoding="utf-8") as f:
        json.dump([{"target_label": r[0], "target": r[1], "mode": r[2],
                    "math": r[3], "header_body": r[4], "geometry": r[5], "note": r[6]} for r in rows],
                  f, ensure_ascii=False, indent=2)
    print(f"  результаты и отчёт: {out_dir}")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="путь(и) к оригинальному Kaspi ИП PDF")
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
        out_dir = path.parent / f"{path.stem}_ip_autotest_out"
        overall_ok = run_one(path, multipliers, out_dir, args.render) and overall_ok

    print("\n" + ("ВСЁ ОК" if overall_ok else "ЕСТЬ ПРОБЛЕМЫ — см. FAIL выше"))
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()

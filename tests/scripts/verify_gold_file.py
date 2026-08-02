"""
Универсальный автотест для ЛЮБОГО файла Kaspi Gold (legacy или cert-формат):
прогоняет его через реальный pipeline (тот же диспетчер upscale/downscale,
что main.py:/process) на батарее целей, для каждого результата проверяет:

  1. Математику — main._verify_pdf() (та же проверка, что стоит за /verify):
     баланс через транзакции, баланс через header, running balance, ISI,
     справка = баланс выписки, xref, целостность стримов.
  2. Позиционирование текста и шрифт — программно, по ВСЕМ страницам, без
     ручного разглядывания картинок:
       - слова на одной визуальной строке не накладываются друг на друга
         (перекрытие bounding box'ов по X);
       - текст не пересекает видимую границу своей ячейки таблицы
         (рамка — вектор, а не слово, поэтому предыдущая проверка её не
         видит);
       - правые края право-выровненной колонки «Сумма» совпадают с
         оригиналом (колонка не стала рваной);
       - имя и кегль шрифта нигде не изменились;
       - на cert-справке сумма не уезжает левее заголовка своей колонки
         (ровно тот класс бага, что был найден на реальном файле: короткий
         оригинал после сильного роста цифр вылезал за левую границу
         таблицы).
  3. Круглые/реалистичные цифры — масштабированные пополнения остаются
     целыми тенге на «человеческой» сетке (_round_to_natural), а не
     «232 682,62 ₸» после какого-нибудь цикла-корректировки баланса.

Запуск из корня репозитория:
    python tests/scripts/verify_gold_file.py <путь_к_файлу.pdf>
    python tests/scripts/verify_gold_file.py <путь> --targets 0.6,1.05,2,10,50
    python tests/scripts/verify_gold_file.py <путь> --render

Батарея целей по умолчанию — множители текущего среднемесячного дохода
файла: 0.6 (обычно ниже floor — проверяем, что downscale-защита корректно
срабатывает), 1.05 (near-noop), 2, 10, 50 (сильный рост цифр — именно
здесь чаще всего вылезают позиционные баги).

Результаты (обработанные PDF, при --render — ещё и PNG страниц) кладутся
в <папка_с_исходником>/<имя>_autotest_out/ — рядом с исходным файлом.

Файлы НЕ Kaspi Gold формата (Halyk, Kaspi ИП) — вне охвата этого скрипта,
он их только детектирует и сообщает, что для них есть отдельные validate_*.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# main.py вызывает _init_db() на уровне модуля — подставляем безобидный путь
# ДО импорта, чтобы не трогать реальный journal.db репозитория.
os.environ.setdefault("PDFAI_DB_PATH", os.path.join(tempfile.gettempdir(), "pdfai_gold_autotest_journal.db"))

import fitz  # noqa: E402

import main as m  # noqa: E402
import pdf_service as p  # noqa: E402
from pdf_service_downscale import process_downscale, is_downscale_request, IncomeTooLowError  # noqa: E402
from halyk_pdf_service import detect_halyk_format  # noqa: E402
from kaspi_ip_pdf_service import detect_kaspi_ip_format  # noqa: E402

DEFAULT_TARGET_MULTIPLIERS = [0.6, 1.05, 2, 10, 50]


def find_line_overlaps(page: "fitz.Page", tolerance: float = 0.5) -> list[str]:
    """Слова на одной визуальной строке не должны накладываться по X."""
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
            # Kaspi иногда рисует один и тот же текст дважды в ТОЧНО тех же
            # координатах (напр. футер "АО «Kaspi Bank»..." — воспроизведено
            # уже на оригинале, не связано с обработкой) — визуально это не
            # даёт искажения (глифы идеально совпадают), в отличие от
            # коллизии РАЗНОГО контента. Такие точные дубликаты не репортим.
            if a[3] == b[3] and abs(a[0] - b[0]) < tolerance and abs(a[1] - b[1]) < tolerance:
                continue
            issues.append(
                f"наложение текста на y~{yk:.0f}: {a[3]!r}[до x={a[1]:.1f}] "
                f"vs {b[3]!r}[с x={b[0]:.1f}]"
            )
    return issues


def find_frame_overflows(page: "fitz.Page", tolerance: float = 1.0) -> list[str]:
    """Текст не должен пересекать ВИДИМУЮ границу своей ячейки таблицы.

    `find_line_overlaps` этого не ловит в принципе: рамка — векторная линия, а
    не слово, поэтому число может залезть на границу колонки, ни с чем не
    «столкнувшись». Именно так выглядел реальный баг на gold_statement.pdf:
    итоговый остаток в сводной таблице стр. 1 вылезал вправо за границу ячейки
    (+1.4 pt при ×2 … +5.3 pt при ×200), потому что сдвиг X недобирал прирост
    ширины числа.

    Берём только обведённые прямоугольники (`s`/`fs`) — заливки (`f`) невидимы,
    выход за их край ничего не портит и давал бы ложные срабатывания.
    """
    cells = [
        d["rect"] for d in page.get_drawings()
        if d["type"] in ("s", "fs") and d["rect"].width > 20 and 5 < d["rect"].height < 40
    ]
    if not cells:
        return []

    issues = []
    for w in page.get_text("words"):
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        cy = (y0 + y1) / 2
        host = [r for r in cells if r.y0 - 1 <= cy <= r.y1 + 1 and r.x0 < x1 and x0 < r.x1]
        if not host:
            continue
        if any(r.x0 - tolerance <= x0 and x1 <= r.x1 + tolerance for r in host):
            continue
        r = min(host, key=lambda r: abs((r.x0 + r.x1) / 2 - (x0 + x1) / 2))
        side = "влево" if (r.x0 - x0) > (x1 - r.x1) else "вправо"
        over = max(r.x0 - x0, x1 - r.x1)
        issues.append(
            f"текст за границей ячейки на y~{cy:.0f}: {text!r} вылезает {side} "
            f"на {over:.2f} pt (ячейка x={r.x0:.1f}..{r.x1:.1f})"
        )
    return issues


def amount_right_edges(doc: "fitz.Document", start_page: int) -> dict[float, int]:
    """Правые края колонки «Сумма» (x1 глифа ₸) по всем строкам выписки.

    Суммы в этой колонке право-выровнены, поэтому на неиспорченном файле весь
    набор краёв — это буквально несколько значений (у оригинала: 190.93 для
    1798 строк тела + 300.76/270.26 для сводки). Любое «размывание» этого
    набора после обработки = колонка стала рваной.
    """
    edges: dict[float, int] = {}
    for pn in range(start_page, doc.page_count):
        rows: dict[float, list] = {}
        for w in doc[pn].get_text("words"):
            rows.setdefault(round(w[1], 1), []).append(w)
        for g in rows.values():
            amt = [w for w in g if w[4] == "₸" and w[2] < 400]
            if amt:
                k = round(amt[0][2], 2)
                edges[k] = edges.get(k, 0) + 1
    return edges


def check_column_alignment(orig: "fitz.Document", out: "fitz.Document", start_page: int) -> list[str]:
    """Набор правых краёв колонки «Сумма» обязан совпасть с оригиналом."""
    a = amount_right_edges(orig, start_page)
    b = amount_right_edges(out, start_page)
    extra = sorted(set(b) - set(a))
    if not extra:
        return []
    return [
        f"колонка «Сумма» разъехалась: {sum(b[x] for x in extra)} сумм встали на "
        f"новые правые края {extra[:6]} (в оригинале только {sorted(a)})"
    ]


def check_fonts(orig: "fitz.Document", out: "fitz.Document") -> list[str]:
    """Имя и кегль шрифта не должны меняться нигде в документе.

    Банковский PDF, перерисовавший число другим шрифтом или уменьшивший кегль,
    чтобы «влезло», — самостоятельный признак правки (у Halyk такая проверка
    уже есть в verify_halyk_file.py, для Kaspi Gold её не было).
    """
    def spans(doc):
        res: dict[tuple, int] = {}
        for pn in range(doc.page_count):
            for b in doc[pn].get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for s in line["spans"]:
                        k = (s["font"], round(s["size"], 2))
                        res[k] = res.get(k, 0) + 1
        return res

    a, b = spans(orig), spans(out)
    issues = []
    for k in sorted(set(b) - set(a)):
        issues.append(f"появился новый шрифт/кегль: {k[0]} {k[1]} pt ({b[k]} фрагм.)")
    for k in sorted(set(a) - set(b)):
        issues.append(f"пропал шрифт/кегль оригинала: {k[0]} {k[1]} pt")
    return issues


def check_natural_rounding(out: "fitz.Document", start_page: int) -> list[str]:
    """Критерий «круглые/реалистичные цифры» для Kaspi Gold.

    Реальные пополнения в этом формате всегда целые тенге и лежат на «человеческой»
    сетке `pdf_service._round_to_natural` (шаги 50/100/500/1000 по величине).
    `round(x, 2)` внутри любого цикла-корректировки балансa даёт суммы вида
    «232 682,62 ₸» — арифметика при этом сходится, поэтому ни одна проверка
    математики такое не поймает.
    """
    stmt = p.parse_full_statement(out, start_page=start_page)
    salary = [t for t in stmt.transactions if t.sign > 0 and t.is_salary]
    cents = [t for t in salary if round(t.amount * 100) % 100 != 0]
    offgrid = [t for t in salary if p._round_to_natural(t.amount) != round(t.amount, 2)]
    issues = []
    if cents:
        issues.append(
            f"{len(cents)} из {len(salary)} пополнений с копейками "
            f"(напр. {[f'{t.amount:,.2f}' for t in cents[:3]]})"
        )
    if offgrid:
        issues.append(
            f"{len(offgrid)} из {len(salary)} пополнений вне «человеческой» сетки "
            f"(напр. {[f'{t.amount:,.2f}' for t in offgrid[:3]]})"
        )
    return issues


def check_cert_left_alignment(doc: "fitz.Document", tolerance: float = 1.0) -> list[str]:
    """Сумма на cert-справке не должна уезжать левее заголовка своей колонки.

    Именно этот инвариант поймал реальный баг: короткий оригинальный баланс
    ("1 898,08"), выросший после апскейла до 13+ разрядов, писался со сдвигом
    влево, вылезая за левую границу таблицы — при этом парсер (gap-based
    кластеризация) всё равно читал число правильно, поэтому чисто
    математическая проверка (_verify_pdf) этого не ловит вообще.
    """
    if p.detect_statement_format(doc) != "cert":
        return []

    page = doc[0]
    words = page.get_text("words")
    y_groups: dict[int, list[tuple[float, str]]] = {}
    for w in words:
        x0, y0, text = w[0], w[1], w[4]
        yk = round(y0 / 3) * 3
        y_groups.setdefault(yk, []).append((x0, text))

    lines = []
    for yk in sorted(y_groups):
        ws = sorted(y_groups[yk], key=lambda t: t[0])
        lines.append((yk, " ".join(t for _, t in ws), ws))

    header_y, header_ws = None, None
    for yk, text, ws in lines:
        if "Сумма на счете" in text and "USD" in text and "EUR" in text:
            header_y, header_ws = yk, ws
            break
    if header_y is None:
        return ["не найдена строка заголовка справки (Сумма на счете/USD/EUR) — возможно сломан рендер стр. 0"]

    issues = []
    for yk, text, ws in lines:
        if yk <= header_y or yk > header_y + 30:
            continue
        if "₸" not in text:
            continue
        value_x0 = min(x for x, _ in ws)
        header_x0 = min(x for x, _ in header_ws)
        if value_x0 < header_x0 - tolerance:
            issues.append(
                f"сумма справки уехала левее заголовка колонки: "
                f"value_x0={value_x0:.2f} < header_x0={header_x0:.2f} "
                f"(строка: {text!r})"
            )
        break
    return issues


def geometry_check(pdf_bytes: bytes, orig_bytes: bytes) -> list[str]:
    """Все проверки критерия «позиционирование и шрифт» — по ВСЕМ страницам.

    Раньше сканировались только стр. 0 и 1 (аргумент extra_pages нигде не
    заполнялся), то есть ни одна из ~40 страниц с самими транзакциями не
    проверялась вообще — а именно там живёт колонка «Сумма», ради которой
    проверка и нужна.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    orig = fitz.open(stream=orig_bytes, filetype="pdf")
    issues = []
    fmt = p.detect_statement_format(doc)
    start_page = 1 if fmt == "cert" else 0

    for pn in range(doc.page_count):
        for issue in find_line_overlaps(doc[pn]):
            issues.append(f"[стр. {pn}] {issue}")
        for issue in find_frame_overflows(doc[pn]):
            issues.append(f"[стр. {pn}] {issue}")

    issues.extend(f"[стр. 0] {i}" for i in check_cert_left_alignment(doc))
    issues.extend(check_column_alignment(orig, doc, start_page))
    issues.extend(check_fonts(orig, doc))
    issues.extend(check_natural_rounding(doc, start_page))
    orig.close()
    doc.close()
    return issues


def render_pages(pdf_bytes: bytes, out_dir: Path, label: str, pages: list[int], dpi: int = 150) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for pn in pages:
        if not (0 <= pn < doc.page_count):
            continue
        pix = doc[pn].get_pixmap(dpi=dpi)
        pix.save(str(out_dir / f"{label}_page{pn}.png"))
    doc.close()


def run_one(path: Path, target_multipliers: list[float], out_dir: Path, render: bool) -> bool:
    raw = path.read_bytes()
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        if detect_halyk_format(doc):
            print(f"{path.name}: формат Halyk Bank — этот скрипт только для Kaspi Gold, используйте validate_halyk()")
            return True
        if detect_kaspi_ip_format(doc):
            print(f"{path.name}: формат Kaspi ИП — этот скрипт только для Kaspi Gold, используйте validate_kaspi_ip()")
            return True
        fmt = p.detect_statement_format(doc)
        start_page = 1 if fmt == "cert" else 0
        stmt = p.parse_full_statement(doc, start_page=start_page)
    finally:
        doc.close()

    months = max(1, p._estimate_months([t.date for t in stmt.transactions if t.date]))
    avg_income = stmt.total_income / months if months else 0.0
    print(f"\n=== {path.name} === формат={fmt}, тр-ций={len(stmt.transactions)}, "
          f"мес.={months}, ср.доход/мес≈{avg_income:,.0f} ₸")

    if avg_income <= 0:
        print("  пропуск: не удалось определить средний доход (income<=0)")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    all_ok = True
    rows = []

    for mult in target_multipliers:
        target = avg_income * mult
        label = f"x{mult:g}"
        try:
            use_downscale = is_downscale_request(stmt, target)
            if use_downscale:
                out_bytes = process_downscale(raw, target_monthly_income=target)
            else:
                out_bytes = p.process_pdf_bytes_raw(raw, target_monthly_income=target)
        except IncomeTooLowError as e:
            rows.append((label, target, "floor-guard", "-", "-", str(e)))
            continue
        except p.HeaderCellOverflowError as e:
            rows.append((label, target, "cell-guard", "-", "-", str(e)))
            continue
        except Exception as e:  # noqa: BLE001 — репортим любую поломку, не молчим
            rows.append((label, target, "EXCEPTION", "FAIL", "-", f"{type(e).__name__}: {e}"))
            all_ok = False
            continue

        verify = m._verify_pdf(out_bytes)
        math_ok = verify["passed"]
        geo_issues = geometry_check(out_bytes, orig_bytes=raw)
        geo_ok = len(geo_issues) == 0

        fname = out_dir / f"{path.stem}_{label}.pdf"
        fname.write_bytes(out_bytes)

        if render:
            render_pages(out_bytes, out_dir, f"{path.stem}_{label}", pages=[0, start_page])

        note = "; ".join(verify["issues"] + geo_issues) if (verify["issues"] or geo_issues) else ""
        rows.append((
            label, target,
            "downscale" if use_downscale else "upscale",
            "OK" if math_ok else "FAIL",
            "OK" if geo_ok else "FAIL",
            note,
        ))
        if not (math_ok and geo_ok):
            all_ok = False

    print(f"  {'цель':<8} {'режим':<11} {'математика':<11} {'позиции':<9} примечание")
    for label, target, mode, math_status, geo_status, note in rows:
        print(f"  {label:<8} {mode:<11} {math_status:<11} {geo_status:<9} {note}")

    with open(out_dir / "_report.json", "w", encoding="utf-8") as f:
        json.dump(
            [{"target_label": r[0], "target": r[1], "mode": r[2], "math": r[3], "geometry": r[4], "note": r[5]} for r in rows],
            f, ensure_ascii=False, indent=2,
        )
    print(f"  результаты и отчёт: {out_dir}")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="путь(и) к оригинальному Kaspi Gold PDF")
    parser.add_argument("--targets", default=",".join(str(x) for x in DEFAULT_TARGET_MULTIPLIERS),
                         help="множители текущего среднемесячного дохода через запятую (default: %(default)s)")
    parser.add_argument("--render", action="store_true", help="дополнительно сохранить PNG стр. 0/1 каждого результата")
    args = parser.parse_args()

    multipliers = [float(x) for x in args.targets.split(",")]

    overall_ok = True
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.exists():
            print(f"{path}: файл не найден")
            overall_ok = False
            continue
        out_dir = path.parent / f"{path.stem}_autotest_out"
        ok = run_one(path, multipliers, out_dir, args.render)
        overall_ok = overall_ok and ok

    print("\n" + ("ВСЁ ОК" if overall_ok else "ЕСТЬ ПРОБЛЕМЫ — см. FAIL выше"))
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()

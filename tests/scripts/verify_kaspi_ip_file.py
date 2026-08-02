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
        geo_issues = geometry_check(out_bytes, sample_pages=[1, 2])
        geo_ok = len(geo_issues) == 0

        (out_dir / f"{path.stem}_{label}.pdf").write_bytes(out_bytes)
        if render:
            render_pages(out_bytes, out_dir, f"{path.stem}_{label}", pages=[0, 1])

        note = "; ".join(math_issues + hdr_issues + geo_issues)
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

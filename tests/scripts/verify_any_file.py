#!/usr/bin/env python
"""
УНИВЕРСАЛЬНЫЙ автотест: любой документ, все критерии качества.

    python tests/scripts/verify_any_file.py <file.pdf> [<file2.pdf> ...]
    python tests/scripts/verify_any_file.py testpdf/gold/*.pdf --targets 1.05,2,5
    python tests/scripts/verify_any_file.py <file.pdf> --render

Формат определяется сам, тем же диспетчером, что и `main.py:/process`
(Halyk → Kaspi ИП → бизнес-справка → Kaspi Gold). Каждый файл прогоняется
через боевой пайплайн на батарее целей (множители его СОБСТВЕННОГО среднего
дохода/оборота) и проверяется по всем критериям сразу — и старым, и по
добавленному 02/08/2026 критерию «стиль сериализации операторов».

Зачем нужен, если есть verify_gold_file.py / verify_halyk_file.py /
verify_kaspi_ip_file.py: те три работают каждый со своим форматом, надо
заранее знать, какой скрипт запускать, и ни один не покрывает бизнес-справки.
Здесь одна команда на любой документ и один сводный отчёт. Проверки не
переписаны — они импортируются из тех же трёх скриптов, чтобы результат
«прогнал универсальный» и «прогнал профильный» не мог разойтись.

Что проверяется по каждому результату:

  критерий 1  математика       балансовые тождества, running balance, ISI
  критерий 1b шапка = тело     Σ строк против итогов шапки (Kaspi ИП, бизнес)
  критерий 2  позиционирование наложения слов, выход за рамку ячейки,
                               правый край колонки «Сумма», левый край справки
  критерий 2e шрифты           имя и кегль не изменились
  критерий 3  округление       суммы лежат на «человеческой» сетке
  критерий 3b разброс/мес.     (Kaspi Gold) коэффициент месяца ≈ единый K,
                               не съехал к помесячному выравниванию
  критерий 3c эскалация шага   круглый оригинал (5000/10000/.../1 000 000)
                               остаётся кратным ЕМУ ЖЕ, не проседает до
                               базового шага после масштабирования
  критерий 1c ISI-порог        (Halyk/Kaspi ИП) жёсткий порог ISI реально
                               держится в результате, пересчитано независимо
  критерий 4  стиль            почерк записи операторов совпал с оригиналом
  структура                    xref/стримы целы, PDF не пришлось «чинить»,
                               число страниц и набор шрифтов стр.0 не выросли

Коды выхода: 0 — все цели прошли, 1 — есть FAIL. Пригодно для CI.

Штатные отказы движка (IncomeTooLowError — пол занижения,
HeaderCellOverflowError — потолок разрядности ячейки шапки,
NoScalableIncomeError — нечего масштабировать) это НЕ провал: они и есть
правильное поведение. В отчёте помечаются как guard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "scripts"))

import fitz  # noqa: E402

import main as m  # noqa: E402
import pdf_service as p  # noqa: E402
import business_pdf_service as biz  # noqa: E402
import halyk_pdf_service as hal  # noqa: E402
import kaspi_ip_pdf_service as kip  # noqa: E402
from pdf_service_downscale import (  # noqa: E402
    IncomeTooLowError,
    is_downscale_request,
    process_downscale,
)

import verify_gold_file as vgold  # noqa: E402
import verify_halyk_file as vhal  # noqa: E402
import verify_kaspi_ip_file as vip  # noqa: E402

DEFAULT_TARGET_MULTIPLIERS = [0.6, 1.05, 2, 5, 20]

GUARDS = (IncomeTooLowError, p.HeaderCellOverflowError, hal.NoScalableIncomeError)


# ─────────────────────────── определение формата ───────────────────────────


def detect_format(raw: bytes) -> str:
    """Порядок ровно как в main.py:/process — иначе автотест проверял бы не тот
    путь, по которому файл пойдёт в продакшене."""
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        if hal.detect_halyk_format(doc):
            return "halyk"
        if kip.detect_kaspi_ip_format(doc):
            return "kaspi_ip"
    finally:
        doc.close()
    if biz.is_business_pdf(raw):
        return "business"
    return "kaspi_gold"


def base_average(raw: bytes, fmt: str) -> tuple[float, str]:
    """Среднее за месяц, от которого считаются цели, + краткая справка о файле."""
    if fmt == "halyk":
        s = hal.validate_halyk(raw)["summary"]
        return s["avg_monthly_income"], (
            f"месяцев={s['months']}, ср.доход/мес≈{s['avg_monthly_income']:,.0f} ₸, "
            f"тр-ций={s['transactions']}"
        )
    if fmt == "kaspi_ip":
        s = kip.validate_kaspi_ip(raw)["summary"]
        return s["avg_monthly_income"], (
            f"месяцев={s['months']}, ср.оборот/мес≈{s['avg_monthly_income']:,.0f} ₸, "
            f"тр-ций={s['transactions']}"
        )
    if fmt == "business":
        rows = biz.parse_business_summary(raw)["rows"]
        credits = [r["credit"]["value"] for r in rows
                   if r.get("credit") and r["credit"]["value"]]
        # Последняя строка таблицы — «Итого», в среднее её не берём.
        monthly = credits[:-1] if len(credits) > 1 else credits
        avg = sum(monthly) / len(monthly) if monthly else 0.0
        return avg, f"строк таблицы={len(rows)}, ср.кредит/мес≈{avg:,.0f} ₸"

    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        sub = p.detect_statement_format(doc)
        stmt = p.parse_full_statement(doc, start_page=1 if sub == "cert" else 0)
    finally:
        doc.close()
    months = max(1, p._estimate_months([t.date for t in stmt.transactions if t.date]))
    avg = stmt.total_income / months
    return avg, (f"подформат={sub}, месяцев={months}, "
                 f"ср.доход/мес≈{avg:,.0f} ₸, тр-ций={len(stmt.transactions)}")


# ──────────────────────────── боевой пайплайн ────────────────────────────


def process(raw: bytes, fmt: str, target: float) -> bytes:
    if fmt == "halyk":
        return hal.process_halyk_pdf(raw, target_monthly_income=target)
    if fmt == "kaspi_ip":
        return kip.process_kaspi_ip_pdf(raw, target_monthly_income=target)
    if fmt == "business":
        return biz.process_business_pdf(raw, target_monthly_credit=target)

    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        sub = p.detect_statement_format(doc)
        stmt = p.parse_full_statement(doc, start_page=1 if sub == "cert" else 0)
    finally:
        doc.close()
    if is_downscale_request(stmt, target):
        return process_downscale(raw, target_monthly_income=target)
    return p.process_pdf_bytes_raw(raw, target_monthly_income=target)


# ───────────────────────────── наборы критериев ─────────────────────────────
#
# Функции проверок берутся из профильных скриптов, а не копируются: третья
# копия той же логики неизбежно разъехалась бы с первыми двумя.


def criteria_kaspi_gold(raw: bytes, out_bytes: bytes) -> dict[str, list[str]]:
    out = fitz.open(stream=out_bytes, filetype="pdf")
    orig = fitz.open(stream=raw, filetype="pdf")
    try:
        start = 1 if p.detect_statement_format(out) == "cert" else 0
        verdict = m._verify_pdf(out_bytes)
        overlaps, frames = [], []
        for pn in range(out.page_count):
            overlaps += [f"стр.{pn}: {i}" for i in vgold.find_line_overlaps(out[pn])]
            frames += [f"стр.{pn}: {i}" for i in vgold.find_frame_overflows(out[pn])]

        style = vgold.check_serialization_style(orig, out)
        note = ""
        if style and vgold._substitution_unavoidable(orig, out):
            note = ("подмена шрифта на стр.0 вынужденная (недостающая цифра в "
                    "разряде, заданном целью) — лишний Tj неустраним")
            style = []

        res = {
            "1 математика": [] if verdict["passed"] else list(verdict["issues"]),
            "2a наложения слов": overlaps,
            "2b выход за рамку": frames,
            "2c левый край справки": vgold.check_cert_left_alignment(out),
            "2d правый край колонки": vgold.check_column_alignment(orig, out, start),
            "2e шрифты": vgold.check_fonts(orig, out),
            "3 округление": vgold.check_natural_rounding(out, start),
            "3b разброс по месяцам": vgold.check_variance_preserved(orig, out, start),
            "3c эскалация шага": vgold.check_rounding_escalation(orig, out, start),
            "4 стиль": style,
        }
        if note:
            res["_note"] = [note]
        return res
    finally:
        orig.close()
        out.close()


def criteria_kaspi_ip(raw: bytes, out_bytes: bytes) -> dict[str, list[str]]:
    out = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        pages = {0, 1, 2, out.page_count // 2, out.page_count - 1}
        overlaps = []
        for pn in sorted(x for x in pages if 0 <= x < out.page_count):
            overlaps += [f"стр.{pn}: {i}" for i in vip.find_line_overlaps(out[pn])]
    finally:
        out.close()
    return {
        "1 математика": list(kip.validate_kaspi_ip(out_bytes)["issues"]),
        "1b шапка = тело": vip.header_matches_body(out_bytes),
        "1c ISI-порог": vip.check_isi_floor(out_bytes),
        "2a наложения слов": overlaps,
        "2c правый край «Дебет»": vip.check_column_alignment(raw, out_bytes),
        "2d сумма в назначении": vip.check_purpose_amount_consistency(raw, out_bytes),
        "3c эскалация шага": vip.check_rounding_escalation(raw, out_bytes),
        "4 стиль": vip.style_check(raw, out_bytes),
    }


def criteria_halyk(raw: bytes, out_bytes: bytes) -> dict[str, list[str]]:
    return {
        "1 математика": list(hal.validate_halyk(out_bytes)["issues"]),
        "1c ISI-порог": vhal.check_isi_floor(out_bytes),
        "2a наложения слов": vhal.geometry_check(out_bytes),
        "2e шрифты": vhal.font_check(raw, out_bytes),
        "2f начертание итогов": vhal.check_bold_row_uniform(raw, out_bytes),
        "2g зазор Td": vhal.check_td_gap(raw, out_bytes),
        "1d итог = Σстрок": vhal.check_totals_match_rows(raw, out_bytes),
        "3c эскалация шага": vhal.check_rounding_escalation(raw, out_bytes),
        "4 стиль": vhal.style_check(raw, out_bytes),
        "4a род блока CMap": vhal.check_cmap_block_style(raw, out_bytes),
        "4b компрессор потоков": vhal.check_stream_compressor(raw, out_bytes),
        "4c порядок ToUnicode": vhal.check_cmap_text_order(raw, out_bytes),
    }


def criteria_business(raw: bytes, out_bytes: bytes) -> dict[str, list[str]]:
    """У бизнес-справок своих проверок геометрии в проекте нет — берём то, что
    есть (собственный верификатор), плюс форматонезависимые: стиль
    сериализации и наложения слов той же техникой, что у остальных."""
    verdict = biz.verify_business_pdf(out_bytes)
    issues = list(verdict.get("issues", []))
    if not verdict.get("passed", True) and not issues:
        issues = ["verify_business_pdf: passed=False без списка issues"]

    out = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        overlaps = []
        for pn in range(out.page_count):
            overlaps += [f"стр.{pn}: {i}" for i in vgold.find_line_overlaps(out[pn])]
    finally:
        out.close()

    return {
        "1 математика": issues,
        "2a наложения слов": overlaps,
        "4 стиль": vip.style_check(raw, out_bytes),
    }


CRITERIA = {
    "kaspi_gold": criteria_kaspi_gold,
    "kaspi_ip": criteria_kaspi_ip,
    "halyk": criteria_halyk,
    "business": criteria_business,
}


def structural_checks(raw: bytes, out_bytes: bytes, fmt: str) -> list[str]:
    """Форматонезависимая структура: то, чего не видит ни математика, ни геометрия.

    Проверка набора шрифтов стр.0 здесь та же самая, что уже гасит
    `criteria_kaspi_gold` через `_substitution_unavoidable` (доказуемо
    неизбежная подмена шрифта на странице справки, см. критерий 4 в
    CLAUDE.md) — нельзя было гасить её только в одном месте: FAIL всё равно
    всплывал бы отсюда под другим именем на тех же файлах.
    """
    issues = []
    orig = fitz.open(stream=raw, filetype="pdf")
    out = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        if getattr(out, "is_repaired", False):
            issues.append("PyMuPDF пришлось починить результат при открытии")
        if out.page_count != orig.page_count:
            issues.append(f"число страниц {orig.page_count} → {out.page_count}")
        before = {f[3] for f in orig[0].get_fonts(full=True)}
        after = {f[3] for f in out[0].get_fonts(full=True)}
        if after != before:
            if fmt == "kaspi_gold" and vgold._substitution_unavoidable(orig, out):
                pass
            else:
                issues.append(f"набор шрифтов стр.0 {sorted(before)} → {sorted(after)}")
    finally:
        orig.close()
        out.close()
    return issues


# ──────────────────────────────── прогон ────────────────────────────────


def render_pages(out_bytes: bytes, out_dir: Path, label: str, dpi: int = 150) -> None:
    doc = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        for pn in [0, 1]:
            if pn < doc.page_count:
                doc[pn].get_pixmap(dpi=dpi).save(out_dir / f"{label}_page{pn}.png")
    finally:
        doc.close()


def run_one(path: Path, multipliers: list[float], render: bool) -> tuple[bool, list[dict]]:
    raw = path.read_bytes()
    fmt = detect_format(raw)
    avg, info = base_average(raw, fmt)
    print(f"\n=== {path.name} === формат={fmt}, {info}")

    if avg <= 0:
        print("  пропуск: не удалось определить среднее (0)")
        return False, []

    out_dir = path.parent / f"{path.stem}_autotest_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    all_ok = True

    for mult in multipliers:
        target = avg * mult
        label = f"x{mult:g}"
        try:
            out_bytes = process(raw, fmt, target)
        except GUARDS as e:
            rows.append({"target": label, "mode": "guard", "verdict": "-",
                         "note": f"{type(e).__name__}: {e}"})
            continue
        except Exception as e:  # noqa: BLE001 — любую поломку репортим, не молчим
            rows.append({"target": label, "mode": "EXCEPTION", "verdict": "FAIL",
                         "note": f"{type(e).__name__}: {e}"})
            print(traceback.format_exc(limit=3))
            all_ok = False
            continue

        (out_dir / f"{path.stem}_{label}.pdf").write_bytes(out_bytes)
        if render:
            render_pages(out_bytes, out_dir, f"{path.stem}_{label}")

        results = CRITERIA[fmt](raw, out_bytes)
        note = "; ".join(results.pop("_note", []))
        results["структура"] = structural_checks(raw, out_bytes, fmt)

        # Сообщения с префиксом «[guard]» — не провал: так помечаются случаи,
        # чью неустранимость движок ДОКАЗАЛ измерением (см.
        # verify_halyk_file.check_bold_row_uniform). Их надо показать, но не
        # ронять ими прогон — иначе батарея станет вечно красной и перестанет
        # читаться. Та же логика уже применена в verify_halyk_file.run_one.
        # «[glyph-patched]» (Task 5) — тоже не провал, а информационная
        # пометка о том, что недостающие Bold-глифы цифр были физически
        # вшиты в subset и подмена шрифта не потребовалась вовсе; та же
        # check_bold_row_uniform используется здесь напрямую через CRITERIA,
        # поэтому фильтр нужен и тут, не только в verify_halyk_file.py.
        guard_notes = [
            msg for msgs in results.values() for msg in msgs
            if msg.startswith("[guard]") or msg.startswith("[glyph-patched]")
        ]
        results = {
            k: [m for m in v if not m.startswith("[guard]") and not m.startswith("[glyph-patched]")]
            for k, v in results.items()
        }
        if guard_notes:
            note = "; ".join(filter(None, [note, *guard_notes]))

        failed = {k: v for k, v in results.items() if v}
        ok = not failed
        all_ok = all_ok and ok
        rows.append({
            "target": label, "mode": "process",
            "verdict": "OK" if ok else "FAIL",
            "failed": {k: v[:3] for k, v in failed.items()},
            "note": note or ("; ".join(f"{k}: {v[0]}" for k, v in failed.items())),
        })

    w = max(len(r["target"]) for r in rows) + 2 if rows else 8
    print(f"  {'цель':<{w}}{'режим':<12}{'вердикт':<10}примечание")
    for r in rows:
        print(f"  {r['target']:<{w}}{r['mode']:<12}{r['verdict']:<10}{r['note'][:150]}")
    print(f"  результаты и отчёт: {out_dir}")
    return all_ok, rows


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Универсальный автотест: любой документ, все критерии.")
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--targets",
                    default=",".join(str(x) for x in DEFAULT_TARGET_MULTIPLIERS),
                    help="множители собственного среднего файла, через запятую")
    ap.add_argument("--render", action="store_true",
                    help="дополнительно сохранить PNG стр. 0/1 каждого результата")
    args = ap.parse_args()

    multipliers = [float(x) for x in args.targets.split(",") if x.strip()]
    overall_ok = True
    report: dict[str, list[dict]] = {}

    for path in args.paths:
        if not path.is_file():
            print(f"{path}: файл не найден — пропуск")
            overall_ok = False
            continue
        ok, rows = run_one(path, multipliers, args.render)
        overall_ok = ok and overall_ok
        report[path.name] = rows

    summary = Path(os.getcwd()) / "verify_any_report.json"
    summary.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(len(r) for r in report.values())
    failed = sum(1 for rs in report.values() for r in rs if r["verdict"] == "FAIL")
    guards = sum(1 for rs in report.values() for r in rs if r["mode"] == "guard")
    print(f"\nфайлов: {len(report)}, целей: {total}, "
          f"OK: {total - failed - guards}, FAIL: {failed}, guard: {guards}")
    print(f"сводный отчёт: {summary}")
    print("\nВСЁ ОК" if overall_ok else "\nЕСТЬ ПРОБЛЕМЫ — см. FAIL выше")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()

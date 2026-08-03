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
  3. Шрифт — имя шрифта не меняется; кегль допускается только ≥ базового
     минус узкий допуск (writer штатно слегка ужимает число в самой узкой
     колонке «Комиссия» при переполнении — это by design; резкое падение
     кегля было бы признаком проблемы).

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
    """Шрифт результата не должен использовать новых имён и не должен быть
    ужат сильнее допустимого относительно базового кегля этого имени."""
    orig = _font_sizes(orig_bytes)
    out = _font_sizes(out_bytes)
    base_size: dict[str, float] = {}
    for (name, size), _ in orig.items():
        base_size[name] = max(base_size.get(name, 0.0), size)
    issues = []
    for (name, size), _cnt in out.items():
        if name not in base_size:
            issues.append(f"новый шрифт {name} @ {size} (в оригинале не было)")
        elif size < base_size[name] * _MIN_FONT_RATIO:
            issues.append(f"{name} ужат до {size} (< {base_size[name]*_MIN_FONT_RATIO:.2f} = {int(_MIN_FONT_RATIO*100)}% базового {base_size[name]})")
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
        geo_issues = geometry_check(out_bytes) + style_check(raw, out_bytes)
        geo_ok = len(geo_issues) == 0
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

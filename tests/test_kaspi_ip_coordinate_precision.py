"""
Точность X-координаты в операторе `Tm` для право-выровненной колонки «Дебет»
Kaspi ИП (найдено 2026-08-04 на реальных файлах из `testpdf/kaspiPay`).

Каждая сумма ставится на место командой `1 0 0 1 <x> <y> Tm`. Замер на 4
реальных файлах (только денежные ячейки, отобранные тем же критерием, что
использует сам писатель — `[\\d ]+(,\\d{2})?`): в оригинале право-выровненная
колонка «Дебет» (X в диапазоне 150–240 pt) пишет X с РОВНО двумя знаками после
точки в 6707 из 6707 случаев (IP2/IP3/IP4/kaspiIP). Прочие денежные ячейки
(«Кредит», summary-строки — их X не пересчитывается, `new_x = current_x`) уже
воспроизводят исходную точность байт-в-байт и здесь не тестируются — трогать
их не нужно.

Причина расхождения — `pdf_service._fmt_coord` (общий формататор координат)
рассчитан на конвенцию Kaspi Gold, где сам генератор пишет ПЕРЕМЕННУЮ точность
(«42.5», «211», «510.94995» — обрезка незначащих нулей корректна). У Kaspi ИП
конвенция другая — ФИКСИРОВАННЫЕ 2 знака всегда. `_fmt_coord`'s "5 знаков,
обрезать нули" на пересчитанном (`right_edge - w_new`, плавающая точка) X
почти никогда не оканчивается на ноль, поэтому обрезать нечего: «207.392»
вместо «207.39». Замерено на реальном файле при ×2: 272 из 2286 «Дебет»-ячеек
IP2 получали 3 знака вместо 2 (kaspiIP — 35 из 421).

Тесты не зависят от tests/fixtures/ и проходят в любом чекауте.
"""

import re
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

import kaspi_ip_pdf_service as kip  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import verify_kaspi_ip_file as vkip  # noqa: E402

TM = re.compile(
    rb"1\s+0\s+0\s+1\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Tm\s+"
    rb"(/F\d+)\s+(\d+)\s+Tf\s+"
    rb"\(([^)]*)\)\s*Tj",
)
_AMOUNT_TEXT = re.compile(r"[\d  ]+(,\d{2})?")


def _decimals(x_bytes: bytes) -> int:
    s = x_bytes.decode("ascii")
    return 0 if "." not in s else len(s.split(".", 1)[1])


def _unescape(b: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == 0x5C and i + 1 < len(b):
            out.append(b[i + 1])
            i += 2
        else:
            out.append(b[i])
            i += 1
    return bytes(out)


def _debet_column_decimal_counts(pdf_bytes: bytes) -> dict:
    """Счётчик {знаков_после_точки: количество} для колонки «Дебет» (X 150..240),
    только среди страниц таблицы (стр. 1+) и только там, где текст ячейки —
    денежная сумма (та же фильтрация, что и у писателя)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    cmap, _ = kip.build_dynamic_cmap(doc)

    def dec_text(raw: bytes) -> str:
        b = _unescape(raw)
        return "".join(
            cmap.get(f"{(b[i] << 8 | b[i + 1]):04X}", "?") for i in range(0, len(b) - 1, 2)
        )

    counts: dict = {}
    try:
        for pno in range(1, doc.page_count):
            for x in doc[pno].get_contents():
                try:
                    buf = zlib.decompress(doc.xref_stream_raw(x))
                except Exception:  # noqa: BLE001
                    continue
                for m in TM.finditer(buf):
                    x_val = float(m.group(1))
                    if not (150.0 <= x_val <= 240.0):
                        continue
                    txt = dec_text(m.group(5)).strip()
                    if not _AMOUNT_TEXT.fullmatch(txt):
                        continue
                    d = _decimals(m.group(1))
                    counts[d] = counts.get(d, 0) + 1
    finally:
        doc.close()
    return counts


KASPI_IP_FILES = list(Path(r"C:\Users\Abylay\Desktop\testpdf\kaspiPay").glob("*.pdf"))


class TestFmtCoordIP:
    """Формататор X для право-выровненной колонки: всегда ровно 2 знака."""

    def test_always_two_decimals_regardless_of_trailing_zero(self):
        # Отличие от `pdf_service._fmt_coord`: тот бы дал "211" (без точки
        # вообще) для целого значения — здесь обязаны остаться "211.00".
        assert kip._fmt_coord_debet(211.0) == "211.00"

    def test_does_not_leave_a_third_digit(self):
        # Ровно тот случай из реального файла (IP2, ×2): было "207.392".
        assert kip._fmt_coord_debet(207.392) == "207.39"

    def test_rounds_not_truncates(self):
        assert kip._fmt_coord_debet(207.396) == "207.40"

    def test_negative_zero_normalized(self):
        assert kip._fmt_coord_debet(-0.001) == "0.00"

    def test_diverges_from_shared_fmt_coord_on_purpose(self):
        """Документирует, ПОЧЕМУ нужен отдельный формататор, а не общий.

        `pdf_service._fmt_coord` корректен для Kaspi Gold (переменная
        точность в оригинале) и НЕ подходит для Kaspi ИП (фиксированная).
        Если это когда-нибудь совпадёт — тест не про то, что функции должны
        совпадать, а про то, что они разные ПО КОНСТРУКЦИИ.
        """
        from pdf_service import _fmt_coord

        assert _fmt_coord(211.0) == "211"
        assert kip._fmt_coord_debet(211.0) == "211.00"


@pytest.mark.skipif(not KASPI_IP_FILES, reason="testpdf/kaspiPay недоступна в этом окружении")
class TestRealFileColumnPrecision:
    """Регрессия на реальных файлах: колонка «Дебет» после обработки — 100% 2 знака."""

    @pytest.mark.parametrize("mult", [1.05, 2, 20])
    def test_debet_column_always_two_decimals_after_scaling(self, mult):
        for path in KASPI_IP_FILES:
            raw = path.read_bytes()
            base = kip.validate_kaspi_ip(raw)["summary"]["avg_monthly_income"]
            try:
                out = kip.process_kaspi_ip_pdf(raw, target_monthly_income=base * mult)
            except Exception:  # noqa: BLE001 — floor-guard и т.п. не по теме теста
                continue
            counts = _debet_column_decimal_counts(out)
            bad = {d: n for d, n in counts.items() if d != 2}
            assert not bad, f"{path.name} x{mult}: не 2-значных X в «Дебет»: {bad}"

    @pytest.mark.parametrize("mult", [1.05, 2, 20])
    def test_debet_column_alignment_survives_two_decimal_rounding(self, mult):
        """Регрессия найдена 2026-08-04: фиксация 2 знаков сама по себе не
        ломает выравнивание, но БЕЗ округления `right_edge` ДО вычитания
        ширины нового текста — ломает (610.14/610.16 вместо единственного
        610.15 у оригинала на части ячеек, до 68% на IP4 ×20). Тот же тест,
        что и `verify_kaspi_ip_file.check_column_alignment` в боевой батарее,
        закреплён здесь отдельно, потому что предыдущий тест в этом классе
        (только счётчик знаков) эту регрессию совершенно не видел — оба FAIL
        нашлись одновременно на одном и том же фиксе, разными проверками.
        """
        for path in KASPI_IP_FILES:
            raw = path.read_bytes()
            base = kip.validate_kaspi_ip(raw)["summary"]["avg_monthly_income"]
            try:
                out = kip.process_kaspi_ip_pdf(raw, target_monthly_income=base * mult)
            except Exception:  # noqa: BLE001 — floor-guard и т.п. не по теме теста
                continue
            issues = vkip.check_column_alignment(raw, out)
            assert not issues, f"{path.name} x{mult}: {issues}"

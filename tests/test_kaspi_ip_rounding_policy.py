"""
Политика округления сумм Kaspi ИП и чтение реальных ширин глифов.

Оба механизма закрывают дефекты, найденные 2026-08-04 на реальных файлах из
`testpdf/kaspiPay` (см. соответствующий раздел CLAUDE.md), и оба имеют ветки,
которых ни один реальный файл не проходит, — поэтому они закреплены здесь, а
не только прогоном батареи:

  * `_round_amount` обязана НАСЛЕДОВАТЬ профиль оригинала. Прежняя версия
    навязывала сетку в 1000 ₸ любой сумме, из-за чего у процессинговых счетов
    (IP2/IP3, где кратны тысяче лишь ~6% сумм) в результате круглыми
    становились 100% — распределение, противоположное оригинальному. Отдельно
    вредил пол «меньше шага → ровно 1000 ₸»: он давал 278 одинаковых
    «1 000,00» подряд в колонке дебета.

  * `_primary_glyph_advances` читает ширины из /W шрифта. Её ЗАПАСНОЙ путь
    (вернуть {} и оставить писателю приближённую модель) на реальных файлах не
    исполняется ни разу, поэтому проверяется здесь синтетикой.

Тесты не зависят от tests/fixtures/ и проходят в любом чекауте.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kaspi_ip_pdf_service as kip  # noqa: E402


class TestRoundAmountInheritsOriginal:
    """Круглость и копейки результата определяются оригиналом, а не константой."""

    @pytest.mark.parametrize(
        "original,scaled,expected",
        [
            # Оригинал кратен крупному «человеческому» числу → шаг эскалируется
            # к нему же, а не остаётся базовой тысячей.
            (100_000.0, 703_912.44, 700_000.0),
            (65_000.0, 454_101.10, 455_000.0),
            (5_000.0, 12_480.0, 10_000.0),
            # Оригинал кратен только тысяче → базовый шаг.
            (23_000.0, 46_480.0, 46_000.0),
        ],
    )
    def test_round_original_escalates_step(self, original, scaled, expected):
        assert kip._round_amount(scaled, original=original) == expected

    @pytest.mark.parametrize(
        "original,scaled,expected",
        [
            # Оригинал НЕ кратен тысяче и БЕЗ копеек → результат целый, но не
            # притянут к тысяче: у IP2/IP3 такие суммы составляют ~94% кредита.
            (49_676.0, 99_352.37, 99_352.0),
            (1_748_417.0, 3_496_834.61, 3_496_835.0),
            # Оригинал с копейками → копейки сохраняются.
            (35.15, 70.297, 70.3),
            (123.70, 247.404, 247.4),
        ],
    )
    def test_non_round_original_is_not_snapped_to_thousands(self, original, scaled, expected):
        assert kip._round_amount(scaled, original=original) == pytest.approx(expected)

    def test_cents_never_appear_where_original_had_none(self):
        """Ячейка печатается в форме своего оригинала (`had_decimal`).

        Дробный результат в ячейке без копеек молча терял бы дробную часть при
        печати, а шапка считалась бы по неокруглённому числу — ровно так на
        реальном IP2 возникало расхождение шапка/тело в 1.67…4.18 ₸.
        """
        for original in (49_676.0, 1_748_417.0, 7.0, 999.0):
            got = kip._round_amount(original * 2.0071, original=original)
            assert got % 1 == 0, f"{original} → {got} получил копейки"

    def test_small_amount_is_not_inflated_to_the_round_unit(self):
        """Прежний пол выталкивал КАЖДУЮ мелкую сумму ровно в 1 000 ₸."""
        assert kip._round_amount(70.30, original=35.15) == pytest.approx(70.3)
        assert kip._round_amount(70.30, original=35.15) != kip._ROUND_UNIT

    def test_round_original_below_its_own_step_falls_back_to_base_unit(self):
        """Занижение увело круглую сумму ниже её укрупнённого шага.

        Эскалация работает только ВВЕРХ от базовой тысячи, поэтому здесь надо
        вернуться к ней, а не отдать число с копейками: оригинал был круглым.
        """
        got = kip._round_amount(23_253.49, original=50_000.0)
        assert got == 23_000.0

    def test_zero_or_negative_never_written(self):
        assert kip._round_amount(0.0, original=50_000.0) > 0
        assert kip._round_amount(-5.0, original=50_000.0) > 0

    def test_without_original_keeps_legacy_thousand_grid(self):
        """Вызов без `original` (обратная совместимость) — прежнее поведение."""
        assert kip._round_amount(46_480.0) == 46_000.0
        assert kip._round_amount(10.0) == kip._ROUND_UNIT


class TestGlyphAdvances:
    """Ширины берутся из /W; при неудаче разбора — пустой словарь, не мусор."""

    def test_parses_both_w_array_forms(self):
        # «c [w1 w2 …]» и «c_first c_last w» — обе формы встречаются в /W.
        widths = kip._parse_cid_widths("3 [ 277 ] 19 [ 556 556 ] 40 45 500")
        assert widths[3] == 277.0
        assert widths[19] == 556.0 and widths[20] == 556.0
        assert widths[40] == 500.0 and widths[45] == 500.0

    def test_real_kaspi_ip_widths_are_not_the_naive_half_digit_model(self):
        """Пробел и запятая тут 277, а не 278 (ровно половина цифры).

        Разницы в 1/1000 em хватало, чтобы 280 сумм из 1738 встали на соседний
        правый край — то есть приближение «разделитель = полцифры» неверно
        именно на этом шрифте, а не только теоретически.
        """
        widths = kip._parse_cid_widths("3 [ 277 ] 15 [ 277 333 277 277 556 556 556 ]")
        digit, separator = 556.0, widths[3]
        assert separator == 277.0
        assert separator != digit / 2

    def test_fallback_is_empty_dict_not_exception(self):
        """Если /W не разобрался, писатель обязан получить {} и откатиться."""

        class _NoFontsDoc:
            def xref_length(self):
                return 3

            def xref_object(self, xref):
                return "<< /Type /Page >>"

        assert kip._primary_glyph_advances(_NoFontsDoc(), {"0": "0013"}) == {}

    def test_font_without_digits_is_rejected(self):
        """Шрифт без цифр — не тот, которым набраны суммы: он не годится."""

        class _NoDigitsDoc:
            def xref_length(self):
                return 3

            def xref_object(self, xref):
                if xref == 1:
                    return "<< /Type0 /DescendantFonts [ 2 0 R ] >>"
                return "<< /CIDFontType2 /DW 1000 /W [ 3 [ 277 ] ] /X >>"

        assert kip._primary_glyph_advances(_NoDigitsDoc(), {" ": "0003"}) == {}

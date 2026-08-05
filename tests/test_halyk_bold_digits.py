import pytest
from halyk_bold_digits import DIGIT_GLYPHS, DIGIT_WIDTH_1000, SOURCE_UNITS_PER_EM

_EXPECTED_LENGTHS = {
    "0": 469, "1": 209, "2": 327, "3": 336, "4": 210,
    "5": 448, "6": 334, "7": 201, "8": 485, "9": 319,
}


def test_all_ten_digits_present():
    assert set(DIGIT_GLYPHS.keys()) == set("0123456789")


def test_digit_glyph_lengths_match_extraction():
    for ch, expected_len in _EXPECTED_LENGTHS.items():
        assert len(DIGIT_GLYPHS[ch]) == expected_len, ch


def test_glyphs_are_nonempty_bytes():
    for ch, data in DIGIT_GLYPHS.items():
        assert isinstance(data, bytes)
        assert len(data) > 0


def test_width_and_units_per_em():
    assert DIGIT_WIDTH_1000 == 500.0
    assert SOURCE_UNITS_PER_EM == 2048


def test_glyphs_parse_as_valid_simple_glyf_entries():
    # numberOfContours (int16, первые 2 байта) должен быть положительным
    # (простой контур), не -1 (композитный) — иначе _patch_truetype_glyphs
    # не подходит для этого глифа.
    import struct
    for ch, data in DIGIT_GLYPHS.items():
        num_contours = struct.unpack(">h", data[0:2])[0]
        assert num_contours > 0, f"digit {ch} unexpectedly composite/empty"

import struct
import pytest
from halyk_pdf_service import _try_patch_bold_digit_glyphs
from halyk_bold_digits import DIGIT_GLYPHS
from tests.test_truetype_glyph_patch import build_synthetic_ttf


def _digit_cids_0x13():
    return {ch: f"{0x13 + i:04X}" for i, ch in enumerate("0123456789")}


def test_gate_passes_and_patches_missing_digit():
    # gid 0x13.. соответствуют digit_cids; ставим present digit '2' (gid 0x15)
    # РЕАЛЬНЫМИ байтами эталона (симулирует subset из того же мастер-шрифта),
    # остальные девять цифр — пустые (в т.ч. '1' на gid 0x14, которую патчим).
    glyphs = [b""] * 0x1D
    glyphs[0x15] = DIGIT_GLYPHS["2"]
    font = build_synthetic_ttf(glyphs)

    result = _try_patch_bold_digit_glyphs(font, _digit_cids_0x13())
    assert result is not None
    patched_bytes, added_widths = result
    assert added_widths.get("0014") == 500.0  # digit '1' at gid 0x14

    from pdf_service import _read_truetype_glyph
    assert _read_truetype_glyph(patched_bytes, 0x14) != b""


def test_gate_fails_when_no_digit_available_to_verify():
    # Все десять цифр пустые — сравнивать не с чем, gate обязан отказать.
    glyphs = [b""] * 0x1D
    font = build_synthetic_ttf(glyphs)
    assert _try_patch_bold_digit_glyphs(font, _digit_cids_0x13()) is None


def test_gate_fails_on_mismatched_master_font():
    # Present digit НЕ совпадает с зашитым эталоном -> другой мастер-шрифт,
    # доверять остальным зашитым цифрам нельзя.
    glyphs = [b""] * 0x1D
    glyphs[0x15] = b"\x00\x01totally different bytes here 1234"
    font = build_synthetic_ttf(glyphs)
    assert _try_patch_bold_digit_glyphs(font, _digit_cids_0x13()) is None


def test_gate_fails_on_wrong_units_per_em():
    glyphs = [b""] * 0x1D
    glyphs[0x15] = DIGIT_GLYPHS["2"]
    font = build_synthetic_ttf(glyphs, units_per_em=1000)
    assert _try_patch_bold_digit_glyphs(font, _digit_cids_0x13()) is None


def test_gate_returns_none_when_nothing_missing():
    glyphs = [b""] * 0x1D
    for ch, gid in zip("0123456789", range(0x13, 0x1D)):
        glyphs[gid] = DIGIT_GLYPHS[ch]
    font = build_synthetic_ttf(glyphs)
    assert _try_patch_bold_digit_glyphs(font, _digit_cids_0x13()) is None


def test_gate_swallows_structural_errors_returns_none():
    assert _try_patch_bold_digit_glyphs(b"not a font at all", _digit_cids_0x13()) is None


def test_gate_rejects_too_long_padding():
    # Present digit matches baked reference but has >1 byte of trailing zeros.
    # This indicates structural anomaly (font not from same master), gate should refuse.
    glyphs = [b""] * 0x1D
    # Baked reference + 5 extra zero bytes (too much padding for same-master sanity)
    glyphs[0x15] = DIGIT_GLYPHS["2"] + b"\x00" * 5
    font = build_synthetic_ttf(glyphs)
    assert _try_patch_bold_digit_glyphs(font, _digit_cids_0x13()) is None


def test_gate_accepts_exactly_one_byte_padding():
    # Present digit matches baked reference + exactly 1 zero byte (legal padding),
    # gate should pass and patch missing digits.
    glyphs = [b""] * 0x1D
    glyphs[0x15] = DIGIT_GLYPHS["2"] + b"\x00"
    font = build_synthetic_ttf(glyphs)

    result = _try_patch_bold_digit_glyphs(font, _digit_cids_0x13())
    assert result is not None
    patched_bytes, added_widths = result
    assert added_widths.get("0014") == 500.0  # digit '1' at gid 0x14 was patched

    from pdf_service import _read_truetype_glyph
    assert _read_truetype_glyph(patched_bytes, 0x14) != b""

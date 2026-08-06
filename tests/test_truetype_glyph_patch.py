import struct
import pytest


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def build_synthetic_ttf(glyphs: list[bytes], units_per_em: int = 2048) -> bytes:
    """Собирает минимальный валидный .ttf с N простыми глифами (long loca).

    glyphs[i] — сырые (уже скомпилированные, возможно пустые b"") байты глифа i.
    Раскладка таблиц копирует физический порядок реальных Halyk bold-subset
    файлов: glyf, head, hhea, hmtx, loca, maxp — то есть после glyf идут ещё
    таблицы, которые должны сдвинуться при росте glyf (это и есть то, что
    регрессионно проверяется).
    """
    num_glyphs = len(glyphs)

    glyf_data = bytearray()
    loca_offsets = [0]
    for g in glyphs:
        padded = g if len(g) % 2 == 0 else g + b"\x00"
        glyf_data.extend(padded)
        loca_offsets.append(len(glyf_data))
    glyf_table = bytes(glyf_data)

    loca_table = b"".join(struct.pack(">L", off) for off in loca_offsets)

    head_table = bytearray(54)
    struct.pack_into(">H", head_table, 0, 1)          # majorVersion
    struct.pack_into(">H", head_table, 2, 0)           # minorVersion
    struct.pack_into(">L", head_table, 4, 0x00010000)  # fontRevision
    struct.pack_into(">L", head_table, 8, 0)           # checkSumAdjustment (recomputed later)
    struct.pack_into(">L", head_table, 12, 0x5F0F3CF5)  # magicNumber
    struct.pack_into(">H", head_table, 16, 0)          # flags
    struct.pack_into(">H", head_table, 18, units_per_em)
    struct.pack_into(">h", head_table, 50, 1)          # indexToLocFormat = long
    struct.pack_into(">h", head_table, 52, 0)          # glyphDataFormat
    head_table = bytes(head_table)

    hhea_table = bytearray(36)
    struct.pack_into(">H", hhea_table, 34, num_glyphs)  # numberOfHMetrics
    hhea_table = bytes(hhea_table)

    hmtx_table = b"".join(struct.pack(">Hh", 1024, 0) for _ in range(num_glyphs))

    maxp_table = bytearray(32)
    struct.pack_into(">L", maxp_table, 0, 0x00010000)
    struct.pack_into(">H", maxp_table, 4, num_glyphs)
    maxp_table = bytes(maxp_table)

    cvt_table = b"\x00" * 8
    fpgm_table = b"\x00" * 4
    prep_table = b"\x00" * 4

    tables = [
        ("glyf", glyf_table),
        ("head", head_table),
        ("hhea", hhea_table),
        ("hmtx", hmtx_table),
        ("loca", loca_table),
        ("maxp", maxp_table),
        ("cvt ", cvt_table),
        ("fpgm", fpgm_table),
        ("prep", prep_table),
    ]

    num_tables = len(tables)
    header = struct.pack(">4sHHHH", b"\x00\x01\x00\x00", num_tables, 0, 0, 0)
    dir_size = 16 * num_tables
    body_start = 12 + dir_size

    offsets = {}
    pos = body_start
    body = bytearray()
    for tag, data in tables:
        offsets[tag] = pos
        padded = _pad4(data)
        body.extend(padded)
        pos += len(padded)

    from pdf_service import _ttf_checksum
    dir_entries = bytearray()
    for tag, data in tables:
        checksum = _ttf_checksum(data)
        dir_entries.extend(struct.pack(">4sLLL", tag.encode("ascii"), checksum, offsets[tag], len(data)))

    font = bytearray(header + bytes(dir_entries) + bytes(body))

    # Recompute head.checkSumAdjustment over the whole file
    head_offset = offsets["head"]
    font[head_offset + 8:head_offset + 12] = b"\x00\x00\x00\x00"
    total = _ttf_checksum(bytes(font))
    adjustment = (0xB1B0AFBA - total) & 0xFFFFFFFF
    font[head_offset + 8:head_offset + 12] = struct.pack(">L", adjustment)

    return bytes(font)


from pdf_service import _ttf_checksum, _read_truetype_glyph, _patch_truetype_glyphs


def test_checksum_matches_truetype_formula():
    # 8 нулевых байт -> сумма двух uint32-нулей = 0
    assert _ttf_checksum(b"\x00" * 8) == 0
    # известное значение: один uint32 = 1
    assert _ttf_checksum(struct.pack(">L", 1)) == 1
    # неполное слово дополняется нулями
    assert _ttf_checksum(b"\x00\x00\x00\x01\x00") == 1


def test_read_truetype_glyph_empty_and_nonempty():
    glyphs = [b"", b"AABBCCDD", b""]
    font = build_synthetic_ttf(glyphs)
    assert _read_truetype_glyph(font, 0) == b""
    assert _read_truetype_glyph(font, 1) == b"AABBCCDD"
    assert _read_truetype_glyph(font, 2) == b""


def test_read_truetype_glyph_out_of_range_raises():
    font = build_synthetic_ttf([b"", b"AB"])
    with pytest.raises(ValueError):
        _read_truetype_glyph(font, 5)


def test_patch_fills_empty_glyph_and_preserves_others():
    glyphs = [b"AABB", b"", b"CCDDEE"]  # gid 1 empty, будем патчить
    font = build_synthetic_ttf(glyphs)
    new_glyph = b"1122334455"  # чётная длина
    patched = _patch_truetype_glyphs(font, {1: new_glyph})

    assert _read_truetype_glyph(patched, 0) == b"AABB"
    assert _read_truetype_glyph(patched, 1) == new_glyph
    assert _read_truetype_glyph(patched, 2) == b"CCDDEE"


def test_patch_pads_odd_length_glyph_to_even():
    glyphs = [b"", b"XX"]
    font = build_synthetic_ttf(glyphs)
    patched = _patch_truetype_glyphs(font, {0: b"ABC"})  # 3 байта, нечётно
    assert _read_truetype_glyph(patched, 0) == b"ABC\x00"
    assert _read_truetype_glyph(patched, 1) == b"XX"


def test_patch_shifts_following_tables_and_recomputes_checksum():
    import io
    # Use only valid glyph data: empty simple glyphs
    glyphs = [b"", b"\x00\x00\x00\x00" * 25]  # glyph 1 is 100 bytes, empty glyph format
    font = build_synthetic_ttf(glyphs)
    # Patch glyph 0 with valid glyph data (40 bytes of empty simple glyph format)
    valid_glyph = b"\x00\x00\x00\x00" + b"\x00" * 36  # 40 bytes total
    patched = _patch_truetype_glyphs(font, {0: valid_glyph})

    # Verify patching worked by reading glyphs back
    assert _read_truetype_glyph(patched, 0) == valid_glyph
    assert _read_truetype_glyph(patched, 1) == glyphs[1]  # нетронутый глиф не изменился

    # fontTools (только в тесте) независимо парсит результат и не жалуется
    from fontTools.ttLib import TTFont
    tt = TTFont(io.BytesIO(patched))
    assert tt["maxp"].numGlyphs == 2

    # головной чек-сумма формула: сумма всего файла == магическая константа
    total = _ttf_checksum(patched)
    assert total == 0xB1B0AFBA


def test_patch_out_of_range_gid_raises():
    font = build_synthetic_ttf([b"", b"AB"])
    with pytest.raises(ValueError):
        _patch_truetype_glyphs(font, {5: b"XX"})


def test_patch_missing_tables_raises():
    with pytest.raises(ValueError):
        _patch_truetype_glyphs(b"not a font", {0: b"XX"})

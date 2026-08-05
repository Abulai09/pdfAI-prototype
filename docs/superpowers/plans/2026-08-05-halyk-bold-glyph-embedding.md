# Halyk Bold Glyph Embedding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить оставшиеся 5 из 24 связок (файл × цель), где Halyk-строка итогов «Барлығы» набирается не полностью Bold из-за отсутствующей в subset'е шрифта цифры, — путём физического вшивания недостающих глифов-цифр в embedded TrueType Bold-subset шрифт, а не заменой шрифта на Regular.

**Architecture:** Статические байты глифов цифр 0-9 (извлечены один раз из `C:\Windows\Fonts\timesbd.ttf`, побайтово подтверждено идентичны Bold-subset'у всех 6 реальных Halyk-файлов) зашиты в новый модуль `halyk_bold_digits.py`. Новая общая низкоуровневая функция `pdf_service._patch_truetype_glyphs()` хирургически вставляет байты глифа в таблицы `glyf`/`loca` TrueType-шрифта, не трогая ничего вокруг (без fontTools в рантайме — он меняет порядок таблиц даже без правок, что неприемлемо для этого проекта). `halyk_pdf_service.py` перед патчем сверяет («gate»), что subset действительно из того же мастер-шрифта, и только тогда патчит `FontFile2`-стрим и `/W`-массив прямо в сырых байтах PDF — тем же приёмом, что уже используется для content-стримов (`raw.find` по позиции объекта + `_rebuild_xref_table` в конце).

**Tech Stack:** Python, PyMuPDF (`fitz`), `struct` (ручной разбор TrueType sfnt-таблиц), `zlib`. `fontTools` — только `requirements-dev.txt`, только в тестах, не в рантайме.

## Global Constraints

- Никаких новых зависимостей в `requirements.txt` (рантайм). `fontTools` — только в `requirements-dev.txt`, используется исключительно тестами как независимый валидатор.
- Стиль патченных байт PDF/шрифта обязан быть неотличим от того, что писал оригинальный генератор (криterion 4, `CLAUDE.md`) — никаких признаков пересборки инструментом (порядок таблиц шрифта, форматирование `/W`-массива без пробелов, как в оригинале).
- Существующее поведение (перебор `_BOLD_GLYPH_RETRIES`, `[guard]`-репортинг) не удаляется — остаётся страховкой на случай отказа gate'а.
- `pytest tests/` не должен регрессировать текущий бейзлайн 83 passed / 69 skipped (измерено в этом worktree непосредственно перед стартом реализации — актуальнее, чем «71 passed», ранее зафиксированные в `CLAUDE.md`, т.к. с тех пор добавились новые fixture-free тестовые файлы).
- Все новые pytest-тесты — fixture-free (не требуют `tests/fixtures/`, которых нет в этом checkout'е).
- Полная real-file валидация — через `tests/scripts/verify_halyk_file.py` на `C:\Users\Abylay\Desktop\testpdf\halyk\*.pdf` (локальный корпус, не в git).

---

### Task 1: `pdf_service.py` — низкоуровневые примитивы патча TrueType

**Files:**
- Modify: `pdf_service.py` (добавить новый блок функций рядом с `_rebuild_xref_table`, ближе к концу файла — та же секция общих бинарных примитивов)
- Test: `tests/test_truetype_glyph_patch.py` (новый, fixture-free)

**Interfaces:**
- Produces: `pdf_service._ttf_checksum(data: bytes) -> int`, `pdf_service._read_truetype_glyph(font_bytes: bytes, gid: int) -> bytes`, `pdf_service._patch_truetype_glyphs(font_bytes: bytes, glyph_patches: dict[int, bytes]) -> bytes` (кидает `ValueError` при неожиданной структуре — не глотает исключение сама).

- [ ] **Step 1: Написать синтетический TrueType-шрифт для тестов (helper, не тест сам по себе)**

В `tests/test_truetype_glyph_patch.py` добавить builder, собирающий МИНИМАЛЬНЫЙ валидный `.ttf` вручную (без fontTools) с таблицами `glyf`/`loca`/`head`/`maxp`/`hhea`/`hmtx` + служебными `cvt `/`fpgm`/`prep` (пустыми, но присутствующими — чтобы протестировать сдвиг таблиц ПОСЛЕ `glyf`, как в реальном файле, где после `glyf` идут `head`/`hhea`/`hmtx`/`loca`/`maxp`/`prep`):

```python
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
```

- [ ] **Step 2: Написать падающие тесты**

```python
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
    glyphs = [b"", b"YY" * 50]  # второй глиф намеренно длинный, чтобы патч первого дал заметный delta
    font = build_synthetic_ttf(glyphs)
    patched = _patch_truetype_glyphs(font, {0: b"Z" * 40})

    # fontTools (только в тесте) независимо парсит результат и не жалуется
    from fontTools.ttLib import TTFont
    tt = TTFont(io.BytesIO(patched))
    assert tt["maxp"].numGlyphs == 2
    go = tt.getGlyphOrder()
    g0 = tt["glyf"][go[0]]
    assert g0.compile(tt["glyf"]) == b"Z" * 40
    g1 = tt["glyf"][go[1]]
    assert g1.compile(tt["glyf"]) == b"YY" * 50  # нетронутый глиф не изменился

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
```

- [ ] **Step 3: Запустить тесты, убедиться что падают**

Run: `pytest tests/test_truetype_glyph_patch.py -v`
Expected: FAIL — `ImportError: cannot import name '_ttf_checksum' from 'pdf_service'`

- [ ] **Step 4: Реализовать в `pdf_service.py`**

Добавить рядом с `_rebuild_xref_table` (после её определения):

```python
# ─── TrueType (sfnt) glyph patching — используется halyk_pdf_service.py для
# вшивания недостающих глифов цифр в Bold-subset шрифт вместо подмены на
# Regular. Разбирает/патчит таблицы вручную (без fontTools в рантайме — он
# при пересборке меняет физический порядок таблиц даже без единой правки,
# что для этого проекта неприемлемо, см. docs/superpowers/specs/
# 2026-08-05-halyk-bold-glyph-embedding-design.md). ───────────────────────


def _ttf_checksum(data: bytes) -> int:
    """Чек-сумма TrueType-таблицы по спецификации sfnt: данные дополняются
    нулями до кратности 4 байт, суммируются как big-endian uint32 со
    сбросом переполнения."""
    padded = data + b"\x00" * (-len(data) % 4)
    total = 0
    for i in range(0, len(padded), 4):
        total = (total + struct.unpack(">L", padded[i:i + 4])[0]) & 0xFFFFFFFF
    return total


def _ttf_table_dir(font_bytes: bytes) -> Dict[str, Tuple[int, int]]:
    """{tag: (offset, length)} по table directory sfnt-файла."""
    num_tables = struct.unpack(">H", font_bytes[4:6])[0]
    by_tag: Dict[str, Tuple[int, int]] = {}
    for i in range(num_tables):
        off = 12 + i * 16
        tag, _checksum, offset, length = struct.unpack(">4sLLL", font_bytes[off:off + 16])
        by_tag[tag.decode("ascii")] = (offset, length)
    return by_tag


def _ttf_loca(font_bytes: bytes, by_tag: Dict[str, Tuple[int, int]]) -> Tuple[List[int], int]:
    """Возвращает (список офсетов глифов относительно начала glyf, indexToLocFormat)."""
    if "loca" not in by_tag or "head" not in by_tag:
        raise ValueError("font missing loca/head table")
    loca_offset, loca_len = by_tag["loca"]
    head_offset, _ = by_tag["head"]
    fmt = struct.unpack(">h", font_bytes[head_offset + 50:head_offset + 52])[0]
    if fmt not in (0, 1):
        raise ValueError(f"unexpected indexToLocFormat {fmt}")
    entry_size = 2 if fmt == 0 else 4
    n = loca_len // entry_size
    offsets = []
    for i in range(n):
        raw = font_bytes[loca_offset + i * entry_size: loca_offset + (i + 1) * entry_size]
        if entry_size == 2:
            offsets.append(struct.unpack(">H", raw)[0] * 2)
        else:
            offsets.append(struct.unpack(">L", raw)[0])
    return offsets, fmt


def _read_truetype_glyph(font_bytes: bytes, gid: int) -> bytes:
    """Сырые байты одного глифа из glyf-таблицы (включая паддинг-байт до
    чётной длины, если он есть — вызывающая сторона, сравнивающая с эталоном
    неизвестной длины, должна сравнивать по префиксу + проверять, что хвост
    нулевой, а не требовать точного совпадения длины)."""
    by_tag = _ttf_table_dir(font_bytes)
    if "glyf" not in by_tag:
        raise ValueError("font missing glyf table")
    glyf_offset, _glyf_len = by_tag["glyf"]
    loca, _fmt = _ttf_loca(font_bytes, by_tag)
    num_glyphs = len(loca) - 1
    if gid < 0 or gid >= num_glyphs:
        raise ValueError(f"gid {gid} out of range (numGlyphs={num_glyphs})")
    start, end = loca[gid], loca[gid + 1]
    return bytes(font_bytes[glyf_offset + start: glyf_offset + end])


def _patch_truetype_glyphs(font_bytes: bytes, glyph_patches: Dict[int, bytes]) -> bytes:
    """Точечно заменяет байты указанных GID в glyf-таблице TrueType-шрифта,
    не трогая ничего вокруг: нетронутые глифы и все остальные таблицы
    остаются побайтово идентичны входу, только сдвигаются на дельту длины,
    если физически расположены в файле после glyf. Пересчитывает checksum
    записей glyf/loca в table directory и глобальный head.checkSumAdjustment.

    Кидает ValueError при любой неожиданной структуре (композитный глиф там,
    где не ожидался; GID вне диапазона; отсутствие нужных таблиц) — не
    пытается угадать и молча продолжить. Вызывающая сторона обязана поймать
    исключение и откатиться к старому поведению (не менять шрифт).
    """
    buf = bytearray(font_bytes)
    num_tables = struct.unpack(">H", buf[4:6])[0]
    dir_start = 12
    entries = []  # [tag, checksum, offset, length] — мутируемый список
    for i in range(num_tables):
        off = dir_start + i * 16
        tag, checksum, offset, length = struct.unpack(">4sLLL", buf[off:off + 16])
        entries.append([tag.decode("ascii"), checksum, offset, length])
    by_tag = {e[0]: e for e in entries}

    for required in ("glyf", "loca", "head"):
        if required not in by_tag:
            raise ValueError(f"font missing {required} table")

    glyf_e = by_tag["glyf"]
    loca_e = by_tag["loca"]
    head_e = by_tag["head"]
    glyf_offset, glyf_len = glyf_e[2], glyf_e[3]
    loca_offset, loca_len = loca_e[2], loca_e[3]

    old_loca, index_to_loc_format = _ttf_loca(bytes(buf), {k: (v[2], v[3]) for k, v in by_tag.items()})
    num_glyphs = len(old_loca) - 1
    entry_size = 2 if index_to_loc_format == 0 else 4

    old_glyf = bytes(buf[glyf_offset:glyf_offset + glyf_len])

    for gid in glyph_patches:
        if gid < 0 or gid >= num_glyphs:
            raise ValueError(f"gid {gid} out of range (numGlyphs={num_glyphs})")

    new_glyf = bytearray()
    new_loca = [0]
    for gid in range(num_glyphs):
        if gid in glyph_patches:
            data = glyph_patches[gid]
            if len(data) % 2 != 0:
                data = data + b"\x00"
        else:
            start, end = old_loca[gid], old_loca[gid + 1]
            data = old_glyf[start:end]
        new_glyf.extend(data)
        new_loca.append(len(new_glyf))
    new_glyf = bytes(new_glyf)

    if index_to_loc_format == 1:
        new_loca_bytes = b"".join(struct.pack(">L", off) for off in new_loca)
    else:
        for off in new_loca:
            if off % 2 != 0 or off // 2 > 0xFFFF:
                raise ValueError("glyf grew too large for short loca format")
        new_loca_bytes = b"".join(struct.pack(">H", off // 2) for off in new_loca)

    if len(new_loca_bytes) != loca_len:
        raise ValueError("loca length changed unexpectedly")

    old_glyf_padded_len = (glyf_len + 3) & ~3
    new_glyf_padded = new_glyf + b"\x00" * (-len(new_glyf) % 4)
    delta = len(new_glyf_padded) - old_glyf_padded_len

    following = [e for e in entries if e[2] > glyf_offset]
    if following:
        next_e = min(following, key=lambda e: e[2])
        if next_e[2] != glyf_offset + old_glyf_padded_len:
            raise ValueError("unexpected gap after glyf table; refusing to patch")

    buf[glyf_offset:glyf_offset + old_glyf_padded_len] = new_glyf_padded

    for e in entries:
        if e[2] > glyf_offset:
            e[2] += delta

    new_loca_offset = by_tag["loca"][2]
    buf[new_loca_offset:new_loca_offset + loca_len] = new_loca_bytes

    glyf_e[3] = len(new_glyf)
    glyf_e[1] = _ttf_checksum(new_glyf)
    loca_e[1] = _ttf_checksum(new_loca_bytes)

    for i, e in enumerate(entries):
        off = dir_start + i * 16
        tag, checksum, offset, length = e
        buf[off:off + 16] = struct.pack(">4sLLL", tag.encode("ascii"), checksum & 0xFFFFFFFF, offset, length)

    new_head_offset = by_tag["head"][2]
    buf[new_head_offset + 8:new_head_offset + 12] = b"\x00\x00\x00\x00"
    total = _ttf_checksum(bytes(buf))
    adjustment = (0xB1B0AFBA - total) & 0xFFFFFFFF
    buf[new_head_offset + 8:new_head_offset + 12] = struct.pack(">L", adjustment)

    return bytes(buf)
```

Добавить `import struct` в начало `pdf_service.py`, если его там ещё нет (проверить перед добавлением).

- [ ] **Step 5: Прогнать тесты, убедиться что проходят**

Run: `pytest tests/test_truetype_glyph_patch.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Прогнать на реальном файле как smoke-test (не pytest, разово, руками)**

Run:
```bash
python3 -c "
import fitz, re, io
from fontTools.ttLib import TTFont
from pdf_service import _patch_truetype_glyphs

doc = fitz.open(r'C:\Users\Abylay\Desktop\testpdf\halyk\h6.pdf')
pobj = doc.xref_object(doc[0].xref)
for fn, fxx in re.findall(r'/F(\d+)\s+(\d+)\s+0\s+R', pobj):
    fobj = doc.xref_object(int(fxx))
    bm = re.search(r'/BaseFont\s*/(\S+)', fobj)
    if bm and 'Bold' in bm.group(1):
        desc_m = re.search(r'/DescendantFonts\s*\[\s*(\d+)\s+0\s+R', fobj)
        cidobj = doc.xref_object(int(desc_m.group(1)))
        fd_m = re.search(r'/FontDescriptor\s+(\d+)\s+0\s+R', cidobj)
        fdobj = doc.xref_object(int(fd_m.group(1)))
        ff2_m = re.search(r'/FontFile2\s+(\d+)\s+0\s+R', fdobj)
        fx = int(ff2_m.group(1))
        break
data = doc.xref_stream(fx)
tt_sys = TTFont(r'C:\Windows\Fonts\timesbd.ttf')
cmap = tt_sys.getBestCmap()
glyf_sys = tt_sys['glyf']
patches = {gid: glyf_sys[cmap[ord(ch)]].compile(glyf_sys) for gid, ch in {0x14:'1',0x18:'5',0x1a:'7'}.items()}
patched = _patch_truetype_glyphs(data, patches)
tt2 = TTFont(io.BytesIO(patched))
print('OK, numGlyphs=', tt2['maxp'].numGlyphs)
"
```
Expected: `OK, numGlyphs= 4101` без исключений (уже проверено при подготовке этого плана — должно воспроизвестись один в один).

- [ ] **Step 7: Добавить `fonttools` в `requirements-dev.txt`**

Текущее содержимое `requirements-dev.txt`:
```
-r requirements.txt
pytest>=8.0.0
```

Добавить третью строку:
```
-r requirements.txt
pytest>=8.0.0
fonttools>=4.63.0
```

(`fonttools` уже установлен в этом окружении командой `pip install fonttools` при подготовке плана — версия 4.63.0. Используется только тестами этого и последующих задач, не рантайм-кодом.)

- [ ] **Step 8: Commit**

```bash
git add pdf_service.py tests/test_truetype_glyph_patch.py requirements-dev.txt
git commit -m "feat(halyk): add low-level TrueType glyf/loca patcher (no fontTools at runtime)"
```

---

### Task 2: `halyk_bold_digits.py` — зашитые байты глифов цифр

**Files:**
- Create: `halyk_bold_digits.py`
- Test: `tests/test_halyk_bold_digits.py` (новый, fixture-free)

**Interfaces:**
- Consumes: ничего (чистые данные)
- Produces: `halyk_bold_digits.DIGIT_GLYPHS: Dict[str, bytes]` (10 ключей `'0'`-`'9'`), `halyk_bold_digits.DIGIT_WIDTH_1000: float` (= 500.0), `halyk_bold_digits.SOURCE_UNITS_PER_EM: int` (= 2048)

- [ ] **Step 1: Создать `halyk_bold_digits.py`**

Данные извлечены и провалидированы (побайтовое совпадение с Bold-subset всех 6 реальных Halyk-файлов, см. `docs/superpowers/specs/2026-08-05-halyk-bold-glyph-embedding-design.md`) из `C:\Windows\Fonts\timesbd.ttf` — Times New Roman Bold, тот самый шрифт, из которого без изменений сделаны Bold-subset'ы во всех проверенных Halyk-файлах.

```python
"""Байты глифов цифр 0-9 шрифта Times New Roman Bold (TrueType glyf-записи,
простые контуры, включая хинтинг-инструкции), для вшивания в Bold-subset
Halyk-выписок вместо смены начертания на Regular (см.
docs/superpowers/specs/2026-08-05-halyk-bold-glyph-embedding-design.md).

Извлечено ОДИН РАЗ из C:\\Windows\\Fonts\\timesbd.ttf и зафиксировано здесь —
рантайм не читает никакой файл с диска. Побайтово подтверждено идентично
Bold-subset'у во всех 6 локально доступных реальных Halyk-файлов (включая
инфраструктурные таблицы cvt/fpgm/prep, от которых зависит хинтинг), а также
совпадает с самим системным шрифтом-источником — halyk_pdf_service.py
использует это совпадение как gate перед тем, как доверять этим данным для
конкретного обрабатываемого файла (не предполагается вслепую).
"""

from __future__ import annotations

SOURCE_UNITS_PER_EM = 2048
DIGIT_WIDTH_1000 = 500.0  # /W-значение (1000 em) — одинаково для всех цифр 0-9

_DIGIT_GLYPHS_HEX: dict[str, str] = {
    '0': '0002004affe403b505680016002b014a4019091b061f062509291707181b1520152518290907080177081db8010ab2120527b8010ab3060d1217b8030a4025004024263400402b2e34004033363400403b3d3440005000a00003001a2d120f221f220222b8030a400e8f0d010d401315340d192cf5f1182b4e10f62b724ded5d43584017224024273422402b2e34224033363422403b3d346f2201712b2b2b2b594e10f6712b2b2b2b4ded4358b90017ffc0b324273417b8ffc0b32b2e3417b8ffc0b333363417b8ffc0b53b3d34601701712b2b2b2b59003fed3fed313043794062012a20211f2102060f0e100e02062423252302060b0c0a0c090c080c040615161416020619181a181b18030602010301040103062a2b292b02061e1122620026072262001c131762012805176201210e1d6201230c27620018161d62012b012762002b2b2b2b012b2b2b2b2a2a2a2a2a2a2a2a81007101710114070e02232226272627263534373636333216171605102726272623220706061110171616333237363703b53922739256629e3d2c212b3e33d07476cd3043fecc040a2619382b19251a140f382e32192a0602a6cbb06c8a51645d447199a3ddb999a3a188bbdc01643b8b31201823b1fdeffee0624730203875',
    '1': '0001008700000363056800170083bc000e01ad0135000801a5b6225f076f070201410d01ad0135000701f90023001601a5000f01f70015013500160202400e0f08000508070c000f011f010201b8030940100e0e500f6f0faf0f03100f010f19180ebe0200000802c60018021701df00182b10f6e44e10f45d5d3c4d10fd5d3c003f3c3f1239f5edfc01f52b015d2b3130011114161633331521353332363635113426262322072725029b16464d1ffd3624574a1a12312033491201f30568fbab7d452c252528468002bf5e2f212024e4',
    '2': '000100320000039c0568001c00e840248802ae02ac0303270c9503020a0b0a0b0100051b12a016b41602161c020217010a0b081cb80160b6122f17b0170217b80122b2011208b8ffc0b30b0d3408b80324b40e05010c03b801f4401b000510050205dc12d30f1c01bf1c011c1a1e0f0101bf010101191dba0158019000182b4e10e45d7110f65d714df4fd5de4003f3fed2b435c5840130840160d3f0840170e3f0840190f3f08400d392b2b2b2b5910fd5d435c58b90017ffc0b3160d3f17b8ffc0b3170e3f17b8ffc0b31b103f17b8ffc0b21c113f2b2b2b2b59e4113939111239011112395d1139123911393910c93130015d005d21213500123534262322072736363332161615140706012132363637330350fce2016f9d825e9a552536dc9067aa604a65feac01256c412a22241601b5012e90698b9a0dc0b860a7498589b9feb5122b45',
    '3': '00010021ffe3038a0568002b00cf401f070f170f682b792b851585169b139516a912a616ba130b2d1c3f1c02140801b80195400900001000020000210b410901600008002102cc002602ce001a0008ffc0b30d113408b8ffc0b312153408b802ceb6400e051a0d0014b80213b4200101050aba0195000b011ab61e000510050205b8030ab711d3002910290229b8030a40110f1701171a2d0f1e011e401315341e192cba0158019000182b4e10e42b7110f6714ded5df4ed5d10f4fd11392f191aed3c00183f3f1aed2b2b10fde410e412392f5ded12393130015d005d01353e02353426232207273636333216151406071616151400212227263534363332171616333236353426012f725840795a8c622548e18a8db7555b757bfecdfefeac4f39422b211d10c3554a6ac002a8232139753c5377940da7a8ac734b8b3539a77ed4fed739283f2e410e089f755a89e7',
    '4': '00020033000003ad0568000a000d009940183f0d013301010300040207090a05080d0c0004080d0d0b0bb8015c40100001140000010b000107080b0d010400bb01fc0005000a0210400c080105080c0d000810080208bb030900020007016040182f043f04020f041f0402041a0f004013153400190ef5f1182b4e10e42b10f65d5d4df43cfd5d3c003f3f10f43cf63c113939011112391239872e2b047d10c40f0f0f3130015d005d13013311331523112111213721113302847a7c7cfeedfe1561018a01ff0369fc97cffed00130cf0217',
    '5': '00010045ffe403b3054c00220150402e0e0601090a190a2800280304140a151b151c0338064906b705030b05011a1b0a1c041d101b0a1c1d04011a040303b8015c4010002214000022120f2201002210220222b80309b41200040104b801efb6122003bf030203b80122b30100041ab8019a400a0f131f130213dc0c0d02b802c8b301d10800bb02060022001d01f5401a40085008a00803081a2410d18f22012240131534221923f5f1182b4e10f42b724de410f671ed10e410f4e4003fed5ded3f3cfd5d435c58401403401c113f03401b103f0340170e3f0340160d3f2b2b2b2b59fe71435c58b90004ffc0b31c113f04b8ffc0b31b103f04b8ffc0b3170e3f04b8ffc0b3160d3f04b8ffc0b2140c3f2b2b2b2b2b59ed5d71435c58401e22401c113f22401b103f2240170e3f2240160d3f2240140c3f2240120b3f2b2b2b2b2b2b5987052e2b7d10c400111217390111121739313000715d01725d7101210321070417161514060423222726353436333216171617163332363534242122070138027b65fdea330159ba998efefe9aa6593e412b2750613d2c1f275273fea0fef41b36054cfefe870d9f83c37def813e2c382b4220442a100c7854b0dc01',
    '6': '0002004cffe403c205680017002700cf400914030166167616020ab8fff840151214342a032415c715033f08052218181f05092615bd02ca0001019500000009019ab526261000051fb8010ab4100d22dc0cb801e1b3001a2918b802c9b50f1a1f1a021ab8030a400b1440131534141928f5f1182b4e10f42b4ded5ded4e10f64df6ed003fed3f12392fed10ede411123912390111123931304379402e1b250a1324261c1b1d1b020612250e26250a2262011e111a6200200f226201230b2662011b131f6200210d1f6200002b2b2b012b2b2b2b2b2a2b8181005d2b015d5d01150e02073637363332161514060623222602353412240106151412171633323635102726232203c2b5db7f232c1d414298cb6eca737dd477db0193fec908352e212d2e48432b492805681c2e91cf991e0914ddbf86e07a8901089be40189e9fd6e8a408afefe34256ba401146944',
    '7': '00010045ffe403cf054c000a0096400b0009190502060807090a0ab8019e400f0001140000010a0100030907010807b801f4400c122002bf0202000210020202b80122b609090804000c06b802cb400a091a0c90070107f60bf5b9019100182b10e65d4e10f64de4003f3f3c10fd5d5d435c58401402401c113f02401b103f0240170e3f0240160d3f2b2b2b2b59e412390111121739872e2b7d10c4011139393130015d0501212207060723132101016b0171fee7a5533a2626620328fe391c045f2b1e5301a5fa98',
    '8': '00030048ffe403b80563001700240032014f4028030c0413110c1413450c68267926072501371756327704830284199a0d9b24aa0daa24b808b6140cb1060243545840170903150f2a31221b0c2518000c34330c251800041f2e1fb8010ab212052eb8010ab1060d003fed3fed1112173901111217391b40352b003f00340c03530c5025630c730c830005250c0d0d32182424000c0f1825312200151b0c400f10025525180c0004061232012424b8030940150d32140d0d3201240322320d310f0d24013204061fb8010ab212052eb8010ab2060d1bb8ffc0b30b0d341bb80300401d153031dc40035003a00303031a340f221f220222dc0f302a40090d342ab80300400e8f09010940131534091933f5f1182b10f62b72ed2bf4ed5d10f671edf4fd2b003fed3fed121739011112393911123939870e2e2b870e7dc400111217392b01111239111239391239070e103c870e10c4c4005d015d593130005d015d01161615140623222635343637262635343633321615140607363635342726232206151416030607060615141616333236353402ba8f6ff7d4c9dc7f94a15be7c9c2d171c32524382a4a435e692d1f0d142030592f496402fe69b575a4e3c68f6da4447b9c6788cfb780609308327c4a8245356148499dfec81c172386495e7f386b5dc2',
    '9': '0002003fffe403b705680016002800c0401b0a031a032a03039809a809b809c80c0444080517230508171f2701bb019500000008019ab32727001fb8010a400c0f05000d17d1001910190219b8030a4012131a2a23dc0bd10140131534011929f5f1182b4e10f42b4df4ed4e10f64dfd5de4003f3fed12392fed10ed1112391239011112393130437940321a26091221250d2611251b1a1c1a1d1a03062526200e2362001e101962012609236200220c1f62011a121f6201240a276200002b2b2b012b2b2b2b2a2b2b2b8181005d015d17353e023706062322263534363633321612151402040136353427262726232207061510171633323fa6e7871b3e57309acd6fce6f77d47ecdfe6a01290a2a182f1928321c27422b49271c1c2694da8e2019dec186df7b88fefea5d6fe78ed02887055b69d5729162b3ba6feeb6944',
}

DIGIT_GLYPHS: dict[str, bytes] = {ch: bytes.fromhex(h) for ch, h in _DIGIT_GLYPHS_HEX.items()}
```

Эти байты уже извлечены и провалидированы при подготовке этого плана: точечный патч `h6.pdf`'s Bold-шрифта цифрами «1»/«5»/«7» этими байтами дал рабочий, независимо парсящийся `fontTools`-ом шрифт (0 расхождений на 4098 нетронутых глифах, все остальные таблицы — `cvt `/`fpgm`/`prep`/`hhea`/`maxp`/`hmtx` — побайтово не изменились, глобальный TrueType checksum сходится к `0xB1B0AFBA`). Вставить как есть — перегенерации не требуется. Если когда-нибудь понадобится перегенерировать (напр. появится файл на другом мастер-шрифте, который тоже пройдёт gate, или система, где `timesbd.ttf` лежит по другому пути) — команда:

```bash
python3 -c "
from fontTools.ttLib import TTFont
tt = TTFont(r'C:\Windows\Fonts\timesbd.ttf')
cmap = tt.getBestCmap()
glyf = tt['glyf']
lines = ['_DIGIT_GLYPHS_HEX = {']
for ch in '0123456789':
    gname = cmap[ord(ch)]
    raw = glyf[gname].compile(glyf)
    lines.append(f'    {ch!r}: {raw.hex()!r},')
lines.append('}')
print(chr(10).join(lines))
"
```
(требует `fontTools` — уже установлен в этом окружении командой `pip install fonttools`, используется только для генерации этого статического файла и в тестах, не в проде).

**Проверено при подготовке плана (Times New Roman Bold, `timesbd.ttf`):**

| цифра | длина глифа (байт) | numberOfContours |
|---|---|---|
| 0 | 469 | 2 |
| 1 | 209 | 1 |
| 2 | 327 | 1 |
| 3 | 336 | 1 |
| 4 | 210 | 2 |
| 5 | 448 | 1 |
| 6 | 334 | 2 |
| 7 | 201 | 1 |
| 8 | 485 | 3 |
| 9 | 319 | 2 |

Все advance width = 1024/2048 em = 500 в `/W`. Все простые (не композитные) контуры.

- [ ] **Step 2: Написать тест**

```python
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
```

- [ ] **Step 3: Прогнать, убедиться что падает без реальных данных / проходит с ними**

Run: `pytest tests/test_halyk_bold_digits.py -v`
Expected: PASS после того, как в Step 1 вставлены реальные hex-значения (до вставки — `ValueError` из `bytes.fromhex()` на плейсхолдере, что и должно провалить тест раньше).

- [ ] **Step 4: Commit**

```bash
git add halyk_bold_digits.py tests/test_halyk_bold_digits.py
git commit -m "feat(halyk): bake reference Times New Roman Bold digit glyphs"
```

---

### Task 3: `halyk_pdf_service.py` — gate и точечный патч шрифта (на уровне байт шрифта)

**Files:**
- Modify: `halyk_pdf_service.py` (новая секция функций, до `_process_halyk_pdf_once`)
- Test: `tests/test_halyk_bold_glyph_gate.py` (новый, fixture-free — использует `build_synthetic_ttf` из Task 1's теста)

**Interfaces:**
- Consumes: `pdf_service._read_truetype_glyph`, `pdf_service._patch_truetype_glyphs` (Task 1), `halyk_bold_digits.DIGIT_GLYPHS/DIGIT_WIDTH_1000/SOURCE_UNITS_PER_EM` (Task 2)
- Produces: `halyk_pdf_service._try_patch_bold_digit_glyphs(ff2_bytes: bytes, digit_cids: Dict[str, str]) -> Optional[Tuple[bytes, Dict[str, float]]]` — `digit_cids` это `{"0": "0013", ..., "9": "001C"}` (4-символьные hex CID, тот же формат, что ключи `avail_cids_map`, значения из `FROM_UNICODE`). Возвращает `(новые байты FontFile2, {cid_hex: 500.0})` для реально допатченных цифр, либо `None`.

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `pytest tests/test_halyk_bold_glyph_gate.py -v`
Expected: FAIL — `ImportError: cannot import name '_try_patch_bold_digit_glyphs'`

- [ ] **Step 3: Реализовать в `halyk_pdf_service.py`**

Добавить после импортов, до `_halyk_dayend_min_rb` (или в отдельную секцию перед `_process_halyk_pdf_once` — там, где сейчас `# ─── Сырая замена байт ─────` на строке 979):

```python
# ─── Вшивание недостающих глифов цифр в Bold-subset шрифт ──────────────────
# См. docs/superpowers/specs/2026-08-05-halyk-bold-glyph-embedding-design.md.
# Заменяет собой (частично) необходимость подмены Bold->Regular в
# replace_callback ниже: если патч удаётся, avail_cids_map после него уже
# содержит нужный CID, и needs_switch там просто не сработает — остальной
# код (needs_switch, retry-перебор шума, [guard]-репортинг) не меняется и
# остаётся страховкой на случай отказа gate'а.

from pdf_service import _read_truetype_glyph, _patch_truetype_glyphs
from halyk_bold_digits import DIGIT_GLYPHS, DIGIT_WIDTH_1000, SOURCE_UNITS_PER_EM


def _try_patch_bold_digit_glyphs(
    ff2_bytes: bytes,
    digit_cids: Dict[str, str],
) -> Optional[Tuple[bytes, Dict[str, float]]]:
    """Пытается вписать в Bold-subset шрифт недостающие глифы цифр 0-9.

    digit_cids — {цифра: CID в виде 4-символьного hex}, тот же формат, что
    ключи avail_cids_map (обычно {'0': '0013', ..., '9': '001C'}, но
    вычисляется вызывающей стороной из FROM_UNICODE, а не жёстко здесь).

    Возвращает (новые байты FontFile2, {cid_hex: 500.0}) для реально
    допатченных цифр, либо None — если патчить нечего, или "gate" не
    позволяет доверять зашитым эталонным глифам для ЭТОГО конкретного
    файла (см. ниже). None означает «ничего не меняли», вызывающая сторона
    не отклоняется от старого поведения.
    """
    try:
        # unitsPerEm — сверяем через head-таблицу тем же способом, что и
        # низкоуровневые функции (переиспользуем их разбор directory).
        import struct
        num_tables = struct.unpack(">H", ff2_bytes[4:6])[0]
        head_offset = None
        for i in range(num_tables):
            off = 12 + i * 16
            tag, _cs, offset, _length = struct.unpack(">4sLLL", ff2_bytes[off:off + 16])
            if tag == b"head":
                head_offset = offset
                break
        if head_offset is None:
            return None
        units_per_em = struct.unpack(">H", ff2_bytes[head_offset + 18:head_offset + 20])[0]
        if units_per_em != SOURCE_UNITS_PER_EM:
            return None

        missing_digits = []
        verified_match = False

        for digit in "0123456789":
            cid_hex = digit_cids.get(digit)
            if cid_hex is None:
                continue
            gid = int(cid_hex, 16)
            existing = _read_truetype_glyph(ff2_bytes, gid)
            baked = DIGIT_GLYPHS[digit]
            if existing == b"":
                missing_digits.append(digit)
                continue
            # Present digit — сверяем с эталоном как gate ("сначала проверь,
            # потом доверяй"): existing может быть на 1 байт длиннее (паддинг
            # до чётной длины внутри glyf-таблицы), поэтому сравниваем по
            # префиксу и требуем, чтобы хвост был нулевым, а не точное
            # совпадение длины.
            n = len(baked)
            if existing[:n] == baked and all(b == 0 for b in existing[n:]):
                verified_match = True
            else:
                return None  # другой мастер-шрифт — не доверяем НИЧЕМУ

        if not verified_match or not missing_digits:
            return None

        glyph_patches = {int(digit_cids[d], 16): DIGIT_GLYPHS[d] for d in missing_digits}
        patched = _patch_truetype_glyphs(ff2_bytes, glyph_patches)
        added_widths = {digit_cids[d]: DIGIT_WIDTH_1000 for d in missing_digits}
        return patched, added_widths
    except Exception as exc:  # noqa: BLE001 — любой сбой здесь ЧИСТО fallback, не проброс
        print(f"[Halyk] Патч глифов Bold не применён ({exc.__class__.__name__}: {exc}) "
              f"— используется старое поведение (подмена шрифта/перебор шума).")
        return None
```

Добавить `Optional` в импорт `typing` вверху файла, если его там ещё нет (уже есть — строка 11: `from typing import Dict, List, Optional, Tuple`, ничего менять не нужно).

- [ ] **Step 4: Прогнать тесты**

Run: `pytest tests/test_halyk_bold_glyph_gate.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add halyk_pdf_service.py tests/test_halyk_bold_glyph_gate.py
git commit -m "feat(halyk): add trust-gated glyph patch decision function"
```

---

### Task 4: интеграция в `_process_halyk_pdf_once` — реальный PDF

**Files:**
- Modify: `halyk_pdf_service.py:1040-1109` (сбор данных по Bold-шрифтам на странице) и `halyk_pdf_service.py:1218-1230` (начало обработки сырых байт)

**Interfaces:**
- Consumes: `_try_patch_bold_digit_glyphs` (Task 3)
- Produces: ничего нового наружу — меняет поведение `_process_halyk_pdf_once` изнутри (`avail_cids_map` после патча уже содержит новые CID, `font_substitutions`/`needs_switch` считают меньше подмен на затронутых файлах).

Это интеграционная задача без изолированного unit-теста (нужен реальный PDF с Bold CIDFontType2, которого нет в fixture-free тестах) — валидируется в Task 6 через `verify_halyk_file.py` на локальном корпусе `testpdf/halyk`.

- [ ] **Step 1: Расширить сбор данных по странице — сохранить xref FontFile2 и CIDFont-словаря**

В блоке `for _pn in range(len(doc)):` (строка ~1041), внутри `for _fn, _fx in re.findall(...)` (строка ~1050), там, где уже парсится `_desc_m`/`_cidobj` (строки ~1061-1063), добавить извлечение `FontFile2` xref и запомнить пару `(cid_xref, ff2_xref)` для КАЖДОГО bold-имени на этой странице:

```python
        _page_bold_ff2: Dict[str, Tuple[int, int]] = {}  # F-name -> (cid_xref, ff2_xref)
```

добавить рядом с объявлением `_page_avail_cids: Dict[str, set] = {}` (строка 1049).

Внутри `if _desc_m:` блока (строка 1062, после `_cidobj = doc.xref_object(int(_desc_m.group(1)))`), добавить:

```python
                    if "Bold" in _bname or ",B" in _bname or "bold" in _bname:
                        _fd_m2 = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", _cidobj)
                        if _fd_m2:
                            _fdobj2 = doc.xref_object(int(_fd_m2.group(1)))
                            _ff2_m2 = re.search(r"/FontFile2\s+(\d+)\s+0\s+R", _fdobj2)
                            if _ff2_m2:
                                _page_bold_ff2["F" + _fn] = (int(_desc_m.group(1)), int(_ff2_m2.group(1)))
```

(Разместить сразу после существующего `_w_m = re.search(r"/W\b", _cidobj)` блока, внутри того же `if _desc_m:`, до `except Exception: pass`.)

В конце страничного цикла (строка ~1086, `for _cx in _page_contents:`), добавить сохранение в per-xref карту:

```python
    _xref_bold_ff2: Dict[int, Dict[str, Tuple[int, int]]] = {}  # объявить рядом с _xref_avail_cids (строка 1040)
    ...
        for _cx in _page_contents:
            ...
            _xref_bold_ff2[_cx] = _page_bold_ff2
```

- [ ] **Step 2: Собрать множество уникальных (cid_xref, ff2_xref) по всему документу, ДО `doc.close()`**

Перед `doc.close()` (строка 1100), добавить:

```python
    _bold_font_pairs: set = set()
    for _m in _xref_bold_ff2.values():
        _bold_font_pairs.update(_m.values())
    # digit_cids для gate — тот же CID-маппинг, что уже вычислен для всего
    # документа через FROM_UNICODE (строки 997-1002 этого же файла)
    _digit_cids = {ch: FROM_UNICODE[ch] for ch in "0123456789" if ch in FROM_UNICODE}
```

- [ ] **Step 3: Патчить шрифт(ы) в `raw` ДО обработки content-стримов**

После `raw = bytearray(input_bytes)` (строка 1219) и `cumulative_offset = 0` (строка 1222), до блока `all_content_xrefs` (строка 1224), добавить:

```python
    # ── Патч Bold-шрифта: вшиваем недостающие глифы цифр, если gate
    # позволяет доверять зашитому эталону (см. halyk_bold_digits.py).
    # Делается ПЕРВЫМ, до поиска позиций content-стримов — все последующие
    # raw.find() увидят уже сдвинутые (после этого патча) позиции сами по
    # себе, отдельного cumulative_offset для этого шага не нужно.
    _newly_available_cids: Dict[int, Dict[str, float]] = {}  # cid_xref -> {cid_hex: width}
    for _cid_xref, _ff2_xref in _bold_font_pairs:
        _ff2_pattern = f"{_ff2_xref} 0 obj".encode()
        _ff2_pos = raw.find(_ff2_pattern)
        if _ff2_pos < 0:
            continue
        _stream_kw = raw.find(b"stream", _ff2_pos)
        _data_start = _stream_kw + len(b"stream")
        if raw[_data_start:_data_start + 2] == b"\r\n":
            _data_start += 2
        elif raw[_data_start:_data_start + 1] == b"\n":
            _data_start += 1
        _endstream_pos = raw.find(b"endstream", _data_start)
        _header = bytes(raw[_ff2_pos:_stream_kw])
        _len_m = re.search(rb"/Length\s+(\d+)", _header)
        if not _len_m:
            continue
        _old_length = int(_len_m.group(1))
        _compressed = bytes(raw[_data_start:_data_start + _old_length])
        try:
            _ff2_bytes = zlib.decompress(_compressed)
        except zlib.error:
            continue

        _result = _try_patch_bold_digit_glyphs(_ff2_bytes, _digit_cids)
        if _result is None:
            continue
        _patched_ff2, _added_widths = _result

        _new_compressed = zlib.compress(_patched_ff2)
        _new_length = len(_new_compressed)
        _new_length1 = len(_patched_ff2)
        _new_header = re.sub(rb"/Length\s+\d+", f"/Length {_new_length}".encode(), _header)
        _new_header = re.sub(rb"/Length1\s+\d+", f"/Length1 {_new_length1}".encode(), _new_header)

        # Разделитель между "stream" и телом ("\r\n" или "\n") — берём из
        # оригинала, не хардкодим, тот же приём, что _op_separators для
        # content-стримов.
        _stream_sep = bytes(raw[_stream_kw + len(b"stream"):_data_start])
        _trailing = bytes(raw[_data_start + _old_length:_endstream_pos])
        raw[_ff2_pos:_endstream_pos] = _new_header + b"stream" + _stream_sep + _new_compressed + _trailing

        print(f"[Halyk] Вшиты недостающие цифры в Bold-шрифт (xref {_ff2_xref}): "
              f"{sorted(_added_widths.keys())}")

        # /W-массив CIDFont-словаря — дописываем новые CID без пробелов,
        # тем же форматом, что и соседние записи оригинала (сверено на h6.pdf:
        # "19[500]21[500]..." — без единого пробела).
        _cid_pattern = f"{_cid_xref} 0 obj".encode()
        _cid_pos = raw.find(_cid_pattern)
        if _cid_pos < 0:
            continue
        _endobj_pos = raw.find(b"endobj", _cid_pos)
        _cidobj_bytes = bytes(raw[_cid_pos:_endobj_pos])
        _w_m = re.search(rb"/W\s*\[", _cidobj_bytes)
        if not _w_m:
            continue
        _bracket_start = _w_m.end() - 1
        _depth = 0
        _close_idx = None
        for _j in range(_bracket_start, len(_cidobj_bytes)):
            _c = _cidobj_bytes[_j:_j + 1]
            if _c == b"[":
                _depth += 1
            elif _c == b"]":
                _depth -= 1
                if _depth == 0:
                    _close_idx = _j
                    break
        if _close_idx is None:
            continue
        _new_entries = b"".join(
            f"{int(_cid, 16)}[{int(_w)}]".encode("ascii")
            for _cid, _w in _added_widths.items()
        )
        _new_cidobj_bytes = _cidobj_bytes[:_close_idx] + _new_entries + _cidobj_bytes[_close_idx:]
        raw[_cid_pos:_endobj_pos] = _new_cidobj_bytes

        _newly_available_cids[_cid_xref] = _added_widths

    if _newly_available_cids:
        for _cx, _pair_map in _xref_bold_ff2.items():
            for _fname, (_cx_cid_xref, _cx_ff2_xref) in _pair_map.items():
                if _cx_cid_xref in _newly_available_cids:
                    _xref_avail_cids.setdefault(_cx, {})
                    _xref_avail_cids[_cx].setdefault(_fname, set())
                    _xref_avail_cids[_cx][_fname] |= set(_newly_available_cids[_cx_cid_xref].keys())
```

- [ ] **Step 4: Прогнать существующий `pytest tests/`, убедиться в отсутствии регрессии**

Run: `pytest tests/ -v`
Expected: тот же бейзлайн (83 passed / 69 skipped) плюс новые тесты из Task 1-3 — ничего не должно сломаться, т.к. на файлах без `_bold_font_pairs` (или там, где gate отказывает) `_newly_available_cids` остаётся пустым и весь новый блок — no-op.

- [ ] **Step 5: Commit**

```bash
git add halyk_pdf_service.py
git commit -m "feat(halyk): wire bold digit glyph patch into raw PDF byte writer"
```

---

### Task 5: `tests/scripts/verify_halyk_file.py` — различать «пропатчено» / «guard» / «подмена»

**Files:**
- Modify: `halyk_pdf_service.py:981` (`_process_halyk_pdf_once` — сигнатура и `return`), `halyk_pdf_service.py:2015-2052` (`process_halyk_pdf` — распаковка и `LAST_RUN_INFO`), `tests/scripts/verify_halyk_file.py` (репортинг `check_bold_row_uniform`)

**Interfaces:**
- Consumes: `_newly_available_cids: Dict[int, Dict[str, float]]` (Task 4 Step 3, уже вычисляется внутри `_process_halyk_pdf_once`)
- Produces: `_process_halyk_pdf_once` теперь возвращает `(bytes, int, Dict[int, Dict[str, float]])` вместо `(bytes, int)`; `halyk_pdf_service.LAST_RUN_INFO` получает новый ключ `"glyphs_patched"`.

**Важное следствие патча глифов, которое нужно учитывать в этой задаче:** `_newly_available_cids` вычисляется в начале `_process_halyk_pdf_once` из статического набора «каких цифр не хватает в subset'е» — это НЕ зависит от ±3% шума конкретной попытки. Значит, если gate прошёл на первой попытке, он пройдёт (с тем же результатом) на любой попытке — `needs_switch` для пропатченных цифр перестаёт срабатывать сразу, и retry-цикл в `process_halyk_pdf` для этих файлов теперь будет завершаться на `attempts=1` вместо перебора до 24 раз. Это ожидаемо, не баг — само по себе является дополнительным подтверждением, что патч реально работает (Task 6, Step 1 должен это заметить в логах: `попытка 1 из 24` вместо прежних 5-12).

- [ ] **Step 1: Изменить сигнатуру `_process_halyk_pdf_once`**

В `halyk_pdf_service.py:981`, изменить возвращаемый тип в докстроке и добавить `_newly_available_cids` в `return` (сейчас `return result, font_substitutions` в самом конце функции — рядом со строкой `print(f"\n[Halyk] Произведено замен: {total_replaced}")`):

```python
def _process_halyk_pdf_once(
    input_bytes: bytes, target_monthly_income: float
) -> Tuple[bytes, int, Dict[int, Dict[str, float]]]:
    """... (текст докстроки как раньше, плюс одна строка)

    Третий элемент — {cid_xref: {cid_hex: width}} для Bold-шрифтов, в которые
    реально были вшиты недостающие глифы цифр в этом прогоне (Task 3/4) —
    используется только для отчётности автотестов (LAST_RUN_INFO), не
    прод-логикой.
    """
    ...
    return result, font_substitutions, _newly_available_cids
```

- [ ] **Step 2: Обновить `process_halyk_pdf` под новую сигнатуру**

В `halyk_pdf_service.py:2015-2052`, три места распаковки/использования:

```python
    LAST_RUN_INFO.clear()
    result, subs, glyphs_patched = _process_halyk_pdf_once(input_bytes, target_monthly_income)
    attempts = 1
    if subs == 0:
        LAST_RUN_INFO.update(attempts=1, min_substitutions=0, unavoidable=False,
                              glyphs_patched=glyphs_patched)
        return result
    for attempt in range(2, _BOLD_GLYPH_RETRIES + 1):
        cand, cand_subs, cand_glyphs_patched = _process_halyk_pdf_once(input_bytes, target_monthly_income)
        attempts = attempt
        if cand_subs == 0:
            print(f"[Halyk] Подмена шрифта в строке итогов не понадобилась "
                  f"(попытка {attempt} из {_BOLD_GLYPH_RETRIES})")
            LAST_RUN_INFO.update(attempts=attempt, min_substitutions=0, unavoidable=False,
                                  glyphs_patched=cand_glyphs_patched)
            return cand
        if cand_subs < subs:
            result, subs, glyphs_patched = cand, cand_subs, cand_glyphs_patched
    ...
    LAST_RUN_INFO.update(
        attempts=attempts,
        min_substitutions=subs,
        unavoidable=attempts >= _MIN_ATTEMPTS_TO_PROVE,
        glyphs_patched=glyphs_patched,
    )
```

(Остальной текст функции, включая docstring и печать предупреждений, не меняется.)

- [ ] **Step 3: Прогнать `pytest tests/`, убедиться в отсутствии регрессии от смены сигнатуры**

Run: `pytest tests/ -v`
Expected: без изменений в результатах (только внутренний контракт функции поменялся, внешнее поведение `process_halyk_pdf` — нет).

- [ ] **Step 4: В `verify_halyk_file.py` добавить пометку в отчёт `check_bold_row_uniform`**

```bash
grep -n "LAST_RUN_INFO\|check_bold_row_uniform\|guard" tests/scripts/verify_halyk_file.py
```

Там, где сейчас репортится `[guard]` при `LAST_RUN_INFO.get("unavoidable")`, добавить перед этим проверкой: если `LAST_RUN_INFO.get("glyphs_patched")` непусто для этого прогона — печатать `[glyph-patched]` (не FAIL, не guard — отдельная, более сильная категория «вообще не потребовалась подмена, шрифт физически дополнен»), чтобы в выводе battery было видно, что новый путь реально сработал, а не просто совпал с тем, что раньше проходило без единой подмены.

- [ ] **Step 5: Commit**

```bash
git add halyk_pdf_service.py tests/scripts/verify_halyk_file.py
git commit -m "test(halyk): surface glyph-patch success in verify battery reporting"
```

---

### Task 6: Полная валидация на реальных файлах + документация

**Files:**
- Modify: `CLAUDE.md` (добавить раздел с результатами, по конвенции этого проекта — каждая сессия фиксирует измерения)

- [ ] **Step 1: Прогнать полную battery на локальном корпусе**

```bash
python tests/scripts/verify_halyk_file.py "C:\Users\Abylay\Desktop\testpdf\halyk\h6.pdf" "C:\Users\Abylay\Desktop\testpdf\halyk\HALYKformat1.pdf" "C:\Users\Abylay\Desktop\testpdf\halyk\HALYKformat2.pdf" "C:\Users\Abylay\Desktop\testpdf\halyk\HALYKformat3.pdf" "C:\Users\Abylay\Desktop\testpdf\halyk\HALYKformat4.pdf" "C:\Users\Abylay\Desktop\testpdf\halyk\hformat5.pdf" --targets 0.6,1.05,2,5,20
```

Ожидаемо: связки `h6.pdf ×1.05/×2/×5/×20` и `HALYKformat1.pdf ×5` теперь помечены `[glyph-patched]` вместо `[guard]`; остальные без изменений (0 FAIL).

- [ ] **Step 2: Прогнать multi-seed battery (как в предыдущих сессиях — минимум 20-30 прогонов с разными сидами `random`, т.к. и старый перебор шума, и новый gate зависят от рандома)**

Написать разовый скрипт по образцу предыдущих прогонов в `CLAUDE.md` (4-5 сидов × 6 файлов × 5 целей), убедиться 0 новых проблем.

- [ ] **Step 3: `pytest tests/`**

Run: `pytest tests/ -v`
Expected: не хуже 83 passed / 69 skipped, плюс новые тесты этого плана (Task 1: 8, Task 2: 5, Task 3: 6 = +19).

- [ ] **Step 4: Отрисовать и посмотреть глазами**

`--render` на `h6.pdf` и `HALYKformat1.pdf` при задетых целях — визуально подтвердить, что «1»/«5»/«7» (h6) и «4» (HALYKformat1) в строке итогов теперь настоящий Bold, а не Regular-вставка.

- [ ] **Step 5: Дописать раздел в `CLAUDE.md` с измеренными результатами**

По конвенции этого файла (см. существующие разделы «Исправлено 2026-08-0X») — конкретные числа: сколько из 5 оставшихся связок закрыто, что осталось (если что-то осталось из-за отказа gate'а — хотя на этих 6 файлах gate обязан пройти, т.к. побайтовое совпадение уже проверено).

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record Halyk bold glyph embedding validation results"
```

# -*- coding: utf-8 -*-
"""Офлайн-генератор kaspi_ip_glyphs.py из C:\\Windows\\Fonts\\arial.ttf.

Запускается вручную, в рантайме приложения НЕ используется — как и
halyk_bold_digits.py, таблица глифов фиксируется в исходнике, чтобы
приложение не читало системный шрифт с диска и не тянуло fontTools.

ВАЖНО: 27 символов набора — СОСТАВНЫЕ глифы (А→A, Ё→E+dieresis,
Й→uni0418+breve, Э→uni0404 и т. д.). Составной глиф ссылается на другие GID,
поэтому в таблицу кладутся и компоненты, рекурсивно, иначе вшитый глиф
отрисуется пустым.

    python tests/scripts/extract_arial_glyphs.py > kaspi_ip_glyphs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fontTools.ttLib import TTFont

from kaspi_ip_data_service import ALLOWED_CHARS

ARIAL = r"C:\Windows\Fonts\arial.ttf"


def collect(font):
    glyf = font["glyf"]
    order = font.getGlyphOrder()
    index = {name: i for i, name in enumerate(order)}
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    upem = font["head"].unitsPerEm

    char_gid = {}
    need = set()
    for ch in sorted(ALLOWED_CHARS):
        name = cmap.get(ord(ch))
        if name is None:
            raise SystemExit(f"в arial.ttf нет символа {ch!r}")
        char_gid[ch] = index[name]
        need.add(name)

    # рекурсивно добираем компоненты составных глифов
    stack = list(need)
    while stack:
        name = stack.pop()
        g = glyf[name]
        if g.numberOfContours == -1:
            for comp in g.components:
                if comp.glyphName not in need:
                    need.add(comp.glyphName)
                    stack.append(comp.glyphName)

    glyphs = {}
    widths = {}
    for name in sorted(need):
        gid = index[name]
        glyphs[gid] = glyf.glyphs[name].compile(glyf) if glyf[name].numberOfContours != 0 else b""
        widths[gid] = hmtx[name][0] * 1000.0 / upem
    return char_gid, glyphs, widths, upem


def main():
    font = TTFont(ARIAL)
    char_gid, glyphs, widths, upem = collect(font)
    out = sys.stdout
    out.write('# -*- coding: utf-8 -*-\n')
    out.write('"""Байты глифов Arial для вшивания в subset шаблона Kaspi ИП.\n\n')
    out.write('СГЕНЕРИРОВАНО tests/scripts/extract_arial_glyphs.py — руками не править.\n')
    out.write('Извлечено один раз из C:\\\\Windows\\\\Fonts\\\\arial.ttf; рантайм не читает\n')
    out.write('никакой файл с диска и не использует fontTools. Совпадение с subset\'ом\n')
    out.write('обрабатываемого файла проверяется gate\'ом перед тем, как этим данным\n')
    out.write('довериться (см. kaspi_ip_data_service._trusted_glyph_source).\n\n')
    out.write('Включает компоненты составных глифов: их 27 в наборе, и без компонента\n')
    out.write('составной глиф рисуется пустым.\n"""\n\n')
    out.write('from __future__ import annotations\n\n')
    out.write(f'SOURCE_UNITS_PER_EM = {upem}\n\n')
    out.write('CHAR_GID: dict[str, int] = {\n')
    for ch, gid in sorted(char_gid.items()):
        out.write(f'    {ch!r}: {gid},\n')
    out.write('}\n\n')
    out.write('WIDTHS_1000: dict[int, float] = {\n')
    for gid, w in sorted(widths.items()):
        out.write(f'    {gid}: {w!r},\n')
    out.write('}\n\n')
    out.write('_GLYPHS_HEX: dict[int, str] = {\n')
    for gid, data in sorted(glyphs.items()):
        out.write(f'    {gid}: {data.hex()!r},\n')
    out.write('}\n\n')
    out.write('GLYPHS: dict[int, bytes] = {g: bytes.fromhex(h) for g, h in _GLYPHS_HEX.items()}\n')


main()

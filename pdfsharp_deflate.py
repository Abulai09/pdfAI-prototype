# -*- coding: utf-8 -*-
"""Байт-точное воспроизведение deflate-компрессора, которым написаны оригиналы Halyk.

ЗАЧЕМ ЭТО СУЩЕСТВУЕТ
--------------------
Оригиналы Halyk выпущены генератором `PDFsharp 1.50 / MigraDoc` (см. /Producer),
а он сжимает потоки НЕ через zlib, а через собственную копию SharpZipLib
(`PdfSharp.SharpZipLib.Zip.Compression.Deflater`). Это самостоятельная
реализация DEFLATE, а не обёртка над zlib, и её вывод побайтово отличается от
`zlib.compress` при любых параметрах.

Замер 2026-08-07 на корпусе `testpdf/halyk` (6 файлов): из 56 потоков (content
страниц + ToUnicode + FontFile2) НИ ОДИН не воспроизводится `zlib.compress`
ни на одном уровне 0..9; перебор 405 конфигураций
(level × memLevel × strategy) на отдельном потоке — тоже пусто (ближайший по
длине даёт 994 байта против 993 у оригинала). Обратное тоже верно: любой поток,
переписанный нами через `zlib.compress`, воспроизводится `zlib.compress(данные, 6)`
ТОЧНО — то есть «переписан python-скриптом» читается без всякого эталона, простой
проверкой в три строки. До этой правки так светились 145 потоков на 24 связках.

ЧТО ЭТО ЗА КОД
--------------
Порт `DeflaterEngine` + `DeflaterHuffman` + `DeflaterPending` из SharpZipLib
(ветка master, https://github.com/icsharpcode/SharpZipLib) на Python, один в
один по алгоритму. Отличия от zlib, из-за которых вывод и расходится, — не
косметические:

* своё построение дерева Хаффмана (куча с упаковкой `freq<<8 | depth`,
  свой порядок слияния и своя раздача длин) — при равных частотах длины кодов
  распределяются иначе, чем у zlib;
* нет адаптивного досрочного разрыва блока (zlib каждые ~8K символов решает,
  не пора ли закрыть блок; здесь блок закрывается строго по заполнению буфера
  в 16384 символа);
* `COMPR_FUNC[4] = FAST`, тогда как zlib на уровне 4 идёт в `deflate_slow`;
* окно начинается с индекса 1 (`blockStart = strstart = 1`), а не с нуля.

ПРОВЕРКА КОРРЕКТНОСТИ — не «похоже», а тождество: `compress()` обязан
побайтово воспроизвести все 56 оригинальных потоков корпуса из их же
распакованного содержимого. Это и есть оракул; постоянная проверка —
`check_stream_compressor` в `tests/scripts/verify_halyk_file.py`.

ЕСЛИ ОРАКУЛ КОГДА-НИБУДЬ НЕ СОЙДЁТСЯ на новом реальном файле — значит его
выпустил другой генератор, и подставлять этот компрессор ему НЕЛЬЗЯ:
вызывающая сторона (`halyk_pdf_service._pick_stream_compressor`) сперва
проверяет воспроизводимость на нетронутых потоках самого файла и только потом
доверяет ему. Это та же дисциплина «сначала проверь, потом доверяй», что у
gate'а вшивания глифов.
"""

from typing import List, Optional

# ── Константы DeflaterConstants ───────────────────────────────────────────────
MAX_MATCH = 258
MIN_MATCH = 3
MAX_WBITS = 15
WSIZE = 1 << MAX_WBITS
WMASK = WSIZE - 1
DEFAULT_MEM_LEVEL = 8
HASH_BITS = DEFAULT_MEM_LEVEL + 7
HASH_SIZE = 1 << HASH_BITS
HASH_MASK = HASH_SIZE - 1
HASH_SHIFT = (HASH_BITS + MIN_MATCH - 1) // MIN_MATCH
MIN_LOOKAHEAD = MAX_MATCH + MIN_MATCH + 1
MAX_DIST = WSIZE - MIN_LOOKAHEAD
PENDING_BUF_SIZE = 1 << (DEFAULT_MEM_LEVEL + 8)
MAX_BLOCK_SIZE = min(65535, PENDING_BUF_SIZE - 5)

STORED_BLOCK = 0
STATIC_TREES = 1
DYN_TREES = 2

DEFLATE_STORED = 0
DEFLATE_FAST = 1
DEFLATE_SLOW = 2

GOOD_LENGTH = [0, 4, 4, 4, 4, 8, 8, 8, 32, 32]
MAX_LAZY = [0, 4, 5, 6, 4, 16, 16, 32, 128, 258]
NICE_LENGTH = [0, 8, 16, 32, 16, 32, 128, 128, 258, 258]
MAX_CHAIN = [0, 4, 8, 32, 16, 32, 128, 256, 1024, 4096]
COMPR_FUNC = [0, 1, 1, 1, 1, 2, 2, 2, 2, 2]

TOO_FAR = 4096

# ── Константы DeflaterHuffman ─────────────────────────────────────────────────
BUFSIZE = 1 << (DEFAULT_MEM_LEVEL + 6)
LITERAL_NUM = 286
DIST_NUM = 30
BITLEN_NUM = 19
REP_3_6 = 16
REP_3_10 = 17
REP_11_138 = 18
EOF_SYMBOL = 256
BL_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]
_BIT4_REVERSE = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]


def _bit_reverse(v: int) -> int:
    """DeflaterHuffman.BitReverse — разворот 16 бит по 4-битным тетрадам."""
    return (_BIT4_REVERSE[v & 0xF] << 12
            | _BIT4_REVERSE[(v >> 4) & 0xF] << 8
            | _BIT4_REVERSE[(v >> 8) & 0xF] << 4
            | _BIT4_REVERSE[v >> 12])


def _lcode(length: int) -> int:
    if length == 255:
        return 285
    code = 257
    while length >= 8:
        code += 4
        length >>= 1
    return code + length


def _dcode(distance: int) -> int:
    code = 0
    while distance >= 4:
        code += 2
        distance >>= 1
    return code + distance


# Статические таблицы (static tree) — как в статическом конструкторе C#.
_STATIC_L_CODES: List[int] = [0] * LITERAL_NUM
_STATIC_L_LENGTH: List[int] = [0] * LITERAL_NUM
_STATIC_D_CODES: List[int] = [0] * DIST_NUM
_STATIC_D_LENGTH: List[int] = [0] * DIST_NUM


def _init_static() -> None:
    i = 0
    while i < 144:
        _STATIC_L_CODES[i] = _bit_reverse((0x030 + i) << 8)
        _STATIC_L_LENGTH[i] = 8
        i += 1
    while i < 256:
        _STATIC_L_CODES[i] = _bit_reverse((0x190 - 144 + i) << 7)
        _STATIC_L_LENGTH[i] = 9
        i += 1
    while i < 280:
        _STATIC_L_CODES[i] = _bit_reverse((0x000 - 256 + i) << 9)
        _STATIC_L_LENGTH[i] = 7
        i += 1
    while i < LITERAL_NUM:
        _STATIC_L_CODES[i] = _bit_reverse((0x0C0 - 280 + i) << 8)
        _STATIC_L_LENGTH[i] = 8
        i += 1
    for j in range(DIST_NUM):
        _STATIC_D_CODES[j] = _bit_reverse(j << 11)
        _STATIC_D_LENGTH[j] = 5


_init_static()


class _Pending:
    """PendingBuffer: побитовая запись LSB-first + счётчик «недослитых» байт.

    `dirty` повторяет `IsFlushed` оригинала: настоящий `Deflater` сливает
    накопленные ЦЕЛЫЕ байты в выходной буфер вызывающего на каждой итерации,
    поэтому внутренний цикл `DeflaterEngine.Deflate` крутится ровно пока
    очередной блок ещё не записан.
    """

    __slots__ = ("out", "bits", "bit_count", "dirty")

    def __init__(self) -> None:
        self.out = bytearray()
        self.bits = 0
        self.bit_count = 0
        self.dirty = False

    def write_bits(self, b: int, count: int) -> None:
        self.bits |= (b & ((1 << count) - 1) if count < 32 else b) << self.bit_count
        self.bit_count += count
        while self.bit_count >= 8:
            self.out.append(self.bits & 0xFF)
            self.bits >>= 8
            self.bit_count -= 8
            self.dirty = True

    def write_short(self, s: int) -> None:
        self.out.append(s & 0xFF)
        self.out.append((s >> 8) & 0xFF)
        self.dirty = True

    def write_short_msb(self, s: int) -> None:
        self.out.append((s >> 8) & 0xFF)
        self.out.append(s & 0xFF)
        self.dirty = True

    def write_block(self, data, offset: int, length: int) -> None:
        self.out += data[offset:offset + length]
        self.dirty = True

    def align_to_byte(self) -> None:
        if self.bit_count > 0:
            self.out.append(self.bits & 0xFF)
            self.dirty = True
        self.bits = 0
        self.bit_count = 0


class _Tree:
    """DeflaterHuffman.Tree — построение дерева и раздача длин кодов."""

    __slots__ = ("freqs", "length", "codes", "bl_counts", "max_length",
                 "min_num_codes", "num_codes", "pending")

    def __init__(self, pending: _Pending, elems: int, min_codes: int, max_length: int) -> None:
        self.pending = pending
        self.min_num_codes = min_codes
        self.max_length = max_length
        self.freqs = [0] * elems
        self.bl_counts = [0] * max_length
        self.length: Optional[List[int]] = None
        self.codes: Optional[List[int]] = None
        self.num_codes = 0

    def reset(self) -> None:
        for i in range(len(self.freqs)):
            self.freqs[i] = 0
        self.codes = None
        self.length = None

    def write_symbol(self, code: int) -> None:
        self.pending.write_bits(self.codes[code] & 0xFFFF, self.length[code])

    def set_static_codes(self, codes: List[int], lengths: List[int]) -> None:
        self.codes = codes
        self.length = lengths

    def build_codes(self) -> None:
        next_code = [0] * self.max_length
        code = 0
        self.codes = [0] * len(self.freqs)
        for bits in range(self.max_length):
            next_code[bits] = code
            code += self.bl_counts[bits] << (15 - bits)
        for i in range(self.num_codes):
            bits = self.length[i]
            if bits > 0:
                self.codes[i] = _bit_reverse(next_code[bits - 1])
                next_code[bits - 1] += 1 << (16 - bits)

    def build_tree(self) -> None:
        freqs = self.freqs
        num_symbols = len(freqs)
        heap = [0] * num_symbols
        heap_len = 0
        max_code = 0
        for n in range(num_symbols):
            freq = freqs[n]
            if freq != 0:
                pos = heap_len
                heap_len += 1
                while pos > 0:
                    ppos = (pos - 1) // 2
                    if freqs[heap[ppos]] > freq:
                        heap[pos] = heap[ppos]
                        pos = ppos
                    else:
                        break
                heap[pos] = n
                max_code = n

        # Минимум два листа — иначе символ закодировался бы нулём бит.
        while heap_len < 2:
            if max_code < 2:
                max_code += 1
                node = max_code
            else:
                node = 0
            heap[heap_len] = node
            heap_len += 1

        self.num_codes = max(max_code + 1, self.min_num_codes)

        num_leafs = heap_len
        childs = [0] * (4 * heap_len - 2)
        values = [0] * (2 * heap_len - 1)
        num_nodes = num_leafs
        for i in range(heap_len):
            node = heap[i]
            childs[2 * i] = node
            childs[2 * i + 1] = -1
            values[i] = freqs[node] << 8
            heap[i] = i

        while True:
            first = heap[0]
            heap_len -= 1
            last = heap[heap_len]
            ppos = 0
            path = 1
            while path < heap_len:
                if path + 1 < heap_len and values[heap[path]] > values[heap[path + 1]]:
                    path += 1
                heap[ppos] = heap[path]
                ppos = path
                path = path * 2 + 1
            last_val = values[last]
            while True:
                path = ppos
                if path <= 0:
                    break
                ppos = (path - 1) // 2
                if values[heap[ppos]] > last_val:
                    heap[path] = heap[ppos]
                else:
                    break
            heap[path] = last

            second = heap[0]
            last = num_nodes
            num_nodes += 1
            childs[2 * last] = first
            childs[2 * last + 1] = second
            mindepth = min(values[first] & 0xFF, values[second] & 0xFF)
            last_val = values[first] + values[second] - mindepth + 1
            values[last] = last_val

            ppos = 0
            path = 1
            while path < heap_len:
                if path + 1 < heap_len and values[heap[path]] > values[heap[path + 1]]:
                    path += 1
                heap[ppos] = heap[path]
                ppos = path
                path = ppos * 2 + 1
            while True:
                path = ppos
                if path <= 0:
                    break
                ppos = (path - 1) // 2
                if values[heap[ppos]] > last_val:
                    heap[path] = heap[ppos]
                else:
                    break
            heap[path] = last

            if heap_len <= 1:
                break

        if heap[0] != len(childs) // 2 - 1:
            raise RuntimeError("Heap invariant violated")

        self._build_length(childs)

    def _build_length(self, childs: List[int]) -> None:
        self.length = [0] * len(self.freqs)
        num_nodes = len(childs) // 2
        num_leafs = (num_nodes + 1) // 2
        overflow = 0
        for i in range(self.max_length):
            self.bl_counts[i] = 0

        lengths = [0] * num_nodes
        lengths[num_nodes - 1] = 0
        for i in range(num_nodes - 1, -1, -1):
            if childs[2 * i + 1] != -1:
                bit_length = lengths[i] + 1
                if bit_length > self.max_length:
                    bit_length = self.max_length
                    overflow += 1
                lengths[childs[2 * i]] = lengths[childs[2 * i + 1]] = bit_length
            else:
                bit_length = lengths[i]
                self.bl_counts[bit_length - 1] += 1
                self.length[childs[2 * i]] = lengths[i]

        if overflow == 0:
            return

        incr_bit_len = self.max_length - 1
        while True:
            incr_bit_len -= 1
            while self.bl_counts[incr_bit_len] == 0:
                incr_bit_len -= 1
            while True:
                self.bl_counts[incr_bit_len] -= 1
                incr_bit_len += 1
                self.bl_counts[incr_bit_len] += 1
                overflow -= 1 << (self.max_length - 1 - incr_bit_len)
                if not (overflow > 0 and incr_bit_len < self.max_length - 1):
                    break
            if overflow <= 0:
                break

        self.bl_counts[self.max_length - 1] += overflow
        self.bl_counts[self.max_length - 2] -= overflow

        node_ptr = 2 * num_leafs
        for bits in range(self.max_length, 0, -1):
            n = self.bl_counts[bits - 1]
            while n > 0:
                child_ptr = 2 * childs[node_ptr]
                node_ptr += 1
                if childs[child_ptr + 1] == -1:
                    self.length[childs[child_ptr]] = bits
                    n -= 1

    def get_encoded_length(self) -> int:
        return sum(f * l for f, l in zip(self.freqs, self.length))

    def calc_bl_freq(self, bl_tree: "_Tree") -> None:
        curlen = -1
        i = 0
        while i < self.num_codes:
            count = 1
            nextlen = self.length[i]
            if nextlen == 0:
                max_count = 138
                min_count = 3
            else:
                max_count = 6
                min_count = 3
                if curlen != nextlen:
                    bl_tree.freqs[nextlen] += 1
                    count = 0
            curlen = nextlen
            i += 1
            while i < self.num_codes and curlen == self.length[i]:
                i += 1
                count += 1
                if count >= max_count:
                    break
            if count < min_count:
                bl_tree.freqs[curlen] += count
            elif curlen != 0:
                bl_tree.freqs[REP_3_6] += 1
            elif count <= 10:
                bl_tree.freqs[REP_3_10] += 1
            else:
                bl_tree.freqs[REP_11_138] += 1

    def write_tree(self, bl_tree: "_Tree") -> None:
        curlen = -1
        i = 0
        while i < self.num_codes:
            count = 1
            nextlen = self.length[i]
            if nextlen == 0:
                max_count = 138
                min_count = 3
            else:
                max_count = 6
                min_count = 3
                if curlen != nextlen:
                    bl_tree.write_symbol(nextlen)
                    count = 0
            curlen = nextlen
            i += 1
            while i < self.num_codes and curlen == self.length[i]:
                i += 1
                count += 1
                if count >= max_count:
                    break
            if count < min_count:
                while count > 0:
                    count -= 1
                    bl_tree.write_symbol(curlen)
            elif curlen != 0:
                bl_tree.write_symbol(REP_3_6)
                self.pending.write_bits(count - 3, 2)
            elif count <= 10:
                bl_tree.write_symbol(REP_3_10)
                self.pending.write_bits(count - 3, 3)
            else:
                bl_tree.write_symbol(REP_11_138)
                self.pending.write_bits(count - 11, 7)


class _Huffman:
    """DeflaterHuffman."""

    __slots__ = ("pending", "literal_tree", "dist_tree", "bl_tree",
                 "d_buf", "l_buf", "last_lit", "extra_bits")

    def __init__(self, pending: _Pending) -> None:
        self.pending = pending
        self.literal_tree = _Tree(pending, LITERAL_NUM, 257, 15)
        self.dist_tree = _Tree(pending, DIST_NUM, 1, 15)
        self.bl_tree = _Tree(pending, BITLEN_NUM, 4, 7)
        self.d_buf = [0] * BUFSIZE
        self.l_buf = bytearray(BUFSIZE)
        self.last_lit = 0
        self.extra_bits = 0

    def reset(self) -> None:
        self.last_lit = 0
        self.extra_bits = 0
        self.literal_tree.reset()
        self.dist_tree.reset()
        self.bl_tree.reset()

    def is_full(self) -> bool:
        return self.last_lit >= BUFSIZE

    def tally_lit(self, literal: int) -> bool:
        self.d_buf[self.last_lit] = 0
        self.l_buf[self.last_lit] = literal
        self.last_lit += 1
        self.literal_tree.freqs[literal] += 1
        return self.is_full()

    def tally_dist(self, distance: int, length: int) -> bool:
        self.d_buf[self.last_lit] = distance
        self.l_buf[self.last_lit] = length - 3
        self.last_lit += 1
        lc = _lcode(length - 3)
        self.literal_tree.freqs[lc] += 1
        if 265 <= lc < 285:
            self.extra_bits += (lc - 261) // 4
        dc = _dcode(distance - 1)
        self.dist_tree.freqs[dc] += 1
        if dc >= 4:
            self.extra_bits += dc // 2 - 1
        return self.is_full()

    def send_all_trees(self, bl_tree_codes: int) -> None:
        self.bl_tree.build_codes()
        self.literal_tree.build_codes()
        self.dist_tree.build_codes()
        self.pending.write_bits(self.literal_tree.num_codes - 257, 5)
        self.pending.write_bits(self.dist_tree.num_codes - 1, 5)
        self.pending.write_bits(bl_tree_codes - 4, 4)
        for rank in range(bl_tree_codes):
            self.pending.write_bits(self.bl_tree.length[BL_ORDER[rank]], 3)
        self.literal_tree.write_tree(self.bl_tree)
        self.dist_tree.write_tree(self.bl_tree)

    def compress_block(self) -> None:
        lit_tree = self.literal_tree
        dist_tree = self.dist_tree
        pending = self.pending
        for i in range(self.last_lit):
            litlen = self.l_buf[i]
            dist = self.d_buf[i]
            if dist != 0:
                dist -= 1
                lc = _lcode(litlen)
                lit_tree.write_symbol(lc)
                bits = (lc - 261) // 4
                if 0 < bits <= 5:
                    pending.write_bits(litlen & ((1 << bits) - 1), bits)
                dc = _dcode(dist)
                dist_tree.write_symbol(dc)
                bits = dc // 2 - 1
                if bits > 0:
                    pending.write_bits(dist & ((1 << bits) - 1), bits)
            else:
                lit_tree.write_symbol(litlen)
        lit_tree.write_symbol(EOF_SYMBOL)

    def flush_stored_block(self, stored, stored_offset: int, stored_length: int,
                           last_block: bool) -> None:
        self.pending.write_bits((STORED_BLOCK << 1) + (1 if last_block else 0), 3)
        self.pending.align_to_byte()
        self.pending.write_short(stored_length)
        self.pending.write_short(~stored_length & 0xFFFF)
        self.pending.write_block(stored, stored_offset, stored_length)
        self.reset()

    def flush_block(self, stored, stored_offset: int, stored_length: int,
                    last_block: bool) -> None:
        self.literal_tree.freqs[EOF_SYMBOL] += 1
        self.literal_tree.build_tree()
        self.dist_tree.build_tree()
        self.literal_tree.calc_bl_freq(self.bl_tree)
        self.dist_tree.calc_bl_freq(self.bl_tree)
        self.bl_tree.build_tree()

        # В оригинале счётчик правится ПРЯМО В УСЛОВИИ цикла
        # (`for (int i = 18; i > blTreeCodes; i--)`), поэтому после первого же
        # попадания условие `i > blTreeCodes` становится ложным и цикл встаёт.
        # Наивный `range(18, 4, -1)` вычисляется один раз и продолжает крутиться,
        # перезаписывая значение более низким — из-за этого HCLEN получался
        # короче и поток расходился с оригинала уже на 4-м байте.
        bl_tree_codes = 4
        for i in range(18, bl_tree_codes, -1):
            if self.bl_tree.length[BL_ORDER[i]] > 0:
                bl_tree_codes = i + 1
                break
        opt_len = (14 + bl_tree_codes * 3 + self.bl_tree.get_encoded_length()
                   + self.literal_tree.get_encoded_length()
                   + self.dist_tree.get_encoded_length() + self.extra_bits)

        static_len = self.extra_bits
        for i in range(LITERAL_NUM):
            static_len += self.literal_tree.freqs[i] * _STATIC_L_LENGTH[i]
        for i in range(DIST_NUM):
            static_len += self.dist_tree.freqs[i] * _STATIC_D_LENGTH[i]

        if opt_len >= static_len:
            opt_len = static_len

        if stored_offset >= 0 and stored_length + 4 < opt_len >> 3:
            self.flush_stored_block(stored, stored_offset, stored_length, last_block)
        elif opt_len == static_len:
            self.pending.write_bits((STATIC_TREES << 1) + (1 if last_block else 0), 3)
            self.literal_tree.set_static_codes(_STATIC_L_CODES, _STATIC_L_LENGTH)
            self.dist_tree.set_static_codes(_STATIC_D_CODES, _STATIC_D_LENGTH)
            self.compress_block()
            self.reset()
        else:
            self.pending.write_bits((DYN_TREES << 1) + (1 if last_block else 0), 3)
            self.send_all_trees(bl_tree_codes)
            self.compress_block()
            self.reset()


class _Engine:
    """DeflaterEngine."""

    def __init__(self, level: int) -> None:
        self.pending = _Pending()
        self.huffman = _Huffman(self.pending)
        self.window = bytearray(2 * WSIZE)
        self.head = [0] * HASH_SIZE
        self.prev = [0] * WSIZE
        self.ins_h = 0
        self.match_start = 0
        self.match_len = MIN_MATCH - 1
        self.prev_available = False
        self.block_start = 1
        self.strstart = 1
        self.lookahead = 0
        self.input_buf = b""
        self.input_off = 0
        self.input_end = 0
        self.adler = 1
        self.good_length = GOOD_LENGTH[level]
        self.max_lazy = MAX_LAZY[level]
        self.nice_length = NICE_LENGTH[level]
        self.max_chain = MAX_CHAIN[level]
        self.compression_function = COMPR_FUNC[level]

    # ── вспомогательное ──────────────────────────────────────────────────────
    def set_input(self, data: bytes) -> None:
        self.input_buf = data
        self.input_off = 0
        self.input_end = len(data)

    def needs_input(self) -> bool:
        return self.input_end == self.input_off

    def _update_hash(self) -> None:
        w = self.window
        s = self.strstart
        self.ins_h = (w[s] << HASH_SHIFT) ^ w[s + 1]

    def _insert_string(self) -> int:
        w = self.window
        s = self.strstart
        h = ((self.ins_h << HASH_SHIFT) ^ w[s + MIN_MATCH - 1]) & HASH_MASK
        match = self.head[h]
        self.prev[s & WMASK] = match
        self.head[h] = s & 0xFFFF
        self.ins_h = h
        return match

    def _slide_window(self) -> None:
        w = self.window
        w[0:WSIZE] = w[WSIZE:2 * WSIZE]
        self.match_start -= WSIZE
        self.strstart -= WSIZE
        self.block_start -= WSIZE
        head = self.head
        for i in range(HASH_SIZE):
            m = head[i]
            head[i] = m - WSIZE if m >= WSIZE else 0
        prev = self.prev
        for i in range(WSIZE):
            m = prev[i]
            prev[i] = m - WSIZE if m >= WSIZE else 0

    def _fill_window(self) -> None:
        if self.strstart >= WSIZE + MAX_DIST:
            self._slide_window()
        if self.lookahead < MIN_LOOKAHEAD and self.input_off < self.input_end:
            more = 2 * WSIZE - self.lookahead - self.strstart
            if more > self.input_end - self.input_off:
                more = self.input_end - self.input_off
            dst = self.strstart + self.lookahead
            chunk = self.input_buf[self.input_off:self.input_off + more]
            self.window[dst:dst + more] = chunk
            self.adler = _adler32(chunk, self.adler)
            self.input_off += more
            self.lookahead += more
        if self.lookahead >= MIN_MATCH:
            self._update_hash()

    # ── поиск самого длинного совпадения ─────────────────────────────────────
    def _find_longest_match(self, cur_match: int) -> bool:
        """Порт FindLongestMatch.

        Развёрнутые вручную сравнения по 8 в оригинале эквивалентны «длине
        общего префикса, ограниченной scanMax»: цепочка `switch((scanMax-scan)%8)`
        + цикл по 8 подобраны так, что проверка `scan == scanMax` попадает ровно
        на границу. Поэтому здесь длина общего префикса считается разом через
        XOR двух срезов — это то же значение, только без побайтового цикла на
        Python (иначе один поток в 70 КБ считался бы десятки секунд).
        """
        window = self.window
        prev = self.prev
        strstart = self.strstart
        lookahead = self.lookahead
        max_compare = MAX_MATCH if MAX_MATCH < lookahead else lookahead
        scan_max = strstart + max_compare - 1
        limit = strstart - MAX_DIST
        if limit < 0:
            limit = 0
        chain_length = self.max_chain
        nice_length = self.nice_length if self.nice_length < lookahead else lookahead
        match_len = self.match_len
        if match_len < MIN_MATCH - 1:
            match_len = MIN_MATCH - 1

        if strstart + match_len > scan_max:
            self.match_len = match_len
            return False

        scan_end1 = window[strstart + match_len - 1]
        scan_end = window[strstart + match_len]
        if match_len >= self.good_length:
            chain_length >>= 2

        w0 = window[strstart]
        w1 = window[strstart + 1]
        match_start = self.match_start
        found = False

        while True:
            m = cur_match
            if (window[m + match_len] == scan_end
                    and window[m + match_len - 1] == scan_end1
                    and window[m] == w0
                    and window[m + 1] == w1):
                # длина общего префикса, но не длиннее max_compare
                a = window[strstart:strstart + max_compare]
                b = window[m:m + max_compare]
                if a == b:
                    common = max_compare
                else:
                    x = int.from_bytes(a, "big") ^ int.from_bytes(b, "big")
                    common = max_compare - ((x.bit_length() + 7) >> 3)
                if common > match_len:
                    match_start = cur_match
                    match_len = common
                    found = True
                    if match_len >= nice_length:
                        break
                    scan_end1 = window[strstart + match_len - 1]
                    scan_end = window[strstart + match_len]
            cur_match = prev[cur_match & WMASK]
            chain_length -= 1
            if cur_match <= limit or chain_length == 0:
                break

        self.match_len = match_len
        if found:
            self.match_start = match_start
        return match_len >= MIN_MATCH

    # ── стратегии ────────────────────────────────────────────────────────────
    def _deflate_fast(self, flush: bool, finish: bool) -> bool:
        if self.lookahead < MIN_LOOKAHEAD and not flush:
            return False
        huffman = self.huffman
        while self.lookahead >= MIN_LOOKAHEAD or flush:
            if self.lookahead == 0:
                huffman.flush_block(self.window, self.block_start,
                                    self.strstart - self.block_start, finish)
                self.block_start = self.strstart
                return False
            if self.strstart > 2 * WSIZE - MIN_LOOKAHEAD:
                self._slide_window()
            hash_head = 0
            if self.lookahead >= MIN_MATCH:
                hash_head = self._insert_string()
            if (self.lookahead >= MIN_MATCH and hash_head != 0
                    and self.strstart - hash_head <= MAX_DIST
                    and self._find_longest_match(hash_head)):
                full = huffman.tally_dist(self.strstart - self.match_start, self.match_len)
                self.lookahead -= self.match_len
                if self.match_len <= self.max_lazy and self.lookahead >= MIN_MATCH:
                    while self.match_len > 1:
                        self.match_len -= 1
                        self.strstart += 1
                        self._insert_string()
                    self.strstart += 1
                else:
                    self.strstart += self.match_len
                    if self.lookahead >= MIN_MATCH - 1:
                        self._update_hash()
                self.match_len = MIN_MATCH - 1
                if not full:
                    continue
            else:
                huffman.tally_lit(self.window[self.strstart])
                self.strstart += 1
                self.lookahead -= 1
            if huffman.is_full():
                last_block = finish and self.lookahead == 0
                huffman.flush_block(self.window, self.block_start,
                                    self.strstart - self.block_start, last_block)
                self.block_start = self.strstart
                return not last_block
        return True

    def _deflate_slow(self, flush: bool, finish: bool) -> bool:
        if self.lookahead < MIN_LOOKAHEAD and not flush:
            return False
        huffman = self.huffman
        window = self.window
        while self.lookahead >= MIN_LOOKAHEAD or flush:
            if self.lookahead == 0:
                if self.prev_available:
                    huffman.tally_lit(window[self.strstart - 1])
                self.prev_available = False
                huffman.flush_block(window, self.block_start,
                                    self.strstart - self.block_start, finish)
                self.block_start = self.strstart
                return False

            if self.strstart >= 2 * WSIZE - MIN_LOOKAHEAD:
                self._slide_window()

            prev_match = self.match_start
            prev_len = self.match_len
            if self.lookahead >= MIN_MATCH:
                hash_head = self._insert_string()
                if (hash_head != 0 and self.strstart - hash_head <= MAX_DIST
                        and self._find_longest_match(hash_head)):
                    if (self.match_len <= 5
                            and self.match_len == MIN_MATCH
                            and self.strstart - self.match_start > TOO_FAR):
                        self.match_len = MIN_MATCH - 1

            if prev_len >= MIN_MATCH and self.match_len <= prev_len:
                huffman.tally_dist(self.strstart - 1 - prev_match, prev_len)
                prev_len -= 2
                while True:
                    self.strstart += 1
                    self.lookahead -= 1
                    if self.lookahead >= MIN_MATCH:
                        self._insert_string()
                    prev_len -= 1
                    if prev_len <= 0:
                        break
                self.strstart += 1
                self.lookahead -= 1
                self.prev_available = False
                self.match_len = MIN_MATCH - 1
            else:
                if self.prev_available:
                    huffman.tally_lit(window[self.strstart - 1])
                self.prev_available = True
                self.strstart += 1
                self.lookahead -= 1

            if huffman.is_full():
                length = self.strstart - self.block_start
                if self.prev_available:
                    length -= 1
                last_block = finish and self.lookahead == 0 and not self.prev_available
                huffman.flush_block(window, self.block_start, length, last_block)
                self.block_start += length
                return not last_block
        return True

    def deflate(self, flush: bool, finish: bool) -> bool:
        self.pending.dirty = False
        while True:
            self._fill_window()
            can_flush = flush and self.input_off == self.input_end
            if self.compression_function == DEFLATE_SLOW:
                progress = self._deflate_slow(can_flush, finish)
            elif self.compression_function == DEFLATE_FAST:
                progress = self._deflate_fast(can_flush, finish)
            else:
                raise ValueError("уровень 0 (stored) не поддержан")
            if self.pending.dirty or not progress:
                return progress


def _adler32(data: bytes, start: int = 1) -> int:
    a = start & 0xFFFF
    b = (start >> 16) & 0xFFFF
    for chunk_start in range(0, len(data), 5552):
        for byte in data[chunk_start:chunk_start + 5552]:
            a += byte
            b += a
        a %= 65521
        b %= 65521
    return ((b << 16) | a) & 0xFFFFFFFF


def compress(data: bytes, level: int = 6) -> bytes:
    """Сжать `data` ровно так, как это сделал бы PDFsharp/SharpZipLib.

    Возвращает поток с zlib-обёрткой (заголовок + adler32), пригодный для
    `/FlateDecode` — и, в отличие от `zlib.compress`, НЕ воспроизводимый
    python-zlib ни на одном уровне.
    """
    if not 1 <= level <= 9:
        raise ValueError("поддерживаются уровни 1..9")
    eng = _Engine(level)

    # Заголовок zlib — как его пишет Deflater (INIT_STATE).
    header = (8 + ((MAX_WBITS - 8) << 4)) << 8
    level_flags = (level - 1) >> 1
    if level_flags < 0 or level_flags > 3:
        level_flags = 3
    header |= level_flags << 6
    header += 31 - (header % 31)
    eng.pending.write_short_msb(header)

    eng.set_input(data)
    while not eng.needs_input():
        if not eng.deflate(False, False):
            break
    while eng.deflate(True, True):
        pass
    eng.pending.align_to_byte()
    adler = eng.adler
    eng.pending.write_short_msb((adler >> 16) & 0xFFFF)
    eng.pending.write_short_msb(adler & 0xFFFF)
    return bytes(eng.pending.out)

"""
Тесты стиля сериализации операторов в переписанных content-стримах.

Форензик-разбор 11 результатов против 3 оригиналов (02/08/2026) показал три
механических признака вмешательства — при полностью совпадающих метаданных,
датах, /ID, числе объектов и корректном xref. Все три — про то, КАК записан
оператор, а не про то, что он делает:

  1. Избыточные нули в координатах: оригинал пишет «42.5», «211», «219.31»
     (незначащие нули отброшены), писатель писал «42.50000», «211.00000».
     0 таких чисел в каждом из 3 оригиналов против 102…163 в каждом из
     11 результатов — разделение полное, промежуточных значений нет.
  2. Формат gold_statement: оригинал ВСЕГДА разносит Td и Tj по разным
     строкам, писатель склеивал их в одну (0 склеенных строк в оригинале
     против 198…247 в результатах).
  3. Формат «Выписка по счету» (Kaspi ИП): оригинал пишет «)Tj» вплотную,
     писатель вставлял пробел — «) Tj» (0 против 102…104).

Суть признаков не в том, какой стиль «правильный», а в том, что оригинал
однороден на 100%, а в результате появляется группа строк, выбивающаяся из
почерка документа, и её размер совпадает с числом изменённых сумм.

Критерий качества 2 (позиционирование): обе правки МЕНЯЮТ ТОЛЬКО БАЙТЫ
ЗАПИСИ, а не геометрию. `_fmt_coord` сохраняет ту же точность округления до
5 знаков, что и прежний `:.5f`; пробел, перевод строки и их отсутствие —
эквивалентные разделители токенов в content-стриме PDF.

Тесты не зависят от tests/fixtures/ и проходят в любом чекауте.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pdf_service import _fmt_coord, _op_separators


# ─────────────────────── Признак 1: избыточные нули ───────────────────────

def test_fmt_coord_drops_trailing_zeros_of_whole_number():
    # Оригинал пишет «211», писатель писал «211.00000».
    assert _fmt_coord(211.0) == "211"


def test_fmt_coord_keeps_all_significant_decimals():
    # У оригинала тоже бывает 5 знаков — обрезать точность нельзя,
    # иначе поедет геометрия (критерий 2).
    assert _fmt_coord(510.94995) == "510.94995"


def test_fmt_coord_drops_only_the_insignificant_zeros():
    assert _fmt_coord(42.50) == "42.5"
    assert _fmt_coord(219.31) == "219.31"


def test_fmt_coord_rounds_identically_to_the_old_five_decimal_format():
    # Значение обязано остаться прежним: новая запись — это ровно тот же
    # `:.5f`, только без хвостовых нулей.
    for x in (0.0, 42.5, 126.7545, 510.94995, 219.31, 1234.567891, -3.2):
        assert float(_fmt_coord(x)) == float(f"{x:.5f}")


def test_fmt_coord_negative_zero_is_written_as_plain_zero():
    # «-0» — сам по себе признак машинной записи; оригинал так не пишет.
    assert _fmt_coord(-0.0) == "0"
    assert _fmt_coord(-0.000001) == "0"


def test_fmt_coord_never_emits_bare_dot_or_empty_string():
    for x in (0.0, -0.0, 1e-9, 100.0, -100.0):
        out = _fmt_coord(x)
        assert out not in ("", ".", "-", "-."), out
        assert not out.endswith("."), out


def test_fmt_coord_keeps_negative_sign():
    # Halyk-паттерн допускает отрицательные координаты (dx при переносе).
    assert _fmt_coord(-15.25) == "-15.25"


# ────────────── Признаки 2 и 3: разделители вокруг строки-аргумента ──────────────

def test_op_separators_preserves_newline_between_td_and_hex_string():
    # Формат gold_statement: оригинал разносит Td и Tj по строкам.
    sep_open, sep_close = _op_separators(b"42.5 510.94995 Td\n<000300030D5F> Tj")
    assert sep_open == b"\n"
    assert sep_close == b" "


def test_op_separators_preserves_absence_of_space_before_tj():
    # Формат «Выписка по счету»: оригинал пишет «)Tj» вплотную.
    sep_open, sep_close = _op_separators(b"1 0 0 1 211 456 Tm\n/F1 8 Tf\n(abc)Tj")
    assert sep_open == b"\n"
    assert sep_close == b""


def test_op_separators_ignores_inline_font_switch_before_the_string():
    # Halyk: между Td и <hex> может стоять собственный «/F0 8 Tf» токена.
    # Разделителем считается то, что стоит вплотную ПЕРЕД строкой-аргументом.
    sep_open, sep_close = _op_separators(b"12 0 Td /F0 8 Tf <00AB> Tj")
    assert sep_open == b" "
    assert sep_close == b" "


def test_op_separators_handles_crlf_line_endings():
    sep_open, sep_close = _op_separators(b"42.5 510.5 Td\r\n<00AB>\r\nTj")
    assert sep_open == b"\r\n"
    assert sep_close == b"\r\n"


def test_op_separators_uses_the_last_string_before_tj():
    # Cert-страница может эмитить несколько Tj подряд; нас интересует
    # разделитель у ПОСЛЕДНЕГО — именно он предшествует финальному Tj.
    sep_open, sep_close = _op_separators(b"1 2 Td /F1 8 Tf <00AB> Tj /F2 8 Tf<00CD>Tj")
    assert sep_open == b""
    assert sep_close == b""


def test_op_separators_defaults_to_single_space_when_nothing_matches():
    # Деградация должна быть в прежнее поведение, а не в исключение.
    assert _op_separators(b"no operators here") == (b" ", b" ")


# ────────────────── Регрессия: побайтовое воспроизведение оригинала ──────────────────

@pytest.mark.parametrize("original", [
    b"42.5 510.94995 Td\n<000300030D5F> Tj",          # gold_statement
    b"211 456 Td\n<0003> Tj",                          # целая координата
    b"219.31 385 Td <00AB>Tj",                         # смешанный стиль
])
def test_hex_rebuild_is_byte_identical_when_the_value_does_not_change(original):
    """Если сумма не изменилась, переписанный токен обязан совпасть с
    оригиналом ПОБАЙТОВО — иначе мы оставляем след там, где правки нет."""
    import re
    m = re.match(
        rb"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+Td\s*<([0-9A-Fa-f]+)>\s*Tj",
        original,
    )
    assert m is not None, "паттерн должен матчить оба стиля оригинала"
    x, y_str, hex_str = float(m.group(1)), m.group(2).decode(), m.group(3).decode()

    sep_open, sep_close = _op_separators(m.group(0))
    rebuilt = (
        f"{_fmt_coord(x)} {y_str} Td".encode("ascii")
        + sep_open + f"<{hex_str}>".encode("ascii")
        + sep_close + b"Tj"
    )
    assert rebuilt == original


def test_paren_rebuild_is_byte_identical_when_the_value_does_not_change():
    original = b"1 0 0 1 211 456 Tm\n/F1 8 Tf\n(0123)Tj"
    sep_open, sep_close = _op_separators(original)
    rebuilt = (
        b"1 0 0 1 " + _fmt_coord(211.0).encode("ascii") + b" 456 Tm\n"
        + b"/F1 8 Tf" + sep_open + b"(0123)" + sep_close + b"Tj"
    )
    assert rebuilt == original

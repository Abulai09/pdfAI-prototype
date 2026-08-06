"""Fixture-free тесты для переписывания суммы внутри текста «Назначение
платежа» (см. docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md).
Строки — синтетические, но формат (разделители, структура фразы) взят из
реальных примеров, задокументированных в спеке."""

from kaspi_ip_pdf_service import (
    _locate_purpose_amount,
    _format_purpose_amount,
    _unescape_pdf_literal,
    _escape_pdf_literal,
)


# ─── _locate_purpose_amount ─────────────────────────────────────────────────

def test_locate_no_thousands_sep_dash_decimal():
    line = 'г. Сумма 145000-00 тенге без НДС (филиал'
    loc = _locate_purpose_amount(line, 145000.0)
    assert loc is not None
    start, end, thousands_sep, decimal_sep, has_decimal = loc
    assert line[start:end] == "145000-00"
    assert thousands_sep == ""
    assert decimal_sep == "-"
    assert has_decimal is True


def test_locate_space_thousands_dash_decimal():
    line = 'ТОО ALTRA TYRES БИН/ИИН Оплата за транспортные услуги по счету № 0000000153 от 17.06.26 Сумма 475 000-00 тенге в т.ч. НД'
    loc = _locate_purpose_amount(line, 475000.0)
    assert loc is not None
    start, end, thousands_sep, decimal_sep, has_decimal = loc
    assert line[start:end] == "475 000-00"
    assert thousands_sep == " "
    assert decimal_sep == "-"
    assert has_decimal is True


def test_locate_space_thousands_comma_decimal():
    line = "Оплата по счету №44 от 01.01.26 Сумма 65 000,00 тенге без НДС"
    loc = _locate_purpose_amount(line, 65000.0)
    assert loc is not None
    start, end, thousands_sep, decimal_sep, has_decimal = loc
    assert line[start:end] == "65 000,00"
    assert thousands_sep == " "
    assert decimal_sep == ","
    assert has_decimal is True


def test_locate_no_decimal_at_all():
    line = "Оплата за услуги, сумма 75000 тенге без НДС"
    loc = _locate_purpose_amount(line, 75000.0)
    assert loc is not None
    start, end, thousands_sep, decimal_sep, has_decimal = loc
    assert line[start:end] == "75000"
    assert has_decimal is False
    assert decimal_sep == ""


def test_locate_multi_group_thousands():
    line = "счета №311 от 02.12.2025г Сумма 1 080 000-00 тенге в т.ч. НДС(без НДС) 0-00"
    loc = _locate_purpose_amount(line, 1080000.0)
    assert loc is not None
    start, end, thousands_sep, decimal_sep, has_decimal = loc
    assert line[start:end] == "1 080 000-00"
    assert thousands_sep == " "


def test_locate_returns_none_when_amount_split_across_lines():
    # Строка обрывается ровно на "65" — продолжение "000,00" на СЛЕДУЮЩЕЙ
    # визуальной строке (см. спеку: реальный пример "Болеар Казмед",
    # kaspiIP.pdf). Одна строка не содержит суммы целиком.
    line = "по сч №151 от 15.06.2026 г. Сумма - 65"
    assert _locate_purpose_amount(line, 65000.0) is None


def test_locate_returns_none_when_digits_are_substring_of_account_number():
    # "190000" внутри 17-значного номера счёта не должно матчиться —
    # цифры с обеих сторон, граница (?<!\d)/(?!\d) не выполняется.
    line = "IBAN KZ1122330000000190000123 Оплата за товар"
    assert _locate_purpose_amount(line, 190000.0) is None


def test_locate_returns_none_below_minimum():
    line = "Сумма 500-00 тенге"
    assert _locate_purpose_amount(line, 500.0) is None  # < _PURPOSE_AMOUNT_MIN


def test_locate_returns_none_for_empty_line():
    assert _locate_purpose_amount("", 100000.0) is None


# ─── _format_purpose_amount ─────────────────────────────────────────────────

def test_format_no_thousands_sep():
    assert _format_purpose_amount(145000.0, "", "-", True) == "145000-00"


def test_format_space_thousands_dash_decimal():
    assert _format_purpose_amount(475000.0, " ", "-", True) == "475 000-00"


def test_format_space_thousands_comma_decimal():
    assert _format_purpose_amount(65000.0, " ", ",", True) == "65 000,00"


def test_format_multi_group_regrouping():
    # Новая сумма длиннее старой (7 цифр) — обязана перегруппироваться по 3
    # разряда с конца, а не унаследовать старую разбивку.
    assert _format_purpose_amount(5346000.0, " ", "-", True) == "5 346 000-00"


def test_format_no_decimal():
    assert _format_purpose_amount(75000.0, "", "", False) == "75000"


def test_format_no_decimal_with_thousands_sep():
    assert _format_purpose_amount(1500000.0, " ", "", False) == "1 500 000"


# ─── _unescape_pdf_literal / _escape_pdf_literal ────────────────────────────
# Найдено 2026-08-06 на реальном файле (IP4.pdf): в отличие от денежных
# ячеек (только "безопасные" CID цифр/разделителей), CID-байты произвольного
# текста назначения платежа регулярно попадают в 0x28/0x29/0x5C — байты,
# которые PDF literal-строка обязана экранировать backslash'ем. Замер:
# 1420 из 5889 Tj-строк на одной странице содержат хотя бы один такой байт.

def test_unescape_removes_backslash_before_special_bytes():
    # b"\\(" -> b"(" (экранированная открывающая скобка внутри CID-потока)
    assert _unescape_pdf_literal(b"AB\\(CD") == b"AB(CD"
    assert _unescape_pdf_literal(b"AB\\)CD") == b"AB)CD"
    assert _unescape_pdf_literal(b"AB\\\\CD") == b"AB\\CD"


def test_unescape_leaves_plain_bytes_untouched():
    assert _unescape_pdf_literal(b"\x02E\x02b\x02p") == b"\x02E\x02b\x02p"


def test_escape_inserts_backslash_before_special_bytes():
    assert _escape_pdf_literal(b"AB(CD") == b"AB\\(CD"
    assert _escape_pdf_literal(b"AB)CD") == b"AB\\)CD"
    assert _escape_pdf_literal(b"AB\\CD") == b"AB\\\\CD"


def test_escape_leaves_plain_bytes_untouched():
    assert _escape_pdf_literal(b"\x02E\x02b\x02p") == b"\x02E\x02b\x02p"


def test_escape_unescape_roundtrip():
    # Байты, реально встреченные в дампе IP4.pdf (2-байтные CID, чей младший
    # байт совпадает с "(", ")" или "\\").
    raw = b"\x02E\x02b\x02p\x02_\x02\\\x02h\x02c\x00\x03\x02k\x02q\x02_\x02l\x00\x1d"
    escaped = _escape_pdf_literal(raw)
    assert _unescape_pdf_literal(escaped) == raw

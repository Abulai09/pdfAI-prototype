# -*- coding: utf-8 -*-
"""Интеграционные тесты подстановки реквизитов в шаблон Kaspi ИП.

Скипаются, если шаблона нет в чекауте (см. tests/test_kaspi_ip_data_fields.py
и kaspi_ip_data_service.template_path()).
"""
from __future__ import annotations

import re

import pytest
import fitz

import kaspi_ip_data_service as kid

pytestmark = pytest.mark.skipif(
    not kid.template_path().exists(),
    reason=f"нет шаблона {kid.template_path()} — см. Task 1",
)

NEW = kid.KaspiIPFields(
    account="KZ11722S000099887766",
    period_from="01.02.2025",
    period_to="01.02.2026",
    last_movement="31.01.2026 09:15",
    iin="990101300123",
    client_name="ИП ТЕСТОВ ТЕСТ",
)

OLD_ACCOUNT = "KZ45722S000034195994"
OLD_IIN = "810503400268"
OLD_PERIOD = "18.07.2025 - 18.07.2026"
OLD_MOVED = "17.07.2026 23:03"


def _all_text(pdf_bytes):
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(d[i].get_text() for i in range(d.page_count))
    finally:
        d.close()


def test_fixed_fields_replaced_everywhere():
    out = kid.substitute_fixed_length(kid.load_template(), NEW)
    text = _all_text(out)
    assert text.count(NEW.account) == 13
    assert text.count(NEW.iin) == 17
    assert OLD_ACCOUNT not in text
    assert OLD_IIN not in text
    assert "01.02.2025 - 01.02.2026" in text
    assert "31.01.2026 09:15" in text


def test_amounts_and_dates_untouched():
    """Мы не трогаем ни одной суммы и ни одной даты операции.

    Даты шапки (период, дата последнего движения) СОВПАДАЮТ по написанию с
    настоящими датами операций в таблице шаблона (18.07.2025 — первая строка
    таблицы, 17.07.2026 — последняя и одновременно "дата последнего движения").
    Поэтому сравнивать множества дат "до" и "после" вычитанием старых/новых
    значений НЕЛЬЗЯ — это случайно спишет и настоящие даты операций. Правильно:
    заменить в тексте оригинала ИМЕННО подстроку шапки (период и дата движения
    встречаются в оригинале ровно по разу) и сравнить получившийся текст с
    результатом напрямую.
    """
    before = _all_text(kid.load_template())
    after = _all_text(kid.substitute_fixed_length(kid.load_template(), NEW))

    money = lambda t: sorted(re.findall(r"\d[\d  ]*,\d{2}", t))
    assert money(after) == money(before)

    assert before.count(OLD_PERIOD) == 1
    assert before.count(OLD_MOVED) == 1
    expected_after = (
        before.replace(OLD_PERIOD, "01.02.2025 - 01.02.2026")
              .replace(OLD_MOVED, "31.01.2026 09:15")
    )
    op_dates = lambda t: sorted(re.findall(r"\b\d{2}\.\d{2}\.20\d{2}\b", t))
    assert op_dates(after) == op_dates(expected_after)


def test_structure_intact():
    out = kid.substitute_fixed_length(kid.load_template(), NEW)
    d = fitz.open(stream=out, filetype="pdf")
    try:
        assert d.page_count == 101
        assert d.is_repaired is False
    finally:
        d.close()


def test_embed_adds_missing_glyph_and_keeps_existing():
    """«Ы» нет в subset'е шаблона — после вшивания она обязана появиться
    и в карте символов, и в /W, и в ToUnicode."""
    import pdf_service
    raw = kid.load_template()
    d = fitz.open(stream=raw, filetype="pdf")
    _, before = pdf_service.build_dynamic_cmap(d)
    d.close()
    assert "Ы" not in before

    out, added = kid.embed_missing_glyphs(raw, {"Ы", "Ю"})
    assert set(added) == {"Ы", "Ю"}

    d = fitz.open(stream=out, filetype="pdf")
    try:
        _, after = pdf_service.build_dynamic_cmap(d)
        assert "Ы" in after and "Ю" in after
        # старые символы не потеряны
        assert all(ch in after for ch in before)
        assert d.page_count == 101 and d.is_repaired is False
    finally:
        d.close()


def test_embed_is_noop_when_nothing_missing():
    raw = kid.load_template()
    out, added = kid.embed_missing_glyphs(raw, {"А", "Б", "1"})
    assert added == {}
    assert out is raw

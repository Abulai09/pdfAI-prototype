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


def test_substitute_fields_writes_all_five():
    out = kid.substitute_fields(kid.load_template(), NEW)
    text = _all_text(out)
    assert NEW.client_name in text
    assert "ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА" not in text
    assert text.count(NEW.account) == 13
    assert text.count(NEW.iin) == 17
    assert "01.02.2025 - 01.02.2026" in text
    assert "31.01.2026 09:15" in text


def test_substitute_fields_name_with_missing_glyph():
    """«Ы» нет в subset'е — имя всё равно обязано напечататься целиком."""
    fields = kid.KaspiIPFields(**{**NEW.__dict__, "client_name": "ИП САТЫБАЛДЫ ЮЛИЯ"})
    out = kid.substitute_fields(kid.load_template(), fields)
    assert "ИП САТЫБАЛДЫ ЮЛИЯ" in _all_text(out)


def test_substitute_fields_rejects_too_long_name():
    long_name = "ИП " + "О" * 200
    fields = kid.KaspiIPFields(**{**NEW.__dict__, "client_name": long_name})
    with pytest.raises(kid.SubstitutionError) as e:
        kid.substitute_fields(kid.load_template(), fields)
    assert "не помещается" in str(e.value)


# --- производные формы имени в теле документа --------------------------------
# Замер на шаблоне: 181 × «Нагима Турехановна А.», 61 × «Нагима А.» в колонке
# контрагента и 1 подпись отчёта. Все три — то же лицо, что и в шапке, поэтому
# подстановка только шапки оставляла новый ИИН рядом со старым именем.

OLD_IN_ROWS = "Нагима Турехановна А."
OLD_SHORT = "Нагима А."
OLD_SIGNATURE = "Аблаева Нагима Турехановна"


def test_template_has_the_measured_derived_forms():
    """Оракул правила вывода: формы шаблона = формы, выведенные из его шапки.

    Если однажды подложат шаблон другого владельца, этот тест покраснеет
    первым и покажет, что правило надо перепроверять, а не молча заменит
    ноль вхождений.
    """
    text = _all_text(kid.load_template())
    forms = kid.derive_name_forms("ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА")
    assert (forms.in_rows, forms.short, forms.signature) == \
           (OLD_IN_ROWS, OLD_SHORT, OLD_SIGNATURE)
    assert text.count(OLD_IN_ROWS) == 181
    assert text.count(OLD_SHORT) == 61
    assert text.count(OLD_SIGNATURE) == 1


def test_derived_names_replaced_in_transaction_rows():
    out = kid.substitute_fields(kid.load_template(), NEW)
    text = _all_text(out)
    forms = kid.derive_name_forms(NEW.client_name)
    # у «ИП ТЕСТОВ ТЕСТ» отчества нет, поэтому обе формы совпадают
    assert forms.in_rows == forms.short == "Тест Т."
    assert text.count("Тест Т.") == 181 + 61
    assert OLD_IN_ROWS not in text
    assert OLD_SHORT not in text


def test_signature_gets_new_name_and_period_end():
    """Дата в подписи привязана к концу периода: отчёт не может быть
    сформирован через полгода после периода, за который он выдан."""
    out = kid.substitute_fields(kid.load_template(), NEW)
    text = _all_text(out)
    assert "Отчет сформирован пользователем Тестов Тест 01.02.2026 13:45" in text
    assert OLD_SIGNATURE not in text


def test_signature_date_change_does_not_touch_operation_dates():
    """«18.07.2026» стоит и в подписи, и в датах операций.

    Слепая замена этой подстроки испортила бы таблицу, поэтому меняется
    составная строка «имя + дата», уникальная по построению.
    """
    before = _all_text(kid.load_template())
    after = _all_text(kid.substitute_fields(kid.load_template(), NEW))
    op = lambda t: sorted(re.findall(r"\b\d{2}\.\d{2}\.20\d{2}\b", t))
    expected = (
        before.replace(OLD_PERIOD, "01.02.2025 - 01.02.2026")
              .replace(OLD_MOVED, "31.01.2026 09:15")
              .replace(f"{OLD_SIGNATURE} 18.07.2026", "Тестов Тест 01.02.2026")
    )
    assert op(after) == op(expected)


def test_derived_substitution_keeps_amounts():
    before = _all_text(kid.load_template())
    after = _all_text(kid.substitute_fields(kid.load_template(), NEW))
    money = lambda t: sorted(re.findall(r"\d[\d  ]*,\d{2}", t))
    assert money(after) == money(before)


def test_rejects_derived_name_too_wide_for_the_row_cell():
    """Колонка контрагента отведена под 227.6 pt — отказ, а не наезд на ИИК."""
    fields = kid.KaspiIPFields(
        **{**NEW.__dict__, "client_name": "ИП ТЕСТОВ " + "О" * 30 + " " + "О" * 30}
    )
    with pytest.raises(kid.SubstitutionError) as e:
        kid.substitute_fields(kid.load_template(), fields)
    assert "строк" in str(e.value)


def test_wrapped_counterparty_cells_are_a_known_gap():
    """Три ячейки контрагента обёрнуты на три строки и НЕ заменяются.

    Отложено сознательно (решение пользователя 2026-08-09): перевёрстка
    узкой многострочной ячейки — тот же класс, что отложенная «сумма в
    назначении платежа». Тест фиксирует границу, чтобы её снятие было
    осознанным изменением, а не случайностью.
    """
    text = _all_text(kid.substitute_fields(kid.load_template(), NEW))
    assert text.count("ИП АБЛАЕВА НАГИМА") == 3


def test_substitute_fields_keeps_font_set():
    before = fitz.open(stream=kid.load_template(), filetype="pdf")
    after = fitz.open(stream=kid.substitute_fields(kid.load_template(), NEW), filetype="pdf")
    try:
        assert {(f[3], f[4]) for f in after[0].get_fonts(full=True)} == \
               {(f[3], f[4]) for f in before[0].get_fonts(full=True)}
    finally:
        before.close(); after.close()

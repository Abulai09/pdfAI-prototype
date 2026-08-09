# -*- coding: utf-8 -*-
"""Юнит-тесты модели реквизитов Kaspi ИП: загрузка шаблона, дефолты, валидация.

Фикстур не требует — работает в любом чекауте.
"""
from __future__ import annotations

import pytest

import kaspi_ip_data_service as kid


def test_template_path_uses_env_override(monkeypatch, tmp_path):
    """Путь к шаблону обязан переопределяться переменной окружения —
    файл лежит вне git, и у разных машин он в разных местах."""
    custom = tmp_path / "мой_шаблон.pdf"
    monkeypatch.setenv("PDFAI_KASPI_IP_TEMPLATE", str(custom))
    assert kid.template_path() == custom


def test_template_path_default_is_templates_dir(monkeypatch):
    monkeypatch.delenv("PDFAI_KASPI_IP_TEMPLATE", raising=False)
    assert kid.template_path().name == "kaspi_ip.pdf"
    assert kid.template_path().parent.name == "templates"


def test_load_template_missing_raises_clear_error(monkeypatch, tmp_path):
    """Отсутствие шаблона — понятная ошибка с путём, а не голый
    FileNotFoundError из недр."""
    monkeypatch.setenv("PDFAI_KASPI_IP_TEMPLATE", str(tmp_path / "нет.pdf"))
    with pytest.raises(kid.TemplateNotFoundError) as e:
        kid.load_template()
    assert "нет.pdf" in str(e.value)


import datetime


def test_default_fields_period_is_last_year_to_today():
    """Период по умолчанию — сегодня минус год … сегодня."""
    f = kid.default_fields()
    today = datetime.date.today()
    assert f.period_to == today.strftime("%d.%m.%Y")
    assert f.period_from == (today - datetime.timedelta(days=365)).strftime("%d.%m.%Y")
    assert f.last_movement.startswith(today.strftime("%d.%m.%Y"))
    # Реквизиты не подставляются из шаблона — иначе в форме светились бы
    # чужие настоящие данные.
    assert f.account == "" and f.iin == "" and f.client_name == ""


def test_period_text_has_fixed_length_23():
    f = kid.KaspiIPFields(account="KZ45722S000034195994", period_from="01.01.2025",
                          period_to="31.12.2025", last_movement="31.12.2025 10:00",
                          iin="810503400268", client_name="ИП ТЕСТОВ ТЕСТ")
    assert kid.period_text(f) == "01.01.2025 - 31.12.2025"
    assert len(kid.period_text(f)) == 23


def _ok_fields(**kw):
    base = dict(account="KZ45722S000034195994", period_from="01.01.2025",
                period_to="31.12.2025", last_movement="31.12.2025 10:00",
                iin="810503400268", client_name="ИП ТЕСТОВ ТЕСТ")
    base.update(kw)
    return kid.KaspiIPFields(**base)


def test_validate_accepts_correct_fields():
    assert kid.validate_fields(_ok_fields()) == []


@pytest.mark.parametrize("bad", ["KZ4572", "kz45722s000034195994", "QQ45722S000034195994",
                                 "KZ45722S000034195994X"])
def test_validate_rejects_bad_account(bad):
    errs = kid.validate_fields(_ok_fields(account=bad))
    assert any("Лицевой счёт" in e for e in errs)


@pytest.mark.parametrize("bad", ["81050340026", "8105034002689", "81050340026a", ""])
def test_validate_rejects_bad_iin(bad):
    errs = kid.validate_fields(_ok_fields(iin=bad))
    assert any("ИИН/БИН" in e for e in errs)


def test_validate_rejects_reversed_period():
    errs = kid.validate_fields(_ok_fields(period_from="31.12.2025", period_to="01.01.2025"))
    assert any("Период" in e for e in errs)


def test_validate_rejects_bad_last_movement():
    errs = kid.validate_fields(_ok_fields(last_movement="31.12.2025"))
    assert any("Дата последнего движения" in e for e in errs)


def test_validate_rejects_empty_name():
    errs = kid.validate_fields(_ok_fields(client_name="   "))
    assert any("Наименование" in e for e in errs)


def test_validate_rejects_unsupported_character():
    """В шрифте нет иероглифов — отказываем на вводе, а не молча рисуем пусто."""
    errs = kid.validate_fields(_ok_fields(client_name="ИП 東京"))
    assert any("недоступн" in e.lower() for e in errs)


# --- производные формы имени -------------------------------------------------
# В шаблоне имя клиента напечатано ЧЕТЫРЬМЯ разными способами, и подстановка
# только полной формы из шапки оставляет остальные три от прежнего владельца
# счёта. Правило вывода читается из самого шаблона: применённое к его же
# наименованию, оно обязано дать ровно те строки, что там напечатаны.


def test_derive_name_forms_from_full_fio():
    """Правило обязано воспроизвести формы САМОГО шаблона — это его оракул."""
    forms = kid.derive_name_forms("ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА")
    assert forms.in_rows == "Нагима Турехановна А."
    assert forms.short == "Нагима А."
    assert forms.signature == "Аблаева Нагима Турехановна"


def test_derive_name_forms_without_patronymic():
    forms = kid.derive_name_forms("ИП ТЕСТОВ ТЕСТ")
    assert forms.in_rows == "Тест Т."
    assert forms.short == "Тест Т."
    assert forms.signature == "Тестов Тест"


def test_derive_name_forms_single_word_uses_it_everywhere():
    """Разбирать нечего — все формы совпадают с самим наименованием."""
    forms = kid.derive_name_forms("ТОО КАСПИЙ")
    assert forms.in_rows == forms.short == forms.signature == "Каспий"


def test_derive_name_forms_strips_only_known_legal_prefix():
    """«ИП» — правовая форма, а не фамилия; «Каспий» в начале — фамилия."""
    assert kid.derive_name_forms("ИП ПЕТРОВ ПЁТР").signature == "Петров Пётр"
    assert kid.derive_name_forms("КАСПИЙ ПЁТР").signature == "Каспий Пётр"

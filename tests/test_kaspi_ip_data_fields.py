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

# -*- coding: utf-8 -*-
"""Подстановка реквизитов клиента во встроенный шаблон выписки Kaspi ИП.

В отличие от `kaspi_ip_pdf_service`, который пересчитывает суммы, этот модуль
не трогает ни одной цифры таблицы: он меняет ровно пять текстовых полей шапки
(лицевой счёт, период, дату последнего движения, ИИН/БИН, наименование
клиента) и те же счёт с ИИН внутри «Назначения платежа».

Дизайн: docs/superpowers/specs/2026-08-07-kaspi-ip-data-substitution-design.md
"""

from __future__ import annotations

import os
from pathlib import Path

# Шаблон — настоящая выписка, поэтому в git он НЕ лежит (см. .gitignore).
# Путь переопределяется переменной окружения, как PDFAI_DB_PATH/PDFAI_STATIC_DIR.
_DEFAULT_TEMPLATE = Path(__file__).resolve().parent / "templates" / "kaspi_ip.pdf"


class TemplateNotFoundError(RuntimeError):
    """Шаблон не найден по ожидаемому пути."""


def template_path() -> Path:
    env = os.environ.get("PDFAI_KASPI_IP_TEMPLATE")
    return Path(env) if env else _DEFAULT_TEMPLATE


def load_template() -> bytes:
    path = template_path()
    if not path.exists():
        raise TemplateNotFoundError(
            f"Шаблон выписки Kaspi ИП не найден: {path}. Положите файл туда "
            f"или укажите путь в переменной окружения PDFAI_KASPI_IP_TEMPLATE."
        )
    return path.read_bytes()

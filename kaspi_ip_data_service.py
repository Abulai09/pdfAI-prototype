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


import datetime
import re
from dataclasses import dataclass

# Набор символов, которые модуль умеет напечатать. Ограничен не шрифтом
# (в arial.ttf есть всё перечисленное), а тем, для чего заморожены контуры
# в kaspi_ip_glyphs.py — см. tests/scripts/extract_arial_glyphs.py.
ALLOWED_CHARS = frozenset(
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "ӘҒҚҢӨҰҮҺІ"
    "әғқңөұүһі"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " .,-/\"'()№"
)

_ACCOUNT_RE = re.compile(r"^KZ\d{2}[0-9A-Z]{16}$")
_IIN_RE = re.compile(r"^\d{12}$")
_DATE_FMT = "%d.%m.%Y"
_DATETIME_FMT = "%d.%m.%Y %H:%M"


@dataclass
class KaspiIPFields:
    account: str
    period_from: str
    period_to: str
    last_movement: str
    iin: str
    client_name: str


def default_fields() -> KaspiIPFields:
    """Значения по умолчанию. Реквизиты пустые: подставлять их из шаблона
    значило бы показывать пользователю чужие настоящие данные."""
    now = datetime.datetime.now()
    today = now.date()
    return KaspiIPFields(
        account="",
        period_from=(today - datetime.timedelta(days=365)).strftime(_DATE_FMT),
        period_to=today.strftime(_DATE_FMT),
        last_movement=now.strftime(_DATETIME_FMT),
        iin="",
        client_name="",
    )


def period_text(fields: KaspiIPFields) -> str:
    """Ровно та форма, какой период напечатан в шаблоне: 23 символа."""
    return f"{fields.period_from} - {fields.period_to}"


def _parse(value: str, fmt: str):
    try:
        return datetime.datetime.strptime(value, fmt)
    except ValueError:
        return None


def validate_fields(fields: KaspiIPFields) -> list[str]:
    """Список человекочитаемых ошибок; пустой список — всё в порядке."""
    errors: list[str] = []

    if not _ACCOUNT_RE.match(fields.account or ""):
        errors.append(
            "Лицевой счёт: ожидается 20 символов вида KZ + 2 цифры + "
            "16 цифр или заглавных латинских букв"
        )
    if not _IIN_RE.match(fields.iin or ""):
        errors.append("ИИН/БИН: ожидается ровно 12 цифр")

    d_from = _parse(fields.period_from or "", _DATE_FMT)
    d_to = _parse(fields.period_to or "", _DATE_FMT)
    if d_from is None or d_to is None:
        errors.append("Период: обе даты в формате ДД.ММ.ГГГГ")
    elif d_from > d_to:
        errors.append("Период: начало периода позже его конца")

    if _parse(fields.last_movement or "", _DATETIME_FMT) is None:
        errors.append("Дата последнего движения: формат ДД.ММ.ГГГГ ЧЧ:ММ")

    name = (fields.client_name or "").strip()
    if not name:
        errors.append("Наименование клиента: поле не может быть пустым")
    else:
        bad = sorted({c for c in name if c not in ALLOWED_CHARS})
        if bad:
            errors.append(
                "Наименование клиента: недоступные для шрифта символы — "
                + " ".join(repr(c) for c in bad)
            )
    return errors

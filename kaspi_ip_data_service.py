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


import zlib
from typing import Callable, Dict, List

import fitz

from pdf_service import build_dynamic_cmap, _rebuild_xref_table

# Токен показа текста ровно в той форме, в какой его пишет генератор шаблона
# (замер: 215 таких токенов на стр. 0, ноль TJ-массивов, ноль '/"). `Tf`
# НЕОБЯЗАТЕЛЕН: строка, перенесённая внутри многострочной ячейки (например
# «ИП АБЛАЕВА НАГИМА» / «ТУРЕХАНОВНА БИН/ИИН»), пишет второй Tm БЕЗ повторного
# «/Fx N Tf» — шрифт и кегль наследуются от предыдущего Tj той же ячейки.
_TOKEN_RE = re.compile(
    rb"1 0 0 1 ([\d.\-]+) ([\d.\-]+) Tm\s*(?:/F(\d+) ([\d.]+) Tf\s*)?\("
    rb"((?:\\.|[^\\)])*)\)\s*Tj",
    re.S,
)


class SubstitutionError(RuntimeError):
    """Подстановка невозможна — поле не найдено, текст не влезает и т. п."""


def _unescape_pdf_string(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i:i + 1] == b"\\" and i + 1 < len(data):
            out.append(data[i + 1])
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _escape_pdf_string(data: bytes) -> bytes:
    """Экранирование строки PDF: скобки и обратный слэш.

    Обязательно, а не косметика: у буквы «в» CID `025C`, младший байт
    которого 0x5C — сам обратный слэш. Без экранирования байт-последовательность
    CID «в» ищется и пишется в потоке НЕ в том виде, в каком реальный
    генератор её туда положил, — искомый токен просто не находится.
    """
    out = bytearray()
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out.append(0x5C)
        out.append(b)
    return bytes(out)


def _decode_cid(data: bytes, to_unicode: Dict[str, str]) -> str:
    return "".join(
        to_unicode.get(f"{data[i]:02X}{data[i + 1]:02X}".upper(), "?")
        for i in range(0, len(data) - 1, 2)
    )


def _encode_cid(text: str, from_unicode: Dict[str, str]) -> bytes:
    """Текст → CID-байты. Отсутствие символа в карте — отказ, а не '?'."""
    out = bytearray()
    for ch in text:
        cid = from_unicode.get(ch)
        if cid is None:
            raise SubstitutionError(
                f"символа {ch!r} нет в карте шрифта — сперва вшейте его глиф"
            )
        out += bytes.fromhex(cid)
    return bytes(out)


def _page_tokens(doc, pno: int) -> List[dict]:
    """Все токены показа текста страницы: координаты, смещения, текст."""
    to_unicode, _ = build_dynamic_cmap(doc)
    data = doc[pno].read_contents()
    tokens = []
    for m in _TOKEN_RE.finditer(data):
        body = _unescape_pdf_string(m.group(5))
        tokens.append({
            "x": float(m.group(1)),
            "y": float(m.group(2)),
            "text": _decode_cid(body, to_unicode),
            "start": m.start(5),
            "end": m.end(5),
        })
    return tokens


def _find_value_token(tokens: List[dict], label: str) -> dict:
    """Токен-значение — тот, что стоит правее метки на той же строке."""
    label_tokens = [t for t in tokens if t["text"].strip() == label]
    if len(label_tokens) != 1:
        raise SubstitutionError(
            f"метка {label!r} найдена {len(label_tokens)} раз(а), ожидалась ровно одна"
        )
    y = label_tokens[0]["y"]
    x = label_tokens[0]["x"]
    same_row = [t for t in tokens if abs(t["y"] - y) < 0.5 and t["x"] > x]
    if len(same_row) != 1:
        raise SubstitutionError(
            f"справа от метки {label!r} найдено {len(same_row)} токен(ов), ожидался один"
        )
    return same_row[0]


# Метки полей шапки. Значение поля — токен на ТОЙ ЖЕ строке (Y), но правее.
# Ищем по метке, а не по зашитому Y: так конвенция читается из самого файла.
_LABEL_ACCOUNT = "Лицевой счет:"
_LABEL_PERIOD = "Период:"
_LABEL_LAST_MOVEMENT = "Дата последнего движения:"
_LABEL_IIN = "ИИН/БИН:"
_LABEL_CLIENT = "Наименование клиента:"


def _replace_stream(raw: bytearray, xref: int, new_body: bytes,
                    compress: Callable[[bytes], bytes]) -> None:
    """Заменяет содержимое потока объекта `xref` в сырых байтах на месте.

    Поток нарезается по объявленному /Length, а не по поиску endstream:
    пересжатый вывод может случайно оканчиваться на 0x0D, и догадка о
    разделителе съела бы реальный байт (та же ошибка уже ловилась в
    проверке целостности стримов обоих валидаторов).
    """
    pos = raw.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise SubstitutionError(f"объект {xref} не найден в байтах документа")
    kw = raw.find(b"stream", pos)
    start = kw + len(b"stream")
    if raw[start:start + 2] == b"\r\n":
        start += 2
    elif raw[start:start + 1] == b"\n":
        start += 1
    header = bytes(raw[pos:kw])
    lm = re.search(rb"/Length\s+(\d+)", header)
    if not lm:
        raise SubstitutionError(f"у объекта {xref} нет /Length")
    old_len = int(lm.group(1))
    end = raw.find(b"endstream", start)
    new_comp = compress(new_body)
    new_header = re.sub(rb"/Length\s+\d+", f"/Length {len(new_comp)}".encode(), header)
    sep = bytes(raw[kw + len(b"stream"):start])
    tail = bytes(raw[start + old_len:end])
    raw[pos:end] = new_header + b"stream" + sep + new_comp + tail


def substitute_fixed_length(pdf_bytes: bytes, fields: KaspiIPFields) -> bytes:
    """Меняет четыре поля фиксированной длины во всём документе.

    Лицевой счёт (20 символов), ИИН/БИН (12 цифр), период
    (23 символа) и дата последнего движения (16 символов) имеют длину,
    заданную самим форматом, поэтому это замена РОВНО ТОЙ ЖЕ длины: ни одна
    координата не пересчитывается, переносы строк внутри «Назначения платежа»
    не трогаются. Замер на шаблоне: счёт встречается 13 раз, ИИН — 17 раз,
    на 15 страницах, в том числе внутри более длинных токенов.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        _, from_unicode = build_dynamic_cmap(doc)
        tokens = _page_tokens(doc, 0)
        old_account = _find_value_token(tokens, _LABEL_ACCOUNT)["text"]
        old_iin = _find_value_token(tokens, _LABEL_IIN)["text"]
        old_period = _find_value_token(tokens, _LABEL_PERIOD)["text"]
        old_moved = _find_value_token(tokens, _LABEL_LAST_MOVEMENT)["text"]

        pairs = [
            (old_account, fields.account),
            (old_iin, fields.iin),
            (old_period, period_text(fields)),
            (old_moved, fields.last_movement),
        ]
        for old, new in pairs:
            if len(old) != len(new):
                raise SubstitutionError(
                    f"длина значения изменилась: {old!r} ({len(old)}) → "
                    f"{new!r} ({len(new)}); подстановка этой длины не выполняется "
                    f"без пересчёта вёрстки"
                )

        replacements = [
            (_escape_pdf_string(_encode_cid(old, from_unicode)),
             _escape_pdf_string(_encode_cid(new, from_unicode)))
            for old, new in pairs
        ]
        streams = {}
        for pno in range(doc.page_count):
            for xref in doc[pno].get_contents():
                if xref not in streams:
                    streams[xref] = doc.xref_stream(xref)
    finally:
        doc.close()

    raw = bytearray(pdf_bytes)
    for xref, body in streams.items():
        new_body = body
        for old_cid, new_cid in replacements:
            new_body = new_body.replace(old_cid, new_cid)
        if new_body != body:
            _replace_stream(raw, xref, new_body, zlib.compress)
    return _rebuild_xref_table(bytes(raw))

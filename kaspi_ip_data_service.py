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


# Правовые формы, которые в этом поле стоят ПЕРЕД фамилией и частью имени не
# являются. Список закрытый: снимать любое первое слово нельзя, иначе у
# «КАСПИЙ ПЁТР» фамилия потеряется.
_LEGAL_PREFIXES = ("ИП", "ТОО", "АО", "КХ", "ПК", "ЧП")


@dataclass(frozen=True)
class NameForms:
    """Четыре написания имени клиента, встречающиеся в шаблоне.

    Замер на шаблоне (`ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА`): полная форма стоит в
    шапке 1 раз, `Нагима Турехановна А.` — 181 раз и `Нагима А.` — 61 раз в
    колонке контрагента, `Аблаева Нагима Турехановна` — 1 раз в подписи
    отчёта. Заменять только шапку значит оставить 243 вхождения имени
    прежнего владельца счёта рядом с уже подставленным ИИН.
    """
    full: str
    in_rows: str
    short: str
    signature: str


def derive_name_forms(client_name: str) -> NameForms:
    """Выводит производные написания имени из наименования клиента.

    Правило снято с самого шаблона и им же проверяется: применённое к его
    наименованию, оно обязано дать те строки, что в нём напечатаны (см.
    `test_derive_name_forms_from_full_fio`). Разбор позиционный —
    «Фамилия Имя Отчество» после правовой формы, — поэтому на наименовании,
    которое не является ФИО («ТОО ЩЕРБАКОВ И ПАРТНЁРЫ»), он даст осмысленную
    по построению, но бессмысленную по сути строку. Это принятое ограничение
    (решение пользователя 2026-08-09): формат — выписка ИП, где поле почти
    всегда ФИО, а альтернатива требовала бы двух дополнительных полей ввода.
    """
    full = " ".join((client_name or "").split())
    words = full.split()
    if words and words[0].upper() in _LEGAL_PREFIXES:
        words = words[1:]

    if len(words) < 2:
        one = words[0].capitalize() if words else full
        return NameForms(full=full, in_rows=one, short=one, signature=one)

    surname, given = words[0], words[1]
    patronymic = words[2] if len(words) > 2 else ""
    initial = f"{surname[0].upper()}."
    short = f"{given.capitalize()} {initial}"
    in_rows = f"{given.capitalize()} {patronymic.capitalize()} {initial}" if patronymic else short
    return NameForms(
        full=full,
        in_rows=in_rows,
        short=short,
        signature=" ".join(w.capitalize() for w in words),
    )


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
from typing import Callable, Dict, List, Optional, Tuple

import fitz

from pdf_service import (
    build_dynamic_cmap,
    _rebuild_xref_table,
    _patch_truetype_glyphs,
    _read_truetype_glyph,
    _w_array_insert_sorted,
    _cmap_add_mappings,
)
import kaspi_ip_glyphs

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
        tokens = _page_tokens(doc, 0)
        pairs = [
            (_find_value_token(tokens, _LABEL_ACCOUNT)["text"], fields.account),
            (_find_value_token(tokens, _LABEL_IIN)["text"], fields.iin),
            (_find_value_token(tokens, _LABEL_PERIOD)["text"], period_text(fields)),
            (_find_value_token(tokens, _LABEL_LAST_MOVEMENT)["text"],
             fields.last_movement),
        ]
    finally:
        doc.close()

    for old, new in pairs:
        if len(old) != len(new):
            raise SubstitutionError(
                f"длина значения изменилась: {old!r} ({len(old)}) → "
                f"{new!r} ({len(new)}); подстановка этой длины не выполняется "
                f"без пересчёта вёрстки"
            )
    return _replace_cid_strings(pdf_bytes, pairs)


def _font_objects(doc) -> tuple:
    """(xref FontFile2, xref CIDFont-словаря, xref ToUnicode) основного шрифта."""
    ff2 = cid_obj = tu = None
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref, compressed=True) or ""
        if "/CIDFontType2" in obj:
            cid_obj = xref
            m = re.search(r"/FontFile2\s+(\d+)\s+0\s+R", obj)
            if not m:
                dm = re.search(r"/FontDescriptor\s+(\d+)\s+0\s+R", obj)
                if dm:
                    m = re.search(r"/FontFile2\s+(\d+)\s+0\s+R",
                                  doc.xref_object(int(dm.group(1)), compressed=True) or "")
            if m:
                ff2 = int(m.group(1))
        if "/Type0" in obj:
            m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            if m:
                tu = int(m.group(1))
    if ff2 is None or cid_obj is None or tu is None:
        raise SubstitutionError("не найдены объекты шрифта (FontFile2 / CIDFont / ToUnicode)")
    return ff2, cid_obj, tu


def _glyph_matches_reference(subset_glyph: bytes, reference: bytes) -> bool:
    """Сравнение глифа документа с замороженным эталоном — С ДОПУСКОМ на
    паддинг-байт чётности.

    Найдено при проверке Task 5 на реальном шаблоне: `kaspi_ip_glyphs.GLYPHS`
    получен через `fontTools`-компиляцию (`glyf.compile()`), которая паддинг
    НЕ добавляет, а `_read_truetype_glyph` читает СЫРЫЕ байты `glyf`-таблицы
    документа, где паддинг-байт для выравнивания на чётную длину МОЖЕТ
    присутствовать — это прямо задокументировано в docstring
    `_read_truetype_glyph`. Строгое сравнение (`==`) на настоящем шаблоне даёт
    48 ложных расхождений из 125 присутствующих символов — gate был бы
    сломан на собственном же эталонном файле. Тот же допуск («1 паддинг-байт,
    хвост нулевой») уже применяется в Halyk (`_try_patch_bold_digit_glyphs`).
    """
    if subset_glyph == reference:
        return True
    if len(subset_glyph) == len(reference) + 1 and subset_glyph[:-1] == reference \
            and subset_glyph[-1] == 0:
        return True
    return False


def _trusted_glyph_source(font_bytes: bytes, from_unicode: Dict[str, str]) -> bool:
    """Gate: доверять замороженным контурам можно, только если КАЖДЫЙ уже
    присутствующий в subset'е символ побайтово совпал с эталоном (с допуском
    на паддинг — см. `_glyph_matches_reference`).

    Тот же принцип, что у `_try_patch_bold_digit_glyphs` в Halyk: сначала
    проверь, потом доверяй. Замер на шаблоне: 125 присутствующих символов из
    125 совпали. Файл, собранный из другого мастер-шрифта, обязан получить
    отказ, а не чужие контуры.
    """
    for ch, cid in from_unicode.items():
        gid = int(cid, 16)
        expected = kaspi_ip_glyphs.GLYPHS.get(gid)
        if expected is None:
            continue
        if not _glyph_matches_reference(_read_truetype_glyph(font_bytes, gid), expected):
            return False
    return True


def _composite_components(glyph_data: bytes) -> List[int]:
    """GID компонентов составного глифа. Простой глиф → пустой список."""
    if len(glyph_data) < 10:
        return []
    num_contours = int.from_bytes(glyph_data[0:2], "big", signed=True)
    if num_contours >= 0:
        return []
    out = []
    pos = 10
    while pos + 4 <= len(glyph_data):
        flags = int.from_bytes(glyph_data[pos:pos + 2], "big")
        out.append(int.from_bytes(glyph_data[pos + 2:pos + 4], "big"))
        pos += 4
        pos += 4 if flags & 0x0001 else 2       # ARG_1_AND_2_ARE_WORDS
        if flags & 0x0008:                       # WE_HAVE_A_SCALE
            pos += 2
        elif flags & 0x0040:                     # X_AND_Y_SCALE
            pos += 4
        elif flags & 0x0080:                     # TWO_BY_TWO
            pos += 8
        if not flags & 0x0020:                   # MORE_COMPONENTS
            break
    return out


def _find_w_array_bounds(cidobj_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Границы (bracket, close) вложенного /W-массива в СЫРЫХ байтах объекта
    CIDFont (включая заголовок «N 0 obj» — сама функция от заголовка не
    зависит, ей нужен только сам массив).

    /W — ВЛОЖЕННЫЙ массив (`[3[277]5[354]…]`), поэтому конец ищем счётчиком
    вложенности, а не первой попавшейся «]» (та закрывает первую же запись
    вида `[277]`) — тот же приём, что в `halyk_pdf_service.
    _prune_bold_orphan_glyphs`.
    """
    m = re.search(rb"/W\s*\[", cidobj_bytes)
    if not m:
        return None
    bracket = m.end() - 1
    depth = 0
    for i in range(bracket, len(cidobj_bytes)):
        ch = cidobj_bytes[i:i + 1]
        if ch == b"[":
            depth += 1
        elif ch == b"]":
            depth -= 1
            if depth == 0:
                return bracket, i
    return None


def _insert_widths(cidobj_bytes: bytes, chars: List[str]) -> Optional[bytes]:
    """Дописывает ширины новых CID в /W тем же стилем, каким массив написан.

    Работает на СЫРЫХ байтах объекта (как их вернул `raw.find(...)`), а не на
    тексте `doc.xref_object()` — тот переформатирует объект (переставляет
    ключи, меняет пробелы вокруг `/W`; замерено на шаблоне: `/W [3[277]…`
    → `/W[3[277]…` без пробела, плюс лишние `/Type/Font/Subtype/…` спереди).
    Запись переформатированного текста обратно в файл была бы ровно тем же
    классом признака, что и критерий 4 «стиль сериализации операторов».
    """
    bounds = _find_w_array_bounds(cidobj_bytes)
    if bounds is None:
        return None
    bracket, close = bounds
    entries = {
        f"{kaspi_ip_glyphs.CHAR_GID[c]:04X}": kaspi_ip_glyphs.WIDTHS_1000[
            kaspi_ip_glyphs.CHAR_GID[c]
        ]
        for c in chars
    }
    return _w_array_insert_sorted(cidobj_bytes, bracket, close, entries)


def _raw_object_bounds(raw: bytes, xref: int) -> Tuple[int, int]:
    """(начало «N 0 obj», начало «endobj») объекта `xref` в сырых байтах."""
    pos = raw.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise SubstitutionError(f"объект {xref} не найден в байтах документа")
    end = raw.find(b"endobj", pos)
    if end < 0:
        raise SubstitutionError(f"у объекта {xref} нет endobj")
    return pos, end


def embed_missing_glyphs(pdf_bytes: bytes, chars: set) -> tuple:
    """Вшивает глифы символов, которых нет в subset'е документа.

    Возвращает (новые байты, {символ: CID-hex}). Если вшивать нечего —
    возвращает вход БЕЗ ИЗМЕНЕНИЙ и пустую карту.

    Составные глифы (их 27 в наборе: А→A, Ё→E+dieresis, Й→uni0418+breve,
    Э→uni0404 …) вшиваются ВМЕСТЕ с компонентами: составной глиф ссылается на
    другие GID, и без компонента он отрисуется пустым. Компоненты в /W и
    ToUnicode не попадают — они никогда не показываются как самостоятельный
    CID, а ширина им не нужна.

    Все три правки — glyf/loca, /W и ToUnicode — готовятся из ОДНОГО снимка
    байт и применяются либо все, либо ни одной.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        _, from_unicode = build_dynamic_cmap(doc)
        missing = sorted(c for c in chars if c not in from_unicode)
        if not missing:
            return pdf_bytes, {}
        ff2_xref, cid_xref, tu_xref = _font_objects(doc)
        font_bytes = doc.xref_stream(ff2_xref)
        tu_body = doc.xref_stream(tu_xref)
    finally:
        doc.close()

    # Объект CIDFont читается ИЗ СЫРЫХ БАЙТ, не через doc.xref_object() —
    # см. docstring _insert_widths про переформатирование.
    cid_start, cid_end = _raw_object_bounds(pdf_bytes, cid_xref)
    cid_obj = pdf_bytes[cid_start:cid_end]

    if not _trusted_glyph_source(font_bytes, from_unicode):
        raise SubstitutionError(
            "subset шрифта документа не совпал с эталонным Arial — "
            "вшивание глифов отменено"
        )

    unknown = [c for c in missing if c not in kaspi_ip_glyphs.CHAR_GID]
    if unknown:
        raise SubstitutionError(
            "нет замороженных контуров для символов: " + " ".join(repr(c) for c in unknown)
        )

    # GID к вшиванию: сами символы + рекурсивно компоненты составных глифов.
    want_gids = {kaspi_ip_glyphs.CHAR_GID[c] for c in missing}
    present_gids = {int(cid, 16) for cid in from_unicode.values()}
    patches: Dict[int, bytes] = {}
    stack = list(want_gids)
    while stack:
        gid = stack.pop()
        if gid in patches or gid in present_gids:
            continue
        data = kaspi_ip_glyphs.GLYPHS.get(gid)
        if data is None:
            raise SubstitutionError(f"нет замороженного контура для GID {gid}")
        patches[gid] = data
        for comp_gid in _composite_components(data):
            stack.append(comp_gid)

    new_font = _patch_truetype_glyphs(font_bytes, patches)
    added = {c: f"{kaspi_ip_glyphs.CHAR_GID[c]:04X}" for c in missing}

    new_cid_obj = _insert_widths(cid_obj, missing)
    new_tu = _cmap_add_mappings(
        tu_body, [(added[c], f"{ord(c):04X}") for c in missing]
    )
    if new_cid_obj is None or new_tu is None:
        raise SubstitutionError("не удалось обновить /W или ToUnicode — правка отменена")

    raw = bytearray(pdf_bytes)
    # FontFile2 физически стоит в файле РАНЬШЕ CIDFont (замер на шаблоне:
    # xref 219 на позиции 422602, xref 221 на 469206) — правка его длины
    # сдвигает всё, что идёт следом. _replace_stream от этого не страдает
    # сама (ищет объект заново через raw.find() при каждом вызове), но
    # cid_start/cid_end, снятые с ПЕРВОНАЧАЛЬНЫХ pdf_bytes, после такого
    # сдвига уже не те индексы в `raw` — поэтому границы CIDFont
    # пересчитываются заново, ПОСЛЕ обеих правок потоков, а не до них.
    _replace_stream(raw, ff2_xref, new_font, zlib.compress)
    _replace_stream(raw, tu_xref, new_tu, zlib.compress)
    cid_start, cid_end = _raw_object_bounds(bytes(raw), cid_xref)
    raw[cid_start:cid_end] = new_cid_obj
    return _rebuild_xref_table(bytes(raw)), added


from kaspi_ip_pdf_service import _primary_glyph_advances

# Замер на шаблоне: значение наименования занимает 148 pt, соседей на строке
# правее нет, до края отведённой области ещё ~482 pt. Поле лево-выровнено,
# поэтому X начала не пересчитывается — ширина нужна только чтобы отказать,
# если текст не помещается.
MAX_NAME_WIDTH_PT = 482.0


def _text_width_pt(text: str, advances: Dict[str, float], size: float) -> float:
    return sum(advances.get(ch, 0.5) for ch in text) * size


# Замер на шаблоне: колонка контрагента отведена под 227.6 pt при кегле 8, и
# это значение ОДИНАКОВО у всех 242 ячеек (min = median = max по замеру зазора
# до соседней ячейки ИИК) — то есть это фиксированная сетка таблицы, а не
# свойство конкретной строки. Ячейка лево-выровнена и соседей на строке не
# имеет, поэтому координата начала не пересчитывается; ширина нужна только
# чтобы отказать, а не нарисовать имя поверх ИИК.
MAX_ROW_NAME_WIDTH_PT = 227.6

_ROW_FONT_SIZE = 8.0

# Дата в подписи отчёта. Заменять её отдельной подстрокой НЕЛЬЗЯ: «18.07.2026»
# стоит и в датах операций, и слепая замена испортила бы таблицу. Поэтому
# меняется составная строка «подпись + дата», уникальная по построению.
_SIGNATURE_DATE_RE = r"\s+(\d{2}\.\d{2}\.\d{4})"


def _replace_cid_strings(pdf_bytes: bytes, pairs: List[Tuple[str, str]]) -> bytes:
    """Заменяет строки во ВСЕХ потоках содержимого, сравнивая CID-байты.

    Позиции не трогаются: правится только содержимое скобочной строки, а
    `Tm`/`Td` остаются прежними. Для лево-выровненных ячеек без соседей это
    и есть верное поведение — длина текста меняется, якорь нет.
    """
    if not pairs:
        return pdf_bytes
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        _, from_unicode = build_dynamic_cmap(doc)
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


def substitute_derived_names(pdf_bytes: bytes, fields: KaspiIPFields) -> bytes:
    """Меняет производные написания имени в строках таблицы и в подписи.

    Прежние формы не зашиты в код, а ВЫВОДЯТСЯ тем же правилом из наименования,
    которое стоит в шапке обрабатываемого документа. Поэтому если однажды
    подложат шаблон другого владельца, заменится его имя, а не чужое; а если
    правило перестанет воспроизводить напечатанные формы, они просто не
    найдутся, и функция ничего не испортит.

    Вызывается ДО замены шапки: именно оттуда читается прежнее наименование.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        old_name = _find_value_token(_page_tokens(doc, 0), _LABEL_CLIENT)["text"]
        _, from_unicode = build_dynamic_cmap(doc)
        advances = _primary_glyph_advances(doc, from_unicode)
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    finally:
        doc.close()

    old = derive_name_forms(old_name)
    new = derive_name_forms(fields.client_name)

    for form in (new.in_rows, new.short):
        width = _text_width_pt(form, advances, _ROW_FONT_SIZE)
        if width > MAX_ROW_NAME_WIDTH_PT:
            raise SubstitutionError(
                f"производная форма имени {form!r} не помещается в строку "
                f"таблицы: {width:.1f} pt при доступных "
                f"{MAX_ROW_NAME_WIDTH_PT:.0f} pt"
            )

    # Подпись идёт первой: она самая длинная и содержит формы покороче как
    # части, поэтому заменять её после них значило бы искать уже изменённое.
    pairs: List[Tuple[str, str]] = []
    m = re.search(re.escape(old.signature) + _SIGNATURE_DATE_RE, text)
    if m:
        pairs.append((m.group(0), f"{new.signature} {fields.period_to}"))
    for old_form, new_form in ((old.in_rows, new.in_rows), (old.short, new.short)):
        if old_form and old_form != new_form and old_form in text:
            pairs.append((old_form, new_form))
    return _replace_cid_strings(pdf_bytes, pairs)


def substitute_fields(pdf_bytes: bytes, fields: KaspiIPFields) -> bytes:
    """Подставляет все пять реквизитов. Публичная точка входа модуля.

    Порядок важен: глифы вшиваются ПЕРВЫМИ, иначе `_encode_cid` не найдёт CID
    для новых символов имени. Поля фиксированной длины идут вторыми — они не
    зависят от вшивания. Производные формы имени — третьими, пока в шапке ещё
    стоит ПРЕЖНЕЕ наименование, из которого выводятся заменяемые строки.
    Имя шапки пишется последним.
    """
    errors = validate_fields(fields)
    if errors:
        raise SubstitutionError("; ".join(errors))

    name = fields.client_name.strip()
    forms = derive_name_forms(name)
    needed = set(name) | set(forms.in_rows) | set(forms.short) | set(forms.signature)
    working, _added = embed_missing_glyphs(pdf_bytes, needed)
    working = substitute_fixed_length(working, fields)
    working = substitute_derived_names(working, fields)

    doc = fitz.open(stream=working, filetype="pdf")
    try:
        _, from_unicode = build_dynamic_cmap(doc)
        advances = _primary_glyph_advances(doc, from_unicode)
        tokens = _page_tokens(doc, 0)
        token = _find_value_token(tokens, _LABEL_CLIENT)
        # Смещения токенов посчитаны по read_contents(), который СКЛЕИВАЕТ все
        # потоки страницы. Писать мы будем в один поток, поэтому если их больше
        # одного — смещения не совпадут, и это отказ, а не тихая порча байтов.
        contents = doc[0].get_contents()
        if len(contents) != 1:
            raise SubstitutionError(
                f"у страницы 0 потоков содержимого: {len(contents)}, ожидался один"
            )
        content_xref = contents[0]
        body = doc.xref_stream(content_xref)
        size = 8.0
        width = _text_width_pt(name, advances, size)
        if width > MAX_NAME_WIDTH_PT:
            raise SubstitutionError(
                f"наименование клиента не помещается в поле: {width:.1f} pt "
                f"при доступных {MAX_NAME_WIDTH_PT:.0f} pt"
            )
        new_cid = _escape_pdf_string(_encode_cid(name, from_unicode))
        new_body = body[:token["start"]] + new_cid + body[token["end"]:]
    finally:
        doc.close()

    raw = bytearray(working)
    _replace_stream(raw, content_xref, new_body, zlib.compress)
    return _rebuild_xref_table(bytes(raw))

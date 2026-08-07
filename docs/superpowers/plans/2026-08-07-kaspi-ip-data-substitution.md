# Подстановка реквизитов в шаблон Kaspi ИП — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Новый режим приложения: пользователь вводит пять своих реквизитов, программа выдаёт выписку Kaspi ИП со встроенного шаблона, где эти реквизиты подставлены, а суммы, даты операций, шрифт и позиции не тронуты.

**Architecture:** Отдельный модуль `kaspi_ip_data_service.py` работает по готовым байтам шаблона: четыре поля из пяти имеют фиксированную длину по формату, поэтому заменяются побайтово без единого сдвига вёрстки; наименование клиента переписывается тем же механизмом `Tm/Tf/(CID)Tj`, что и остальной писатель Kaspi ИП. Недостающие глифы вшиваются в шрифт из замороженных контуров Arial, транзитивно вместе с компонентами составных глифов.

**Tech Stack:** Python 3.13, PyMuPDF (`fitz`), `zlib`, FastAPI, ванильный JS в `static/index.html`. `fontTools` — только для офлайн-генерации таблицы глифов, в рантайме не используется.

## Global Constraints

- Комментарии, докстринги и сообщения об ошибках — на русском, как во всём проекте.
- Линтера и форматтера в проекте НЕТ. Стиль подгоняется под окружающий файл вручную.
- Реальные PDF-выписки НИКОГДА не коммитятся. Шаблон живёт в `templates/`, папка в `.gitignore`.
- Сжатие потоков — `zlib.compress(data)`. Замерено: все 103 потока шаблона воспроизводятся `zlib.compress(данные, 6)` побайтово, то есть `iTextSharp.LGPLv2.Core` пишет ровно как python-zlib. `pdfsharp_deflate` здесь НЕ применять.
- Координаты пишутся через `pdf_service._fmt_coord`, разделители операторов — через `pdf_service._op_separators` (критерий 4 «стиль сериализации»).
- Нарезка потока по объявленному `/Length`, никогда по поиску `endstream` с догадкой о разделителе.
- Любая неуверенность (не найдено поле, не сошёлся gate, не влезает текст) — ОТКАЗ с понятным сообщением, а не «сделаем как получится».

## Замеренные факты о шаблоне

Все замеры сделаны на `C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP2.pdf`, 101 страница, `/Producer: iTextSharp.LGPLv2.Core 1.6.7.0`.

Текст показывается ИСКЛЮЧИТЕЛЬНО так (215 токенов на стр. 0, ноль `TJ`-массивов, ноль `'`/`"`):

```
BT
1 0 0 1 <X> <Y> Tm
/F1 8 Tf
(<CID-байты по 2 на символ>)Tj
ET
```

Поля шапки — по одному токену каждое, метки на `X=43`, значения на `X=211`:

| Y | Метка (X=43) | Значение (X=211) | Символов |
| --- | --- | --- | --- |
| 554 | `Лицевой счет:` | `KZ45722S000034195994` | 20 |
| 540 | `Валюта счета:` | `KZT` | не трогаем |
| 526 | `Период:` | `18.07.2025 - 18.07.2026` | 23 |
| 512 | `Дата последнего движения:` | `17.07.2026 23:03` | 16 |
| 498 | `ИИН/БИН:` | `810503400268` | 12 |
| 484 | `Наименование клиента:` | `ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА` | 29 |

Вне шапки CID-последовательность лицевого счёта встречается **13 раз**, ИИН — **17 раз**, на 15 страницах, внутри более длинных токенов вида `KZ45722S000034195994 ИИН/БИН`.

Шрифт один на весь документ: `OJSMTG+ArialMT`, ресурс `F1`, `/CIDToGIDMap /Identity`, `FontFile2` в xref 219, CIDFont-словарь в xref 221, `ToUnicode` в xref 222. Subset — 133 символа, и **все 133 совпали байт-в-байт** с `C:\Windows\Fonts\arial.ttf` при сверке по GID.

Нет в subset'е: заглавных `Щ`, `Ъ`, `Ы`, `Ю`, латинской `W`, всех казахских `Ә Ғ Қ Ң Ө Ұ Ү Һ І`, латинских строчных `f j w x`.

**27 символов из 156 в Arial — составные глифы:** `А`→`A`, `В`→`B`, `Е`→`E`, `Ё`→`E`+`dieresis`, `Й`→`uni0418`+`breve`, `М`→`M`, `Н`→`H`, `О`→`O`, `Р`→`P`, `С`→`C`, `Т`→`T`, `Х`→`X`, `Э`→`uni0404`, `Я`→`R`, `а`→`a`, `е`→`e`, `ё`→`e`+`dieresis`, `й`→`uni0438`+`breve`, `о`→`o`, `р`→`p`, `с`→`c`, `у`→`y`, `х`→`x`, `э`→`uni0454`, `І`→`I`, `һ`→`h`, `і`→`i`. Составной глиф ссылается на другие GID, поэтому вшивать его нужно ВМЕСТЕ с компонентами, иначе он отрисуется пустым.

## Структура файлов

| Файл | Ответственность |
| --- | --- |
| `kaspi_ip_data_service.py` (новый) | Модель полей, валидация, загрузка шаблона, подстановка реквизитов. Точка входа `substitute_fields`. |
| `kaspi_ip_glyphs.py` (новый) | Замороженные байты глифов Arial + их GID и ширины. Данные, без логики. |
| `tests/scripts/extract_arial_glyphs.py` (новый) | Офлайн-генератор `kaspi_ip_glyphs.py` из `arial.ttf`. Коммитится, чтобы таблицу можно было пересобрать. |
| `tests/scripts/verify_kaspi_ip_data.py` (новый) | Батарея проверок результата подстановки. |
| `tests/test_kaspi_ip_data_fields.py` (новый) | Юнит-тесты валидации и дефолтов, без фикстур. |
| `tests/test_kaspi_ip_data_substitution.py` (новый) | Интеграционные тесты подстановки, скипаются без шаблона. |
| `pdf_service.py` | Принимает общие помощники `/W` и CMap из `halyk_pdf_service`. |
| `halyk_pdf_service.py` | Импортирует перенесённые помощники вместо своих. |
| `main.py` | Новый эндпоинт `/process-kaspi-ip-data` и `/kaspi-ip-data-defaults`. |
| `static/index.html` | Новая вкладка «Реквизиты». |
| `build.spec`, `.gitignore` | Шаблон и новые модули в сборке; `templates/` вне git. |

---

### Task 1: Шаблон — хранение и загрузка

**Files:**
- Create: `kaspi_ip_data_service.py`
- Create: `tests/test_kaspi_ip_data_fields.py`
- Modify: `.gitignore`
- Modify: `build.spec:8-18`

**Interfaces:**
- Consumes: ничего
- Produces: `TemplateNotFoundError`, `template_path() -> Path`, `load_template() -> bytes`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_kaspi_ip_data_fields.py`:

```python
import os
from pathlib import Path

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
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `python -m pytest tests/test_kaspi_ip_data_fields.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kaspi_ip_data_service'`

- [ ] **Step 3: Реализовать минимум**

Создать `kaspi_ip_data_service.py`:

```python
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
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `python -m pytest tests/test_kaspi_ip_data_fields.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Положить шаблон и прописать его в сборку**

```bash
mkdir -p templates
cp "/c/Users/Abylay/Desktop/testpdf/kaspiPay/IP2.pdf" templates/kaspi_ip.pdf
```

В `.gitignore` дописать в конец:

```
# Встроенный шаблон Kaspi ИП — настоящая выписка, в git не кладём
templates/
```

В `build.spec` в список `datas` дописать после `('pdfsharp_deflate.py', '.')`:

```python
        ('kaspi_ip_data_service.py', '.'),
        ('kaspi_ip_glyphs.py', '.'),
        ('templates', 'templates'),
```

- [ ] **Step 6: Проверить, что шаблон читается**

Run: `python -c "import kaspi_ip_data_service as k; print(len(k.load_template()), 'байт')"`
Expected: `478... байт` (порядка 467 КБ)

- [ ] **Step 7: Убедиться, что шаблон не попал в git**

Run: `git status --short templates/`
Expected: пусто (папка игнорируется)

- [ ] **Step 8: Коммит**

```bash
git add kaspi_ip_data_service.py tests/test_kaspi_ip_data_fields.py .gitignore build.spec
git commit -m "feat(kaspi-ip): загрузка встроенного шаблона выписки"
```

---

### Task 2: Модель полей, значения по умолчанию, валидация

**Files:**
- Modify: `kaspi_ip_data_service.py`
- Modify: `tests/test_kaspi_ip_data_fields.py`

**Interfaces:**
- Consumes: ничего из Task 1
- Produces: `KaspiIPFields` (dataclass с полями `account: str`, `period_from: str`, `period_to: str`, `last_movement: str`, `iin: str`, `client_name: str`), `default_fields() -> KaspiIPFields`, `validate_fields(f: KaspiIPFields) -> list[str]`, `period_text(f) -> str`, `ALLOWED_CHARS: frozenset[str]`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/test_kaspi_ip_data_fields.py`:

```python
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
```

- [ ] **Step 2: Прогнать тесты, убедиться что падают**

Run: `python -m pytest tests/test_kaspi_ip_data_fields.py -v`
Expected: FAIL — `AttributeError: module 'kaspi_ip_data_service' has no attribute 'default_fields'`

- [ ] **Step 3: Реализовать**

Дописать в `kaspi_ip_data_service.py` после `load_template`:

```python
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
```

- [ ] **Step 4: Прогнать тесты, убедиться что проходят**

Run: `python -m pytest tests/test_kaspi_ip_data_fields.py -v`
Expected: PASS, 17 passed

- [ ] **Step 5: Коммит**

```bash
git add kaspi_ip_data_service.py tests/test_kaspi_ip_data_fields.py
git commit -m "feat(kaspi-ip): модель реквизитов, дефолты и валидация"
```

---

### Task 3: Замена полей фиксированной длины

**Files:**
- Modify: `kaspi_ip_data_service.py`
- Create: `tests/test_kaspi_ip_data_substitution.py`

**Interfaces:**
- Consumes: `KaspiIPFields`, `load_template`, `period_text` из Task 2
- Produces: `SubstitutionError`, `_page_tokens(doc, pno) -> list[dict]`, `_encode_cid(text, from_unicode) -> bytes`, `_escape_pdf_string(data) -> bytes`, `_replace_stream(raw, xref, new_body, compress) -> bytes`, `substitute_fixed_length(pdf_bytes, fields) -> bytes`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_kaspi_ip_data_substitution.py`:

```python
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
    """Мы не трогаем ни одной суммы и ни одной даты операции."""
    before = _all_text(kid.load_template())
    after = _all_text(kid.substitute_fixed_length(kid.load_template(), NEW))
    money = lambda t: sorted(re.findall(r"\d[\d ]*,\d{2}", t))
    assert money(after) == money(before)
    op_dates = lambda t: sorted(re.findall(r"\b\d{2}\.\d{2}\.20\d{2}\b", t))
    # Период и дата движения тоже даты, поэтому сверяем множество за вычетом их.
    removed = {"18.07.2025", "18.07.2026", "17.07.2026"}
    assert [d for d in op_dates(after) if d not in {"01.02.2025", "01.02.2026", "31.01.2026"}] \
        == [d for d in op_dates(before) if d not in removed]


def test_structure_intact():
    out = kid.substitute_fixed_length(kid.load_template(), NEW)
    d = fitz.open(stream=out, filetype="pdf")
    try:
        assert d.page_count == 101
        assert d.is_repaired is False
    finally:
        d.close()
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py -v`
Expected: FAIL — `AttributeError: module 'kaspi_ip_data_service' has no attribute 'substitute_fixed_length'`

- [ ] **Step 3: Реализовать**

Дописать в `kaspi_ip_data_service.py`:

```python
import zlib
from typing import Callable, Dict, List, Optional

import fitz

from pdf_service import build_dynamic_cmap, _rebuild_xref_table

# Токен показа текста ровно в той форме, в какой его пишет генератор шаблона
# (замер: 215 таких токенов на стр. 0, ноль TJ-массивов, ноль '/").
_TOKEN_RE = re.compile(
    rb"1 0 0 1 ([\d.\-]+) ([\d.\-]+) Tm\s*/F(\d+) ([\d.]+) Tf\s*\("
    rb"((?:\\.|[^\\)])*)\)Tj",
    re.S,
)

# Метки полей шапки. Значение поля — токен на ТОЙ ЖЕ строке (Y), но правее.
# Ищем по метке, а не по зашитому Y: так конвенция читается из самого файла.
_LABEL_ACCOUNT = "Лицевой счет:"
_LABEL_PERIOD = "Период:"
_LABEL_LAST_MOVEMENT = "Дата последнего движения:"
_LABEL_IIN = "ИИН/БИН:"
_LABEL_CLIENT = "Наименование клиента:"


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
    """Экранирование строки PDF: скобки и обратный слэш."""
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
    """Текст → CID-байты. Отсутствие символа в карте — отказ, а не '?'. """
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
        to_unicode, from_unicode = build_dynamic_cmap(doc)
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
            (_encode_cid(old, from_unicode), _encode_cid(new, from_unicode))
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
```

- [ ] **Step 4: Прогнать тесты, убедиться что проходят**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Убедиться, что почерк сжатия не испортился**

Run:
```bash
python -c "
import kaspi_ip_data_service as kid, zlib, fitz, sys
sys.path.insert(0,'tests/scripts')
out = kid.substitute_fixed_length(kid.load_template(), kid.KaspiIPFields(
    account='KZ11722S000099887766', period_from='01.02.2025', period_to='01.02.2026',
    last_movement='31.01.2026 09:15', iin='990101300123', client_name='ИП ТЕСТОВ ТЕСТ'))
d = fitz.open(stream=out, filetype='pdf')
bad = 0
for p in range(d.page_count):
    for x in d[p].get_contents():
        if zlib.compress(d.xref_stream(x), 6) != d.xref_stream_raw(x): bad += 1
print('потоков НЕ как у python-zlib:', bad, '(ожидается 0 — генератор шаблона пишет так же)')
"
```
Expected: `потоков НЕ как у python-zlib: 0`

- [ ] **Step 6: Коммит**

```bash
git add kaspi_ip_data_service.py tests/test_kaspi_ip_data_substitution.py
git commit -m "feat(kaspi-ip): подстановка реквизитов фиксированной длины"
```

---

### Task 4: Перенос общих помощников `/W` и CMap в `pdf_service`

**Files:**
- Modify: `pdf_service.py` (добавить в конец, рядом с `_patch_truetype_glyphs`)
- Modify: `halyk_pdf_service.py:223-556` (удалить перенесённое, импортировать)

**Interfaces:**
- Consumes: ничего
- Produces: `pdf_service._w_array_entries`, `pdf_service._w_array_insert_sorted`, `pdf_service._cmap_bf_style`, `pdf_service._cmap_add_mappings`, `pdf_service._CMAP_MAX_BLOCK_ENTRIES`

Эти функции понадобились второй раз (Task 6). Тянуть их из halyk-модуля в kaspi-модуль было бы неверной зависимостью — в `pdf_service` уже лежат `build_dynamic_cmap`, `_rebuild_xref_table`, `_patch_truetype_glyphs`.

- [ ] **Step 1: Зафиксировать эталон поведения ДО переноса**

Run: `python -m pytest tests/ -q`
Expected: `124 passed, 70 skipped` — записать это число, после переноса оно обязано не измениться.

- [ ] **Step 2: Перенести код**

Из `halyk_pdf_service.py` вырезать целиком и вставить в `pdf_service.py` (в конец файла, после `_patch_truetype_glyphs`), не меняя ни строки тел функций:

- `_w_array_entries`
- `_w_array_insert_sorted`
- `_cmap_bf_style`
- `_CMAP_MAX_BLOCK_ENTRIES`, `_BFRANGE_BLOCK_RE`, `_BFCHAR_BLOCK_RE`
- `_cmap_add_mappings`

`_w_array_remove`, `_cmap_remove_mappings` и `_cmap_reorder` НЕ переносить — они нужны только Halyk.

Важно: `_cmap_remove_mappings` и `_cmap_reorder` остаются в halyk-модуле и пользуются `_BFRANGE_BLOCK_RE`/`_BFCHAR_BLOCK_RE`, поэтому в `halyk_pdf_service.py` эти имена должны появиться в списке импорта.

- [ ] **Step 3: Заменить определения импортом**

В `halyk_pdf_service.py` в блок `from pdf_service import (...)` дописать:

```python
    _w_array_entries,
    _w_array_insert_sorted,
    _cmap_bf_style,
    _cmap_add_mappings,
    _CMAP_MAX_BLOCK_ENTRIES,
    _BFRANGE_BLOCK_RE,
    _BFCHAR_BLOCK_RE,
)
```

- [ ] **Step 4: Проверить, что ничего не сломалось**

Run: `python -m pytest tests/ -q`
Expected: `124 passed, 70 skipped` — ровно как в шаге 1

- [ ] **Step 5: Прогнать батарею Halyk**

Run: `python tests/scripts/verify_halyk_file.py /c/Users/Abylay/Desktop/testpdf/halyk/*.pdf --targets 1.05,2`
Expected: `ВСЁ ОК`, exit code 0

- [ ] **Step 6: Коммит**

```bash
git add pdf_service.py halyk_pdf_service.py
git commit -m "refactor: общие помощники /W и ToUnicode переехали в pdf_service"
```

---

### Task 5: Заморозка глифов Arial

**Files:**
- Create: `tests/scripts/extract_arial_glyphs.py`
- Create: `kaspi_ip_glyphs.py` (генерируется скриптом)

**Interfaces:**
- Consumes: `ALLOWED_CHARS` из Task 2
- Produces: `kaspi_ip_glyphs.GLYPHS: dict[int, bytes]` (GID → байты glyf-записи), `kaspi_ip_glyphs.CHAR_GID: dict[str, int]`, `kaspi_ip_glyphs.WIDTHS_1000: dict[int, float]`, `kaspi_ip_glyphs.SOURCE_UNITS_PER_EM: int`

- [ ] **Step 1: Написать генератор**

Создать `tests/scripts/extract_arial_glyphs.py`:

```python
# -*- coding: utf-8 -*-
"""Офлайн-генератор kaspi_ip_glyphs.py из C:\\Windows\\Fonts\\arial.ttf.

Запускается вручную, в рантайме приложения НЕ используется — как и
halyk_bold_digits.py, таблица глифов фиксируется в исходнике, чтобы
приложение не читало системный шрифт с диска и не тянуло fontTools.

ВАЖНО: 27 символов набора — СОСТАВНЫЕ глифы (А→A, Ё→E+dieresis,
Й→uni0418+breve, Э→uni0404 и т. д.). Составной глиф ссылается на другие GID,
поэтому в таблицу кладутся и компоненты, рекурсивно, иначе вшитый глиф
отрисуется пустым.

    python tests/scripts/extract_arial_glyphs.py > kaspi_ip_glyphs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fontTools.ttLib import TTFont

from kaspi_ip_data_service import ALLOWED_CHARS

ARIAL = r"C:\Windows\Fonts\arial.ttf"


def collect(font):
    glyf = font["glyf"]
    order = font.getGlyphOrder()
    index = {name: i for i, name in enumerate(order)}
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    upem = font["head"].unitsPerEm

    char_gid = {}
    need = set()
    for ch in sorted(ALLOWED_CHARS):
        name = cmap.get(ord(ch))
        if name is None:
            raise SystemExit(f"в arial.ttf нет символа {ch!r}")
        char_gid[ch] = index[name]
        need.add(name)

    # рекурсивно добираем компоненты составных глифов
    stack = list(need)
    while stack:
        name = stack.pop()
        g = glyf[name]
        if g.numberOfContours == -1:
            for comp in g.components:
                if comp.glyphName not in need:
                    need.add(comp.glyphName)
                    stack.append(comp.glyphName)

    glyphs = {}
    widths = {}
    for name in sorted(need):
        gid = index[name]
        glyphs[gid] = glyf.glyphs[name].compile(glyf) if glyf[name].numberOfContours != 0 else b""
        widths[gid] = hmtx[name][0] * 1000.0 / upem
    return char_gid, glyphs, widths, upem


def main():
    font = TTFont(ARIAL)
    char_gid, glyphs, widths, upem = collect(font)
    out = sys.stdout
    out.write('# -*- coding: utf-8 -*-\n')
    out.write('"""Байты глифов Arial для вшивания в subset шаблона Kaspi ИП.\n\n')
    out.write('СГЕНЕРИРОВАНО tests/scripts/extract_arial_glyphs.py — руками не править.\n')
    out.write('Извлечено один раз из C:\\\\Windows\\\\Fonts\\\\arial.ttf; рантайм не читает\n')
    out.write('никакой файл с диска и не использует fontTools. Совпадение с subset\'ом\n')
    out.write('обрабатываемого файла проверяется gate\'ом перед тем, как этим данным\n')
    out.write('довериться (см. kaspi_ip_data_service._trusted_glyph_source).\n\n')
    out.write('Включает компоненты составных глифов: их 27 в наборе, и без компонента\n')
    out.write('составной глиф рисуется пустым.\n"""\n\n')
    out.write('from __future__ import annotations\n\n')
    out.write(f'SOURCE_UNITS_PER_EM = {upem}\n\n')
    out.write('CHAR_GID: dict[str, int] = {\n')
    for ch, gid in sorted(char_gid.items()):
        out.write(f'    {ch!r}: {gid},\n')
    out.write('}\n\n')
    out.write('WIDTHS_1000: dict[int, float] = {\n')
    for gid, w in sorted(widths.items()):
        out.write(f'    {gid}: {w!r},\n')
    out.write('}\n\n')
    out.write('_GLYPHS_HEX: dict[int, str] = {\n')
    for gid, data in sorted(glyphs.items()):
        out.write(f'    {gid}: {data.hex()!r},\n')
    out.write('}\n\n')
    out.write('GLYPHS: dict[int, bytes] = {g: bytes.fromhex(h) for g, h in _GLYPHS_HEX.items()}\n')


main()
```

- [ ] **Step 2: Сгенерировать модуль**

Run: `python tests/scripts/extract_arial_glyphs.py > kaspi_ip_glyphs.py`
Expected: файл создан, без исключений

- [ ] **Step 3: Проверить, что данные согласованы с реальным шрифтом**

Run:
```bash
python -c "
import kaspi_ip_glyphs as g, sys
sys.path.insert(0,'.')
from pdf_service import _read_truetype_glyph
master = open(r'C:\Windows\Fonts\arial.ttf','rb').read()
bad = [gid for gid, data in g.GLYPHS.items() if _read_truetype_glyph(master, gid) != data]
print('символов:', len(g.CHAR_GID), '| глифов (с компонентами):', len(g.GLYPHS), '| расхождений:', len(bad))
"
```
Expected: `расхождений: 0`, глифов заметно больше, чем символов (за счёт компонентов)

- [ ] **Step 4: Проверить, что gate сойдётся на шаблоне**

Run:
```bash
python -c "
import fitz, sys; sys.path.insert(0,'.')
import kaspi_ip_glyphs as g, kaspi_ip_data_service as kid
from pdf_service import build_dynamic_cmap, _read_truetype_glyph
d = fitz.open(stream=kid.load_template(), filetype='pdf')
sub = None
import re
for x in range(1, d.xref_length()):
    o = d.xref_object(x, compressed=True) or ''
    m = re.search(r'/FontFile2\s+(\d+)\s+0\s+R', o)
    if m: sub = d.xref_stream(int(m.group(1)))
_, from_uni = build_dynamic_cmap(d)
same = sum(1 for ch, cid in from_uni.items()
           if ch in g.CHAR_GID and _read_truetype_glyph(sub, int(cid,16)) == g.GLYPHS.get(int(cid,16)))
print('присутствующих символов сверено:', same, 'из', sum(1 for ch in from_uni if ch in g.CHAR_GID))
"
```
Expected: два числа равны — gate на шаблоне сходится

- [ ] **Step 5: Коммит**

```bash
git add tests/scripts/extract_arial_glyphs.py kaspi_ip_glyphs.py
git commit -m "feat(kaspi-ip): замороженные глифы Arial + офлайн-генератор"
```

---

### Task 6: Вшивание недостающих глифов

**Files:**
- Modify: `kaspi_ip_data_service.py`
- Modify: `tests/test_kaspi_ip_data_substitution.py`

**Interfaces:**
- Consumes: `kaspi_ip_glyphs.GLYPHS/CHAR_GID/WIDTHS_1000`, `pdf_service._w_array_insert_sorted`, `pdf_service._cmap_add_mappings`, `_replace_stream` из Task 3
- Produces: `_trusted_glyph_source(doc) -> bool`, `embed_missing_glyphs(pdf_bytes, chars) -> tuple[bytes, dict[str, str]]` — возвращает новые байты и карту `символ → CID` для вшитых

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_kaspi_ip_data_substitution.py`:

```python
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
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py -k embed -v`
Expected: FAIL — `AttributeError: ... has no attribute 'embed_missing_glyphs'`

- [ ] **Step 3: Реализовать**

Дописать в `kaspi_ip_data_service.py`:

```python
from pdf_service import (
    _patch_truetype_glyphs,
    _read_truetype_glyph,
    _w_array_insert_sorted,
    _cmap_add_mappings,
)
import kaspi_ip_glyphs


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


def _trusted_glyph_source(font_bytes: bytes, from_unicode: Dict[str, str]) -> bool:
    """Gate: доверять замороженным контурам можно, только если КАЖДЫЙ уже
    присутствующий в subset'е символ побайтово совпал с эталоном.

    Тот же принцип, что у `_try_patch_bold_digit_glyphs` в Halyk: сначала
    проверь, потом доверяй. Замер на шаблоне: 133 символа из 133 совпали.
    Файл, собранный из другого мастер-шрифта, обязан получить отказ, а не
    чужие контуры.
    """
    for ch, cid in from_unicode.items():
        gid = int(cid, 16)
        expected = kaspi_ip_glyphs.GLYPHS.get(gid)
        if expected is None:
            continue
        if _read_truetype_glyph(font_bytes, gid) != expected:
            return False
    return True


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
        cid_obj = doc.xref_object(cid_xref, compressed=True) or ""
        tu_body = doc.xref_stream(tu_xref)
    finally:
        doc.close()

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
    patches = {}
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
    _replace_stream(raw, ff2_xref, new_font, zlib.compress)
    _replace_stream(raw, tu_xref, new_tu, zlib.compress)
    _replace_cid_object(raw, cid_xref, new_cid_obj)
    return _rebuild_xref_table(bytes(raw)), added


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


def _insert_widths(cid_obj: str, chars: List[str]) -> Optional[str]:
    """Дописывает ширины новых CID в /W тем же стилем, каким массив написан."""
    data = cid_obj.encode("latin-1", "replace")
    m = re.search(rb"/W\s*\[", data)
    if not m:
        return None
    bracket = m.end() - 1
    depth = 0
    close = None
    for i in range(bracket, len(data)):
        ch = data[i:i + 1]
        if ch == b"[":
            depth += 1
        elif ch == b"]":
            depth -= 1
            if depth == 0:
                close = i
                break
    if close is None:
        return None
    entries = {
        f"{kaspi_ip_glyphs.CHAR_GID[c]:04X}": kaspi_ip_glyphs.WIDTHS_1000[
            kaspi_ip_glyphs.CHAR_GID[c]
        ]
        for c in chars
    }
    out = _w_array_insert_sorted(data, bracket, close, entries)
    return None if out is None else out.decode("latin-1")


def _replace_cid_object(raw: bytearray, xref: int, new_obj: str) -> None:
    pos = raw.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise SubstitutionError(f"объект {xref} не найден")
    end = raw.find(b"endobj", pos)
    raw[pos:end] = new_obj.encode("latin-1")
```

- [ ] **Step 4: Прогнать тесты, убедиться что проходят**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Убедиться глазами, что вшитая буква рисуется**

Run:
```bash
python -c "
import kaspi_ip_data_service as kid, fitz
out, added = kid.embed_missing_glyphs(kid.load_template(), {'Ы','Ю','Щ'})
print('вшито:', added)
d = fitz.open(stream=out, filetype='pdf'); d[0].get_pixmap(dpi=150).save('/tmp/embed_check.png'); d.close()
"
```
Затем открыть `/tmp/embed_check.png` и убедиться, что страница 0 выглядит как прежде (вшивание не должно ничего сломать — новые буквы на ней ещё не печатаются).

- [ ] **Step 6: Коммит**

```bash
git add kaspi_ip_data_service.py tests/test_kaspi_ip_data_substitution.py
git commit -m "feat(kaspi-ip): вшивание недостающих глифов Arial с транзитивными компонентами"
```

---

### Task 7: Замена наименования клиента

**Files:**
- Modify: `kaspi_ip_data_service.py`
- Modify: `tests/test_kaspi_ip_data_substitution.py`

**Interfaces:**
- Consumes: всё из Task 3 и Task 6
- Produces: `MAX_NAME_WIDTH_PT = 482.0`, `substitute_fields(pdf_bytes, fields) -> bytes` — публичная точка входа модуля

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/test_kaspi_ip_data_substitution.py`:

```python
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


def test_substitute_fields_keeps_font_set():
    import fitz
    before = fitz.open(stream=kid.load_template(), filetype="pdf")
    after = fitz.open(stream=kid.substitute_fields(kid.load_template(), NEW), filetype="pdf")
    try:
        assert {(f[3], f[4]) for f in after[0].get_fonts(full=True)} == \
               {(f[3], f[4]) for f in before[0].get_fonts(full=True)}
    finally:
        before.close(); after.close()
```

- [ ] **Step 2: Прогнать тесты, убедиться что падают**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py -k substitute_fields -v`
Expected: FAIL — `AttributeError: ... has no attribute 'substitute_fields'`

- [ ] **Step 3: Реализовать**

Дописать в `kaspi_ip_data_service.py`:

```python
from kaspi_ip_pdf_service import _primary_glyph_advances

# Замер на шаблоне: значение наименования занимает 148 pt, соседей на строке
# правее нет, до края отведённой области ещё ~482 pt. Поле лево-выровнено,
# поэтому X начала не пересчитывается — ширина нужна только чтобы отказать,
# если текст не помещается.
MAX_NAME_WIDTH_PT = 482.0


def _text_width_pt(text: str, advances: Dict[str, float], size: float) -> float:
    return sum(advances.get(ch, 0.5) for ch in text) * size


def substitute_fields(pdf_bytes: bytes, fields: KaspiIPFields) -> bytes:
    """Подставляет все пять реквизитов. Публичная точка входа модуля.

    Порядок важен: глифы вшиваются ПЕРВЫМИ, иначе `_encode_cid` не найдёт CID
    для новых символов имени. Поля фиксированной длины идут вторыми — они не
    зависят от вшивания. Имя пишется последним, когда карта символов уже полна.
    """
    errors = validate_fields(fields)
    if errors:
        raise SubstitutionError("; ".join(errors))

    name = fields.client_name.strip()
    working, _added = embed_missing_glyphs(pdf_bytes, set(name))
    working = substitute_fixed_length(working, fields)

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
```

- [ ] **Step 4: Прогнать все тесты модуля**

Run: `python -m pytest tests/test_kaspi_ip_data_substitution.py tests/test_kaspi_ip_data_fields.py -v`
Expected: PASS, 26 passed

- [ ] **Step 5: Посмотреть результат глазами**

Run:
```bash
python -c "
import kaspi_ip_data_service as kid, fitz
f = kid.KaspiIPFields(account='KZ11722S000099887766', period_from='01.02.2025',
    period_to='01.02.2026', last_movement='31.01.2026 09:15', iin='990101300123',
    client_name='ИП САТЫБАЛДЫ ЮЛИЯ ҚАЙРАТҚЫЗЫ')
d = fitz.open(stream=kid.substitute_fields(kid.load_template(), f), filetype='pdf')
d[0].get_pixmap(dpi=150).save('/tmp/kaspi_ip_data_p0.png'); d.close()
"
```
Открыть `/tmp/kaspi_ip_data_p0.png`. Проверить глазами: все пять полей показывают новые значения, буквы `Ы`, `Ю`, `Қ` нарисованы полноценно (не пустые прямоугольники), кегль такой же, как у соседних строк, ничего не наезжает на соседние колонки.

- [ ] **Step 6: Коммит**

```bash
git add kaspi_ip_data_service.py tests/test_kaspi_ip_data_substitution.py
git commit -m "feat(kaspi-ip): подстановка наименования клиента"
```

---

### Task 8: Эндпоинты API

**Files:**
- Modify: `main.py` (после `/process-business`, около строки 790)

**Interfaces:**
- Consumes: `kaspi_ip_data_service.KaspiIPFields/default_fields/validate_fields/substitute_fields/load_template/TemplateNotFoundError/SubstitutionError`
- Produces: `POST /process-kaspi-ip-data`, `GET /kaspi-ip-data-defaults`

- [ ] **Step 1: Реализовать эндпоинты**

В `main.py` рядом с остальными импортами сервисов дописать:

```python
import kaspi_ip_data_service as kaspi_ip_data
```

После эндпоинта `/process-business` дописать:

```python
@app.get("/kaspi-ip-data-defaults")
async def kaspi_ip_data_defaults():
    """Значения по умолчанию для формы реквизитов. Считаются на сервере,
    чтобы форма не зависела от часового пояса браузера."""
    return kaspi_ip_data.default_fields().__dict__


@app.post("/process-kaspi-ip-data")
async def process_kaspi_ip_data_endpoint(
    account: str = Form(...),
    period_from: str = Form(...),
    period_to: str = Form(...),
    last_movement: str = Form(...),
    iin: str = Form(...),
    client_name: str = Form(...),
):
    fields = kaspi_ip_data.KaspiIPFields(
        account=account.strip(),
        period_from=period_from.strip(),
        period_to=period_to.strip(),
        last_movement=last_movement.strip(),
        iin=iin.strip(),
        client_name=client_name.strip(),
    )
    errors = kaspi_ip_data.validate_fields(fields)
    if errors:
        return JSONResponse(status_code=400, content={"error": "; ".join(errors)})

    try:
        out = kaspi_ip_data.substitute_fields(kaspi_ip_data.load_template(), fields)
    except kaspi_ip_data.TemplateNotFoundError as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})
    except kaspi_ip_data.SubstitutionError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Ошибка обработки: {e}"})

    _journal_add("kaspi_ip_template.pdf", 0, 0, "РЕКВИЗИТЫ", "ok")
    return StreamingResponse(
        io.BytesIO(out),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=kaspi_ip_data.pdf"},
    )
```

- [ ] **Step 2: Запустить сервер**

Run: `python main.py`
Expected: `Server starts at http://127.0.0.1:8081`

- [ ] **Step 3: Проверить дефолты**

Run: `curl -s http://127.0.0.1:8081/kaspi-ip-data-defaults`
Expected: JSON с непустыми `period_from`, `period_to`, `last_movement` и пустыми `account`, `iin`, `client_name`

- [ ] **Step 4: Проверить отказ на неверных данных**

Run:
```bash
curl -s -X POST http://127.0.0.1:8081/process-kaspi-ip-data \
  -F account=ПЛОХО -F period_from=01.02.2025 -F period_to=01.02.2026 \
  -F last_movement="31.01.2026 09:15" -F iin=990101300123 -F client_name="ИП ТЕСТОВ"
```
Expected: HTTP 400, JSON с текстом про лицевой счёт

- [ ] **Step 5: Проверить успешный путь**

Run:
```bash
curl -s -X POST http://127.0.0.1:8081/process-kaspi-ip-data \
  -F account=KZ11722S000099887766 -F period_from=01.02.2025 -F period_to=01.02.2026 \
  -F last_movement="31.01.2026 09:15" -F iin=990101300123 \
  -F client_name="ИП САТЫБАЛДЫ ЮЛИЯ" -o /tmp/api_out.pdf && \
python -c "import fitz; d=fitz.open('/tmp/api_out.pdf'); print(d.page_count, 'страниц'); print('ИП САТЫБАЛДЫ ЮЛИЯ' in d[0].get_text())"
```
Expected: `101 страниц` и `True`

- [ ] **Step 6: Коммит**

```bash
git add main.py
git commit -m "feat(api): эндпоинты подстановки реквизитов Kaspi ИП"
```

---

### Task 9: Вкладка в интерфейсе

**Files:**
- Modify: `static/index.html:250` (кнопка вкладки), `static/index.html:301` (панель), блок скриптов около строки 405

**Interfaces:**
- Consumes: `POST /process-kaspi-ip-data`, `GET /kaspi-ip-data-defaults`
- Produces: ничего для других задач

- [ ] **Step 1: Добавить кнопку вкладки**

В `static/index.html` после строки с `data-tab="business"` вставить:

```html
                    <button class="tab-btn" data-tab="ipdata"><span class="tab-icon">🪪</span> Реквизиты</button>
```

- [ ] **Step 2: Добавить панель вкладки**

Перед `<!-- VERIFY TAB -->` вставить:

```html
                <!-- IP DATA TAB -->
                <div class="tab-content" id="tab-ipdata">
                    <div class="form-group">
                        <label class="form-label">Лицевой счёт</label>
                        <input class="input" id="ipAccount" type="text" placeholder="KZ00000A000000000000" maxlength="20" />
                    </div>
                    <div class="form-group">
                        <label class="form-label">Период</label>
                        <div style="display:flex; gap:8px;">
                            <input class="input" id="ipPeriodFrom" type="text" placeholder="ДД.ММ.ГГГГ" />
                            <input class="input" id="ipPeriodTo" type="text" placeholder="ДД.ММ.ГГГГ" />
                        </div>
                        <div class="calc-tag" id="ipPeriodWarn"></div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Дата последнего движения</label>
                        <input class="input" id="ipLastMovement" type="text" placeholder="ДД.ММ.ГГГГ ЧЧ:ММ" />
                    </div>
                    <div class="form-group">
                        <label class="form-label">ИИН/БИН</label>
                        <input class="input" id="ipIin" type="text" inputmode="numeric" placeholder="000000000000" maxlength="12" />
                    </div>
                    <div class="form-group">
                        <label class="form-label">Наименование клиента</label>
                        <input class="input" id="ipClientName" type="text" placeholder="ИП ИВАНОВ ИВАН" />
                        <div class="calc-tag">Суммы, даты операций и назначения платежей не меняются.</div>
                    </div>
                    <button class="btn btn-primary" id="ipDataBtn" onclick="doIpData()">
                        <span id="ipDataBtnText">🪪 Сформировать выписку</span>
                    </button>
                    <div class="status" id="ipDataStatus"></div>
                </div>
```

- [ ] **Step 3: Добавить скрипт**

В конец блока скриптов дописать:

```javascript
// Период шаблона: операции датированы 18.07.2025 – 18.07.2026. Даты операций
// мы не сдвигаем, поэтому если введённый период их не покрывает — часть строк
// окажется за его границами. Предупреждаем, но не блокируем.
const IP_TEMPLATE_FROM = '18.07.2025', IP_TEMPLATE_TO = '18.07.2026';

function ipParseDate(s) {
    const m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec((s || '').trim());
    return m ? new Date(+m[3], +m[2] - 1, +m[1]) : null;
}

function ipCheckPeriod() {
    const from = ipParseDate(document.getElementById('ipPeriodFrom').value);
    const to = ipParseDate(document.getElementById('ipPeriodTo').value);
    const el = document.getElementById('ipPeriodWarn');
    if (!from || !to) { el.textContent = ''; return; }
    const tf = ipParseDate(IP_TEMPLATE_FROM), tt = ipParseDate(IP_TEMPLATE_TO);
    el.textContent = (from > tf || to < tt)
        ? '⚠️ Часть операций (' + IP_TEMPLATE_FROM + ' – ' + IP_TEMPLATE_TO + ') выйдет за границы периода.'
        : '';
}

['ipPeriodFrom', 'ipPeriodTo'].forEach(id =>
    document.getElementById(id).addEventListener('input', ipCheckPeriod));

fetch('/kaspi-ip-data-defaults').then(r => r.json()).then(d => {
    document.getElementById('ipPeriodFrom').value = d.period_from;
    document.getElementById('ipPeriodTo').value = d.period_to;
    document.getElementById('ipLastMovement').value = d.last_movement;
    ipCheckPeriod();
});

async function doIpData() {
    const btn = document.getElementById('ipDataBtn');
    const status = document.getElementById('ipDataStatus');
    const fd = new FormData();
    fd.append('account', document.getElementById('ipAccount').value);
    fd.append('period_from', document.getElementById('ipPeriodFrom').value);
    fd.append('period_to', document.getElementById('ipPeriodTo').value);
    fd.append('last_movement', document.getElementById('ipLastMovement').value);
    fd.append('iin', document.getElementById('ipIin').value);
    fd.append('client_name', document.getElementById('ipClientName').value);
    btn.disabled = true;
    status.textContent = 'Формируем выписку…';
    try {
        const r = await fetch('/process-kaspi-ip-data', { method: 'POST', body: fd });
        if (!r.ok) {
            const err = await r.json();
            status.textContent = '❌ ' + (err.error || 'Ошибка');
            return;
        }
        const blob = await r.blob();
        if (window.pywebview) {
            const b64 = await new Promise(res => {
                const fr = new FileReader();
                fr.onload = () => res(fr.result.split(',')[1]);
                fr.readAsDataURL(blob);
            });
            await window.pywebview.api.save_pdf('kaspi_ip_data.pdf', b64);
        } else {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'kaspi_ip_data.pdf';
            a.click();
        }
        status.textContent = '✅ Готово';
    } catch (e) {
        status.textContent = '❌ ' + e;
    } finally {
        btn.disabled = false;
    }
}
```

- [ ] **Step 4: Проверить в браузере**

Run: `python main.py`, открыть `http://127.0.0.1:8081/app`

Проверить: вкладка «Реквизиты» переключается; период и дата движения предзаполнены; при периоде `01.02.2025 – 01.02.2026` появляется предупреждение про выходящие за границы операции; после заполнения счёта, ИИН и имени кнопка отдаёт PDF; при неверном ИИН показывается текст ошибки из ответа сервера.

- [ ] **Step 5: Коммит**

```bash
git add static/index.html
git commit -m "feat(ui): вкладка «Реквизиты» для подстановки данных Kaspi ИП"
```

---

### Task 10: Батарея проверок результата

**Files:**
- Create: `tests/scripts/verify_kaspi_ip_data.py`

**Interfaces:**
- Consumes: `kaspi_ip_data_service`, `verify_kaspi_ip_file.find_line_overlaps/style_check/check_fonts`
- Produces: CLI-скрипт, exit code 1 при любом FAIL

- [ ] **Step 1: Написать скрипт**

Создать `tests/scripts/verify_kaspi_ip_data.py`:

```python
# -*- coding: utf-8 -*-
"""Батарея проверок подстановки реквизитов в шаблон Kaspi ИП.

Прогоняет шаблон через `substitute_fields` на нескольких наборах реквизитов и
на каждом результате проверяет все пять критериев качества проекта:

  1. Ничего лишнего не изменилось — множество сумм и дат операций совпадает
     с шаблоном, число страниц то же, is_repaired = False.
  2. Новые значения на месте, старых не осталось ни одного.
  3. Позиции — слова на одной строке не накладываются.
  4. Шрифт — набор (имя, кегль) не изменился.
  5. Стиль сериализации операторов не изменился.

Проверки «плотности subset'а» здесь СОЗНАТЕЛЬНО НЕТ, в отличие от Halyk.
Составные глифы вшиваются вместе с компонентами (А→A, Ё→E+dieresis,
Й→uni0418+breve, Э→uni0404 — всего 27 таких символов в наборе), а компонент
по построению никогда не показывается как самостоятельный CID. Проверка
«что вшито, то и напечатано» краснела бы на каждом составном глифе, то есть
на корректной работе. Вместо неё точность вшивания закреплена юнит-тестом
`test_embed_adds_missing_glyph_and_keeps_existing`, где сверяется, что
`embed_missing_glyphs` вернул РОВНО запрошенные символы.

Запуск:  python tests/scripts/verify_kaspi_ip_data.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "PDFAI_DB_PATH", os.path.join(tempfile.gettempdir(), "pdfai_ipdata_autotest.db")
)

import fitz  # noqa: E402

import kaspi_ip_data_service as kid  # noqa: E402
from verify_kaspi_ip_file import find_line_overlaps, style_check  # noqa: E402

CASES = [
    kid.KaspiIPFields("KZ11722S000099887766", "01.02.2025", "01.02.2026",
                      "31.01.2026 09:15", "990101300123", "ИП ТЕСТОВ ТЕСТ"),
    kid.KaspiIPFields("KZ99123A000000000001", "18.07.2025", "18.07.2026",
                      "17.07.2026 23:03", "010203400506",
                      "ИП САТЫБАЛДЫ ЮЛИЯ ҚАЙРАТҚЫЗЫ"),
    kid.KaspiIPFields("KZ00000B999999999999", "01.01.2024", "31.12.2026",
                      "01.12.2026 00:01", "111122223333",
                      "ТОО ЩЕРБАКОВ И ПАРТНЁРЫ"),
]

_MONEY = re.compile(r"\d[\d\u00a0 ]*,\d{2}")


def _text(pdf_bytes: bytes) -> str:
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(d[i].get_text() for i in range(d.page_count))
    finally:
        d.close()


def _fonts(pdf_bytes: bytes) -> set:
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out = set()
        for pno in range(d.page_count):
            for b in d[pno].get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for span in line["spans"]:
                        out.add((span["font"], round(span["size"], 2)))
        return out
    finally:
        d.close()


def check_case(template: bytes, fields: kid.KaspiIPFields) -> list:
    issues = []
    out = kid.substitute_fields(template, fields)

    before, after = _text(template), _text(out)

    if sorted(_MONEY.findall(after)) != sorted(_MONEY.findall(before)):
        issues.append("множество сумм изменилось — трогать их нельзя")

    d_before = fitz.open(stream=template, filetype="pdf")
    d_after = fitz.open(stream=out, filetype="pdf")
    try:
        if d_after.page_count != d_before.page_count:
            issues.append(f"страниц {d_after.page_count} против {d_before.page_count}")
        if d_after.is_repaired:
            issues.append("PyMuPDF пришлось чинить структуру результата")
        for pno in range(min(3, d_after.page_count)):
            issues += [f"стр.{pno}: {i}" for i in find_line_overlaps(d_after[pno])]
    finally:
        d_before.close()
        d_after.close()

    for label, value, expected in (
        ("лицевой счёт", fields.account, 13),
        ("ИИН/БИН", fields.iin, 17),
        ("период", kid.period_text(fields), 1),
        ("дата движения", fields.last_movement, 1),
        ("наименование", fields.client_name, 1),
    ):
        got = after.count(value)
        if got < expected:
            issues.append(f"{label}: найдено {got} вхождений, ожидалось {expected}")

    for old in ("KZ45722S000034195994", "810503400268",
                "ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА", "17.07.2026 23:03"):
        if old in after:
            issues.append(f"старое значение осталось в документе: {old!r}")

    if _fonts(out) != _fonts(template):
        issues.append("набор (шрифт, кегль) изменился")

    issues += style_check(template, out)
    return issues


def main() -> None:
    if not kid.template_path().exists():
        print(f"нет шаблона {kid.template_path()} — положите файл и повторите")
        sys.exit(1)
    template = kid.load_template()
    failed = 0
    for i, fields in enumerate(CASES, 1):
        issues = check_case(template, fields)
        status = "OK" if not issues else "FAIL"
        print(f"[{i}/{len(CASES)}] {fields.client_name:<32} {status}")
        for issue in issues:
            print("      ", issue)
        failed += bool(issues)
    print("ВСЁ ОК" if not failed else f"ПРОВАЛОВ: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Прогнать батарею**

Run: `python tests/scripts/verify_kaspi_ip_data.py`
Expected: три строки `OK`, `ВСЁ ОК`, exit code 0

- [ ] **Step 3: Убедиться, что батарея умеет краснеть**

Временно в `kaspi_ip_data_service.substitute_fields` закомментировать строку `working = substitute_fixed_length(working, fields)`, прогнать батарею.
Expected: FAIL с сообщениями про ненайденные счёт/ИИН/период и оставшиеся старые значения. После проверки строку ВЕРНУТЬ и прогнать снова — снова `ВСЁ ОК`.

- [ ] **Step 4: Прогнать весь набор тестов**

Run: `python -m pytest tests/ -q`
Expected: `150 passed, 70 skipped` (124 прежних + 26 новых), 0 failed

- [ ] **Step 5: Убедиться, что остальные форматы не задеты**

Run: `python tests/scripts/verify_any_file.py /c/Users/Abylay/Desktop/testpdf/kaspiPay/*.pdf --targets 1.05,2`
Expected: `ВСЁ ОК`, 0 FAIL

- [ ] **Step 6: Коммит**

```bash
git add tests/scripts/verify_kaspi_ip_data.py
git commit -m "test(kaspi-ip): батарея проверок подстановки реквизитов"
```

---

### Task 11: Документация в CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (раздел «Architecture» → «Module dependency chain», список эндпоинтов, раздел про Kaspi ИП)

- [ ] **Step 1: Обновить цепочку модулей**

В блоке `Module dependency chain` дописать под `kaspi_ip_pdf_service.py`:

```
└── kaspi_ip_data_service.py  (подстановка реквизитов во встроенный шаблон Kaspi ИП)
    └── kaspi_ip_glyphs.py    (замороженные контуры Arial, включая компоненты составных глифов)
```

- [ ] **Step 2: Дописать эндпоинты в таблицу API**

```markdown
| `POST /process-kaspi-ip-data` | Подстановка реквизитов во встроенный шаблон Kaspi ИП |
| `GET /kaspi-ip-data-defaults` | Значения по умолчанию для формы реквизитов |
```

- [ ] **Step 3: Добавить раздел с замерами**

Новый раздел после раздела про Kaspi ИП, куда перенести из плана: конвенцию токена `Tm/Tf/(CID)Tj`, таблицу полей шапки с координатами и длинами, числа 13/17 вхождений счёта и ИИН, факт «133 из 133 глифов совпали с arial.ttf», список 27 составных глифов и вывод «вшивать транзитивно», а также замер «все 103 потока шаблона воспроизводятся `zlib.compress(данные, 6)` — для Kaspi ИП `pdfsharp_deflate` не нужен».

- [ ] **Step 4: Дописать про переменную окружения**

В раздел «Environment variables»:

```markdown
- `PDFAI_KASPI_IP_TEMPLATE` — путь к шаблону выписки Kaspi ИП (default: `templates/kaspi_ip.pdf`)
```

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md
git commit -m "docs: режим подстановки реквизитов Kaspi ИП"
```

---

## Порядок выполнения

Задачи 1 → 2 → 3 дают работающую подстановку четырёх полей из пяти без всякой работы со шрифтом. Задачи 4 → 5 → 6 добавляют шрифтовую часть, 7 замыкает пятое поле. 8 → 9 выводят это в интерфейс, 10 → 11 закрепляют проверками и документацией.

Задачу 4 (перенос помощников) можно делать в любой момент до задачи 6, но не позже: задача 6 импортирует перенесённые функции из `pdf_service`.

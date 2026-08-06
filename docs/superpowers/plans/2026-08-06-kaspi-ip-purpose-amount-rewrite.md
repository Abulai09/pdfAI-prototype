# Kaspi ИП: переписывать сумму внутри текста «Назначение платежа» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Когда отмасштабированная сумма транзакции продублирована в тексте «Назначение платежа» (`KaspiIPTransaction.amount_in_purpose`), переписать это число внутри текста тоже — не только в колонке «Кредит», — для случаев, когда сумма целиком лежит в одной визуальной строке (~97% реальных случаев по замеру на `testpdf/kaspiPay`).

**Architecture:** Расширяет существующий однопроходный байтовый писатель `process_kaspi_ip_pdf` (`tm_pat.sub(replace_tm, decompressed)`). Парсер (`_parse_transactions_from_page`) при сборке `purpose` дополнительно сохраняет на транзакции bbox+размер шрифта каждой исходной строки назначения (`purpose_line_bboxes`). Перед основным байтовым проходом строится очередь `page_replace_purpose`, отобранная gate'ом по эмпирически измеренной правой границе ячейки (максимальный `x1` среди ВСЕХ строк назначения по документу). Внутри `replace_tm`, в уже существующей ветке «эта Tj-строка не денежная ячейка», добавляется проверка очереди и точечная замена цифрового прогона суммы, весь остальной текст строки не трогается.

**Tech Stack:** Python, PyMuPDF (`fitz`) — тот же стек, что и весь `kaspi_ip_pdf_service.py`. Никаких новых зависимостей.

## Global Constraints

- `pytest tests/` не должен регрессировать текущий бейзлайн: **104 passed / 69 skipped** (измерено 2026-08-06 на актуальном `main` сразу после слияния вшивания глифов Halyk и фикса ретраев Kaspi ИП — если при старте этого плана число иное, значит между этим измерением и стартом плана было что-то ещё закоммичено; перепроверить `git log` и взять то число за новый бейзлайн, а не старое).
- Новые чистые юнит-тесты (Task 1) — fixture-free. Тест на Task 2 — fixture-gated (по образцу `tests/test_kaspi_ip_pdf_service.py`, использует `tests/fixtures/kaspi_ip_*.pdf`; в этом checkout'е таких фикстур нет — тест обязан корректно SKIP через `conftest.py`, не падать).
- Никакой X/Y-рекомпутации для строк назначения — они лево-выровнены (проза, не таблица), координаты Tm переиспользуются байт-в-байт из оригинального совпадения (тот же принцип, что уже применён к колонке «Кредит»/summary-полям в этом файле — см. комментарий у `new_x = current_x`).
- Разделители (пробел/ничего между разрядами, `,`/`-` перед копейками, наличие копеек вовсе) определяются ИЗ КОНКРЕТНОЙ строки через `_locate_purpose_amount`, никогда не хардкодятся общей константой на весь документ — в одном и том же файле уже наблюдаются одновременно `145000-00` и `65 000,00`.
- Gate по ширине — БЕЗ искусственного запаса (по образцу колонки «Дебет» в этом же файле): если новая строка не влезает в эмпирически измеренный правый край — транзакция тихо пропускается (не попадает в очередь на замену), а не обрезается/переносится.
- Полная спецификация: `docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md` — при любом расхождении между этим планом и спекой обязательна сверка с человеком (не выбирать самостоятельно).

---

### Task 1: `_locate_purpose_amount` + `_format_purpose_amount` — позиция и форматирование

**Files:**
- Modify: `kaspi_ip_pdf_service.py` (добавить сразу после `_purpose_repeats_amount`, т.е. после строки с `return re.search(...)` — текущая строка ~316, до `# Страница повёрнута на 90°`)
- Test: `tests/test_kaspi_ip_purpose_rewrite.py` (новый, fixture-free)

**Interfaces:**
- Produces: `kaspi_ip_pdf_service._locate_purpose_amount(line: str, amount: float) -> Optional[Tuple[int, int, str, str, bool]]`, `kaspi_ip_pdf_service._format_purpose_amount(new_amount: float, thousands_sep: str, decimal_sep: str, has_decimal: bool) -> str`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_kaspi_ip_purpose_rewrite.py`:

```python
"""Fixture-free тесты для переписывания суммы внутри текста «Назначение
платежа» (см. docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md).
Строки — синтетические, но формат (разделители, структура фразы) взят из
реальных примеров, задокументированных в спеке."""

from kaspi_ip_pdf_service import _locate_purpose_amount, _format_purpose_amount


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
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `pytest tests/test_kaspi_ip_purpose_rewrite.py -v`
Expected: FAIL — `ImportError: cannot import name '_locate_purpose_amount' from 'kaspi_ip_pdf_service'`

- [ ] **Step 3: Реализовать в `kaspi_ip_pdf_service.py`**

Вставить сразу после конца функции `_purpose_repeats_amount` (после её `return re.search(...)`, перед следующим блочным комментарием `# Страница повёрнута на 90°`):

```python
def _locate_purpose_amount(
    line: str, amount: float
) -> Optional[Tuple[int, int, str, str, bool]]:
    """Находит позицию цифрового прогона суммы `amount` внутри ОДНОЙ визуальной
    строки назначения платежа `line` и определяет формат разделителей,
    использованный именно в этой строке (одна выписка одновременно содержит
    и "145000-00", и "65 000,00" — единой конвенции нет, см. design spec).

    Возвращает (start, end, thousands_sep, decimal_sep, has_decimal):
    - start/end — индексы в `line` (НЕ в компактной форме без пробелов),
      задающие срез line[start:end], который целиком покрывает цифровой
      прогон суммы (целая часть + разделители тысяч + копейки, если есть).
    - thousands_sep — разделитель между группами разрядов (" "/nbsp или "";
      "" если сумма записана без разделителя тысяч, напр. "145000-00").
    - decimal_sep — "," или "-" перед копейками, "" если копеек нет вовсе.
    - has_decimal — есть ли в СТРОКЕ копейки при этой сумме (напр. "Сумма
      75000 тенге" без "-00" — has_decimal=False; отличается от "есть ли
      копейки у самой суммы" — сумма всегда целая тысяча после округления
      recalculate_kaspi_ip, здесь речь о ТЕКСТЕ).

    None — сумма НЕ найдена в line ЦЕЛИКОМ: либо её вообще нет в строке,
    либо цифровой прогон разорван переносом на следующую строку (реальный
    пример: "…Сумма - 65" / "000,00 тенге…" на двух разных Tj) — такие
    случаи вне рамок этой задачи (см. design spec, "Область охвата"),
    вызывающая сторона обязана пропустить транзакцию, не роняя её.
    """
    if amount < _PURPOSE_AMOUNT_MIN or not line:
        return None
    digits = str(int(round(amount)))

    # Компактная форма (без пробела/nbsp) + карта "индекс в compact -> индекс
    # в line" — тот же принцип нормализации, что и в _purpose_repeats_amount,
    # но здесь позиция нужна для СРЕЗА, а не только для bool.
    compact_chars: List[str] = []
    index_map: List[int] = []
    for i, ch in enumerate(line):
        if ch in (" ", "\u00a0"):
            continue
        compact_chars.append(ch)
        index_map.append(i)
    compact = "".join(compact_chars)

    m = re.search(r"(?<!\d)" + re.escape(digits) + r"(?!\d)", compact)
    if m is None:
        return None
    start_c, end_c = m.start(), m.end()

    # thousands_sep: символ (если есть) между первыми двумя группами разрядов
    # цифрового прогона — единственный наблюдаемый в реальных файлах случай
    # (пробел ИЛИ ничего, никогда не "." и не разный внутри одного прогона).
    thousands_sep = ""
    for c_i in range(start_c, end_c - 1):
        gap = index_map[c_i + 1] - index_map[c_i]
        if gap > 1:
            thousands_sep = line[index_map[c_i] + 1 : index_map[c_i + 1]]
            break

    orig_start = index_map[start_c]
    orig_end = index_map[end_c - 1] + 1

    # Копейки: сразу после целой части В КОМПАКТНОЙ форме (без пробела между
    # целой частью и разделителем — подтверждено на всех наблюдаемых реальных
    # примерах), разделитель "," или "-", ровно 2 цифры.
    has_decimal = False
    decimal_sep = ""
    if end_c < len(compact) and compact[end_c] in (",", "-"):
        dec_digits = compact[end_c + 1 : end_c + 3]
        if len(dec_digits) == 2 and dec_digits.isdigit():
            has_decimal = True
            decimal_sep = compact[end_c]
            orig_end = index_map[end_c + 2] + 1

    return orig_start, orig_end, thousands_sep, decimal_sep, has_decimal


def _format_purpose_amount(
    new_amount: float, thousands_sep: str, decimal_sep: str, has_decimal: bool
) -> str:
    """Форматирует new_amount в ТОЙ ЖЕ нотации, что нашла _locate_purpose_amount
    в оригинальной строке — воспроизводит почерк КОНКРЕТНО этой строки, а не
    общую конвенцию файла."""
    int_part = str(int(round(abs(new_amount))))
    if thousands_sep:
        groups = []
        rest = int_part
        while len(rest) > 3:
            groups.insert(0, rest[-3:])
            rest = rest[:-3]
        groups.insert(0, rest)
        int_part = thousands_sep.join(groups)
    if has_decimal:
        cents = round((abs(new_amount) - int(round(abs(new_amount)))) * 100)
        return f"{int_part}{decimal_sep}{cents:02d}"
    return int_part
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `pytest tests/test_kaspi_ip_purpose_rewrite.py -v`
Expected: PASS (15 тестов)

- [ ] **Step 5: Commit**

```bash
git add kaspi_ip_pdf_service.py tests/test_kaspi_ip_purpose_rewrite.py
git commit -m "feat(kaspi-ip): add purpose-text amount locate/format helpers"
```

---

### Task 2: Парсер — bbox+размер шрифта на строках, `purpose_line_bboxes` на транзакции

**Files:**
- Modify: `kaspi_ip_pdf_service.py`:
  - `KaspiIPTransaction` dataclass (строки ~207-221)
  - `_page_lines_with_y` (строки ~477-492)
  - `_parse_transactions_from_page` (строки ~495-619; конкретно блок сборки `purpose_parts`, строки ~582-590, и `lines_with_y`/`lines_text` в начале функции, строки ~499-500, и извлечение `amount_y`, строка ~559)
- Test: `tests/test_kaspi_ip_pdf_service.py` (существующий, fixture-gated — добавить тесты в конец файла)

**Interfaces:**
- Produces: `KaspiIPTransaction.purpose_line_bboxes: List[Tuple[str, float, float, float, float]]` — список `(текст_строки, x0, x1, y_mid, font_size)` для КАЖДОЙ строки, вошедшей в `purpose` (т.е. прошедшей фильтр `_SKIP_RE`), в порядке появления. Пустой список, если ни одна строка не найдена (не должно происходить на практике, т.к. `purpose` всегда строится из тех же строк).
- Consumes: ничего нового — расширяет существующую сигнатуру `_page_lines_with_y(page) -> List[Tuple[str, Optional[Tuple[float,float,float,float]], float]]` (было `List[Tuple[str, Optional[float]]]` — единственный вызывающий код тоже здесь и меняется в этом же таске).

- [ ] **Step 1: Расширить `_page_lines_with_y`**

Заменить текущее тело функции (строки ~477-492):

```python
def _page_lines_with_y(page) -> List[Tuple[str, Optional[Tuple[float, float, float, float]], float]]:
    """
    Строки страницы (как в get_text(), с тем же порядком и разбиением), но
    каждая — с полным bbox и размером шрифта первого спана. Нужны: bbox.y —
    для определения колонки Дебет/Кредит (как раньше), bbox.x0/x1 и размер —
    для gate по ширине при переписывании строк "Назначение платежа" (см.
    process_kaspi_ip_pdf, purpose_line_bboxes на KaspiIPTransaction).
    Строится из get_text("dict"), а не из отдельного вызова get_text(), чтобы
    порядок и разбиение на строки совпадали с bbox гарантированно (единый
    источник).
    """
    result: List[Tuple[str, Optional[Tuple[float, float, float, float]], float]] = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            bbox = line.get("bbox")
            size = spans[0].get("size", 0.0) if spans else 0.0
            result.append((text, tuple(bbox) if bbox else None, size))
    return result
```

- [ ] **Step 2: Обновить единственный вызов в `_parse_transactions_from_page`**

Найти (в начале функции, строки ~499-500):

```python
    lines_with_y = _page_lines_with_y(page)
    lines_text = [t for t, _y in lines_with_y]
```

Заменить на:

```python
    lines_with_y = _page_lines_with_y(page)
    lines_text = [t for t, _bbox, _sz in lines_with_y]
```

Найти извлечение Y для классификации Кредит/Дебет (строка ~559):

```python
        amount_y = lines_with_y[i + 3][1]
        is_credit = amount_y is not None and amount_y < cd_threshold
```

Заменить на:

```python
        amount_bbox = lines_with_y[i + 3][1]
        amount_y = (amount_bbox[1] + amount_bbox[3]) / 2 if amount_bbox else None
        is_credit = amount_y is not None and amount_y < cd_threshold
```

- [ ] **Step 3: Собрать `purpose_line_bboxes` вместе с `purpose_parts`**

Найти текущий блок (строки ~571-590):

```python
        j = i + 4
        block_lines = []
        while j < len(lines_text):
            ln = lines_text[j]
            if _DOC_NUM_RE.match(ln) and j + 1 < len(lines_text) and _DATE_RE.search(lines_text[j + 1]):
                break
            if "Итого" in ln or "Входящий" in ln or "Исходящий" in ln:
                break
            block_lines.append(ln)
            j += 1

        # Назначение: пропускаем IBAN/BIN/BIC/КНП, берём остальное
        purpose_parts = []
        for ln in block_lines:
            if not ln:
                continue
            if _SKIP_RE.match(ln):
                continue
            purpose_parts.append(ln)
        purpose = " ".join(purpose_parts)
```

Заменить на (block_lines теперь хранит индексы, не текст, чтобы иметь доступ к bbox через `lines_with_y`):

```python
        j = i + 4
        block_line_idxs: List[int] = []
        while j < len(lines_text):
            ln = lines_text[j]
            if _DOC_NUM_RE.match(ln) and j + 1 < len(lines_text) and _DATE_RE.search(lines_text[j + 1]):
                break
            if "Итого" in ln or "Входящий" in ln or "Исходящий" in ln:
                break
            block_line_idxs.append(j)
            j += 1

        # Назначение: пропускаем IBAN/BIN/BIC/КНП, берём остальное. Заодно
        # запоминаем bbox+размер каждой вошедшей строки — нужно писателю
        # (process_kaspi_ip_pdf) для переписывания суммы внутри назначения,
        # см. purpose_line_bboxes.
        purpose_parts = []
        purpose_line_bboxes: List[Tuple[str, float, float, float, float]] = []
        for idx in block_line_idxs:
            ln = lines_text[idx]
            if not ln:
                continue
            if _SKIP_RE.match(ln):
                continue
            purpose_parts.append(ln)
            _bbox, _sz = lines_with_y[idx][1], lines_with_y[idx][2]
            if _bbox is not None:
                purpose_line_bboxes.append((ln, _bbox[0], _bbox[2], (_bbox[1] + _bbox[3]) / 2, _sz))
        purpose = " ".join(purpose_parts)
```

- [ ] **Step 4: Передать `purpose_line_bboxes` в `KaspiIPTransaction`**

Найти конструктор транзакции (строки ~605-616):

```python
        tx = KaspiIPTransaction(
            page_num=pg_idx,
            doc_number=line,
            date=date_str,
            amount=amount_val,
            amount_text=amount_text,
            is_credit=is_credit,
            is_scaleable=is_scaleable,
            purpose=purpose[:120],
            new_amount=amount_val,
            amount_in_purpose=amount_in_purpose,
        )
```

Добавить параметр:

```python
        tx = KaspiIPTransaction(
            page_num=pg_idx,
            doc_number=line,
            date=date_str,
            amount=amount_val,
            amount_text=amount_text,
            is_credit=is_credit,
            is_scaleable=is_scaleable,
            purpose=purpose[:120],
            new_amount=amount_val,
            amount_in_purpose=amount_in_purpose,
            purpose_line_bboxes=purpose_line_bboxes,
        )
```

- [ ] **Step 5: Добавить поле в dataclass**

Найти (строки ~216-221):

```python
    purpose: str              # Назначение платежа
    new_amount: float = 0.0
    # Сумма строки продублирована внутри «Назначения платежа» («…Сумма
    # 210 000-00 теңге…»). Такие строки НЕ масштабируются — см.
    # _purpose_repeats_amount и причину там же.
    amount_in_purpose: bool = False
```

Заменить на (заодно исправлен устаревший комментарий — с 2026-08-04 такие строки масштабируются наравне с остальными, заморозка была опробована и откачена, см. CLAUDE.md):

```python
    purpose: str              # Назначение платежа
    new_amount: float = 0.0
    # Сумма строки продублирована внутри «Назначения платежа» («…Сумма
    # 210 000-00 теңге…»). Масштабируются наравне с остальными (заморозка
    # таких строк пробовалась и откачена 2026-08-04 — см. CLAUDE.md,
    # ISI падал ниже жёсткого порога); при этом амбиции переписать саму
    # сумму И внутри текста назначения см. purpose_line_bboxes ниже и
    # docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md.
    amount_in_purpose: bool = False
    # (текст_строки, x0, x1, y_mid, font_size) для КАЖДОЙ визуальной строки,
    # вошедшей в purpose (до обрезки [:120]) — используется писателем
    # (process_kaspi_ip_pdf) чтобы локализовать и переписать цифровой прогон
    # суммы внутри назначения, если amount_in_purpose=True. НЕ пишется в PDF
    # напрямую — вспомогательные данные парсинга.
    purpose_line_bboxes: List[Tuple[str, float, float, float, float]] = field(default_factory=list)
```

- [ ] **Step 6: Добавить fixture-gated тест**

Существующий `tests/test_kaspi_ip_pdf_service.py` уже определяет (в начале файла):
```python
import kaspi_ip_pdf_service as k
FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = {
    "original": FIXTURES_DIR / "kaspi_ip_original.pdf",
    "scored": FIXTURES_DIR / "kaspi_ip_scored.pdf",
}
```
— модуль импортирован как `k` (не `kip`, как в `verify_kaspi_ip_file.py` — РАЗНЫЕ алиасы в разных файлах, использовать именно тот, что уже в файле, который редактируется). `FileNotFoundError`/`pymupdf.FileNotFoundError` → `pytest.skip` обрабатывается автоматически через `tests/conftest.py` (см. CLAUDE.md, "A missing fixture is a SKIP, not a failure") — открывать файл напрямую через `k` + `fitz.open`, ничего вручную не оборачивать в try/except.

Ни `kaspi_ip_original.pdf`, ни `kaspi_ip_scored.pdf` не гарантированно содержат строки с `amount_in_purpose=True` (измерено в design spec: у `IP2.pdf`/`IP3.pdf` таких строк 0 из реальных 4 локальных файлов) — тест обязан корректно пройти и в случае, если в фикстуре таких строк нет вовсе, не считать это провалом.

В конец `tests/test_kaspi_ip_pdf_service.py` добавить:

```python
def test_purpose_line_bboxes_locatable_when_amount_repeated():
    """Если в фикстуре есть строки с amount_in_purpose=True, каждая обязана
    иметь непустой purpose_line_bboxes (иначе process_kaspi_ip_pdf не сможет
    её переписать); большинство (не обязательно 100% — см. design spec,
    "Область охвата") обязаны быть locatable через _locate_purpose_amount.
    Если в фикстуре таких строк нет вовсе (как на IP2/IP3 в design spec) —
    тест тривиально проходит, ничего не проверяя."""
    doc = fitz.open(FIXTURES["original"])
    stmt = k.parse_kaspi_ip_statement(doc)
    doc.close()

    repeated = [t for t in stmt.transactions if t.amount_in_purpose]
    if not repeated:
        pytest.skip("в этой фикстуре нет строк с amount_in_purpose=True")

    locatable = 0
    for tx in repeated:
        assert tx.purpose_line_bboxes, f"пустой purpose_line_bboxes для {tx.doc_number}"
        if any(
            k._locate_purpose_amount(ln, tx.amount) is not None
            for ln, _x0, _x1, _y, _sz in tx.purpose_line_bboxes
        ):
            locatable += 1
    print(f"[note] {locatable}/{len(repeated)} строк с amount_in_purpose locatable")
    assert locatable > 0
```

`fitz` и `pytest` уже импортированы в начале `tests/test_kaspi_ip_pdf_service.py` (строки 18-19) — новых импортов не требуется.

- [ ] **Step 7: Прогнать тесты**

Run: `pytest tests/test_kaspi_ip_pdf_service.py tests/test_kaspi_ip_purpose_rewrite.py -v`
Expected: PASS (новый тест либо PASS на реальной фикстуре, либо SKIP при её отсутствии — в этом checkout'е фикстур нет, ожидается SKIP); Task 1 тесты остаются PASS (не должны были сломаться).

Run: `pytest tests/ -q`
Expected: столько же passed, сколько до Task 1 (Task 1 добавил 15 в тесты нового файла) + без новых FAIL. Skipped может вырасти на 1 (новый fixture-gated тест).

- [ ] **Step 8: Прогнать вручную на реальном файле (smoke test, не pytest)**

```bash
python -c "
import fitz
from kaspi_ip_pdf_service import parse_kaspi_ip_statement, _locate_purpose_amount

doc = fitz.open(r'C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP4.pdf')
stmt = parse_kaspi_ip_statement(doc)
doc.close()
repeated = [t for t in stmt.transactions if t.amount_in_purpose]
covered = sum(
    1 for t in repeated
    if any(_locate_purpose_amount(ln, t.amount) is not None for ln, *_ in t.purpose_line_bboxes)
)
print(f'IP4.pdf: {len(repeated)} repeated, {covered} locatable (ожидается 80 из 82, см. design spec)')
"
```

Expected: `IP4.pdf: 82 repeated, 80 locatable` — совпадает с измерением в design spec. Если число расходится — остановиться и разобраться до перехода к Task 3 (расхождение означает баг в сборе `purpose_line_bboxes`, а не в `_locate_purpose_amount`, который уже протестирован в Task 1).

- [ ] **Step 9: Commit**

```bash
git add kaspi_ip_pdf_service.py tests/test_kaspi_ip_pdf_service.py
git commit -m "feat(kaspi-ip): collect purpose-line bbox+font-size during parsing"
```

---

### Task 3: Писатель — очередь замен с gate по ширине + встраивание в `replace_tm`

**Files:**
- Modify: `kaspi_ip_pdf_service.py:process_kaspi_ip_pdf` — два места:
  1. Между `stmt = recalculate_kaspi_ip(...)` (строка ~1116) и `doc.close()` (строка ~1207) — построение очереди.
  2. Внутри `replace_tm`, ветка `if parsed_cell is None:` (строка ~1408-1409) — встраивание замены.

**Interfaces:**
- Consumes: `_locate_purpose_amount`, `_format_purpose_amount` (Task 1), `KaspiIPTransaction.purpose_line_bboxes` (Task 2)
- Produces: ничего наружу — расширяет `process_kaspi_ip_pdf`'s поведение изнутри. Нет изолированного unit-теста (нужен реальный многостраничный PDF с реальным CID-кодированием) — валидируется в Task 4 через `verify_kaspi_ip_file.py` на реальном корпусе.

- [ ] **Step 1: Построить очередь `page_replace_purpose` до `doc.close()`**

Найти в `process_kaspi_ip_pdf` (сразу после `stmt = recalculate_kaspi_ip(stmt, target_monthly_income)`, строка ~1116, до блока `# ─── Декодер/энкодер...`, строка ~1118):

```python
    stmt = parse_kaspi_ip_statement(doc)
    stmt = recalculate_kaspi_ip(stmt, target_monthly_income)
```

Добавить сразу после (перед строкой `# ─── Декодер/энкодер...`):

```python
    # ── Очередь замен для сумм, продублированных в тексте «Назначение
    # платежа» (см. docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md).
    # Строится ДО doc.close() — нужны bbox/размер строк purpose, собранные
    # при парсинге (tx.purpose_line_bboxes), и GLYPH_EM (уже посчитан выше).
    # Значение в очереди — (old_amount, new_amount): сам текст замены
    # достраивается позже, в replace_tm, ПОВТОРНЫМ вызовом
    # _locate_purpose_amount на РЕАЛЬНОМ decoded-тексте найденной Tj-строки
    # (не на копии, собранной здесь через PyMuPDF) — гарантирует байт-в-байт
    # точность вне заменяемого цифрового прогона, даже если PyMuPDF's
    # get_text("dict") и raw-байтовый decode когда-либо разойдутся в мелочи
    # (напр. невидимый пробел).
    page_replace_purpose: Dict[int, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
    _purpose_max_right = 0.0
    for _tx in stmt.transactions:
        for _ln, _x0, _x1, _y, _sz in _tx.purpose_line_bboxes:
            if _x1 > _purpose_max_right:
                _purpose_max_right = _x1

    def _adv_purpose(text: str, size: float) -> float:
        if GLYPH_EM:
            return sum(GLYPH_EM.get(c, _ARIAL_DIGIT_EM) for c in text) * size
        return sum(0.5 if c in (" ", ",", ".", "-") else 1.0 for c in text) * (_ARIAL_DIGIT_EM * size)

    for _tx in stmt.transactions:
        if not _tx.amount_in_purpose or not _tx.is_scaleable:
            continue
        if abs(_tx.new_amount - _tx.amount) < 0.005:
            continue
        _target_line = None
        _target_x0 = None
        _target_size = None
        for _ln, _x0, _x1, _y, _sz in _tx.purpose_line_bboxes:
            if _locate_purpose_amount(_ln, _tx.amount) is not None:
                _target_line, _target_x0, _target_size = _ln, _x0, _sz
                break
        if _target_line is None:
            continue  # сумма разорвана переносом строки — оставляем как есть

        _loc = _locate_purpose_amount(_target_line, _tx.amount)
        _start, _end, _th_sep, _dec_sep, _has_dec = _loc
        _new_amount_text = _format_purpose_amount(_tx.new_amount, _th_sep, _dec_sep, _has_dec)
        _new_line_preview = _target_line[:_start] + _new_amount_text + _target_line[_end:]
        _new_width = _adv_purpose(_new_line_preview, _target_size)

        if _target_x0 + _new_width > _purpose_max_right:
            continue  # gate: новая строка не влезает в эмпирический край — не трогаем

        page_replace_purpose[_tx.page_num][_target_line].append((_tx.amount, _tx.new_amount))
```

- [ ] **Step 2: Встроить замену в `replace_tm`**

Найти внутри `replace_tm` (строки ~1406-1409):

```python
            decoded = paren_decode(raw_content)
            parsed_cell = _parse_amount_cell(decoded)
            if parsed_cell is None:
                return match.group(0)
```

Заменить на:

```python
            decoded = paren_decode(raw_content)
            parsed_cell = _parse_amount_cell(decoded)
            if parsed_cell is None:
                _pq = page_replace_purpose.get(_pg, {}).get(decoded.strip())
                if _pq:
                    _old_amt, _new_amt = _pq.popleft()
                    _loc2 = _locate_purpose_amount(decoded, _old_amt)
                    if _loc2 is not None:
                        _s2, _e2, _th2, _dc2, _hd2 = _loc2
                        _new_amt_text = _format_purpose_amount(_new_amt, _th2, _dc2, _hd2)
                        _new_line = decoded[:_s2] + _new_amt_text + decoded[_e2:]
                        _new_line_bytes = paren_encode(_new_line)
                        if b'\x00\x00' not in _new_line_bytes:
                            total_replaced += 1
                            print(f"  [IP][purpose] стр.{_pg} {decoded.strip()[:60]!r} → {_new_line[:60]!r}")
                            _so2, _sc2 = _op_separators(match.group(0))
                            return (
                                b"1 0 0 1 " + match.group(1) + b" " + match.group(2) + b" Tm\n" +
                                font_name + b" " + font_size_str.encode("ascii") + b" Tf" + _so2 +
                                b"(" + _new_line_bytes + b")" + _sc2 + b"Tj"
                            )
                return match.group(0)
```

`total_replaced += 1` работает без повторного `nonlocal` — `nonlocal total_replaced` уже объявлен на строке ~1394, в начале `replace_tm`; вставленный код исполняется ВНУТРИ того же тела функции, значит уже находится под этим объявлением (повторное `nonlocal total_replaced` в том же теле функции синтаксически допустимо в Python, но избыточно — не добавлять).

`match.group(1)`/`match.group(2)` — это RAW bytes координат X/Y из оригинального совпадения (`x_str = match.group(1).decode("ascii")` на строке ~1395 подтверждает, что группы 1/2 — bytes, не str). Переиспользуются буквально, без прогона через `_fmt_coord`/`_fmt_coord_debet` — гарантирует байт-в-байт совпадение нотации X/Y с оригиналом (сильнее, чем `_fmt_coord(current_x)`, который лишь ЭМПИРИЧЕСКИ восстанавливает исходную запись для case «X не пересчитан» — здесь исходные байты просто не трогаются вовсе).

- [ ] **Step 3: Прогнать полный pytest**

Run: `pytest tests/ -q`
Expected: без регрессии к числу из Task 2 Step 7 (интеграционные изменения без новых юнит-тестов на этом шаге — валидация в Task 4).

- [ ] **Step 4: Smoke test на реальном файле**

```bash
python -c "
from kaspi_ip_pdf_service import process_kaspi_ip_pdf, parse_kaspi_ip_statement, validate_kaspi_ip
import fitz

raw = open(r'C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP4.pdf', 'rb').read()
doc = fitz.open(stream=raw, filetype='pdf')
stmt = parse_kaspi_ip_statement(doc)
n_months = len(set(t.date[3:] for t in stmt.transactions if t.is_credit and t.is_scaleable))
total = sum(t.amount for t in stmt.transactions if t.is_credit and t.is_scaleable)
target = (total / n_months) * 2

out = process_kaspi_ip_pdf(raw, target)
v = validate_kaspi_ip(out)
print('validate passed:', v['passed'])

# Перечитать текст результата и посчитать, сколько строк с продублированной
# суммой теперь СОВПАДАЮТ (текст содержит НОВУЮ сумму, а не старую).
out_doc = fitz.open(stream=out, filetype='pdf')
out_stmt = parse_kaspi_ip_statement(out_doc)
out_doc.close()
matched = 0
for tx_old, tx_new in zip(
    [t for t in stmt.transactions if t.amount_in_purpose],
    [t for t in out_stmt.transactions if t.amount_in_purpose],
):
    pass  # см. Task 4 check_purpose_amount_consistency для точного счёта
print('processed OK, see Task 4 for exact coverage count')
"
```

Expected: `validate passed: True`, без исключений. Точный подсчёт покрытия (80/82 и т.п.) — в Task 4, где для этого пишется постоянная проверка, а не разовый скрипт.

- [ ] **Step 5: Commit**

```bash
git add kaspi_ip_pdf_service.py
git commit -m "feat(kaspi-ip): rewrite duplicated amount inside purpose text (single-line, width-gated)"
```

---

### Task 4: Проверки — `check_purpose_amount_consistency` + измерение покрытия на реальном корпусе

**Files:**
- Modify: `tests/scripts/verify_kaspi_ip_file.py` (новая функция + подключение в `run_one`)
- Modify: `tests/scripts/verify_any_file.py` (подключение той же функции в `criteria_kaspi_ip`)

**Interfaces:**
- Consumes: `kaspi_ip_pdf_service.parse_kaspi_ip_statement`, `_locate_purpose_amount` (Task 1/2)
- Produces: `check_purpose_amount_consistency(orig_bytes: bytes, out_bytes: bytes) -> list[str]` — тот же контракт, что и остальные check-функции в этом файле (`check_bold_row_uniform` и т.п. в Halyk): список строк-сообщений, где `[guard]`-префикс — не провал, всё остальное — провал.

- [ ] **Step 1: Написать `check_purpose_amount_consistency` в `verify_kaspi_ip_file.py`**

Найти конец файла подобных check-функций (та же секция, где определены остальные `check_*` для Kaspi ИП — открыть файл и найти по образцу существующих сигнатур `def check_...(orig_bytes: bytes, out_bytes: bytes) -> list[str]:`) и добавить:

```python
def check_purpose_amount_consistency(orig_bytes: bytes, out_bytes: bytes) -> list[str]:
    """Для каждой транзакции ОРИГИНАЛА с amount_in_purpose=True, чья сумма при
    данной цели изменилась: либо текст назначения в РЕЗУЛЬТАТЕ теперь содержит
    НОВУЮ сумму (успех, без сообщения), либо это задокументированный skip
    (перенос строки / gate по ширине — см. design spec
    docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md)
    — помечается [guard]. Молчаливое расхождение (ни то, ни другое — текст
    остался со СТАРОЙ суммой без видимой причины) — FAIL.
    """
    orig_doc = fitz.open(stream=orig_bytes, filetype="pdf")
    orig_stmt = kip.parse_kaspi_ip_statement(orig_doc)
    orig_doc.close()

    out_doc = fitz.open(stream=out_bytes, filetype="pdf")
    out_stmt = kip.parse_kaspi_ip_statement(out_doc)
    out_doc.close()

    if len(orig_stmt.transactions) != len(out_stmt.transactions):
        return []  # разное число транзакций — не эта проверка должна это ловить

    issues = []
    for tx_o, tx_n in zip(orig_stmt.transactions, out_stmt.transactions):
        if not tx_o.amount_in_purpose or not tx_o.is_scaleable:
            continue
        if abs(tx_n.new_amount - tx_o.amount) < 0.005:
            continue  # сумма фактически не изменилась при этой цели

        locatable = any(
            kip._locate_purpose_amount(ln, tx_o.amount) is not None
            for ln, *_ in tx_o.purpose_line_bboxes
        )
        if not locatable:
            issues.append(
                f"[guard] {tx_o.doc_number}: сумма разорвана переносом строки в "
                f"назначении — переписывание вне рамок (design spec)"
            )
            continue

        new_digits = str(int(round(tx_n.new_amount)))
        old_digits = str(int(round(tx_o.amount)))
        new_out_purpose = " ".join(ln for ln, *_ in tx_n.purpose_line_bboxes)
        compact = re.sub(r"[ \u00a0]", "", new_out_purpose)
        has_new = re.search(r"(?<!\d)" + new_digits + r"(?!\d)", compact) is not None
        has_old = re.search(r"(?<!\d)" + old_digits + r"(?!\d)", compact) is not None

        if has_new and not has_old:
            continue  # успех — сумма переписана
        if has_old and not has_new:
            issues.append(
                f"{tx_o.doc_number}: назначение платежа всё ещё содержит СТАРУЮ "
                f"сумму {old_digits}, ожидалась новая {new_digits} — gate по "
                f"ширине должен был это отследить, но строка не попала ни в "
                f"successful rewrite, ни в guard"
            )
        # has_new and has_old одновременно, или ни то ни другое — тоже FAIL,
        # неоднозначный/неожиданный случай, требует ручного разбора.
        elif not (has_new and not has_old):
            issues.append(
                f"{tx_o.doc_number}: неоднозначный результат переписывания "
                f"назначения (old_present={has_old}, new_present={has_new})"
            )
    return issues
```

`kaspi_ip_pdf_service` в этом файле уже импортирован как `kip` (строка 46: `import kaspi_ip_pdf_service as kip  # noqa: E402`) — использовать этот алиас, он ПОДТВЕРЖДЁН, дополнительной сверки не требуется.

- [ ] **Step 2: Подключить в `run_one`, и заодно исправить отсутствующую фильтрацию `[guard]`**

`verify_kaspi_ip_file.py`, в отличие от `verify_halyk_file.py`, СЕЙЧАС НЕ фильтрует сообщения с префиксом `[guard]` из провала батареи — любое сообщение, вернувшееся из check-функции, немедленно считается FAIL (`geo_ok = len(geo_issues) == 0` без предварительной фильтрации). Без этого фикса `[guard]`-пометки `check_purpose_amount_consistency` (строки, разорванные переносом) уронят батарею на каждой такой строке, хотя это ожидаемый, задокументированный skip, а не баг.

Найти текущий блок в `run_one` (строки ~328-335):

```python
        geo_issues = (
            geometry_check(out_bytes, sample_pages=[1, 2])
            + style_check(raw, out_bytes)
            + check_isi_floor(out_bytes)
            + check_rounding_escalation(raw, out_bytes)
            + check_column_alignment(raw, out_bytes)
        )
        geo_ok = len(geo_issues) == 0
```

Заменить на:

```python
        geo_issues_all = (
            geometry_check(out_bytes, sample_pages=[1, 2])
            + style_check(raw, out_bytes)
            + check_isi_floor(out_bytes)
            + check_rounding_escalation(raw, out_bytes)
            + check_column_alignment(raw, out_bytes)
            + check_purpose_amount_consistency(raw, out_bytes)
        )
        # «[guard]» — не провал: случаи, чью неустранимость движок ДОКАЗАЛ
        # измерением (см. check_purpose_amount_consistency — перенос строки
        # разрывает сумму, переписывание вне рамок design spec). Показываем
        # в примечании, чтобы не потерялось, но не роняем ими батарею — та
        # же конвенция, что уже применена в verify_halyk_file.py.
        geo_issues = [i for i in geo_issues_all if not i.startswith("[guard]")]
        geo_ok = len(geo_issues) == 0
```

Найти ниже (строка ~341) строку, собирающую итоговое примечание:

```python
        note = "; ".join(math_issues + hdr_issues + geo_issues)
```

Заменить на (используя `geo_issues_all`, а не отфильтрованный `geo_issues`, — иначе `[guard]`-пометки пропадут из отчёта совсем, а не просто перестанут ронять батарею):

```python
        note = "; ".join(math_issues + hdr_issues + geo_issues_all)
```

- [ ] **Step 3: Подключить в `verify_any_file.py`**

`criteria_kaspi_ip` (строки 204-221) уже возвращает словарь `{ключ: [сообщения]}`, а `verify_any_file.py`'s общий цикл (см. Task 2026-08-06 более раннего сеанса — фильтр `[guard]`/`[glyph-patched]` в районе строки ~358) фильтрует ЛЮБОЙ ключ этого словаря автоматически, формат-независимо — отдельного фикса на фильтрацию здесь делать НЕ нужно, только подключить новый ключ.

Найти (строки 213-221):

```python
    return {
        "1 математика": list(kip.validate_kaspi_ip(out_bytes)["issues"]),
        "1b шапка = тело": vip.header_matches_body(out_bytes),
        "1c ISI-порог": vip.check_isi_floor(out_bytes),
        "2a наложения слов": overlaps,
        "2c правый край «Дебет»": vip.check_column_alignment(raw, out_bytes),
        "3c эскалация шага": vip.check_rounding_escalation(raw, out_bytes),
        "4 стиль": vip.style_check(raw, out_bytes),
    }
```

Заменить на:

```python
    return {
        "1 математика": list(kip.validate_kaspi_ip(out_bytes)["issues"]),
        "1b шапка = тело": vip.header_matches_body(out_bytes),
        "1c ISI-порог": vip.check_isi_floor(out_bytes),
        "2a наложения слов": overlaps,
        "2c правый край «Дебет»": vip.check_column_alignment(raw, out_bytes),
        "2d сумма в назначении": vip.check_purpose_amount_consistency(raw, out_bytes),
        "3c эскалация шага": vip.check_rounding_escalation(raw, out_bytes),
        "4 стиль": vip.style_check(raw, out_bytes),
    }
```

(`vip` — уже существующий в `verify_any_file.py` алиас для `verify_kaspi_ip_file` модуля, видно по остальным строкам этой же функции — использовать его, не вводить новый.)

- [ ] **Step 4: Прогнать полную battery на реальном корпусе, измерить покрытие**

```bash
python tests/scripts/verify_kaspi_ip_file.py \
  "C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP2.pdf" \
  "C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP3.pdf" \
  "C:\Users\Abylay\Desktop\testpdf\kaspiPay\IP4.pdf" \
  "C:\Users\Abylay\Desktop\testpdf\kaspiPay\kaspiIP.pdf" \
  --targets 0.6,1.05,2,5,20
```

Expected: exit code 0, 0 FAIL. На `IP4.pdf`/`kaspiIP.pdf` (файлы, где `amount_in_purpose` реально встречается) — `[guard]` пометки только для разорванных переносом строк (не более 2/82 и 1/16 соответственно, см. design spec), FAIL — 0. На `IP2.pdf`/`IP3.pdf` проверка не даёт вообще никаких сообщений (там `amount_in_purpose` не встречается ни разу — измерено в design spec).

Если FAIL появился — остановиться, не переходить к Task 5 без разбора: это означает, что gate пропустил строку, которая на самом деле не влезла, или наоборот отказал строке, которая должна была пройти — оба случая требуют разбора конкретной строки, не автоматического снятия проверки.

- [ ] **Step 5: `pytest tests/` — полный прогон**

Run: `pytest tests/ -q`
Expected: без регрессии к бейзлайну, зафиксированному в начале плана (Global Constraints).

- [ ] **Step 6: Commit**

```bash
git add tests/scripts/verify_kaspi_ip_file.py tests/scripts/verify_any_file.py
git commit -m "test(kaspi-ip): add check_purpose_amount_consistency, wire into both verify scripts"
```

---

### Task 5: Документация — закрыть пункт 7 в CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** нет (документация)

- [ ] **Step 1: Найти пункт 7 плана в CLAUDE.md**

Найти секцию `### 7. Давно известный незакрытый gap Kaspi ИП (не из вчерашних партий)` (ближе к началу файла, в блоке «План на 2026-08-05»).

- [ ] **Step 2: Заменить на «ЗАКРЫТО» с измеренными числами из Task 4**

Формат — по образцу уже существующих в файле секций «ЗАКРЫТО 2026-08-05»/«ЗАКРЫТО 2026-08-06» (см. пункты 1 и 3 в том же файле): что было, что сделано, чем измерено, какие числа получились по факту прогона Task 4 (не гадать — взять реальный вывод battery). Обязательно указать:
- Архитектурное решение (расширение `replace_tm`, gate по эмпирической ширине, without X/Y recompute).
- Реальное измеренное покрытие после Task 4 (сколько `[guard]` из скольки affected на `IP4.pdf`/`kaspiIP.pdf`).
- Что осталось вне рамок (разрыв строки переносом — тот же текст, что уже в design spec, "Область охвата").
- Ссылку на `docs/superpowers/specs/2026-08-06-kaspi-ip-purpose-amount-rewrite-design.md`.

- [ ] **Step 3: Финальный полный регресс**

Run: `pytest tests/ -q`
Expected: PASS, число не меньше бейзлайна.

Run: полная battery `verify_any_file.py` на всех 4 файлах Kaspi ИП × целях 0.6/1.05/2/5/20 — exit 0, 0 FAIL.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: close item 7 (Kaspi IP purpose-text amount duplication)"
```

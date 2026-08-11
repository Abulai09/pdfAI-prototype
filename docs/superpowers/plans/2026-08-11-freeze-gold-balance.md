# Kaspi Gold: заморозка баланса/справки, расход — компенсирующая переменная — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** В Kaspi Gold (`/process`, upscale и downscale) «Доступно на …» и справочные ₸/USD/EUR (cert-формат) перестают пересчитываться и остаются байт-в-байт оригиналом; расход становится величиной, которая растёт/падает вместе с доходом, чтобы тождество `начало + приход − расход = конец` продолжало сходиться с ЗАМОРОЖЕННЫМ `конец`.

**Architecture:** `balance_end` больше не производится из `income − expense` — он читается из оригинала и никогда не переписывается. Вместо этого `new_total_expense := balance_start + new_total_income − balance_end(оригинал)` вычисляется НАПРЯМУЮ, и уже он определяет, во сколько раз растут расходные транзакции и категории шапки. Мид-периодная просадка running balance чинится не подъёмом дохода (это двигало бы `balance_end`), а переносом расходного прироста во времени: с транзакций до дня дефицита — на транзакции после него, при неизменной общей сумме расхода.

**Tech Stack:** Python 3, PyMuPDF (`fitz`), pytest. Правки только в `pdf_service.py` и `pdf_service_downscale.py` (плюс `tests/scripts/verify_gold_file.py` и юнит-тесты). Halyk/Kaspi ИП не трогаются.

## Global Constraints

- Единственный источник истины для «замороженного» значения — `stmt.balance_end` / `cert.balance_kzt` / `cert.balance_usd` / `cert.balance_eur`, как распарсены из ВХОДНОГО файла. Никакой код не должен присваивать им новое значение.
- Все денежные суммы округляются до 2 знаков (`round(x, 2)`), кроме зарплатных transactions, которые продолжают идти через `_round_to_natural(val, original=tx.amount)` — это НЕ меняется этим планом.
- Расходные транзакции (`sign == -1`) в этом плане впервые становятся масштабируемыми — раньше `tx.new_amount = tx.amount` для них было безусловным. Округление для НИХ — `round(x, 2)` (не `_round_to_natural`) до отдельной проверки на реальных файлах (см. Task 3, шаг с TODO-комментарием в коде, а не в этом плане — комментарий явно фиксирует, что это временное решение до замера).
- Halyk (`halyk_pdf_service.py`) и Kaspi ИП (`kaspi_ip_pdf_service.py`) вне охвата — не менять.
- Каждая задача заканчивается зелёными тестами и коммитом. Не переходи к следующей задаче, пока текущая не прошла `pytest`.
- Дизайн-документ: `docs/superpowers/specs/2026-08-11-freeze-gold-balance-design.md` — при любом расхождении между планом и памятью о требованиях сверяться с ним.

---

## File Structure

- **Modify `pdf_service.py`:**
  - Новые функции `_scale_expense_categories()` и `_scale_debit_transactions_exact()` — рядом с `_round_to_natural()` (после строки ~545), в разделе разбора/пересчёта.
  - `recalculate_statement()` (строки 1197-1509 в текущем виде) — переписывается: убирается вся логика «расходы не масштабируем» и старый «Шаг 3» (подъём дохода), добавляется вызов новых функций и новая коррекция просадки через перенос расхода во времени.
  - `build_cert_replacement_entries()` (строка 1715) — добавляется guard «не ставить в очередь, если `new_val == old_val`».
  - `process_pdf_bytes_raw()` (строка 2085) — блок построения `replacement_queue` (строки ~2189-2247): добавляется блок для категорий расхода; блок `BALANCE_END` получает тот же guard «не ставить, если совпадает».
  - `HeaderCellOverflowError`-проверка внутри `recalculate_statement()` (строки ~1470-1482) — убирается `balance_end` из проверяемых полей, добавляется цикл по `stmt.new_expense_categories`.

- **Modify `pdf_service_downscale.py`:**
  - `recalculate_statement_downscale()` (строки 154-387) — то же самое изменение формулы и коррекции, что и в `recalculate_statement`, плюс новый floor `new_total_expense >= 0` взамен части логики `SAFETY_MARGIN`/`post_check_negative_balance`, завязанной на баланс.
  - `compute_min_target_monthly_income()` (строка 121) — форму floor не меняем (он всё ещё защищает от отрицательного `new_total_expense`, просто теперь это буквальный смысл формулы, а не приближение через баланс) — переиспользуется как есть, без изменений кода, только докстринг обновляется в рамках Task 6.

- **Modify `tests/test_pdf_service.py`:**
  - `test_humped_income_recalculates_without_floor` (строка ~706) переписывается — «горб» теперь чинится переносом расхода, а не подъёмом дохода; проверка на `nov_salary > 4_300_000.0` заменяется проверкой, что `balance_end` не изменился и ноябрьский расход вырос.
  - `test_downscale_preserves_balance_equation_expense_not_category_sum` (строка 585) — обновляется: раньше проверял, что категории НЕ меняются, теперь должен проверять, что категории меняются ПРОПОРЦИОНАЛЬНО и их сумма точно равна новому `total_expense`.
  - Новые тесты для `_scale_expense_categories`, `_scale_debit_transactions_exact`, и для инварианта заморозки (`recalculate_statement` не меняет `balance_end`).

- **Modify `tests/scripts/verify_gold_file.py`:**
  - Новая функция `check_balance_frozen()` — байтовое/числовое сравнение `balance_end`/cert-балансов до и после.
  - Новая функция `check_expense_categories_sum()` — сумма категорий шапки после обработки равна `new_total_expense`.
  - Обе подключаются в `run_one()`.

---

### Task 1: `_scale_expense_categories` — точное пропорциональное масштабирование категорий шапки

**Files:**
- Modify: `pdf_service.py` (добавить функцию после `_round_to_natural`, то есть после строки ~545)
- Test: `tests/test_pdf_service.py` (новый блок в конце файла)

**Interfaces:**
- Produces: `_scale_expense_categories(categories: Dict[str, float], new_total_expense: float, old_total_expense: float) -> Dict[str, float]` — публично не экспортируется (модульный `_`-префикс, как остальные хелперы файла), но используется из `recalculate_statement`/`recalculate_statement_downscale` в Task 3 и Task 6.

- [ ] **Step 1: Написать падающий тест**

```python
# в конце tests/test_pdf_service.py

def test_scale_expense_categories_sum_matches_target_exactly():
    """Сумма отмасштабированных категорий должна ТОЧНО равняться
    new_total_expense, даже если сами категории (из-за дублирующихся меток
    в шапке — см. docstring парсера) не суммировались в старый total_expense.
    Последняя категория по порядку словаря получает остаток."""
    categories = {"Переводы": 300_000.0, "Покупки": 100_000.0, "Снятия": 50_000.0}
    # Σ(categories) = 450000, но old_total_expense (из уравнения баланса) = 500000 —
    # намеренное расхождение, как в реальных файлах с дублирующимися метками.
    result = p._scale_expense_categories(
        categories, new_total_expense=1_000_000.0, old_total_expense=500_000.0
    )
    assert set(result.keys()) == set(categories.keys())
    assert round(sum(result.values()), 2) == 1_000_000.0


def test_scale_expense_categories_empty_when_no_categories():
    assert p._scale_expense_categories({}, 1000.0, 500.0) == {}


def test_scale_expense_categories_empty_when_old_total_zero():
    assert p._scale_expense_categories({"Переводы": 100.0}, 1000.0, 0.0) == {}
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `pytest tests/test_pdf_service.py -k test_scale_expense_categories -v`
Expected: FAIL с `AttributeError: module 'pdf_service' has no attribute '_scale_expense_categories'`

- [ ] **Step 3: Реализовать функцию**

Добавить в `pdf_service.py` сразу после конца `_round_to_natural` (после строки ~545, перед следующим `def`):

```python
def _scale_expense_categories(
    categories: Dict[str, float], new_total_expense: float, old_total_expense: float
) -> Dict[str, float]:
    """Пропорционально масштабирует категории расхода шапки стр.1 («Переводы»,
    «Покупки», «Снятия», «Разное») так, чтобы их сумма ТОЧНО совпадала с
    new_total_expense — не приближённо, потому что баланс на конец периода
    теперь заморожен (см. recalculate_statement), и сумма видимых категорий
    обязана сходиться с балансовым тождеством, которое реальный ревьюер может
    проверить сложением четырёх строк шапки.

    old_total_expense — это stmt.total_expense (значение из УРАВНЕНИЯ баланса
    при парсинге, см. parse_full_statement), а НЕ Σ(categories.values()) — эти
    две суммы могут расходиться на реальном файле из-за дублирующихся меток
    («Переводы» и «Переводы на свои счета» перезаписывают друг друга в dict
    при парсинге). Коэффициент считается от надёжного old_total_expense;
    расхождение между ним и Σ(categories) поглощается последней категорией
    вместе с обычным остатком округления — на практике оно мало (см. комментарий
    в parse_full_statement про effective_expense).

    Последняя категория (по порядку словаря — совпадает с порядком появления
    в PDF, см. parse_full_statement) получает остаток вместо своей доли —
    тот же приём распределения остатка, что уже используется в проекте
    (например, в build_cert_replacement_entries для округления валют).
    """
    if not categories or old_total_expense <= 0:
        return {}
    factor = new_total_expense / old_total_expense
    keys = list(categories.keys())
    result: Dict[str, float] = {}
    running = 0.0
    for i, cat in enumerate(keys):
        if i == len(keys) - 1:
            result[cat] = round(new_total_expense - running, 2)
        else:
            val = round(categories[cat] * factor, 2)
            result[cat] = val
            running += val
    return result
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `pytest tests/test_pdf_service.py -k test_scale_expense_categories -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add pdf_service.py tests/test_pdf_service.py
git commit -m "feat(gold): add _scale_expense_categories for exact-sum category scaling"
```

---

### Task 2: `_scale_debit_transactions_exact` — масштабирование расходных транзакций с точной суммой

**Files:**
- Modify: `pdf_service.py` (добавить функцию сразу после `_scale_expense_categories`)
- Test: `tests/test_pdf_service.py`

**Interfaces:**
- Consumes: `Transaction` (уже определён), `random` (уже импортирован в `pdf_service.py`)
- Produces: `_scale_debit_transactions_exact(transactions: List[Transaction], target_total: float, noise: bool = True) -> None` — мутирует `tx.new_amount` ПО МЕСТУ для всех `tx.sign == -1`; используется из `recalculate_statement`/`recalculate_statement_downscale` (Task 3, Task 6).

- [ ] **Step 1: Написать падающий тест**

```python
def test_scale_debit_transactions_exact_sum_matches_target():
    random.seed(1)
    txs = [
        p.Transaction(index=0, sign=-1, amount=100_000.0, date="10.01.26"),
        p.Transaction(index=1, sign=-1, amount=50_000.0, date="11.01.26"),
        p.Transaction(index=2, sign=-1, amount=10_000.0, date="12.01.26"),
        p.Transaction(index=3, sign=1, amount=999_999.0, date="13.01.26"),  # доход — не трогаем
    ]
    p._scale_debit_transactions_exact(txs, target_total=320_000.0)
    debit_sum = round(sum(t.new_amount for t in txs if t.sign == -1), 2)
    assert debit_sum == 320_000.0
    # доходная транзакция не тронута функцией (new_amount остаётся дефолтным 0.0)
    assert txs[3].new_amount == 0.0


def test_scale_debit_transactions_exact_noop_when_no_debits():
    txs = [p.Transaction(index=0, sign=1, amount=1000.0)]
    p._scale_debit_transactions_exact(txs, target_total=5000.0)
    assert txs[0].new_amount == 0.0  # не упало, ничего не сделало


def test_scale_debit_transactions_exact_without_noise_is_deterministic():
    txs_a = [
        p.Transaction(index=0, sign=-1, amount=100_000.0, date="10.01.26"),
        p.Transaction(index=1, sign=-1, amount=50_000.0, date="11.01.26"),
    ]
    txs_b = [
        p.Transaction(index=0, sign=-1, amount=100_000.0, date="10.01.26"),
        p.Transaction(index=1, sign=-1, amount=50_000.0, date="11.01.26"),
    ]
    p._scale_debit_transactions_exact(txs_a, target_total=300_000.0, noise=False)
    p._scale_debit_transactions_exact(txs_b, target_total=300_000.0, noise=False)
    assert [t.new_amount for t in txs_a] == [t.new_amount for t in txs_b]
    assert round(sum(t.new_amount for t in txs_a), 2) == 300_000.0
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `pytest tests/test_pdf_service.py -k test_scale_debit_transactions_exact -v`
Expected: FAIL с `AttributeError`

- [ ] **Step 3: Реализовать**

```python
def _scale_debit_transactions_exact(
    transactions: List["Transaction"], target_total: float, noise: bool = True
) -> None:
    """Масштабирует ВСЕ расходные транзакции (sign == -1) пропорционально так,
    чтобы Σ(new_amount) ТОЧНО равнялась target_total — баланс на конец периода
    теперь заморожен (см. recalculate_statement), и сумма расходов обязана
    попасть в цель без остатка от округления/шума.

    Округление — round(x, 2), НЕ _round_to_natural: в отличие от зарплатных
    пополнений, у которых реальные суммы всегда целые тенге на «человеческом»
    шаге (см. докстринг _round_to_natural), для расходных транзакций Kaspi
    Gold это не подтверждено измерением на реальном файле — расходы (покупки,
    переводы, снятия) правдоподобно несут копейки, как обычные чеки. Решение
    временное: см. TODO в комментарии ниже, куда смотреть при появлении
    доступа к реальным файлам testpdf/gold.

    Транзакция с наибольшей исходной amount получает остаток вместо своей
    шумной доли — минимизирует относительное искажение (то же соображение,
    что и в _scale_expense_categories).
    """
    debit_txs = [t for t in transactions if t.sign == -1]
    if not debit_txs:
        return
    current_total = sum(t.amount for t in debit_txs)
    if current_total <= 0:
        return
    factor = target_total / current_total
    largest = max(debit_txs, key=lambda t: t.amount)
    running = 0.0
    for tx in debit_txs:
        if tx is largest:
            continue
        # TODO(проверить на реальных файлах testpdf/gold): если окажется, что
        # реальные расходные суммы Kaspi Gold всегда целые тенге (как salary),
        # заменить round(x, 2) на _round_to_natural(x, original=tx.amount) —
        # см. критерий 3 в CLAUDE.md.
        epsilon = random.uniform(-0.03, 0.03) if noise else 0.0
        tx.new_amount = round(tx.amount * factor * (1 + epsilon), 2)
        running += tx.new_amount
    largest.new_amount = round(target_total - running, 2)
```

- [ ] **Step 4: Запустить, убедиться что проходит**

Run: `pytest tests/test_pdf_service.py -k test_scale_debit_transactions_exact -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add pdf_service.py tests/test_pdf_service.py
git commit -m "feat(gold): add _scale_debit_transactions_exact for exact-sum expense scaling"
```

---

### Task 3: Переписать `recalculate_statement` — заморозка баланса, расход как производная

**Files:**
- Modify: `pdf_service.py:1197-1509` (полностью заменить тело `recalculate_statement`)
- Test: `tests/test_pdf_service.py` (обновить `test_humped_income_recalculates_without_floor`, добавить новые тесты заморозки)

**Interfaces:**
- Consumes: `_scale_expense_categories` (Task 1), `_scale_debit_transactions_exact` (Task 2), уже существующие `min_dayend_balance`, `first_negative_dayend`, `_date_sort_key`, `_get_month_key`, `_round_to_natural`, `_HEADER_CELL_MAX_SAFE_VALUE`, `HeaderCellOverflowError`, `IncomeTooLowError` (импортируется лениво из `pdf_service_downscale`, как и сейчас).
- Produces: `recalculate_statement(stmt: StatementData, target_monthly_income: float) -> StatementData` — та же сигнатура, что и сейчас. Гарантирует после вызова: `stmt.new_balance_end == stmt.balance_end` (с точностью до `1e-9`, т.е. буквально то же число), `stmt.new_expense_categories` заполнен через `_scale_expense_categories`, `Σ(sign==-1 tx.new_amount) == stmt.balance_start + stmt.new_total_income - stmt.balance_end` (с точностью 0.01).

- [ ] **Step 1: Написать падающие тесты (заменяют старый `test_humped_income_recalculates_without_floor` и добавляют новые)**

Найти в `tests/test_pdf_service.py` существующий `test_humped_income_recalculates_without_floor` (строки ~706-747) и ЗАМЕНИТЬ его целиком на:

```python
def test_humped_income_no_longer_needs_floor_expense_absorbs_it():
    """«Горбатый» доход: крупный зарплатный месяц тратится в том же месяце.
    РАНЬШЕ движок поднимал зарплату (Шаг 3), что двигало balance_end — теперь
    balance_end заморожен, поэтому просадку чинит перенос РАСХОДА во времени:
    расход месяца-«горба» растёт МЕНЬШЕ, чем позднее (после дня дефицита)
    расходы, и итоговый баланс не сдвигается ни на тенге."""
    random.seed(0)
    txs = [
        _tx_full("20.12.25", -1, 1_900_000.0, description="Покупка"),
        _tx_full("10.12.25", +1, 2_000_000.0, is_salary=True, description="Пополнение"),
        _tx_full("20.11.25", -1, 7_500_000.0, description="Перевод"),
        _tx_full("10.11.25", +1, 8_000_000.0, is_salary=True, description="Пополнение"),
        _tx_full("20.07.25", -1, 1_900_000.0, description="Покупка"),
        _tx_full("10.07.25", +1, 2_000_000.0, is_salary=True, description="Пополнение"),
    ]
    stmt = p.StatementData(
        balance_start=100_000.0,
        balance_end=800_000.0,          # 100k + 12M(salary) - 11.3M(expense) — ЗАМОРОЖЕН
        total_income=12_000_000.0,
        total_expense=11_300_000.0,
        transactions=txs,
    )
    target = 4_200_000.0

    result = p.recalculate_statement(stmt, target_monthly_income=target)

    # balance_end НЕ ИЗМЕНИЛСЯ — это и есть новый инвариант
    assert result.new_balance_end == 800_000.0
    # Баланс ≥ 0 на всех границах дней после коррекции
    min_rb, final_rb = p.min_dayend_balance(result.transactions, result.balance_start, "new_amount")
    assert min_rb >= -0.01, f"баланс ушёл в минус: {min_rb}"
    assert round(final_rb, 2) == 800_000.0
    # Тождество: Σ(sign=-1 new_amount) == balance_start + new_total_income - balance_end
    debit_sum = round(sum(t.new_amount for t in result.transactions if t.sign == -1), 2)
    expected_expense = round(result.balance_start + result.new_total_income - result.new_balance_end, 2)
    assert debit_sum == expected_expense


def test_recalculate_statement_freezes_balance_end_on_plain_upscale():
    """Даже без просадки (обычный upscale без коррекции) balance_end не двигается,
    а расходные транзакции вырастают, чтобы компенсировать выросший доход."""
    random.seed(2)
    txs = [
        _tx_full("10.01.26", -1, 300_000.0, description="Покупка"),
        _tx_full("05.01.26", +1, 1_000_000.0, is_salary=True, description="Пополнение"),
    ]
    stmt = p.StatementData(
        balance_start=500_000.0,
        balance_end=1_200_000.0,  # 500k + 1M - 300k
        total_income=1_000_000.0,
        total_expense=300_000.0,
        transactions=txs,
    )
    result = p.recalculate_statement(stmt, target_monthly_income=2_000_000.0)

    assert result.new_balance_end == 1_200_000.0
    debit_tx = next(t for t in result.transactions if t.sign == -1)
    assert debit_tx.new_amount > 300_000.0, "расход должен был вырасти вместе с доходом"


def test_recalculate_statement_scales_expense_categories_proportionally():
    random.seed(3)
    txs = [
        _tx_full("10.01.26", -1, 300_000.0, description="Покупка"),
        _tx_full("05.01.26", +1, 1_000_000.0, is_salary=True, description="Пополнение"),
    ]
    stmt = p.StatementData(
        balance_start=500_000.0,
        balance_end=1_200_000.0,
        total_income=1_000_000.0,
        total_expense=300_000.0,
        expense_categories={"Покупки": 300_000.0},
        transactions=txs,
    )
    result = p.recalculate_statement(stmt, target_monthly_income=2_000_000.0)

    expected_expense = round(result.balance_start + result.new_total_income - result.new_balance_end, 2)
    assert round(sum(result.new_expense_categories.values()), 2) == expected_expense
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `pytest tests/test_pdf_service.py -k "test_humped_income_no_longer_needs_floor_expense_absorbs_it or test_recalculate_statement_freezes_balance_end_on_plain_upscale or test_recalculate_statement_scales_expense_categories_proportionally" -v`
Expected: FAIL (старая формула ещё производит `balance_end` из income/expense, а не наоборот)

- [ ] **Step 3: Переписать `recalculate_statement`**

Заменить весь блок `pdf_service.py:1197-1509` (от `def recalculate_statement` до финального `return stmt` этой функции) на:

```python
def recalculate_statement(stmt: StatementData, target_monthly_income: float) -> StatementData:
    """
    Пересчитывает выписку с единым коэффициентом на весь период.

    1. Группирует SALARY транзакции (Пополнение) по месяцам.
    2. Единый K = target_monthly_income / текущий_ср._доход_мес, применяется
       РАВНОМЕРНО ко всем месяцам (см. комментарий в git-истории про
       «выравнивание убивало естественный разброс дохода»).
    3. Применяет K × (1 ± ε) к каждой зарплатной транзакции.
    4. balance_end ЗАМОРОЖЕН (== stmt.balance_end оригинала, не пересчитывается
       вовсе) — «Доступно на …» и справка ₸/USD/EUR (cert-формат) остаются
       байт-в-байт оригиналом. Расход становится ПРОИЗВОДНОЙ величиной:
       new_total_expense := balance_start + new_total_income − balance_end,
       и именно он определяет, во сколько раз растут расходные транзакции
       и категории шапки (см. _scale_debit_transactions_exact,
       _scale_expense_categories).
    5. Возвраты (is_refund=True, sign=+1) НЕ масштабируются.
    6. Если running balance просаживается ниже нуля в середине периода — не
       поднимаем зарплату (это раньше двигало balance_end, что теперь
       запрещено), а ПЕРЕНОСИМ часть расходного прироста ПОЗЖЕ дня дефицита
       — общая сумма расхода не меняется, меняется только распределение во
       времени.

    ФОРМУЛЫ ИТОГОВ (как в Kaspi):
      total_income  = Σ(salary tx.new_amount)  — без возвратов!
      total_expense = balance_start + new_total_income − balance_end (ФИКСИРОВАН)
      balance_end   = stmt.balance_end оригинала (НЕ ПЕРЕСЧИТЫВАЕТСЯ)
    """
    salary_transactions = [t for t in stmt.transactions if t.is_salary]
    refund_transactions = [t for t in stmt.transactions if t.is_refund]

    if not salary_transactions:
        print("[Engine] ⚠️ Не найдено зарплатных транзакций")
        return stmt

    current_salary_income = sum(t.amount for t in salary_transactions)
    current_refund_total = sum(t.amount for t in refund_transactions)

    # ── Группировка SALARY доходов по месяцам ──
    monthly_income: Dict[str, float] = {}
    for tx in salary_transactions:
        mk = _get_month_key(tx.date) or "unknown"
        monthly_income[mk] = monthly_income.get(mk, 0) + tx.amount

    n_months = len([k for k in monthly_income if k != "unknown"]) or 1
    current_monthly_avg = current_salary_income / max(n_months, 1)
    global_K = target_monthly_income / current_monthly_avg

    if current_monthly_avg <= 0:
        print("[Engine] ⚠️ Текущий доход = 0")
        return stmt

    if global_K < 1.0:
        print(f"[Engine] ⚠️ Цель ({target_monthly_income:,.0f}) < текущего дохода/мес "
              f"({current_monthly_avg:,.0f}). K={global_K:.4f} < 1 — клипуем до 1.0")
        global_K = 1.0

    print(f"\n{'═' * 60}")
    print(f"  ДВИЖОК ПЕРЕСЧЁТА (единый K, balance_end заморожен)")
    print(f"{'═' * 60}")
    print(f"  Текущий ср. зарплатный/мес: {current_monthly_avg:>14,.2f} ₸")
    print(f"  Целевой доход/мес:          {target_monthly_income:>14,.2f} ₸")
    print(f"  Глобальный K:               {global_K:>14.4f}")
    print(f"  Месяцев в выписке:          {n_months}")
    print(f"  Зарплатных транзакций:      {len(salary_transactions)}")
    print(f"  Возвратов (не масштабируем): {len(refund_transactions)} (Σ={current_refund_total:,.2f} ₸)")
    print(f"{'═' * 60}")

    # ── Единый K на весь период (НЕ помесячный) — см. git log для истории бага ──
    month_K: Dict[str, float] = {mk: global_K for mk in monthly_income}

    # ── Шаг 1: Масштабирование зарплатных транзакций ──
    for tx in stmt.transactions:
        if tx.sign == 1 and tx.is_salary and not tx.is_refund:
            mk = _get_month_key(tx.date) or "unknown"
            k = month_K.get(mk, global_K)
            epsilon = random.uniform(-0.03, 0.03)
            tx.new_amount = _round_to_natural(tx.amount * k * (1 + epsilon), original=tx.amount)
        elif tx.sign == 1:
            # Возвраты и прочий non-salary доход — без изменений
            tx.new_amount = tx.amount
        # sign == -1 (расход) обрабатывается ниже, отдельным проходом

    salary_income_pos = sum(
        tx.new_amount for tx in stmt.transactions if tx.is_salary and not tx.is_refund
    )
    refund_topups_neg = sum(
        tx.amount for tx in stmt.transactions
        if tx.description == "Пополнение" and tx.sign == -1
    )
    stmt.new_total_income = round(salary_income_pos - refund_topups_neg, 2)

    # ── balance_end заморожен: используем ОРИГИНАЛЬНОЕ значение, никогда не
    # присваиваем ему ничего производного от income/expense. ──
    stmt.new_balance_end = stmt.balance_end

    # ── Расход — производная величина: столько, сколько нужно, чтобы с
    # ЗАМОРОЖЕННЫМ balance_end тождество баланса продолжало сходиться. ──
    target_total_expense = round(
        stmt.balance_start + stmt.new_total_income - stmt.new_balance_end, 2
    )
    if target_total_expense < 0:
        # При upscale (K >= 1.0) невозможно: new_total_income >= исходного,
        # значит target_total_expense >= исходного total_expense >= 0. Защита
        # оставлена на случай будущих вызовов с необычными входными данными.
        from pdf_service_downscale import IncomeTooLowError  # локальный импорт — избегаем цикла

        raise IncomeTooLowError(
            min_target_monthly_income=target_monthly_income,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="post_check_negative_balance",
            message=(
                f"При замороженном балансе ({stmt.balance_end:,.0f} ₸) и "
                f"доходе {stmt.new_total_income:,.0f} ₸ требуемый расход "
                f"({target_total_expense:,.0f} ₸) отрицателен."
            ),
        )

    _scale_debit_transactions_exact(stmt.transactions, target_total_expense)

    # ── Running balance ──
    current_rb = stmt.balance_start
    for tx in reversed(stmt.transactions):
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        tx.new_balance_after = current_rb

    # ── Коррекция просадки: ПЕРЕНОСИМ расходный прирост во времени, а НЕ
    # поднимаем доход (это раньше двигало balance_end — теперь запрещено).
    # У расходной транзакции ДО дня дефицита ("донор") откатываем часть
    # прироста к оригиналу; у расходной транзакции ПОСЛЕ дня дефицита
    # ("приёмник") на ту же величину прирост увеличиваем — общая сумма
    # расхода не меняется ни на тенге, меняется только момент, когда каждый
    # тенге прироста "потрачен" в хронологии. ──
    individual_income_total = sum(tx.amount for tx in stmt.transactions if tx.sign == 1)
    individual_expense_total = sum(tx.amount for tx in stmt.transactions if tx.sign == -1)
    original_min_rb_deficit = individual_income_total + stmt.balance_start - individual_expense_total
    min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")

    if min_rb < 0 and original_min_rb_deficit >= -1.0:
        print(f"\n  ⚠️ Баланс уходил в минус: {min_rb:,.2f} ₸ (первый дефицитный день ~{neg_date})")
        print(f"  Корректируем: переносим расходный прирост с дней ДО дефицита на дни ПОСЛЕ")
        prev_min = None
        for attempt in range(80):
            min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")
            if min_rb >= -0.01:
                print(f"  ✅ Баланс скорректирован за {attempt} итераций, мин: {min_rb:,.2f} ₸")
                break
            neg_key = _date_sort_key(neg_date)
            donors = [
                tx for tx in stmt.transactions
                if tx.sign == -1 and _date_sort_key(tx.date) <= neg_key
                and tx.new_amount > tx.amount + 0.01
            ]
            receivers = [
                tx for tx in stmt.transactions
                if tx.sign == -1 and _date_sort_key(tx.date) > neg_key
            ]
            if not donors or not receivers:
                print(f"  ⚠️ Нет донора/приёмника для переноса на {neg_date} — коррекция невозможна")
                break
            donor = max(donors, key=lambda t: t.new_amount - t.amount)
            receiver = max(receivers, key=lambda t: t.amount)
            step = min(donor.new_amount - donor.amount, max(1000.0, 0.05 * donor.new_amount))
            donor.new_amount = round(donor.new_amount - step, 2)
            receiver.new_amount = round(receiver.new_amount + step, 2)

            current_rb = stmt.balance_start
            for tx in reversed(stmt.transactions):
                current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
                tx.new_balance_after = current_rb
            min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")

            if prev_min is not None and min_rb <= prev_min + 0.01:
                print(f"  ⚠️ Коррекция не сходится (мин застрял на {min_rb:,.2f} ₸) — стоп")
                break
            prev_min = min_rb
        else:
            print(f"  ⚠️ Коррекция не сошлась за 80 итераций, мин: {min_rb:,.2f} ₸")
    elif min_rb < 0:
        print(f"\n  ℹ️ Running balance минус ({min_rb:,.2f} ₸) — структурная особенность PDF."
              f" Оригинал тоже дефицитен ({original_min_rb_deficit:,.2f} ₸). Не корректируем.")

    if min_rb < -0.01 and original_min_rb_deficit >= -1.0:
        from pdf_service_downscale import IncomeTooLowError  # локальный импорт — избегаем цикла

        new_min = max(target_monthly_income, current_monthly_avg) * 1.10
        raise IncomeTooLowError(
            min_target_monthly_income=new_min,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="post_check_negative_balance",
            message=(
                f"Не удалось удержать неотрицательный running balance при "
                f"{target_monthly_income:,.0f} ₸/мес с замороженным итоговым "
                f"балансом (получилось min={min_rb:,.0f} ₸). Минимально "
                f"рекомендуемый доход: {new_min:,.0f} ₸/мес."
            ),
        )

    # ── Guard'ы ёмкости ячеек шапки. balance_end больше не проверяем — он не
    # пересчитывается и не может переполниться сверх того, чем уже был. ──
    if abs(stmt.new_total_income) > _HEADER_CELL_MAX_SAFE_VALUE:
        raise HeaderCellOverflowError(
            field_name="total_income",
            value=stmt.new_total_income,
            max_safe_value=_HEADER_CELL_MAX_SAFE_VALUE,
        )

    # ── Категории расхода шапки — масштабируются пропорционально, сумма
    # точно совпадает с target_total_expense (см. _scale_expense_categories). ──
    stmt.new_expense_categories = _scale_expense_categories(
        stmt.expense_categories, target_total_expense, stmt.total_expense
    )
    for cat, val in stmt.new_expense_categories.items():
        if abs(val) > _HEADER_CELL_MAX_SAFE_VALUE:
            raise HeaderCellOverflowError(
                field_name=f"expense_category:{cat}",
                value=val,
                max_safe_value=_HEADER_CELL_MAX_SAFE_VALUE,
            )

    # ── Помесячная статистика нового дохода (только salary) ──
    new_monthly: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date) or "unknown"
            new_monthly[mk] = new_monthly.get(mk, 0) + tx.new_amount

    new_avg = sum(new_monthly.values()) / max(len(new_monthly), 1)
    print(f"\n  Σ нового дохода:            {stmt.new_total_income:>14,.2f} ₸")
    print(f"  Σ новых расходов:           {target_total_expense:>14,.2f} ₸")
    print(f"  Баланс конец (заморожен):   {stmt.new_balance_end:>14,.2f} ₸")
    print(f"  Новый средний доход/мес:    {new_avg:>14,.2f} ₸")
    print(f"  Целевой:                    {target_monthly_income:>14,.2f} ₸")
    print(f"  {'─' * 50}")

    return stmt
```

**Важно:** функция `_scale_debit_transactions_exact` (Task 2) должна быть определена ВЫШЕ `recalculate_statement` в файле (она уже добавлена туда в Task 2, порядок не менять).

- [ ] **Step 4: Запустить полный набор тестов файла**

Run: `pytest tests/test_pdf_service.py -v`
Expected: PASS для новых/обновлённых тестов. Обрати внимание на любые ДРУГИЕ тесты, завязанные на старое поведение «расходы не масштабируются» или на `stmt.new_balance_end` как производную величину — если такие найдутся при прогоне (FAIL с сообщением про `total_expense`/`balance_end`), это ожидаемо и они обновляются тем же способом, что и `test_humped_income_...` выше: искать по grep `new_balance_end|total_expense` в файле теста и проверить, не проверяет ли тест старую формулу.

- [ ] **Step 5: Commit**

```bash
git add pdf_service.py tests/test_pdf_service.py
git commit -m "feat(gold): freeze balance_end in recalculate_statement, expense absorbs income growth"
```

---

### Task 4: Запись изменённых категорий в PDF + guard «не трогать, если не изменилось»

**Files:**
- Modify: `pdf_service.py:1715-1738` (`build_cert_replacement_entries`)
- Modify: `pdf_service.py:2189-2247` (блок построения `replacement_queue` внутри `process_pdf_bytes_raw`)
- Test: `tests/test_pdf_service.py`

**Interfaces:**
- Consumes: `stmt.new_expense_categories` (заполнено в Task 3), `stmt.expense_category_texts` (уже существует, заполняется парсером).
- Produces: не меняет сигнатуры; `build_cert_replacement_entries` теперь может вернуть пустой словарь чаще (когда `new_val == old_val`, что теперь ВСЕГДА так после Task 3).

- [ ] **Step 1: Написать падающий тест**

```python
def test_build_cert_replacement_entries_skips_unchanged_balance():
    """Раньше эта функция ставила запись в очередь всегда, если old_val > 0.
    Теперь balance_end заморожен, поэтому cert.new_balance_kzt всегда равен
    cert.balance_kzt — писать в PDF нечего, и функция обязана вернуть пустой
    словарь (иначе байты страницы справки будут ничем не оправданно
    затронуты записью, даже если итоговое число совпадает)."""
    cert = p.CertificateData(
        balance_kzt_text="143 170,28", balance_kzt=143170.28,
        balance_usd_text="308,20", balance_usd=308.20,
        balance_eur_text="263,31", balance_eur=263.31,
        new_balance_kzt=143170.28, new_balance_usd=308.20, new_balance_eur=263.31,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert entries == {}


def test_build_cert_replacement_entries_still_writes_when_changed():
    """Регрессия: guard не должен ломать случай, когда значение ДЕЙСТВИТЕЛЬНО
    изменилось (не должен получиться этот план)."""
    cert = p.CertificateData(
        balance_kzt_text="143 170,28", balance_kzt=143170.28,
        new_balance_kzt=200000.00, new_balance_usd=0.0, new_balance_eur=0.0,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert "CERT_KZT:14317028" in entries
    assert entries["CERT_KZT:14317028"][0] == 200000.00
```

- [ ] **Step 2: Запустить, убедиться что первый тест падает**

Run: `pytest tests/test_pdf_service.py -k "test_build_cert_replacement_entries_skips_unchanged_balance or test_build_cert_replacement_entries_still_writes_when_changed" -v`
Expected: первый тест FAIL (текущий код всегда пишет запись при `old_val > 0`), второй PASS уже сейчас.

- [ ] **Step 3: Добавить guard в `build_cert_replacement_entries`**

В `pdf_service.py`, внутри цикла `for key_prefix, text, old_val, new_val in _pairs:` (строка ~1731), после существующей строки `if not text or old_val <= 0: continue` добавить:

```python
        if not text or old_val <= 0:
            continue
        if abs(new_val - old_val) < 0.005:
            # Значение не изменилось (balance_end теперь заморожен — см.
            # recalculate_statement) — не трогаем эту ячейку вовсе, вместо
            # того чтобы записывать в PDF те же самые цифры.
            continue
        digits = "".join(ch for ch in text if ch.isdigit())
```

(Строка `digits = "".join(...)` уже существует чуть ниже — просто вставить новую проверку ПЕРЕД ней, не дублируя.)

- [ ] **Step 4: Тот же guard для `BALANCE_END` в `process_pdf_bytes_raw`**

В `pdf_service.py`, строки ~2219-2224:

```python
    if stmt.balance_end_text:
        key = _clean(stmt.balance_end_text, prefix="HDR:")
        if key != "HDR:" and abs(stmt.new_balance_end - stmt.balance_end) >= 0.005:
            if key not in replacement_queue:
                replacement_queue[key] = _deque()
            replacement_queue[key].append((stmt.new_balance_end, "BALANCE_END"))
```

- [ ] **Step 5: Добавить запись категорий расхода в очередь**

В `pdf_service.py`, сразу после блока `BALANCE_END` (после кода из Step 4, до блока `# CERT-балансы`), заменить комментарий `# Расходные категории заголовка — НЕ меняем (банк верифицирует с базой)` на:

```python
    # Расходные категории заголовка — растут вместе с расходом (см. Task 3
    # docs/superpowers/plans/2026-08-11-freeze-gold-balance.md): balance_end
    # заморожен, поэтому расход — производная величина, и категории обязаны
    # оставаться консистентны с ней (иначе их сумма разойдётся с балансовым
    # тождеством — новый видимый признак подделки взамен старого).
    for cat, new_val in stmt.new_expense_categories.items():
        old_text = stmt.expense_category_texts.get(cat, "")
        old_val = stmt.expense_categories.get(cat, 0.0)
        if not old_text or old_val <= 0:
            continue
        if abs(new_val - old_val) < 0.005:
            continue
        key = _clean(old_text, prefix="HDR:")
        if key == "HDR:":
            continue
        if key not in replacement_queue:
            replacement_queue[key] = _deque()
        replacement_queue[key].append((new_val, f"EXPENSE_CATEGORY:{cat}"))
```

- [ ] **Step 6: Запустить тесты**

Run: `pytest tests/test_pdf_service.py -v`
Expected: все PASS.

- [ ] **Step 7: Commit**

```bash
git add pdf_service.py tests/test_pdf_service.py
git commit -m "feat(gold): write scaled expense categories to PDF, skip unchanged cert/balance cells"
```

---

### Task 5: Мирроринг в `pdf_service_downscale.recalculate_statement_downscale`

**Files:**
- Modify: `pdf_service_downscale.py:154-387` (тело `recalculate_statement_downscale`)
- Modify: `pdf_service_downscale.py:121-135` (докстринг `compute_min_target_monthly_income` — обновить смысл, код НЕ менять)
- Test: `tests/test_pdf_service.py` (обновить `test_downscale_preserves_balance_equation_expense_not_category_sum`, строка 585)

**Interfaces:**
- Consumes: `pdf_service._scale_expense_categories`, `pdf_service._scale_debit_transactions_exact` — добавить в импорт из `pdf_service` (строка 25-32 файла).
- Produces: та же сигнатура `recalculate_statement_downscale(stmt, target_monthly_income) -> StatementData`. Тот же инвариант, что у upscale: `new_balance_end == balance_end` оригинала.

- [ ] **Step 1: Написать падающий тест (замена существующего)**

В `tests/test_pdf_service.py` найти `test_downscale_preserves_balance_equation_expense_not_category_sum` (строка 585-610) и ЗАМЕНИТЬ на:

```python
def test_downscale_freezes_balance_and_scales_categories_to_match():
    """balance_end заморожен и при downscale тоже: доход падает, а вместе с
    ним падает и расход (категории шапки — пропорционально), чтобы
    balance_start + new_income - new_expense == balance_end (неизменный)."""
    import random as _random
    import pdf_service_downscale as pd
    _random.seed(42)

    stmt = p.StatementData(
        balance_start=1_000_000.0,
        balance_end=1_000_000.0,
        total_expense=500_000.0,
        expense_categories={"Переводы": 300_000.0, "Покупки": 200_000.0},
        expense_category_texts={"Переводы": "300 000,00", "Покупки": "200 000,00"},
    )
    stmt.transactions = [
        p.Transaction(index=0, sign=1, is_salary=True, is_refund=False,
                      amount=1_500_000.0, date="01.06.26",
                      original_amount_text="+ 1 500 000,00 ₸"),
        p.Transaction(index=1, sign=-1, is_salary=False, is_refund=False,
                      amount=500_000.0, date="01.06.26",
                      original_amount_text="- 500 000,00 ₸"),
    ]
    # текущий ср. доход = 1.5M/мес; занижаем до 750k (в 2 раза), с запасом
    # (balance_start большой, downscale-floor не мешает)
    out = pd.recalculate_statement_downscale(stmt, 750_000.0)

    assert out.new_balance_end == 1_000_000.0
    expected_expense = round(out.balance_start + out.new_total_income - out.new_balance_end, 2)
    assert round(sum(out.new_expense_categories.values()), 2) == expected_expense
    debit_sum = round(sum(t.new_amount for t in out.transactions if t.sign == -1), 2)
    assert debit_sum == expected_expense
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `pytest tests/test_pdf_service.py -k test_downscale_freezes_balance_and_scales_categories_to_match -v`
Expected: FAIL (текущий downscale ещё копирует категории без изменений и производит `new_balance_end` из старой формулы).

- [ ] **Step 3: Обновить импорты в `pdf_service_downscale.py`**

Строки 25-32, заменить:

```python
import pdf_service
from pdf_service import (
    StatementData,
    Transaction,
    _get_month_key,
    _round_to_natural,
    _scale_debit_transactions_exact,
    _scale_expense_categories,
    _date_sort_key,
    first_negative_dayend,
    min_dayend_balance,
    parse_full_statement,
)
```

(Добавлены `_scale_debit_transactions_exact`, `_scale_expense_categories`, `_date_sort_key`, `first_negative_dayend` — все уже существуют в `pdf_service.py`, `_date_sort_key`/`first_negative_dayend` там уже определены и используются `recalculate_statement`, просто не были импортированы сюда раньше, т.к. downscale не делал коррекцию через них.)

- [ ] **Step 4: Заменить формулу баланса/расхода**

В `pdf_service_downscale.py`, заменить блок строк 255-267 (комментарий `# ── Расходы НЕ масштабируем ──` + Шаг 1) на:

```python
    # ── Шаг 1: Масштабирование salary с дисперсией ──
    print(f"\n  Масштабирование транзакций:")
    for tx in stmt.transactions:
        if tx.sign == 1 and tx.is_salary and not tx.is_refund:
            mk = _get_month_key(tx.date) or "unknown"
            k = month_K.get(mk, global_K)
            epsilon = random.uniform(-0.03, 0.03)
            tx.new_amount = _round_to_natural(tx.amount * k * (1 + epsilon), original=tx.amount)
        elif tx.sign == 1:
            tx.new_amount = tx.amount
        # sign == -1 обрабатывается ниже

    salary_income_pos = sum(
        tx.new_amount for tx in stmt.transactions if tx.is_salary and not tx.is_refund
    )
    refund_topups_neg = sum(
        tx.amount for tx in stmt.transactions
        if tx.description == "Пополнение" and tx.sign == -1
    )
    stmt.new_total_income = round(salary_income_pos - refund_topups_neg, 2)

    # balance_end заморожен — та же инвариант, что и в upscale-движке.
    stmt.new_balance_end = stmt.balance_end

    target_total_expense = round(
        stmt.balance_start + stmt.new_total_income - stmt.new_balance_end, 2
    )
    if target_total_expense < 0:
        new_min = target_monthly_income * 1.10
        raise IncomeTooLowError(
            min_target_monthly_income=new_min,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="below_balance_floor",
            message=(
                f"При замороженном балансе ({stmt.balance_end:,.0f} ₸) занижение "
                f"дохода до {target_monthly_income:,.0f} ₸/мес требует "
                f"отрицательного расхода ({target_total_expense:,.0f} ₸) — "
                f"занижайте меньше."
            ),
        )

    _scale_debit_transactions_exact(stmt.transactions, target_total_expense)
```

- [ ] **Step 5: Заменить running balance + post-check коррекцию (строки, бывшие 269-320) на перенос во времени вместо подъёма salary**

Заменить блок от `# ── Шаг 2: Running balance ──` (была строка 269) до конца блока `# ── ПРОВЕРКА 3: min_rb ≥ 0 (post-check) ──` (была строка 320, кончается на `raise IncomeTooLowError(...)` перед `# ── Итоги (формулы Kaspi) ──`) на:

```python
    # ── Running balance ──
    current_rb = stmt.balance_start
    for tx in reversed(stmt.transactions):
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        tx.new_balance_after = current_rb
    min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")

    # ── Коррекция просадки: перенос расходного прироста во времени (та же
    # логика, что в pdf_service.recalculate_statement — см. её комментарий).
    # НЕ поднимаем salary: это двигало бы balance_end, что теперь запрещено. ──
    if min_rb < 0:
        print(f"\n  ⚠️ После пересчёта min_rb={min_rb:,.2f}, переносим расходный прирост во времени")
        prev_min = None
        for attempt in range(80):
            min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")
            if min_rb >= -0.01:
                print(f"  ✅ Скорректировано за {attempt} итераций, min_rb={min_rb:,.2f}")
                break
            neg_key = _date_sort_key(neg_date)
            donors = [
                tx for tx in stmt.transactions
                if tx.sign == -1 and _date_sort_key(tx.date) <= neg_key
                and tx.new_amount > tx.amount + 0.01
            ]
            receivers = [
                tx for tx in stmt.transactions
                if tx.sign == -1 and _date_sort_key(tx.date) > neg_key
            ]
            if not donors or not receivers:
                print(f"  ⚠️ Нет донора/приёмника для переноса на {neg_date}")
                break
            donor = max(donors, key=lambda t: t.new_amount - t.amount)
            receiver = max(receivers, key=lambda t: t.amount)
            step = min(donor.new_amount - donor.amount, max(1000.0, 0.05 * donor.new_amount))
            donor.new_amount = round(donor.new_amount - step, 2)
            receiver.new_amount = round(receiver.new_amount + step, 2)

            current_rb = stmt.balance_start
            for tx in reversed(stmt.transactions):
                current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
                tx.new_balance_after = current_rb
            min_rb, neg_date = first_negative_dayend(stmt.transactions, stmt.balance_start, "new_amount")

            if prev_min is not None and min_rb <= prev_min + 0.01:
                print(f"  ⚠️ Коррекция не сходится (мин застрял на {min_rb:,.2f} ₸) — стоп")
                break
            prev_min = min_rb
        else:
            print(f"  ⚠️ Коррекция не сошлась за 80 итераций, min_rb={min_rb:,.2f}")

        if min_rb < -0.01:
            new_min = max(min_target, target_monthly_income) * 1.10
            raise IncomeTooLowError(
                min_target_monthly_income=new_min,
                current_expense=stmt.total_expense,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="post_check_negative_balance",
                message=(
                    f"Не удалось удержать неотрицательный running balance при "
                    f"{target_monthly_income:,.0f} ₸/мес с замороженным итоговым "
                    f"балансом (min_rb={min_rb:,.0f} ₸). Минимально рекомендуемый "
                    f"доход: {new_min:,.0f} ₸/мес."
                ),
            )
```

- [ ] **Step 6: Заменить блок «Итоги» (категории и финальную страховку)**

Заменить существующий блок начиная с `# ── Итоги (формулы Kaspi) ──` (был на строке 322 до правок Step 4/5 — теперь идёт сразу после кода из Step 5) до конца функции (было до строки 387, `return stmt`) на:

```python
    # ── Категории расхода — масштабируются пропорционально (как в upscale) ──
    stmt.new_expense_categories = _scale_expense_categories(
        stmt.expense_categories, target_total_expense, stmt.total_expense
    )

    # ── Помесячная статистика ──
    new_monthly: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date) or "unknown"
            new_monthly[mk] = new_monthly.get(mk, 0) + tx.new_amount

    new_avg = sum(new_monthly.values()) / max(len(new_monthly), 1)
    print(f"\n  {'─' * 50}")
    print(f"  Σ нового дохода:            {stmt.new_total_income:>14,.2f} ₸")
    print(f"  Σ новых расходов:           {target_total_expense:>14,.2f} ₸")
    print(f"  Баланс конец (заморожен):   {stmt.new_balance_end:>14,.2f} ₸")
    print(f"  Новый средний доход/мес:    {new_avg:>14,.2f} ₸")
    print(f"  Целевой:                    {target_monthly_income:>14,.2f} ₸")
    print(f"  {'─' * 50}")

    return stmt
```

(Обрати внимание: `target_total_expense` вычислен в Step 4 и остаётся в области видимости функции — отдельно пересчитывать не нужно. Старый блок «Финальная страховка: не должно остаться отрицательного B_end» удалён — он больше не имеет смысла, `new_balance_end` теперь буквально равен `stmt.balance_end`, отрицательным стать не может.)

- [ ] **Step 7: Запустить тесты**

Run: `pytest tests/test_pdf_service.py -v`
Expected: все PASS, включая `test_downscale_succeeds_with_slack` (строка 752) — если он падает, проверить, не завязан ли он на старую формулу категорий/баланса; при необходимости обновить тем же способом, что Step 1.

- [ ] **Step 8: Commit**

```bash
git add pdf_service_downscale.py tests/test_pdf_service.py
git commit -m "feat(gold): mirror balance-freeze/expense-compensation in downscale engine"
```

---

### Task 6: Новые проверки в `verify_gold_file.py`

**Files:**
- Modify: `tests/scripts/verify_gold_file.py`

**Interfaces:**
- Consumes: `pdf_service.parse_certificate_page`, `pdf_service.parse_full_statement`, `pdf_service.detect_statement_format` (уже импортированы как `p.*`).
- Produces: `check_balance_frozen(orig: "fitz.Document", out: "fitz.Document") -> list[str]`, `check_expense_categories_sum(out: "fitz.Document", start_page: int) -> list[str]` — обе подключаются в `run_one()`.

- [ ] **Step 1: Добавить `check_balance_frozen`**

В `tests/scripts/verify_gold_file.py`, после функции `check_variance_preserved` (строка 235-272, вставить сразу после её конца, перед `check_rounding_escalation`):

```python
def check_balance_frozen(orig: "fitz.Document", out: "fitz.Document") -> list[str]:
    """«Доступно на …» и справка ₸/USD/EUR (cert-формат) обязаны остаться
    ЧИСЛЕННО равны оригиналу на любой цели — recalculate_statement больше не
    пересчитывает balance_end (см. docs/superpowers/specs/2026-08-11-
    freeze-gold-balance-design.md)."""
    issues = []
    fmt = p.detect_statement_format(orig)
    start_page = 1 if fmt == "cert" else 0

    orig_stmt = p.parse_full_statement(orig, start_page=start_page)
    out_stmt = p.parse_full_statement(out, start_page=start_page)
    if abs(orig_stmt.balance_end - out_stmt.balance_end) > 0.01:
        issues.append(
            f"balance_end изменился: было {orig_stmt.balance_end:,.2f}, "
            f"стало {out_stmt.balance_end:,.2f}"
        )

    if fmt == "cert":
        orig_cert = p.parse_certificate_page(orig)
        out_cert = p.parse_certificate_page(out)
        for label, o_val, n_val in (
            ("KZT", orig_cert.balance_kzt, out_cert.balance_kzt),
            ("USD", orig_cert.balance_usd, out_cert.balance_usd),
            ("EUR", orig_cert.balance_eur, out_cert.balance_eur),
        ):
            if o_val > 0 and abs(o_val - n_val) > 0.01:
                issues.append(f"справка {label} изменилась: было {o_val:,.2f}, стало {n_val:,.2f}")

    return issues


def check_expense_categories_sum(out: "fitz.Document", start_page: int) -> list[str]:
    """Сумма категорий расхода шапки (после обработки) обязана точно
    совпадать с производным total_expense = balance_start + new_total_income
    − balance_end — иначе видимая арифметика шапки не сходится с балансовым
    тождеством (см. дизайн-документ, раздел про категории)."""
    issues = []
    stmt = p.parse_full_statement(out, start_page=start_page)
    if not stmt.expense_categories:
        return issues
    expected = round(stmt.balance_start + stmt.total_income - stmt.balance_end, 2)
    actual = round(sum(stmt.expense_categories.values()), 2)
    if abs(expected - actual) > 0.05:
        issues.append(
            f"Σ категорий расхода ({actual:,.2f}) ≠ производного total_expense "
            f"({expected:,.2f}), Δ={actual - expected:+,.2f}"
        )
    return issues
```

- [ ] **Step 2: Подключить в `run_one`**

Найти в `run_one()` (строка 479+) место, где вызываются остальные `check_*` функции на обработанном файле (по аналогии с `check_variance_preserved`/`check_rounding_escalation` — искать `check_variance_preserved(` в теле `run_one`), и добавить рядом:

```python
    issues += check_balance_frozen(orig_doc, out_doc)
    issues += check_expense_categories_sum(out_doc, start_page)
```

(`orig_doc`, `out_doc`, `start_page` — уже существующие локальные переменные в `run_one`, используемые соседними вызовами `check_*`; использовать те же имена, что и в окружающем коде.)

- [ ] **Step 3: Прогнать скрипт на локальном корпусе (если доступен)**

Run: `python tests/scripts/verify_gold_file.py <путь к любому файлу из C:\Users\Abylay\Desktop\testpdf\gold> --targets 1.05,2,10`
Expected: `check_balance_frozen`/`check_expense_categories_sum` не выдают issues; если корпус недоступен в этой рабочей копии — пропустить шаг и явно сообщить об этом при подведении итогов задачи (не считать шаг проваленным молча).

- [ ] **Step 4: Commit**

```bash
git add tests/scripts/verify_gold_file.py
git commit -m "test(gold): add check_balance_frozen and check_expense_categories_sum to autotest battery"
```

---

### Task 7: Полная регрессия и обновление CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (добавить раздел с датой, по конвенции остальных записей файла)
- No new code files.

- [ ] **Step 1: Полный прогон pytest**

Run: `pytest tests/ -v`
Expected: все тесты PASS (кроме ожидаемых SKIP из-за отсутствующих `tests/fixtures/*.pdf` — см. `CLAUDE.md`, раздел «Tests»). Если что-то падает — вернуться к соответствующей задаче, не переходить дальше.

- [ ] **Step 2: Полная батарея на реальных файлах (если доступны)**

Run: `python tests/scripts/verify_gold_file.py <все файлы из C:\Users\Abylay\Desktop\testpdf\gold> --targets 0.6,1.05,2,10,50`
Expected: 0 FAIL. Записать фактические цифры (сколько файлов/целей прогнано, что показали новые проверки) — они понадобятся для записи в CLAUDE.md на Step 3. Если корпус недоступен в текущей рабочей копии — явно это указать, не выдумывать цифры.

- [ ] **Step 3: Добавить раздел в CLAUDE.md**

Добавить новый раздел сразу после заголовка `## найденные ошибки каспи пей` (в самое начало списка находок, по убыванию даты — та же конвенция, что у остальных записей файла), с текстом по образцу существующих записей: что было (баланс/справка пересчитывались), что стало (заморожены, расход — производная), какие цифры дал прогон батареи из Step 2, и явная пометка про TODO из Task 2 (округление расходных транзакций до проверки на реальных файлах).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: заморозка баланса/справки Kaspi Gold — итоги прогона (2026-08-11)"
```

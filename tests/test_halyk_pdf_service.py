"""
Регрессионные тесты для halyk_pdf_service.py.

Фикстуры в tests/fixtures/halyk_nav_*.pdf — реальные Halyk-выписки (nav-формат,
Times New Roman CID). process_halyk_pdf() не различает "оригинал" и "уже
посчитанный" файл — он просто парсит текущие числа в шапке/транзакциях и
пересчитывает их заново, поэтому уже обработанные образцы годятся как вход
ровно так же, как и нетронутая выписка.

Каждый файл прогоняется через полный цикл parse -> recalculate ->
process_halyk_pdf (запись байт) -> validate_halyk, на нескольких целевых
доходах (downscale / upscale / около текущего среднего), и проверяется, что
validate_halyk считает результат математически согласованным (это тот же
набор проверок, что использует production-эндпоинт POST /verify).
"""
from __future__ import annotations

import random
from pathlib import Path

import fitz
import pytest

import halyk_pdf_service as h
from pdf_service_downscale import IncomeTooLowError

FIXTURES_DIR = Path(__file__).parent / "fixtures"

FIXTURES = {
    "multicurrency": FIXTURES_DIR / "halyk_nav_multicurrency.pdf",   # USD/TRY + KZT, "Автоконвертация"
    "singlecurrency": FIXTURES_DIR / "halyk_nav_singlecurrency.pdf",  # KZT-only, "Оплата картотеки", P2P К2
    "minimal": FIXTURES_DIR / "halyk_nav_minimal.pdf",                # маленький, 10 транзакций
}


def _raw(name: str) -> bytes:
    return FIXTURES[name].read_bytes()


@pytest.fixture(params=sorted(FIXTURES), ids=sorted(FIXTURES))
def fixture_name(request):
    return request.param


# ─── Парсинг ────────────────────────────────────────────────────────────────

def test_all_fixtures_detected_as_halyk():
    for name, path in FIXTURES.items():
        doc = fitz.open(path)
        assert h.detect_halyk_format(doc), f"{name}: не распознан как Halyk"
        doc.close()


def test_parse_header_totals_found(fixture_name):
    """Шапка "Всего:"/"Барлығы:" должна разбираться напрямую (без фолбэка по
    транзакциям) на всех трёх реальных образцах — иначе байтовая замена этих
    полей в process_halyk_pdf будет пропущена (см. total_kiri_s_text is None)."""
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = h.parse_halyk_statement(doc)
    doc.close()
    assert stmt.total_kiri_s_text is not None
    assert stmt.total_shyghys_text is not None


def test_parse_balance_identity_matches_header(fixture_name):
    """opening + Σкіріс(KZT) − Σшығыс(KZT) − |комиссия| == closing (из шапки)."""
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = h.parse_halyk_statement(doc)
    doc.close()

    sum_kiri = sum(t.kiri_s for t in stmt.transactions if t.kiri_s > 0 and t.currency == "KZT")
    sum_shy = sum(abs(t.shyghys) for t in stmt.transactions if t.shyghys < 0 and t.currency == "KZT")
    calc_closing = round(stmt.opening_balance + sum_kiri - sum_shy - abs(stmt.total_commission), 2)

    assert calc_closing == pytest.approx(stmt.closing_balance, abs=0.01)
    # Итог шапки должен совпадать с суммой транзакций (иначе это уже
    # сломанный входной документ, а не наш баг).
    assert stmt.total_kiri_s == pytest.approx(sum_kiri, abs=0.01)
    assert abs(stmt.total_shyghys) == pytest.approx(sum_shy, abs=0.01)


# ─── Сквозной цикл process -> validate ──────────────────────────────────────

# (fixture, target_monthly_income) — downscale и upscale в пределах floor-правил
# (below_balance_floor / too_aggressive 30% / post_check_negative_balance).
#
# ВАЖНО: цели подобраны под КОНКРЕТНЫЙ снапшот файлов в tests/fixtures/ (см.
# tests/scripts/print_fixture_targets.py) — они НЕ пересчитываются на лету. Если
# фикстуры когда-нибудь будут перегенерированы/заменены новыми файлами (у
# каждого прогона "живого" приложения свои случайные суммы), эти числа нужно
# пересчитать тем же скриптом, иначе тесты упадут не из-за регрессии в коде,
# а из-за того, что зашитые таргеты ушли ниже floor нового файла.
ROUNDTRIP_CASES = [
    ("multicurrency", 50_000_000),    # downscale (выше floor ~38.4M)
    ("multicurrency", 230_000_000),   # upscale
    ("singlecurrency", 50_000_000),   # downscale (выше floor ~38.1M)
    ("singlecurrency", 230_000_000),  # upscale
    ("minimal", 50_000_000),          # downscale (выше floor ~38.5M)
    ("minimal", 230_000_000),         # upscale
]


@pytest.mark.parametrize("fixture_name, target", ROUNDTRIP_CASES)
def test_process_then_validate_passes(fixture_name, target):
    random.seed(1234)  # ±3% шум в recalculate_halyk — фиксируем для воспроизводимости
    raw = _raw(fixture_name)

    out = h.process_halyk_pdf(raw, target)

    # PDF должен остаться валидным и открываемым.
    doc = fitz.open(stream=out, filetype="pdf")
    doc.close()

    result = h.validate_halyk(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{target}: {failed}"


def test_process_near_noop_target_still_consistent(fixture_name):
    """Таргет ≈ текущему среднему доходу — минимальные изменения, но результат
    всё равно должен быть математически согласован (регрессия на округления/
    шум ±3%, а не только на «явные» пере- и недосчёты)."""
    random.seed(1234)
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = h.parse_halyk_statement(doc)
    doc.close()

    month_salary: dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary and tx.kiri_s > 0:
            month_salary.setdefault(tx.op_date[3:], 0.0)
            month_salary[tx.op_date[3:]] += tx.kiri_s
    current_avg = sum(month_salary.values()) / len(month_salary)

    out = h.process_halyk_pdf(_raw(fixture_name), current_avg)
    result = h.validate_halyk(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{current_avg}: {failed}"


# ─── Регрессия: строка "Всего:" (встроенный /F Tf между Td и <hex>Tj) ───────

def test_header_totals_are_actually_rewritten_in_bytes():
    """Регрессия конкретного бага: числа в строке "Всего:" этого документа
    кодируются как "X Y Td /F0 8 Tf <hex> Tj" — со встроенной сменой шрифта
    между Td и Tj. Старый td_pattern такое не матчил, из-за чего шапка
    "Всего:" молча оставалась старой, даже когда все транзакции уже были
    пересчитаны (validate_halyk падал на проверке "Итого (шапка)")."""
    random.seed(1234)
    raw = _raw("singlecurrency")
    doc = fitz.open(stream=raw, filetype="pdf")
    stmt_before = h.parse_halyk_statement(doc)
    doc.close()

    out = h.process_halyk_pdf(raw, 50_000_000)

    doc = fitz.open(stream=out, filetype="pdf")
    text = doc[0].get_text()
    doc.close()

    idx = text.find("Всего:")
    assert idx >= 0
    header_slice = text[idx:idx + 60]

    # Старое значение НЕ должно остаться в шапке...
    old_kiri_digits = h._clean_digits(stmt_before.total_kiri_s_text)
    assert h._clean_digits(header_slice) [:len(old_kiri_digits)] != old_kiri_digits

    # ...а validate_halyk должен подтвердить согласованность шапки и транзакций.
    result = h.validate_halyk(out)
    hdr_check = next(c for c in result["checks"] if c["name"] == "Итого (шапка)")
    assert hdr_check["ok"], hdr_check["detail"]


# ─── Floor-проверки (IncomeTooLowError) ─────────────────────────────────────

def test_too_aggressive_downscale_raises():
    """target < 30% текущего среднего должен быть отклонён (too_aggressive)."""
    raw = _raw("minimal")
    doc = fitz.open(stream=raw, filetype="pdf")
    stmt = h.parse_halyk_statement(doc)
    doc.close()

    with pytest.raises(IncomeTooLowError) as exc_info:
        h.recalculate_halyk(stmt, 1_000_000)  # << 30% floor (~3.78M для этого файла)
    assert exc_info.value.reason == "too_aggressive"


def test_validate_suggested_min_is_achievable(fixture_name):
    """suggested_min из validate_halyk (используется фронтендом как "Подставить
    минимум?") не должен сам провоцировать IncomeTooLowError при обратной
    подаче в /process — иначе подсказка вводит пользователя в тупик."""
    raw = _raw(fixture_name)
    result_before = h.validate_halyk(raw)
    suggested_min = result_before["summary"]["suggested_min"]
    target_income = suggested_min / h._INCOME_K

    random.seed(1234)
    # Не должно бросить IncomeTooLowError.
    out = h.process_halyk_pdf(raw, target_income)
    result = h.validate_halyk(out)
    assert result["passed"], [c for c in result["checks"] if not c["ok"]]


# ─── Юнит-тест: группировка слов по Y (фикс #5) ─────────────────────────────

class _FakePage:
    """Минимальная заглушка под page.get_text("words") для теста без PDF."""

    def __init__(self, words):
        self._words = words

    def get_text(self, mode):
        assert mode == "words"
        return self._words


def test_group_words_by_y_does_not_split_row_at_rounding_boundary():
    """Раньше группировка шла через round(y/tol)*tol: два слова одной строки
    с y=0.99 и y=1.01 (tol=2.0) попадали в РАЗНЫЕ бакеты (round(0.495)=0 vs
    round(0.505)=1 у Python round-half-even фактически 0 и 0, но при tol=1.0
    легко подобрать пару, реально расходящуюся по бакетам: 0.49 -> 0, 0.51 ->
    1). Новая anchor-based группировка сравнивает с Y первого слова строки,
    а не с сеткой, и должна держать оба слова в одной строке."""
    words = [
        (10.0, 0.49, 20.0, 10.0, "01.01.2026", 0, 0, 0),
        (60.0, 0.51, 70.0, 10.0, "01.01.2026", 0, 0, 1),
        (110.0, 0.50, 130.0, 10.0, "Описание", 0, 0, 2),
    ]
    page = _FakePage(words)
    rows = h._group_words_by_y(page, tol=1.0)
    assert len(rows) == 1
    _, row = rows[0]
    assert [t for _, t in row] == ["01.01.2026", "01.01.2026", "Описание"]


def test_group_words_by_y_still_splits_genuinely_different_rows():
    """Устойчивость к границе не должна схлопывать РАЗНЫЕ строки в одну."""
    words = [
        (10.0, 100.0, 20.0, 110.0, "Строка1", 0, 0, 0),
        (10.0, 200.0, 20.0, 210.0, "Строка2", 0, 0, 1),
    ]
    page = _FakePage(words)
    rows = h._group_words_by_y(page, tol=2.0)
    assert len(rows) == 2


# ─── _halyk_dayend_min_rb: внутридневной порядок не создаёт ложный овердрафт ──
# Фикстур не требует — конструирует транзакции вручную (тот же класс бага,
# что реально ловился и был исправлен на Kaspi Gold: op_date/дата без времени,
# порядок нескольких транзакций одной даты в PDF произволен).

def _htx(date, kiri=0.0, shyghys=0.0, currency="KZT"):
    return h.HalykTransaction(
        op_date=date, description="", kiri_s=kiri, shyghys=shyghys,
        kiri_s_text="", shyghys_text="", currency=currency,
        new_kiri_s=kiri, new_shyghys=shyghys,
    )


def test_halyk_dayend_min_ignores_intraday_dip():
    """Дебеты перед покрывающими их кредитами ТОГО ЖЕ дня не должны считаться
    овердрафтом — op_date не содержит времени, порядок внутри даты произволен.
    Сортировка (по _sort_key в vызывающем коде) хронологическая: старые сначала."""
    txs = [
        _htx("01.02.2026", shyghys=-30.0),
        _htx("01.02.2026", shyghys=-30.0),
        _htx("01.02.2026", shyghys=-40.0),
        _htx("01.02.2026", kiri=50.0),
        _htx("01.02.2026", kiri=40.0),
    ]
    min_rb = h._halyk_dayend_min_rb(txs, opening_balance=50.0, use_scaled=False)
    assert min_rb >= 0.0, f"день начинается и заканчивается в плюсе — day-boundary минус не должно быть: {min_rb}"

    naive = 50.0
    naive_min = naive
    for t in txs:
        naive += t.kiri_s - abs(t.shyghys)
        naive_min = min(naive_min, naive)
    assert naive_min < 0.0  # именно это раньше ловилось бы как ложный минус per-transaction


def test_halyk_dayend_min_catches_real_overdraft():
    """Если баланс отрицателен НА ГРАНИЦЕ дня — реальный овердрафт, должен ловиться."""
    txs = [
        _htx("01.02.2026", shyghys=-100.0),  # раньше: конец дня 01.02 = 50-100 = -50
        _htx("02.02.2026", kiri=10.0),       # позже
    ]
    min_rb = h._halyk_dayend_min_rb(txs, opening_balance=50.0, use_scaled=False)
    assert min_rb < 0.0


def test_halyk_dayend_min_currency_filter_and_final_point():
    """Не-KZT транзакции не входят в тенговый баланс; финальная точка учитывается."""
    txs = [
        _htx("01.02.2026", kiri=100.0, currency="USD"),  # игнорируется
        _htx("02.02.2026", shyghys=-30.0),
    ]
    min_rb = h._halyk_dayend_min_rb(txs, opening_balance=50.0, use_scaled=False)
    assert min_rb == 20.0  # 50 - 30 (USD-транзакция не в счёт)

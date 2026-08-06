"""
Регрессионные тесты для kaspi_ip_pdf_service.py — те же принципы, что и в
test_halyk_pdf_service.py.

Фикстуры: tests/fixtures/kaspi_ip_original.pdf (нетронутая выписка ИП,
"ИП ЖЕҢІЛ САПАР") и tests/fixtures/kaspi_ip_scored.pdf (тот же счёт после
/process). process_kaspi_ip_pdf() не различает "оригинал" и "уже
посчитанный" файл — оба годятся как вход одинаково, поэтому оба используются
как отдельные фикстуры (не как up-/downscale одного и того же файла), чтобы
покрыть оба реалистичных стартовых состояния (низкий ISI до обработки,
стабильный после).
"""
from __future__ import annotations

import random
from pathlib import Path

import fitz
import pytest

import kaspi_ip_pdf_service as k
from pdf_service_downscale import IncomeTooLowError

FIXTURES_DIR = Path(__file__).parent / "fixtures"

FIXTURES = {
    "original": FIXTURES_DIR / "kaspi_ip_original.pdf",
    "scored": FIXTURES_DIR / "kaspi_ip_scored.pdf",
}


def _raw(name: str) -> bytes:
    return FIXTURES[name].read_bytes()


@pytest.fixture(params=sorted(FIXTURES), ids=sorted(FIXTURES))
def fixture_name(request):
    return request.param


# ─── Парсинг ────────────────────────────────────────────────────────────────

def test_all_fixtures_detected_as_kaspi_ip():
    for name, path in FIXTURES.items():
        doc = fitz.open(path)
        assert k.detect_kaspi_ip_format(doc), f"{name}: не распознан как Kaspi ИП"
        doc.close()


def test_parse_balance_identity_matches_header(fixture_name):
    """opening + total_credit − total_debit == closing (из "Итого обороты")."""
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = k.parse_kaspi_ip_statement(doc)
    doc.close()
    s = stmt.summary

    calc_closing = round(s.opening_balance + s.total_credit - s.total_debit, 2)
    assert calc_closing == pytest.approx(s.closing_balance, abs=0.01)

    sum_credit = sum(t.amount for t in stmt.transactions if t.is_credit)
    sum_debit = sum(t.amount for t in stmt.transactions if not t.is_credit)
    assert s.total_credit == pytest.approx(sum_credit, abs=0.01)
    assert s.total_debit == pytest.approx(sum_debit, abs=0.01)


# ─── Сквозной цикл process -> validate ──────────────────────────────────────

# ВАЖНО: цели подобраны под КОНКРЕТНЫЙ снапшот файлов в tests/fixtures/ (см.
# tests/scripts/print_fixture_targets.py). Если фикстуры заменят новыми
# файлами, пересчитать этим же скриптом.
ROUNDTRIP_CASES = [
    ("original", 1_300_000),   # downscale (выше floor ~970k)
    ("original", 5_800_000),   # upscale
    ("scored", 44_600_000),    # downscale (выше floor ~34.3M)
    ("scored", 205_700_000),   # upscale
]


@pytest.mark.parametrize("fixture_name, target", ROUNDTRIP_CASES)
def test_process_then_validate_passes(fixture_name, target):
    random.seed(1234)
    raw = _raw(fixture_name)

    out = k.process_kaspi_ip_pdf(raw, target)

    doc = fitz.open(stream=out, filetype="pdf")
    doc.close()

    result = k.validate_kaspi_ip(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{target}: {failed}"


def test_process_near_noop_target_still_consistent(fixture_name):
    random.seed(1234)
    raw = _raw(fixture_name)
    v_before = k.validate_kaspi_ip(raw)
    current_avg = v_before["summary"]["avg_monthly_income"]

    out = k.process_kaspi_ip_pdf(raw, current_avg)
    result = k.validate_kaspi_ip(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{current_avg}: {failed}"


# ─── Running balance: специфика Kaspi ИП (масштабируемый дебет) ────────────

def test_running_balance_never_negative_after_process(fixture_name):
    """CLAUDE.md: масштабируемый дебет (Бейбит К. переводы) растёт вместе с
    кредитом даже при UPSCALE — единственный из трёх банков-модулей, где
    повышение дохода тоже может увести running balance в минус. Явно
    перепроверяем цепочку на выходном файле (а не полагаемся только на
    внутреннюю проверку recalculate_kaspi_ip)."""
    random.seed(1234)
    raw = _raw(fixture_name)
    target = 5_800_000 if fixture_name == "original" else 205_700_000
    out = k.process_kaspi_ip_pdf(raw, target)

    doc = fitz.open(stream=out, filetype="pdf")
    stmt = k.parse_kaspi_ip_statement(doc)
    doc.close()
    s = stmt.summary

    sorted_txs = sorted(
        reversed(stmt.transactions),
        key=lambda t: (k._date_key(t.date), 0 if t.is_credit else 1),
    )
    rb = s.opening_balance
    min_rb = rb
    for t in sorted_txs:
        rb = round(rb + t.amount if t.is_credit else rb - t.amount, 2)
        min_rb = min(min_rb, rb)
    assert min_rb >= -0.01, f"running balance ушёл в минус: {min_rb}"


# ─── Floor-проверки (IncomeTooLowError) ─────────────────────────────────────

def test_too_aggressive_downscale_raises():
    raw = _raw("scored")
    doc = fitz.open(stream=raw, filetype="pdf")
    stmt = k.parse_kaspi_ip_statement(doc)
    doc.close()

    with pytest.raises(IncomeTooLowError) as exc_info:
        k.recalculate_kaspi_ip(stmt, 1_000_000)  # << 30% floor (~34.3M для этого файла)
    assert exc_info.value.reason == "too_aggressive"


def test_validate_suggested_min_is_achievable(fixture_name):
    raw = _raw(fixture_name)
    result_before = k.validate_kaspi_ip(raw)
    suggested_min = result_before["summary"]["suggested_min"]
    target_income = suggested_min / k._INCOME_K

    random.seed(1234)
    out = k.process_kaspi_ip_pdf(raw, target_income)
    result = k.validate_kaspi_ip(out)
    assert result["passed"], [c for c in result["checks"] if not c["ok"]]


# ─── Task 2 (2026-08-06): purpose_line_bboxes ────────────────────────────────

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

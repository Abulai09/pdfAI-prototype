"""
Регрессионные тесты для pdf_service.py (Kaspi Gold, cert-формат) — тот же
принцип round-trip, что и в test_halyk_pdf_service.py / test_kaspi_ip_pdf_service.py.

Фикстуры (все — реальные выписки cert-формата, "Справка об остатке" на стр. 0):
  kaspi_gold_cert_original.pdf  — активная карта, 2333 транзакции / 13 мес,
                                   содержит self-transfer «Поступление»/«Зачисление»
  kaspi_gold_cert_scored.pdf    — та же карта после ОДНОГО прогона /process
                                   (уже масштабированная) — покрывает сценарий
                                   повторной обработки уже посчитанного файла,
                                   как и original/scored пара в test_kaspi_ip_*
  kaspi_gold_cert_small.pdf     — короткая выписка, 157 транзакций / 2 мес,
                                   тоже содержит self-transfer
  kaspi_gold_cert_original2.pdf — другая активная карта, 3581 транзакция / 13
                                   мес, БЕЗ единой self-transfer строки —
                                   независимая проверка, что фиксы для
                                   «Поступление»/«Зачисление» не ломают файлы,
                                   где этого типа вообще нет
  kaspi_gold_cert_scored2.pdf   — та же карта после ОДНОГО прогона /process

original/small содержат строки типа «Поступление»/«Зачисление» (self-transfer с
депозитного субсчёта на карту) — это то самое, что раньше приводило к
расхождению баланса (Δ) при парсинге, и то самое, из-за чего REFUND_TYPE_WORDS
Y-фильтр в process_pdf_bytes_raw сдвигал зарплатные суммы на чужие ячейки при
записи (см. CLAUDE.md / комментарии у TX_TYPES_SELF_TRANSFER и
page_refund_cs_ys в pdf_service.py). Тесты здесь закрепляют оба фикса, а
original2/scored2 (без self-transfer) — что тот же код путь не сломан для
файлов без этого типа операций.
"""
from __future__ import annotations

import os
import random
import re
from collections import defaultdict
from pathlib import Path

import fitz
import pytest

import main as m
import pdf_service as p
from pdf_service_downscale import IncomeTooLowError, process_downscale

FIXTURES_DIR = Path(__file__).parent / "fixtures"

FIXTURES = {
    "original": FIXTURES_DIR / "kaspi_gold_cert_original.pdf",
    "scored": FIXTURES_DIR / "kaspi_gold_cert_scored.pdf",
    "small": FIXTURES_DIR / "kaspi_gold_cert_small.pdf",
    "original2": FIXTURES_DIR / "kaspi_gold_cert_original2.pdf",
    "scored2": FIXTURES_DIR / "kaspi_gold_cert_scored2.pdf",
}

# Фикстуры, содержащие хотя бы одну self-transfer строку («Поступление»/
# «Зачисление») — original2/scored2 её не имеют (см. докстринг модуля).
FIXTURES_WITH_SELF_TRANSFER = ["original", "scored", "small"]


def _raw(name: str) -> bytes:
    return FIXTURES[name].read_bytes()


@pytest.fixture(params=sorted(FIXTURES), ids=sorted(FIXTURES))
def fixture_name(request):
    return request.param


# ─── Формат / парсинг ────────────────────────────────────────────────────────

def test_all_fixtures_detected_as_cert_format():
    for name, path in FIXTURES.items():
        doc = fitz.open(path)
        assert p.detect_statement_format(doc) == "cert", f"{name}: не распознан как cert-формат"
        doc.close()


def test_parse_balance_identity_matches_header(fixture_name):
    """B_start + Σ(+) − Σ(−) == B_end по транзакциям (включая self-transfer
    'Поступление'/'Зачисление' — без них расхождение достигало 19M ₸ на
    original)."""
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = p.parse_full_statement(doc, start_page=1)
    doc.close()

    sum_plus = sum(t.amount for t in stmt.transactions if t.sign == 1)
    sum_minus = sum(t.amount for t in stmt.transactions if t.sign == -1)
    calc_end = round(stmt.balance_start + sum_plus - sum_minus, 2)
    assert calc_end == pytest.approx(stmt.balance_end, abs=0.01)


@pytest.mark.parametrize("fixture_name", FIXTURES_WITH_SELF_TRANSFER)
def test_self_transfer_rows_recognized_and_not_salary(fixture_name):
    """'Поступление'/'Зачисление' должны парситься как транзакции (не
    выпадать из stmt.transactions), но НЕ считаться зарплатой (is_salary),
    чтобы не масштабироваться как реальный доход."""
    doc = fitz.open(FIXTURES[fixture_name])
    stmt = p.parse_full_statement(doc, start_page=1)
    doc.close()

    self_transfers = [
        t for t in stmt.transactions
        if t.sign == 1 and t.description in ("Поступление", "Зачисление")
    ]
    assert self_transfers, f"{fixture_name}: не найдено ни одной self-transfer строки"
    assert all(t.is_refund for t in self_transfers)
    assert not any(t.is_salary for t in self_transfers)


# ─── Сквозной цикл upscale -> verify ──────────────────────────────────────────

# ВАЖНО: цели подобраны под КОНКРЕТНЫЙ снапшот файлов в tests/fixtures/. Если
# фикстуры заменят новыми файлами, пересчитать avg_monthly_income через
# main._verify_pdf(raw)["summary"] и обновить эти значения.
ROUNDTRIP_CASES = [
    ("original", 4_100_000),    # upscale ~1.5x (avg ≈ 2.73M)
    ("original", 8_200_000),    # upscale ~3x
    ("scored", 19_100_000),     # upscale ~1.5x поверх уже посчитанного (avg ≈ 12.73M)
    ("scored", 38_200_000),     # upscale ~3x поверх уже посчитанного
    ("small", 440_000),         # upscale ~1.5x (avg ≈ 291K)
    ("small", 870_000),         # upscale ~3x
    ("original2", 7_100_000),   # upscale ~1.5x (avg ≈ 4.71M, БЕЗ self-transfer)
    ("original2", 14_100_000),  # upscale ~3x
    ("scored2", 19_100_000),    # upscale ~1.5x поверх уже посчитанного (avg ≈ 12.75M)
    ("scored2", 38_300_000),    # upscale ~3x поверх уже посчитанного
]


@pytest.mark.parametrize("fixture_name, target", ROUNDTRIP_CASES)
def test_upscale_then_verify_passes(fixture_name, target):
    random.seed(1234)
    raw = _raw(fixture_name)

    out = p.process_pdf_bytes_raw(raw, target)

    doc = fitz.open(stream=out, filetype="pdf")
    doc.close()

    result = m._verify_pdf(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{target}: {failed}"


def test_process_near_noop_target_still_consistent(fixture_name):
    """Target чуть выше текущего среднего (K клипуется к ~1) — минимальные
    правки. На some файлах (напр. original: расходы структурно покрываются
    не только зарплатой, но и self-transfer-«Поступление»/«Зачисление»,
    которые никогда не масштабируются) near-noop таргет всё равно может
    потребовать коррекции running balance («Шаг 3» в recalculate_statement),
    из-за которой итоговый баланс уходит в минус — recalculate_statement
    теперь ЯВНО отказывает (IncomeTooLowError) вместо тихой записи PDF с
    визуально неверным (беззнаковым) отрицательным балансом (см. комментарий
    у new_balance_end в pdf_service.py). Оба исхода — либо согласованный
    результат, либо явный отказ — приемлемы; недопустима только тихая порча."""
    random.seed(1234)
    raw = _raw(fixture_name)
    v_before = m._verify_pdf(raw)
    current_avg = v_before["summary"]["avg_monthly_income"]

    try:
        out = p.process_pdf_bytes_raw(raw, current_avg * 1.05)
    except IncomeTooLowError:
        return

    result = m._verify_pdf(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], f"{fixture_name}@{current_avg * 1.05:,.0f}: {failed}"


# ─── Регрессия: REFUND_TYPE_WORDS / page_refund_cs_ys ────────────────────────

@pytest.mark.parametrize("fixture_name, target", [
    ("original", 4_100_000),
    ("scored", 19_100_000),
    ("small", 440_000),
    ("original2", 7_100_000),
    ("scored2", 19_100_000),
])
def test_every_income_transaction_gets_its_own_value(fixture_name, target):
    """Регрессия: раньше REFUND_TYPE_WORDS/y_has_refund_type в
    process_pdf_bytes_raw не распознавали self-transfer-строки
    ('Поступление'/'Зачисление') как «возврат» на уровне сырых байт (их
    тип-слово физически не лежит среди Td/Tj-токенов того же content-стрима,
    что и сумма). Их REFUND_IDENTITY-слот в очереди замены не потреблялся
    физической ячейкой и оставался «застрявшим» в начале общей по значению
    очереди "IN:<сумма>" — следующая ячейка с тем же числом (обычно
    зарплатная) получала ЕГО значение вместо своего, и так каскадно сдвигало
    ВСЕ последующие ячейки с этим числом (воспроизведено на
    kaspi_gold_cert_original.pdf: 179 из 536 «+»-ячеек получали чужое
    значение). Явно проверяем, что после обработки каждая транзакция находит
    ИМЕННО своё намеченное значение среди фактически записанных, сопоставляя
    по (page_num, date)."""
    random.seed(1234)
    raw = _raw(fixture_name)

    captured = {}
    _orig_recalc = p.recalculate_statement

    def _capture(stmt, tgt):
        result = _orig_recalc(stmt, tgt)
        captured["stmt"] = result
        return result

    p.recalculate_statement = _capture
    try:
        out = p.process_pdf_bytes_raw(raw, target)
    finally:
        p.recalculate_statement = _orig_recalc

    stmt = captured["stmt"]

    doc = fitz.open(stream=out, filetype="pdf")
    stmt_out = p.parse_full_statement(doc, start_page=1)
    doc.close()

    out_lookup: dict = defaultdict(list)
    for t in stmt_out.transactions:
        if t.sign == 1:
            out_lookup[(t.page_num, t.date)].append(t.amount)

    mismatches = []
    for t in stmt.transactions:
        if t.sign != 1:
            continue
        want = t.amount if t.is_refund else t.new_amount
        candidates = out_lookup.get((t.page_num, t.date), [])
        if not any(abs(c - want) < 0.5 for c in candidates):
            mismatches.append((t.page_num, t.date, t.is_refund, t.amount, want, candidates))

    assert not mismatches, (
        f"{fixture_name}@{target}: {len(mismatches)} транзакций получили чужое "
        f"значение: {mismatches[:5]}"
    )


# ─── Floor-проверки (IncomeTooLowError) ───────────────────────────────────────

@pytest.mark.parametrize("fixture_name", ["original", "small", "original2"])
def test_downscale_below_floor_raises(fixture_name):
    """На ЭТИХ файлах-фикстурах floor (below_balance_floor) выше текущего
    среднего зарплатного дохода — на original/small потому что расходы
    карты значительной частью покрываются self-transfer'ами с депозита, а
    не зарплатой; на original2 (без self-transfer вовсе) — просто потому,
    что реальные расходы этой карты близки к доходу и не оставляют запаса
    — т.е. ЛЮБОЙ downscale-запрос ниже текущего среднего гарантированно
    должен быть отклонён IncomeTooLowError. Это не баг, а реальная
    особенность данных; фиксируем текущее поведение как регрессионный
    барьер. "scored"/"scored2" сюда НЕ входят — см.
    test_downscale_success_when_above_floor ниже: там floor (унаследованный
    от того же расходного профиля) уже НИЖЕ среднего, раздутого предыдущим
    прогоном /process, и downscale реально работает."""
    raw = _raw(fixture_name)
    v_before = m._verify_pdf(raw)
    current_avg = v_before["summary"]["avg_monthly_income"]

    with pytest.raises(IncomeTooLowError):
        process_downscale(raw, current_avg * 0.9)


@pytest.mark.parametrize("fixture_name", ["scored", "scored2"])
def test_downscale_success_when_above_floor(fixture_name):
    """"scored"/"scored2" — единственные фикстуры, где floor реально НИЖЕ
    текущего среднего (среднее раздуто предыдущим прогоном /process) — это
    единственные реальные образцы, на которых можно проверить, что
    downscale как таковой (не только его floor-guard) физически работает и
    даёт согласованный результат."""
    random.seed(1234)
    raw = _raw(fixture_name)
    v_before = m._verify_pdf(raw)
    current_avg = v_before["summary"]["avg_monthly_income"]

    out = process_downscale(raw, current_avg * 0.5)
    result = m._verify_pdf(out)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["passed"], failed


# ─── Регрессия: справка (стр. 0) должна отражать новый баланс выписки ────────
#
# Найдено на реальном файле (не входит в репозиторий): после апскейла страница
# «Справка об остатке» (стр. 0) физически не менялась ни одним байтом — cert
# показывал СТАРЫЙ остаток (напр. ₸ 364,16), а приложенная выписка на стр. 1+
# заканчивалась совершенно другим новым балансом (напр. ₸ 128 245 609,16).
# Причина: recalculate_with_certificate() считает cert.new_balance_kzt/usd/eur,
# но ничего не кладёт под эти значения в replacement_queue — а код, который эту
# очередь ЧИТАЕТ по ключам "CERT_KZT:"/"CERT_USD:"/"CERT_EUR:" (в
# process_pdf_bytes_raw), уже присутствует и просто никогда не находит совпадений.
# Ниже — юнит-тесты на функцию, которая должна строить эти ключи, без PDF
# round-trip (тестовые PDF-фикстуры сейчас недоступны на диске).

def test_build_cert_replacement_entries_writes_new_kzt_balance():
    cert = p.CertificateData(
        balance_kzt=364.16, balance_kzt_text="364,16",
        new_balance_kzt=128_245_609.16,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert entries["CERT_KZT:36416"][0] == pytest.approx(128_245_609.16)


def test_build_cert_replacement_entries_strips_thousands_separator():
    """Оригинальный текст на справке содержит пробелы как разделители тысяч
    ("1 234 567,89") — ключ должен собираться из голых цифр, как и ожидает
    читающий код (clean_key в cert_paren_callback), иначе замена не найдётся."""
    cert = p.CertificateData(
        balance_kzt=1_234_567.89, balance_kzt_text="1 234 567,89",
        new_balance_kzt=2_000_000.00,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert "CERT_KZT:123456789" in entries
    assert entries["CERT_KZT:123456789"][0] == pytest.approx(2_000_000.00)


def test_build_cert_replacement_entries_includes_usd_eur_when_rate_known():
    cert = p.CertificateData(
        balance_kzt=364.16, balance_kzt_text="364,16", new_balance_kzt=128_245_609.16,
        balance_usd=0.78, balance_usd_text="0,78", new_balance_usd=274_713.05,
        balance_eur=0.68, balance_eur_text="0,68", new_balance_eur=314_812.00,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert entries["CERT_USD:078"][0] == pytest.approx(274_713.05)
    assert entries["CERT_EUR:068"][0] == pytest.approx(314_812.00)


def test_build_cert_replacement_entries_skips_usd_when_rate_unknown():
    """Если исходный USD/EUR-баланс был 0 (курс не восстановить), нельзя
    затирать значение на странице нулём — recalculate_with_certificate в этом
    случае оставляет new_balance_usd на дефолте (0.0), и записывать его нельзя."""
    cert = p.CertificateData(
        balance_kzt=364.16, balance_kzt_text="364,16", new_balance_kzt=128_245_609.16,
        balance_usd=0.0, balance_usd_text="", new_balance_usd=0.0,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert not any(k.startswith("CERT_USD:") for k in entries)


# ─── Регрессия: /verify должен видеть рассогласование справки и выписки ──────

def test_check_cert_balance_ok_when_matching():
    result = m._check_cert_balance(cert_balance_kzt=364.16, stmt_balance_end=364.16)
    assert result["ok"] is True


def test_check_cert_balance_fails_when_mismatched():
    """Это именно тот случай, который /verify пропускал: cert-баланс не
    менялся при обработке, поэтому после апскейла он расходится с новым
    balance_end на всю сумму прироста дохода."""
    result = m._check_cert_balance(cert_balance_kzt=364.16, stmt_balance_end=128_245_609.16)
    assert result["ok"] is False


# ─── Регрессия: «Целостность стримов» не должна ложно бить валидные PDF ──────

def _old_buggy_stream_check(raw: bytes) -> int:
    """Воспроизводит СТАРУЮ эвристику (endswith \\r\\n / \\n), которую заменяет
    _check_stream_integrity — используется здесь только чтобы доказать разницу
    в поведении на одном и том же входе."""
    import zlib as _zlib
    stream_errors = 0
    for obj_m in re.finditer(rb"(\d+)\s+0\s+obj", raw):
        stream_start = raw.find(b"stream", obj_m.end(), obj_m.end() + 500)
        if stream_start < 0:
            continue
        ds = stream_start + 6
        if raw[ds:ds+1] == b'\r':
            ds += 2
        else:
            ds += 1
        es = raw.find(b"endstream", ds)
        if es < 0:
            continue
        sd = raw[ds:es]
        if sd.endswith(b'\r\n'):
            sd = sd[:-2]
        elif sd.endswith(b'\n'):
            sd = sd[:-1]
        try:
            _zlib.decompress(sd)
        except Exception:
            stream_errors += 1
    return stream_errors


def _make_synthetic_stream_pdf(payload: bytes) -> bytes:
    """Строит один-object synthetic PDF-фрагмент: 'stream\\n<compressed>\\n
    endstream'. Единственный (не двухбайтовый \\r\\n) разделитель после данных
    — это тот самый случай, где старая эвристика ошибается: если последний
    байт compressed САМ по себе 0x0D, то sd.endswith(b'\\r\\n') совпадёт по
    случайности (0x0D из payload + genuine 0x0A разделитель), хотя реальный
    разделитель — всего 1 байт, а не 2."""
    import zlib as _zlib
    compressed = _zlib.compress(payload)
    return (
        b"7 0 obj\n<< /Length " + str(len(compressed)).encode() + b" >>\nstream\n"
        + compressed + b"\nendstream\nendobj\n"
    )


def _find_payload_whose_compressed_form_ends_in_cr() -> bytes:
    """zlib.compress() детерминирован для одного и того же входа — перебираем
    случайные (но воспроизводимые, seed фиксирован) полезные нагрузки, пока
    сжатый результат не закончится на 0x0D ('\\r'), то есть байтом, который
    старая эвристика ошибочно примет за начало trailing CRLF перед endstream."""
    import zlib as _zlib
    rng = random.Random(42)
    for _ in range(10000):
        payload = bytes(rng.randrange(256) for _ in range(40))
        if _zlib.compress(payload).endswith(b"\r"):
            return payload
    raise AssertionError("не удалось подобрать payload для регрессии — увеличьте диапазон")


def test_stream_integrity_length_based_survives_trailing_cr_byte():
    """Регрессия: если сжатые байты потока сами заканчиваются на 0x0D, старая
    эвristика (main.py, до фикса) отрезает лишний байт полезной нагрузки,
    считая его половиной CRLF перед 'endstream', и decompress падает на
    полностью корректном PDF — ложное 'N битых стримов'. Новый метод режет
    строго по /Length и не зависит от того, чем заканчивается сжатый поток."""
    payload = _find_payload_whose_compressed_form_ends_in_cr()
    raw = _make_synthetic_stream_pdf(payload)

    # Подтверждаем, что старая эвристика на этом же входе ломается — иначе
    # тест ничего не доказывает.
    assert _old_buggy_stream_check(raw) == 1

    assert m._check_stream_integrity(raw) == 0


# ─── Регрессия: salary-транзакция с new_amount == amount должна ────────────
# ─── всё равно резервировать свой слот в очереди замен ──────────────────────
#
# Найдено на реальном файле (не в репозитории): при апскейле с таргетом, где
# K_month одного конкретного месяца округлился близко к 1.0, часть salary-
# транзакций в этом месяце получают new_amount == amount. process_pdf_bytes_raw
# (до фикса) пропускал добавление записи в replacement_queue для ТАКИХ
# транзакций (условие `tx.new_amount != tx.amount`) — хотя комментарий в самом
# коде прямо заявляет "ВСЕ транзакции sign==+1 идут в IN: очередь". Refund-
# транзакции всегда резервируют identity-слот (REFUND_IDENTITY) именно по этой
# причине; salary-транзакции с K≈1 — нет. Итог: 28 из 1334 доходных
# транзакций в реальном файле получали ЧУЖОЕ значение — та же каскадная
# FIFO-подмена, что уже была задокументирована для self-transfer/возвратов
# (TX_TYPES_SELF_TRANSFER, REFUND_IDENTITY), но с новым триггером.

def test_income_replacement_entries_reserves_slot_even_when_amount_unchanged():
    stmt = p.StatementData()
    stmt.transactions = [
        p.Transaction(index=0, sign=1, is_salary=True, is_refund=False,
                      amount=85_000.0, new_amount=141_500.0,
                      original_amount_text="+ 85 000,00 ₸"),
        # K_month ≈ 1 для этой транзакции — new_amount совпал со старым.
        p.Transaction(index=1, sign=1, is_salary=True, is_refund=False,
                      amount=85_000.0, new_amount=85_000.0,
                      original_amount_text="+ 85 000,00 ₸"),
        p.Transaction(index=2, sign=1, is_salary=True, is_refund=False,
                      amount=85_000.0, new_amount=143_000.0,
                      original_amount_text="+ 85 000,00 ₸"),
    ]
    queue = p.build_income_replacement_entries(stmt)
    entries = list(queue["IN:85000,00"])
    assert len(entries) == 3, f"ожидалось 3 слота (по одному на транзакцию), получили {entries}"
    assert [v for v, _ in entries] == [141_500.0, 85_000.0, 143_000.0]


def test_income_replacement_entries_refund_reserves_identity_slot():
    stmt = p.StatementData()
    stmt.transactions = [
        p.Transaction(index=0, sign=1, is_salary=False, is_refund=True,
                      amount=20_000.0, new_amount=0.0,
                      original_amount_text="+ 20 000,00 ₸"),
    ]
    queue = p.build_income_replacement_entries(stmt)
    entries = list(queue["IN:20000,00"])
    assert entries == [(20_000.0, "REFUND_IDENTITY")]


# ─── Регрессия: recalculate_with_certificate игнорировала recalc_fn ─────────
#
# Найдено на реальном файле (не в репозитории): process_pdf_bytes_raw для
# fmt=="cert" всегда вызывал recalculate_with_certificate(cert, stmt, target),
# а та ЖЁСТКО вызывала recalculate_statement() (upscale-движок) внутри себя,
# полностью игнорируя recalc_fn, переданный вызывающей стороной.
# pdf_service_downscale.process_downscale() передаёт
# recalc_fn=recalculate_statement_downscale именно чтобы получить downscale-
# движок с его тремя floor-проверками (IncomeTooLowError) — для cert-формата
# (текущий формат Kaspi Gold, введён в 2026) этот движок никогда не
# запускался. На практике это означало: запрос "уменьшить доход" на
# cert-формате проходил БЕЗ единой ошибки, но фактически исполнялся
# upscale-движком — доход не уменьшался, а мог даже слегка вырасти.

def test_recalculate_with_certificate_uses_custom_recalc_fn():
    calls = []

    def fake_recalc(stmt, target):
        calls.append(target)
        stmt.new_balance_end = 999.0
        return stmt

    cert = p.CertificateData(balance_kzt=100.0, balance_kzt_text="100,00")
    stmt = p.StatementData(balance_start=0.0, balance_end=100.0)
    stmt.transactions = []

    _, out_stmt = p.recalculate_with_certificate(cert, stmt, 12_345.0, recalc_fn=fake_recalc)

    assert calls == [12_345.0], "recalc_fn не был вызван с переданным таргетом"
    assert out_stmt.new_balance_end == 999.0, "recalculate_with_certificate не использовала результат recalc_fn"


def test_recalculate_with_certificate_defaults_to_upscale_engine():
    """Без явного recalc_fn поведение не должно измениться (upscale по умолчанию)."""
    cert = p.CertificateData(balance_kzt=29_880.80, balance_kzt_text="29 880,80")
    stmt = p.StatementData(balance_start=29_880.80, balance_end=29_880.80)
    stmt.transactions = []

    cert_out, stmt_out = p.recalculate_with_certificate(cert, stmt, 100_000.0)
    assert cert_out.new_balance_kzt == stmt_out.new_balance_end


# ─── Регрессия: pre-check в process_downscale парсил cert-формат со стр. 0 ──
#
# Тот же паттерн, что уже учтён в /process (main.py) и process_pdf_bytes_raw
# (см. комментарий в main.py:_verify_pdf про cert-формат: реальная выписка
# начинается со стр. 1, стр. 0 — справка) — но pdf_service_downscale.py
# отдельно делал pre-check floor'ов ДО тяжёлой обработки PDF и не применял ту
# же сдвижку. На реальном файле это совпадение не проявлялось (парсер по
# факту falls back на сумму транзакций), но полагаться на такое совпадение
# нельзя — это тот же класс бага, который для main.py._verify_pdf уже был
# явно исправлен.

_ARIAL_PATH = r"C:\Windows\Fonts\arial.ttf"


@pytest.mark.skipif(not os.path.exists(_ARIAL_PATH), reason="нужен Arial с поддержкой кириллицы для синтетической cert-страницы")
def test_process_downscale_precheck_uses_correct_start_page_for_cert_format():
    import pdf_service_downscale as pd

    doc = fitz.open()
    page0 = doc.new_page()
    page0.insert_text(
        (50, 50), "СПРАВКА об остатке на счете",
        fontfile=r"C:\Windows\Fonts\arial.ttf", fontname="F0",
    )
    raw = doc.write()
    doc.close()

    captured_start_pages = []
    _orig_parse = pd.parse_full_statement

    def _spy(doc_arg, start_page=0):
        captured_start_pages.append(start_page)
        stmt = p.StatementData(balance_start=1000.0, balance_end=1000.0, total_expense=500.0)
        stmt.transactions = [
            p.Transaction(index=0, sign=1, is_salary=True, amount=1000.0, date="01.01.26", new_amount=1000.0),
        ]
        return stmt

    pd.parse_full_statement = _spy
    try:
        try:
            pd.process_downscale(raw, 100_000.0)
        except Exception:
            pass  # интересует только start_page, с которым вызван parse_full_statement
    finally:
        pd.parse_full_statement = _orig_parse

    assert captured_start_pages, "parse_full_statement (pre-check) не был вызван"
    assert captured_start_pages[0] == 1, (
        f"pre-check должен парсить cert-формат со стр. 1 (справка на стр. 0), "
        f"а не start_page={captured_start_pages[0]}"
    )


# ─── Регрессия: downscale перезаписывал total_expense суммой категорий ──────
#
# Найдено на реальном файле (не в репозитории): после исправления recalc_fn
# (выше) downscale наконец пошёл в правильном направлении, но "Баланс
# (транзакции)" в /verify стал падать с Δ = -2 539 262.48 ₸ на выходном PDF.
# Причина: recalculate_statement_downscale() в конце безусловно перезаписывал
# stmt.total_expense суммой stmt.expense_categories.values() (сырые числа из
# шапки PDF) — но pdf_service.py::recalculate_statement (upscale-движок)
# явно НЕ делает так же, с комментарием "там дублируются строки типа
# 'Переводы' и 'Переводы на свои счета' (одинаковый ключ → перезапись)" —
# т.е. сумма категорий может не совпадать с уже корректным total_expense,
# посчитанным при парсинге через уравнение баланса. new_balance_end считался
# с этим неверным total_expense, ломая тождество на уровне транзакций.

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


def test_stream_integrity_detects_actually_corrupted_stream():
    raw = _make_synthetic_stream_pdf(b"some normal payload")
    # Портим сами compressed-байты, не меняя /Length
    broken = bytearray(raw)
    stream_pos = raw.find(b"stream\n") + len(b"stream\n")
    broken[stream_pos] ^= 0xFF
    assert m._check_stream_integrity(bytes(broken)) == 1


# ─── min_dayend_balance: внутридневной порядок не создаёт ложный овердрафт ────
# Эти тесты НЕ требуют фикстур — конструируют транзакции вручную, поэтому
# работают в любом чекауте (в т.ч. без tests/fixtures/*.pdf).

def _tx(date, sign, amount):
    return p.Transaction(index=0, date=date, sign=sign, amount=amount, new_amount=amount)


def test_dayend_balance_ignores_intraday_dip():
    """Пять дебетов, за которыми идут покрывающие их кредиты ТОГО ЖЕ дня, не
    должны считаться овердрафтом: внутридневного порядка у Kaspi нет, и min
    замеряется только на границе дня. Это ровно тот −54,17 ₸ на 12.12.25, из-за
    которого немодифицированный реальный gold_statement.pdf проваливал /verify.

    Транзакции хранятся от НОВЫХ к СТАРЫМ (как в PDF); обход идёт reversed().
    Внутри дня 02.02.26 в порядке обхода сначала пойдут дебеты (−100 суммарно),
    уводя внутридневной баланс в −50, но кредиты (+90) того же дня закрывают
    его до +40 к границе дня.
    """
    txs = [
        # новые → старые; внутри 02.02.26: кредиты стоят ПЕРВЫМИ в списке,
        # значит в reversed() они окажутся ПОСЛЕ дебетов → внутридневной минус
        _tx("02.02.26", +1, 40.0),
        _tx("02.02.26", +1, 50.0),
        _tx("02.02.26", -1, 30.0),
        _tx("02.02.26", -1, 30.0),
        _tx("02.02.26", -1, 40.0),
    ]
    # balance_start = 50 (самая ранняя дата = конец списка, дата 01... нет — тут
    # одна дата; старт 50): дебеты −100 уводят в −50 внутри дня, кредиты +90
    # возвращают к +40 на границе дня.
    min_rb, final_rb = p.min_dayend_balance(txs, balance_start=50.0)
    assert final_rb == 40.0          # 50 − 100 + 90
    assert min_rb >= 0.0             # на границе дня минуса нет
    # Контроль: наивный per-transaction минимум БЫЛ бы отрицательным
    naive = 50.0
    naive_min = naive
    for t in reversed(txs):
        naive += t.sign * t.amount
        naive_min = min(naive_min, naive)
    assert naive_min < 0.0           # именно это раньше ловилось как ложный минус


def test_dayend_balance_catches_real_overdraft():
    """Если баланс отрицателен НА ГРАНИЦЕ дня (реальный овердрафт) — ловим."""
    txs = [
        _tx("03.02.26", +1, 10.0),   # позже
        _tx("02.02.26", -1, 100.0),  # раньше: уводит день в минус и он там и кончается
    ]
    min_rb, final_rb = p.min_dayend_balance(txs, balance_start=50.0)
    assert min_rb < 0.0              # конец дня 02.02.26 = 50 − 100 = −50


def test_dayend_balance_final_is_order_independent():
    """Финальный RB = balance_start + Σ(sign·amount) вне зависимости от порядка."""
    txs = [_tx("02.02.26", +1, 100.0), _tx("01.02.26", -1, 30.0)]
    min_rb, final_rb = p.min_dayend_balance(txs, balance_start=5.0)
    assert final_rb == 75.0          # 5 − 30 + 100


# ─── first_negative_dayend + коррекция «горбатого» дохода (без фикстур) ───────

def _tx_full(date, sign, amount, is_salary=False, is_refund=False, description=""):
    return p.Transaction(index=0, date=date, sign=sign, amount=amount, new_amount=amount,
                         is_salary=is_salary, is_refund=is_refund, description=description)


def test_first_negative_dayend_reports_date():
    """Возвращает дату самой ранней ОТРИЦАТЕЛЬНОЙ границы дня (не внутридневной)."""
    txs = [
        _tx_full("03.02.26", +1, 10.0),   # новее
        _tx_full("02.02.26", -1, 100.0),  # старее: конец дня 02.02.26 = 50-100 = -50
    ]
    min_rb, neg_date = p.first_negative_dayend(txs, balance_start=50.0)
    assert min_rb < 0
    assert neg_date == "02.02.26"


def test_first_negative_dayend_none_when_all_positive():
    txs = [_tx_full("02.02.26", +1, 100.0), _tx_full("01.02.26", -1, 30.0)]
    min_rb, neg_date = p.first_negative_dayend(txs, balance_start=50.0)
    assert neg_date is None


def test_humped_income_no_longer_needs_floor_expense_absorbs_it():
    """«Горбатый» доход: крупный зарплатный месяц тратится в том же месяце.
    РАНЬШЕ движок поднимал зарплату (Шаг 3), что двигало balance_end — теперь
    balance_end заморожен, поэтому просадку чинит перенос РАСХОДА во времени:
    расход месяца-«горба» растёт МЕНЬШЕ, чем позднее (после дня дефицита)
    расходы, и итоговый баланс не сдвигается ни на тенге."""
    random.seed(0)
    # newest → oldest (как в PDF). Хронологически: 07 → 11 → 12; в каждом месяце
    # зарплата (раньше) предшествует расходу (позже) — валидный оригинал.
    txs = [
        _tx_full("20.12.25", -1, 1_900_000.0, description="Покупка"),
        _tx_full("10.12.25", +1, 2_000_000.0, is_salary=True, description="Пополнение"),
        _tx_full("20.11.25", -1, 7_500_000.0, description="Перевод"),   # тратит «горб»
        _tx_full("10.11.25", +1, 8_000_000.0, is_salary=True, description="Пополнение"),  # «горб»
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


# ─── Downscale engine: успешное занижение при наличии запаса (без фикстур) ────

def test_downscale_succeeds_with_slack():
    """Движок downscale (recalculate_statement_downscale) должен успешно занижать
    доход, когда в выписке есть запас (баланс не впритык к нулю). На всех 8
    реальных Kaspi Gold файлах, которыми располагает этот репозиторий, реальные
    расходы клиента практически равны реальному доходу (баланс держится только
    за счёт убывающего стартового остатка) — там downscale физически невозможен
    даже на 0.1% (это свойство ДАННЫХ клиента, не баг движка). Здесь же строим
    синтетическую выписку с явным запасом (доход намного выше расходов, большой
    стартовый баланс), чтобы подтвердить: сам МЕХАНИЗМ downscale (floor-расчёт,
    day-boundary running balance) работает корректно, когда условия позволяют.
    """
    from pdf_service_downscale import (
        recalculate_statement_downscale, is_downscale_request,
        compute_min_target_monthly_income, IncomeTooLowError,
    )

    # balance_end теперь ЗАМОРОЖЕН (см. recalculate_statement_downscale) — "запас
    # на занижение" определяется тем, НАСКОЛЬКО МАЛО (balance_end - balance_start)
    # относительно диапазона целей, а не тем, насколько расход мал относительно
    # дохода (как было при старой, дорасходной модели). Здесь income ≈ expense
    # (баланс почти не растёт за период), поэтому даже сильно заниженный доход
    # оставляет требуемому расходу место остаться положительным.
    def make_stmt():
        txs = [
            _tx_full("20.03.26", -1, 4_700_000.0, description="Покупка"),
            _tx_full("10.03.26", +1, 5_000_000.0, is_salary=True, description="Пополнение"),
            _tx_full("20.02.26", -1, 4_700_000.0, description="Покупка"),
            _tx_full("10.02.26", +1, 5_000_000.0, is_salary=True, description="Пополнение"),
            _tx_full("20.01.26", -1, 4_700_000.0, description="Покупка"),
            _tx_full("10.01.26", +1, 5_000_000.0, is_salary=True, description="Пополнение"),
        ]
        return p.StatementData(
            balance_start=2_000_000.0, balance_end=2_000_000.0 + 15_000_000.0 - 14_100_000.0,
            total_income=15_000_000.0, total_expense=14_100_000.0, transactions=txs,
        )

    stmt = make_stmt()
    avg = 5_000_000.0
    min_target = compute_min_target_monthly_income(stmt)
    assert min_target < avg * 0.5, "с таким запасом floor должен быть далеко ниже среднего"

    for mult in (0.9, 0.7, 0.5, 0.35):
        random.seed(3)
        stmt_copy = make_stmt()
        target = avg * mult
        assert is_downscale_request(stmt_copy, target)
        result = recalculate_statement_downscale(stmt_copy, target)  # не должно бросить
        min_rb, final_rb = p.min_dayend_balance(result.transactions, result.balance_start, "new_amount")
        assert min_rb >= -0.01, f"x{mult}: баланс ушёл в минус: {min_rb}"
        assert result.new_balance_end >= 0, f"x{mult}: итоговый баланс отрицателен"

    # Ниже MAX_DOWNSCALE_FACTOR (0.30 от среднего) — должен сработать floor "too_aggressive"
    from pdf_service_downscale import MAX_DOWNSCALE_FACTOR
    stmt_copy = make_stmt()
    with pytest.raises(IncomeTooLowError) as exc_info:
        recalculate_statement_downscale(stmt_copy, avg * (MAX_DOWNSCALE_FACTOR - 0.05))
    assert exc_info.value.reason == "too_aggressive"


# ─── _scale_expense_categories: точное попадание в сумму (без фикстур) ────────
# Часть плана "заморозка баланса/справки Kaspi Gold" (2026-08-11) — расход
# теперь производная величина, категории шапки должны масштабироваться так,
# чтобы их сумма ТОЧНО совпадала с новым total_expense.

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


# ─── _scale_debit_transactions_exact: точное попадание в сумму (без фикстур) ──

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


# ─── build_cert_replacement_entries: не трогать, если значение не изменилось ──

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
    изменилось."""
    cert = p.CertificateData(
        balance_kzt_text="143 170,28", balance_kzt=143170.28,
        new_balance_kzt=200000.00, new_balance_usd=0.0, new_balance_eur=0.0,
    )
    entries = p.build_cert_replacement_entries(cert)
    assert "CERT_KZT:14317028" in entries
    assert entries["CERT_KZT:14317028"][0] == 200000.00

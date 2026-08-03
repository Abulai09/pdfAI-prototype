"""
Модуль занижения дохода (downscale) для скоринга Kaspi Gold.

Изолирован от рабочего алгоритма завышения. Использует ту же
проверенную бинарную подмену через `pdf_service.process_pdf_bytes_raw`,
но с собственной функцией пересчёта математики (recalc_fn).

Главные отличия от upscale:
  • снят клип K<1 (теперь занижение реально работает);
  • введён жёсткий математический floor — ниже него raise IncomeTooLowError
    (нельзя занижать так, чтобы баланс ушёл в минус);
  • при min_rb < SAFETY_MARGIN после пересчёта — повторный raise с
    обновлённой рекомендацией;
  • никогда не получим отрицательный B_end (проблема знака «−» в header
    снимается архитектурно, а не патчем cmap).
"""
from __future__ import annotations

import random
from typing import Dict, List

import fitz

import pdf_service
from pdf_service import (
    StatementData,
    Transaction,
    _get_month_key,
    _round_to_natural,
    min_dayend_balance,
    parse_full_statement,
)


# ─────────────────────────────────────────────────────────────────
#  Константы
# ─────────────────────────────────────────────────────────────────

# Минимальный финальный остаток на счёте (₸).
# Ниже — палево: «впритык к нулю» легко детектится.
SAFETY_MARGIN = 100_000.0

# Максимально допустимое занижение зарплаты относительно текущего среднего.
# 0.30 = занижение более чем в 3.3 раза подозрительно.
MAX_DOWNSCALE_FACTOR = 0.30

# Совпадение с main.py: банк показывает ~39.14% от оборота как «доход».
# Используется только для UI-подсказки (min_desired_income).
INCOME_K = 0.3914


# ─────────────────────────────────────────────────────────────────
#  Исключение
# ─────────────────────────────────────────────────────────────────


class IncomeTooLowError(Exception):
    """
    Запрошенный доход ниже математически допустимого минимума.

    Атрибуты:
        min_target_monthly_income — минимально допустимый ср.доход/мес (₸).
        min_desired_income        — соответствующее «желаемое» значение для UI
                                     (с учётом INCOME_K).
        current_expense           — суммарные расходы за период (₸).
        current_monthly_avg       — текущий ср.зарплатный доход/мес (₸).
        n_months                  — кол-во месяцев в выписке.
        reason                    — машинно-читаемая причина:
                                     "below_balance_floor"
                                     "too_aggressive"
                                     "post_check_negative_balance".
        message                   — человеко-читаемая подсказка.
    """

    def __init__(
        self,
        min_target_monthly_income: float,
        current_expense: float,
        current_monthly_avg: float,
        n_months: int,
        reason: str,
        message: str,
    ):
        self.min_target_monthly_income = round(min_target_monthly_income, 2)
        self.min_desired_income = round(min_target_monthly_income * INCOME_K, 2)
        self.current_expense = round(current_expense, 2)
        self.current_monthly_avg = round(current_monthly_avg, 2)
        self.n_months = n_months
        self.reason = reason
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "reason": self.reason,
            "min_target_monthly_income": self.min_target_monthly_income,
            "min_desired_income": self.min_desired_income,
            "current_expense": self.current_expense,
            "current_monthly_avg": self.current_monthly_avg,
            "n_months": self.n_months,
        }


# ─────────────────────────────────────────────────────────────────
#  Helper
# ─────────────────────────────────────────────────────────────────


def _count_months(stmt: StatementData) -> int:
    """Кол-во уникальных месяцев в зарплатных транзакциях."""
    months = set()
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date)
            if mk:
                months.add(mk)
    return max(len(months), 1)


def compute_min_target_monthly_income(stmt: StatementData) -> float:
    """
    Минимально допустимый ср. зарплатный доход/мес для безопасного занижения.

    Формула:
        min_target = (total_expense - balance_start + SAFETY_MARGIN) / n_months

    Гарантирует: B_end = B_start + income - expense ≥ SAFETY_MARGIN.
    """
    n = _count_months(stmt)
    required_total_income = stmt.total_expense - stmt.balance_start + SAFETY_MARGIN
    if required_total_income <= 0:
        # Стартового баланса хватает покрыть все расходы — занижение не лимитировано.
        return 0.0
    return required_total_income / n


def _current_monthly_avg(stmt: StatementData) -> float:
    salary_total = sum(t.amount for t in stmt.transactions if t.is_salary)
    n = _count_months(stmt)
    return salary_total / max(n, 1)


def is_downscale_request(stmt: StatementData, target_monthly_income: float) -> bool:
    """True, если запрос на занижение (target ниже текущего среднего)."""
    return target_monthly_income < _current_monthly_avg(stmt)


# ─────────────────────────────────────────────────────────────────
#  Движок пересчёта (downscale)
# ─────────────────────────────────────────────────────────────────


def recalculate_statement_downscale(
    stmt: StatementData, target_monthly_income: float
) -> StatementData:
    """
    Пересчёт выписки в режиме занижения дохода.

    Алгоритм идентичен upscale-движку с одним отличием:
      • разрешено K<1 (занижение salary);
      • перед расчётом — три проверки floor; нарушение → raise IncomeTooLowError;
      • при min_rb<0 после первого прогона — пересчёт min_target и raise.
    """
    salary_transactions = [t for t in stmt.transactions if t.is_salary]
    refund_transactions = [t for t in stmt.transactions if t.is_refund]

    if not salary_transactions:
        print("[Downscale] ⚠️ Не найдено зарплатных транзакций")
        return stmt

    current_salary_income = sum(t.amount for t in salary_transactions)
    current_refund_total = sum(t.amount for t in refund_transactions)

    # ── Группировка SALARY доходов по месяцам ──
    monthly_income: Dict[str, float] = {}
    for tx in salary_transactions:
        mk = _get_month_key(tx.date) or "unknown"
        monthly_income[mk] = monthly_income.get(mk, 0) + tx.amount

    n_months = len([k for k in monthly_income if k != "unknown"]) or 1
    current_monthly_avg = current_salary_income / n_months

    if current_monthly_avg <= 0:
        print("[Downscale] ⚠️ Текущий доход = 0, нечего занижать")
        return stmt

    # ── ПРОВЕРКА 1: target ≥ min_target (баланс не уходит в минус) ──
    min_target = compute_min_target_monthly_income(stmt)
    if target_monthly_income < min_target:
        raise IncomeTooLowError(
            min_target_monthly_income=min_target,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="below_balance_floor",
            message=(
                f"Слишком низкий целевой доход. При расходах "
                f"{stmt.total_expense:,.0f} ₸ и стартовом балансе "
                f"{stmt.balance_start:,.0f} ₸ за {n_months} мес "
                f"минимально возможный ср. доход = "
                f"{min_target:,.0f} ₸/мес "
                f"(желаемый ≥ {min_target * INCOME_K:,.0f} ₸/мес)."
            ),
        )

    # ── ПРОВЕРКА 2: target ≥ MAX_DOWNSCALE_FACTOR × current_avg ──
    floor_aggressive = current_monthly_avg * MAX_DOWNSCALE_FACTOR
    if target_monthly_income < floor_aggressive:
        raise IncomeTooLowError(
            min_target_monthly_income=floor_aggressive,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="too_aggressive",
            message=(
                f"Слишком резкое занижение: запрошено "
                f"{target_monthly_income:,.0f} ₸/мес, текущий ср. доход "
                f"{current_monthly_avg:,.0f} ₸/мес. Минимум "
                f"({MAX_DOWNSCALE_FACTOR * 100:.0f}% от текущего) = "
                f"{floor_aggressive:,.0f} ₸/мес."
            ),
        )

    global_K = target_monthly_income / current_monthly_avg

    print(f"\n{'═' * 60}")
    print(f"  ДВИЖОК ЗАНИЖЕНИЯ ДОХОДА (downscale)")
    print(f"{'═' * 60}")
    print(f"  Текущий ср. зарплатный/мес: {current_monthly_avg:>14,.2f} ₸")
    print(f"  Целевой доход/мес:          {target_monthly_income:>14,.2f} ₸")
    print(f"  Глобальный K:               {global_K:>14.4f}  (<1 — занижение)")
    print(f"  Месяцев в выписке:          {n_months}")
    print(f"  Зарплатных транзакций:      {len(salary_transactions)}")
    print(f"  Возвратов (не масштабируем): {len(refund_transactions)} "
          f"(Σ={current_refund_total:,.2f} ₸)")
    print(f"  Min target (floor):         {min_target:>14,.2f} ₸")
    print(f"  Aggressive floor (30%):     {floor_aggressive:>14,.2f} ₸")
    print(f"{'═' * 60}")

    # ── Единый K на весь период (НЕ помесячный) ──
    # Раньше здесь считался K_month = target / доход_месяца для каждого
    # месяца отдельно, что выравнивало все месяцы к одной и той же сумме —
    # тот же баг, что и в pdf_service.recalculate_statement (см. CLAUDE.md,
    # "Исправлено 2026-08-03"). Единый K сохраняет естественный помесячный
    # разброс оригинала, только равномерно масштабированный к цели.
    print(f"\n  Единый K на весь период: {global_K:.4f} (без помесячного выравнивания)")
    month_K: Dict[str, float] = {}
    for mk in sorted(monthly_income.keys()):
        month_K[mk] = global_K
        if mk != "unknown":
            mi = monthly_income[mk]
            print(f"    {mk}: доход {mi:>14,.2f} → ×{global_K:.4f} ≈ {mi * global_K:>14,.2f}")

    # ── Расходы НЕ масштабируем ──
    print(f"\n  K_exp (расходы):  1.0000 (расходы НЕ масштабируются)")

    # ── Шаг 1: Масштабирование salary с дисперсией ──
    print(f"\n  Масштабирование транзакций:")
    for tx in stmt.transactions:
        if tx.sign == 1 and tx.is_salary and not tx.is_refund:
            mk = _get_month_key(tx.date) or "unknown"
            k = month_K.get(mk, global_K)
            epsilon = random.uniform(-0.03, 0.03)
            tx.new_amount = _round_to_natural(tx.amount * k * (1 + epsilon), original=tx.amount)
        else:
            tx.new_amount = tx.amount

    # ── Шаг 2: Running balance ──
    # Минимум замеряем на границах дней (min_dayend_balance): внутридневной
    # порядок операций произволен, дип между дебетами и покрывающими их
    # кредитами того же дня — не реальный овердрафт.
    current_rb = stmt.balance_start
    for tx in reversed(stmt.transactions):
        current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
        tx.new_balance_after = current_rb
    min_rb, _ = min_dayend_balance(stmt.transactions, stmt.balance_start, "new_amount")

    # ── ПРОВЕРКА 3: min_rb ≥ 0 (post-check) ──
    # Из-за epsilon-разброса возможен небольшой выход в минус. Если так —
    # точечно поднимаем salary (×1.02) до восстановления, max 5 итераций.
    # Если не получилось — raise с уточнённым min_target.
    if min_rb < 0:
        print(f"\n  ⚠️ После пересчёта min_rb={min_rb:,.2f}, поднимаем salary")
        for attempt in range(5):
            for tx in stmt.transactions:
                if tx.sign == 1 and tx.is_salary and not tx.is_refund:
                    # _round_to_natural, не round(x, 2) — реальные Kaspi Gold
                    # salary всегда целые тенге в «человеческом» шаге; голый
                    # round(x, 2) после нескольких итераций ×1.02 оставляет
                    # произвольные копейки (тот же класс проблемы, что и в
                    # recalculate_statement's Шаг 3 — см. её комментарий).
                    tx.new_amount = _round_to_natural(tx.new_amount * 1.02, original=tx.amount)
            current_rb = stmt.balance_start
            for tx in reversed(stmt.transactions):
                current_rb = round(current_rb + tx.sign * tx.new_amount, 2)
                tx.new_balance_after = current_rb
            min_rb, _ = min_dayend_balance(stmt.transactions, stmt.balance_start, "new_amount")
            if min_rb >= 0:
                print(f"  ✅ Скорректировано за {attempt + 1} итераций, "
                      f"min_rb={min_rb:,.2f}")
                break
        else:
            # Не смогли — это значит floor рассчитан неверно для данной выписки
            # (например, расходы сосредоточены в начале периода, а доходы — в конце).
            # Поднимаем рекомендацию на 10% и raise.
            new_min = max(min_target, target_monthly_income) * 1.10
            raise IncomeTooLowError(
                min_target_monthly_income=new_min,
                current_expense=stmt.total_expense,
                current_monthly_avg=current_monthly_avg,
                n_months=n_months,
                reason="post_check_negative_balance",
                message=(
                    f"Не удалось удержать неотрицательный баланс при "
                    f"{target_monthly_income:,.0f} ₸/мес "
                    f"(min_rb={min_rb:,.0f} ₸). Минимально рекомендуемый "
                    f"доход: {new_min:,.0f} ₸/мес."
                ),
            )

    # ── Итоги (формулы Kaspi) ──
    salary_income_pos = sum(
        tx.new_amount for tx in stmt.transactions
        if tx.is_salary and not tx.is_refund
    )
    refund_topups_neg = sum(
        tx.amount for tx in stmt.transactions
        if tx.description == "Пополнение" and tx.sign == -1
    )
    stmt.new_total_income = round(salary_income_pos - refund_topups_neg, 2)

    # Расходы: оставляем ОРИГИНАЛЬНЫЕ из header PDF (вычислены по уравнению
    # баланса при парсинге) — как и upscale-движок (pdf_service.py,
    # recalculate_statement). НЕ пересчитываем через expense_categories: там
    # дублируются строки типа «Переводы» и «Переводы на свои счета»
    # (одинаковый ключ → перезапись), из-за чего сумма категорий может не
    # совпадать с уже корректным stmt.total_expense — это ломало тождество
    # баланса на уровне транзакций (воспроизведено на реальном файле:
    # Δ = -2 539 262.48 ₸ после исправления recalc_fn выше).
    stmt.new_balance_end = round(
        stmt.balance_start + stmt.new_total_income - stmt.total_expense, 2
    )

    # Категории — без изменений
    if stmt.expense_categories:
        for cat, old_val in stmt.expense_categories.items():
            stmt.new_expense_categories[cat] = old_val

    # ── Помесячная статистика ──
    new_monthly: Dict[str, float] = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            mk = _get_month_key(tx.date) or "unknown"
            new_monthly[mk] = new_monthly.get(mk, 0) + tx.new_amount

    print(f"\n  {'─' * 50}")
    print(f"  Новый доход по месяцам:")
    for mk in sorted(new_monthly.keys()):
        deviation = (
            (new_monthly[mk] - target_monthly_income) / target_monthly_income * 100
        )
        print(f"    {mk}: {new_monthly[mk]:>14,.2f} ₸ ({deviation:>+5.1f}%)")

    new_avg = sum(new_monthly.values()) / max(len(new_monthly), 1)
    print(f"\n  Σ нового дохода:            {stmt.new_total_income:>14,.2f} ₸")
    print(f"  Σ расходов:                 {stmt.total_expense:>14,.2f} ₸")
    print(f"  Новый баланс конец:         {stmt.new_balance_end:>14,.2f} ₸")
    print(f"  Новый средний доход/мес:    {new_avg:>14,.2f} ₸")
    print(f"  Целевой:                    {target_monthly_income:>14,.2f} ₸")
    print(f"  {'─' * 50}")

    # Финальная страховка: не должно остаться отрицательного B_end
    if stmt.new_balance_end < 0:
        raise IncomeTooLowError(
            min_target_monthly_income=min_target * 1.05,
            current_expense=stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="post_check_negative_balance",
            message=(
                f"Итоговый баланс отрицательный ({stmt.new_balance_end:,.0f} ₸). "
                f"Увеличьте целевой доход."
            ),
        )

    return stmt


# ─────────────────────────────────────────────────────────────────
#  Публичный entry-point
# ─────────────────────────────────────────────────────────────────


def process_downscale(
    input_bytes: bytes, target_monthly_income: float
) -> bytes:
    """
    Обрабатывает PDF в режиме занижения.

    Использует ту же бинарную подмену, что и upscale (через
    `pdf_service.process_pdf_bytes_raw`), но с recalc_fn=
    `recalculate_statement_downscale`. Перед обработкой делает
    предварительную проверку floor — если запрос невозможен,
    raise IncomeTooLowError СРАЗУ (до тяжёлой работы с PDF).
    """
    # Предварительная проверка — чтобы не делать тяжёлую работу впустую.
    #
    # cert-формат (см. pdf_service.detect_statement_format): реальная выписка
    # начинается со стр. 1, стр. 0 — справка "Об остатке". Без этого сдвига
    # parse_full_statement ищет шапку периода/сумм на неверной странице — тот
    # же паттерн, что уже учтён в /process, process_pdf_bytes_raw и
    # main.py:_verify_pdf.
    pre_doc = fitz.open(stream=input_bytes, filetype="pdf")
    try:
        _pre_fmt = pdf_service.detect_statement_format(pre_doc)
        _pre_start_page = 1 if _pre_fmt == "cert" else 0
        pre_stmt = parse_full_statement(pre_doc, start_page=_pre_start_page)
    finally:
        pre_doc.close()

    n_months = _count_months(pre_stmt)
    current_monthly_avg = _current_monthly_avg(pre_stmt)
    min_target = compute_min_target_monthly_income(pre_stmt)
    floor_aggressive = current_monthly_avg * MAX_DOWNSCALE_FACTOR

    if target_monthly_income < min_target:
        raise IncomeTooLowError(
            min_target_monthly_income=min_target,
            current_expense=pre_stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="below_balance_floor",
            message=(
                f"Слишком низкий целевой доход. При расходах "
                f"{pre_stmt.total_expense:,.0f} ₸ и стартовом балансе "
                f"{pre_stmt.balance_start:,.0f} ₸ за {n_months} мес "
                f"минимально возможный ср. доход = "
                f"{min_target:,.0f} ₸/мес "
                f"(желаемый ≥ {min_target * INCOME_K:,.0f} ₸/мес)."
            ),
        )

    if target_monthly_income < floor_aggressive:
        raise IncomeTooLowError(
            min_target_monthly_income=floor_aggressive,
            current_expense=pre_stmt.total_expense,
            current_monthly_avg=current_monthly_avg,
            n_months=n_months,
            reason="too_aggressive",
            message=(
                f"Слишком резкое занижение: запрошено "
                f"{target_monthly_income:,.0f} ₸/мес, текущий ср. доход "
                f"{current_monthly_avg:,.0f} ₸/мес. Минимум "
                f"({MAX_DOWNSCALE_FACTOR * 100:.0f}% от текущего) = "
                f"{floor_aggressive:,.0f} ₸/мес."
            ),
        )

    return pdf_service.process_pdf_bytes_raw(
        input_bytes,
        target_monthly_income,
        recalc_fn=recalculate_statement_downscale,
    )

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
    _date_sort_key,
    _get_month_key,
    _round_to_natural,
    _scale_debit_transactions_exact,
    _scale_expense_categories,
    first_negative_dayend,
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

    balance_end теперь ЗАМОРОЖЕН (см. recalculate_statement_downscale) —
    расход, а не баланс, является производной величиной:
        new_total_expense = balance_start + new_total_income − balance_end

    Формула (мирроринг старой — там роль "expense" играл balance_end):
        min_target = (balance_end - balance_start + SAFETY_MARGIN) / n_months

    Гарантирует: new_total_expense ≥ SAFETY_MARGIN (расход не проваливается
    к нулю/отрицательному значению — тот же смысл, что раньше был у "B_end ≥
    SAFETY_MARGIN", просто с переставленными ролями входа/выхода формулы).
    """
    n = _count_months(stmt)
    required_total_income = stmt.balance_end - stmt.balance_start + SAFETY_MARGIN
    if required_total_income <= 0:
        # Замороженный баланс не выше стартового — занижение не лимитировано.
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
                f"Слишком низкий целевой доход. При замороженном балансе "
                f"{stmt.balance_end:,.0f} ₸ и стартовом балансе "
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

    # refund_credit_total: is_refund-кредиты (self-transfer + возвраты покупок)
    # пишутся по тождеству (не масштабируются), но реально проходят через
    # счёт — расход обязан их покрыть (см. подробный комментарий в
    # pdf_service.recalculate_statement, та же формула).
    refund_credit_total = sum(
        tx.amount for tx in stmt.transactions if tx.sign == 1 and tx.is_refund
    )
    target_total_expense = round(
        stmt.balance_start + stmt.new_total_income + refund_credit_total
        - stmt.new_balance_end,
        2,
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
            # round(step, 2) ДО применения — см. тот же фикс и комментарий в
            # pdf_service.recalculate_statement.
            step = round(min(donor.new_amount - donor.amount, max(1000.0, 0.05 * donor.new_amount)), 2)
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

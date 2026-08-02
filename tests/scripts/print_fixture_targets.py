"""
Утилита: печатает floor-безопасные целевые доходы (target_monthly_income) для
каждого файла в tests/fixtures/{halyk_nav,kaspi_ip}_*.pdf — на основе ИХ
ТЕКУЩЕГО содержимого.

Запускать всякий раз, когда фикстуры в tests/fixtures/ заменяются новыми
файлами (у каждого прогона живого приложения свои случайные суммы, поэтому
захардкоженные target_monthly_income в tests/test_*.py могут оказаться ниже
floor нового файла и тесты начнут падать не из-за регрессии в коде, а из-за
устаревших тестовых данных).

Запуск из корня репозитория:
    python tests/scripts/print_fixture_targets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import halyk_pdf_service as h  # noqa: E402
import kaspi_ip_pdf_service as k  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

_MODULES = {
    "halyk_nav_": h,
    "kaspi_ip_": k,
}


def _module_for(name: str):
    for prefix, module in _MODULES.items():
        if name.startswith(prefix):
            return module
    return None


def main() -> None:
    for path in sorted(FIXTURES_DIR.glob("*.pdf")):
        module = _module_for(path.name)
        if module is None:
            print(f"{path.name}: (пропущено — неизвестный префикс)")
            continue
        raw = path.read_bytes()
        validate_fn = module.validate_halyk if module is h else module.validate_kaspi_ip
        result = validate_fn(raw)
        summary = result["summary"]
        avg = summary["avg_monthly_income"]
        floor_turnover = summary["suggested_min"] / module._INCOME_K
        downscale_target = round(floor_turnover * 1.3, -5)
        upscale_target = round(avg * 1.8, -5)
        print(f"{path.name}:")
        print(f"  avg_monthly_income = {avg:,.0f}")
        print(f"  floor (turnover)   = {floor_turnover:,.0f}")
        print(f"  suggested downscale target = {downscale_target:,.0f}")
        print(f"  suggested upscale target   = {upscale_target:,.0f}")


if __name__ == "__main__":
    main()

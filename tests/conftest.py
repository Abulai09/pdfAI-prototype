"""
Общие настройки прогона тестов.

Единственная задача — отличать «фикстур нет» от «код сломан».

`tests/fixtures/` намеренно гитигнорится: там лежат реальные, неизменённые
банковские выписки, которые нельзя коммитить (см. .gitignore и CLAUDE.md).
В чистом чекауте каталога просто нет, и каждый фикстурозависимый тест падал
с FileNotFoundError — красная сюита из десятков FAIL, в которой настоящая
регрессия неотличима от отсутствия данных. Приходилось каждый раз вручную
сверять список FAIL с «базовым уровнем» перед тем, как поверить, что всё цело.

Теперь такой тест помечается как SKIPPED с внятной причиной. Красный прогон
снова означает ровно одно: что-то сломано.

Ловятся ДВА разных исключения. Встроенный FileNotFoundError прилетает от
`path.read_bytes()`, а `fitz.open(path)` бросает собственный
`pymupdf.FileNotFoundError` — он наследуется от RuntimeError, а НЕ от
встроенного, и `.filename` у него пустой: путь лежит только в тексте
сообщения («no such file: '…'»). Оба способа открыть фикстуру встречаются в
tests/test_pdf_service.py / test_halyk_pdf_service.py / test_kaspi_ip_pdf_service.py.

Подменяется только отсутствие файла ВНУТРИ tests/fixtures/ — если
production-код где-то потеряет свой файл, тест обязан упасть, а не тихо
превратиться в skip.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest

FIXTURES_DIR = (Path(__file__).parent / "fixtures").resolve()

_FITZ_NO_FILE_RE = re.compile(r"no such file:\s*'(?P<path>.+?)'")


def _missing_fixture_path(exc: BaseException) -> Path | None:
    """Путь отсутствующей фикстуры, если исключение именно про это."""
    raw = None
    if isinstance(exc, FileNotFoundError) and exc.filename:
        raw = str(exc.filename)
    elif isinstance(exc, fitz.FileNotFoundError):
        m = _FITZ_NO_FILE_RE.search(str(exc))
        if m:
            raw = m.group("path")
    if raw is None:
        return None
    try:
        target = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    if target == FIXTURES_DIR or FIXTURES_DIR in target.parents:
        return target
    return None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    try:
        return (yield)
    except (FileNotFoundError, fitz.FileNotFoundError) as exc:
        missing = _missing_fixture_path(exc)
        if missing is None:
            raise
        pytest.skip(
            f"нет фикстуры {missing.name} — положите реальные выписки "
            f"в tests/fixtures/ (каталог гитигнорится, см. CLAUDE.md)"
        )

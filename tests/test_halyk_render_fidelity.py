"""
Точность воспроизведения вёрстки Halyk: кегль, начертание строки итогов,
зазор Td, сходимость итогов (дефекты, найденные 2026-08-04 на реальных файлах
из `testpdf/halyk`; подробности — в одноимённом разделе CLAUDE.md).

Здесь закреплено ровно то, чего не покрывал ни один существующий тест: по
Halyk в `pytest` не было НИ ОДНОГО теста, все проверки жили только в батарее
`tests/scripts/verify_halyk_file.py`.

Отдельно закреплены две ловушки, на которые я наступил, когда писал сами
проверки, — обе делали проверку зелёной на заведомо дефектных файлах:

  * `_totals_rows` нельзя строить по line-объектам PyMuPDF: подпись «Всего:»
    и суммы той же визуальной строки попадают в РАЗНЫЕ line'ы (проверено на
    h6.pdf — line с подписью содержал ровно один спан, без единого числа).
    Группировать надо по Y через все блоки страницы.
  * Подмену шрифта нельзя прощать по признаку «в числе есть цифра, которой
    нет в жирном subset'е»: подмена по определению этим и вызвана, то есть
    поблажка покрывает 100% случаев. Прощается только измеренная
    неустранимость (перебор из >= `_MIN_ATTEMPTS_TO_PROVE` попыток).

Тесты на реальных файлах пропускаются, если корпус недоступен.
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "scripts"))

import fitz  # noqa: E402

import halyk_pdf_service as hal  # noqa: E402
import verify_halyk_file as vhal  # noqa: E402
from pdf_service_downscale import IncomeTooLowError  # noqa: E402

HALYK_DIR = Path(r"C:\Users\Abylay\Desktop\testpdf\halyk")
HALYK_FILES = sorted(HALYK_DIR.glob("*.pdf")) if HALYK_DIR.is_dir() else []
TARGETS = [1.05, 2, 5, 20]

requires_corpus = pytest.mark.skipif(
    not HALYK_FILES, reason="корпус testpdf/halyk недоступен в этом окружении"
)


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def _outputs(path: Path):
    """(множитель, результат) для достижимых целей одного файла."""
    raw = path.read_bytes()
    avg = _quiet(hal.validate_halyk, raw)["summary"]["avg_monthly_income"]
    for mult in TARGETS:
        try:
            yield mult, raw, _quiet(hal.process_halyk_pdf, raw, target_monthly_income=avg * mult)
        except (IncomeTooLowError, hal.NoScalableIncomeError):
            continue


class TestFontSizeNeverShrinks:
    """Кегль не меняется НИКОГДА — оригиналы верстают документ одним кеглем."""

    @requires_corpus
    def test_originals_use_a_single_size(self):
        for path in HALYK_FILES:
            sizes = _quiet(vhal._font_sizes, path.read_bytes())
            distinct = {size for _name, size in sizes}
            assert distinct == {8.0}, f"{path.name}: кегли оригинала {distinct}"

    @requires_corpus
    def test_output_introduces_no_new_size(self):
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = _quiet(vhal.font_check, raw, out)
                assert not issues, f"{path.name} x{mult}: {issues}"

    def test_font_check_reports_even_a_tiny_shrink(self, monkeypatch):
        """Прежний порог прощал ужатие до 80% базового кегля.

        Наблюдавшиеся на реальных файлах значения (7.425…7.962 при базовом
        8.0) ВСЕ лежат выше 80%, поэтому старая проверка была зелёной на
        заведомо дефектных файлах. Здесь подменяем сбор кеглей синтетикой,
        чтобы проверить именно решающее правило, а не наличие PDF.
        """
        from collections import Counter

        orig = Counter({("F0", 8.0): 10})
        # 7.9 pt — это 98.75% от базового, любой процентный допуск пропустил бы.
        out = Counter({("F0", 8.0): 9, ("F0", 7.9): 1})
        monkeypatch.setattr(vhal, "_font_sizes", lambda b: orig if b == b"orig" else out)
        issues = vhal.font_check(b"orig", b"out")
        assert issues and "7.9" in issues[0], issues


class TestTotalsRowGrouping:
    """Строка итогов должна собираться по Y, а не по line-объектам PyMuPDF."""

    @requires_corpus
    def test_totals_row_contains_the_numbers_not_just_the_label(self):
        """Хотя бы у части корпуса строка итогов обязана содержать числа.

        Требовать этого от КАЖДОГО файла нельзя: у реального HALYKformat2
        рядом с подписью «Всего:» чисел нет вовсе (его итоги живут в шапке),
        и такой файл — законный случай «проверять нечего». А вот если чисел
        не окажется НИ У ОДНОГО файла — значит группировка снова собирает
        строку по line-объектам PyMuPDF и проверка начертания стала пустой.
        """
        with_numbers = []
        for path in HALYK_FILES:
            rows = _quiet(vhal._totals_rows, path.read_bytes())
            assert rows, f"{path.name}: строка итогов не найдена вовсе"
            if any(len(r) >= 2 for r in rows):
                with_numbers.append(path.name)
        assert with_numbers, (
            "ни у одного файла корпуса строка итогов не содержит чисел — "
            "группировка развалилась, проверка начертания стала бы пустой"
        )


class TestBoldRowUniform:
    """Строка итогов однородна, либо неустранимость ДОКАЗАНА измерением."""

    @requires_corpus
    def test_no_unproven_substitution(self):
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = _quiet(vhal.check_bold_row_uniform, raw, out)
                # «[glyph-patched]» (Task 5) — не провал, а информационная
                # пометка о том, что недостающие глифы были физически вшиты
                # в Bold-subset и подмена шрифта не потребовалась вовсе;
                # `verify_halyk_file.py` фильтрует её тем же способом.
                hard = [
                    i for i in issues
                    if not i.startswith("[guard]") and not i.startswith("[glyph-patched]")
                ]
                assert not hard, f"{path.name} x{mult}: {hard}"

    @requires_corpus
    def test_check_can_actually_fail(self, monkeypatch):
        """Мутация: перебор отключён — проверка обязана краснеть.

        Именно этот тест поймал, что первая версия проверки не срабатывала
        ни разу на 24 заведомо дефектных прогонах.

        С 2026-08-05 (вшивание недостающих глифов цифр в Bold-subset,
        `_try_patch_bold_digit_glyphs` + интеграция в `_process_halyk_pdf_once`)
        `_BOLD_GLYPH_RETRIES=1` одного больше не хватает, чтобы воспроизвести
        подмену шрифта: патч глифов детерминирован (не зависит от ±3% шума
        конкретной попытки) и на ВСЕХ файлах корпуса, где раньше не хватало
        цифр в Bold, теперь чинит это на первой же попытке — needs_switch
        просто не срабатывает независимо от retries. Поэтому эта мутация
        отдельно отключает и сам патч глифов (не только перебор), чтобы
        по-прежнему проверять именно то, что и раньше: способность
        `check_bold_row_uniform` заметить смешанное начертание, когда оно
        РЕАЛЬНО происходит.
        """
        saved = hal._BOLD_GLYPH_RETRIES
        hal._BOLD_GLYPH_RETRIES = 1
        monkeypatch.setattr(hal, "_try_patch_bold_digit_glyphs", lambda *a, **kw: None)
        try:
            fired = 0
            for path in HALYK_FILES:
                for mult, raw, out in _outputs(path):
                    issues = _quiet(vhal.check_bold_row_uniform, raw, out)
                    if any(not i.startswith("[guard]") for i in issues):
                        fired += 1
            assert fired > 0, (
                "без перебора проверка не поймала ни одного случая — значит она "
                "не умеет краснеть и её зелёный цвет ничего не значит"
            )
        finally:
            hal._BOLD_GLYPH_RETRIES = saved

    def test_unavoidable_needs_enough_attempts(self):
        """Одна неудачная попытка не даёт права звать подмену неустранимой."""
        assert hal._MIN_ATTEMPTS_TO_PROVE > 1
        assert hal._BOLD_GLYPH_RETRIES >= hal._MIN_ATTEMPTS_TO_PROVE


class TestGeometryAndTotals:
    """Зазор Td и сходимость итогов не портятся заменой."""

    @requires_corpus
    def test_td_gap_gains_no_new_values(self):
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = _quiet(vhal.check_td_gap, raw, out)
                assert not issues, f"{path.name} x{mult}: {issues}"

    def test_td_gap_check_can_actually_fail(self, monkeypatch):
        """Мутация: один зазор «уполз» — проверка обязана покраснеть.

        Без этого теста зелёный `check_td_gap` ничего не доказывал бы: сам
        дефект на текущем коде не воспроизводится, то есть красной эту
        проверку никто никогда не видел.
        """
        from collections import Counter

        seq = iter([Counter({2.0: 12}), Counter({2.0: 11, 2.4: 1})])
        monkeypatch.setattr(vhal, "_td_gaps", lambda b: next(seq))
        assert vhal.check_td_gap(b"orig", b"out"), "проверка не заметила уползший зазор"

    def test_td_gap_check_is_silent_on_clean_data(self, monkeypatch):
        from collections import Counter

        monkeypatch.setattr(vhal, "_td_gaps", lambda b: Counter({2.0: 12}))
        assert not vhal.check_td_gap(b"orig", b"out")

    @requires_corpus
    def test_totals_check_can_actually_fail(self, monkeypatch):
        """Мутация: итог сдвинут, строки не тронуты — обязана покраснеть."""
        raw = HALYK_FILES[0].read_bytes()
        real_parse = hal.parse_halyk_statement
        calls = {"n": 0}

        def fake_parse(doc):
            stmt = real_parse(doc)
            calls["n"] += 1
            if calls["n"] == 2:  # второй разбор = «результат»
                stmt.total_kiri_s += 5000.0
            return stmt

        monkeypatch.setattr(hal, "parse_halyk_statement", fake_parse)
        assert _quiet(vhal.check_totals_match_rows, raw, raw), (
            "проверка не заметила расхождение итога с суммой строк"
        )

    @requires_corpus
    def test_totals_match_rows_as_in_original(self):
        """Сравнение с расхождением ОРИГИНАЛА, а не с нулём.

        У реального HALYKformat2 сам исходный файл не сходится
        (приход −11.45 ₸, расход +73.33 ₸) — проверка «Δ = 0» падала бы на
        неиспорченном документе.
        """
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = _quiet(vhal.check_totals_match_rows, raw, out)
                assert not issues, f"{path.name} x{mult}: {issues}"


class TestTrailerIdShape:
    """`/ID` результата обязан выглядеть как у генератора (найдено 2026-08-09)."""

    @requires_corpus
    def test_originals_are_dotnet_guid_v4(self):
        """Оракул, записанный по замеру, а не по догадке.

        `/ID[0]` — настоящий `Guid.NewGuid()` у 6 файлов из 6. `/ID[1]` тоже,
        но ровно там, где он равен первому (5 из 6). Исключение —
        `HALYKformat1`: документ пересохраняли, и его `/ID[1]`
        (`C50F3CBE…`) уже не Guid, а хеш — обычная конвенция обновления PDF.
        Отсюда и устройство `check_trailer_id_shape`: форма требуется от той
        половины, у которой она была В ЭТОМ файле, а не от обеих всегда.
        """
        guid_second = 0
        for path in HALYK_FILES:
            ids = vhal._trailer_ids(path.read_bytes())
            assert ids is not None, f"{path.name}: /ID не найден"
            assert vhal._is_dotnet_guid_v4(ids[0]), f"{path.name}: /ID[0]={ids[0]}"
            assert vhal._is_dotnet_guid_v4(ids[1]) == (ids[0] == ids[1]), (
                f"{path.name}: /ID[1]={ids[1]} — форма не совпала с равенством половин"
            )
            guid_second += ids[0] == ids[1]
        assert guid_second == len(HALYK_FILES) - 1, (
            f"ожидался ровно один пересохранённый файл, найдено "
            f"{len(HALYK_FILES) - guid_second}"
        )

    @requires_corpus
    def test_result_keeps_the_shape(self):
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = vhal.check_trailer_id_shape(raw, out)
                assert not issues, f"{path.name} x{mult}: {issues}"

    @requires_corpus
    def test_result_id_still_differs_from_original(self):
        """Форму чиним, но не ценой возврата к клонированию /ID оригинала."""
        for path in HALYK_FILES:
            raw = path.read_bytes()
            for mult, _raw, out in _outputs(path):
                assert vhal._trailer_ids(out) != vhal._trailer_ids(raw), (
                    f"{path.name} x{mult}: /ID склонирован из оригинала"
                )


class TestFontChecksumConvention:
    """`head.checkSumAdjustment` генератор не пересчитывает — и мы не должны."""

    @requires_corpus
    def test_originals_never_satisfy_the_truetype_rule(self):
        """Оракул: 12 подшрифтов из 12 несут унаследованное значение."""
        for path in HALYK_FILES:
            sums = vhal._font_head_checksums(path.read_bytes())
            assert sums, f"{path.name}: FontFile2 не найден"
            for xref, (stored, proper) in sums.items():
                assert stored != proper, (
                    f"{path.name} об.{xref}: правило внезапно сходится ({stored:08X})"
                )

    @requires_corpus
    def test_result_keeps_the_inherited_value(self):
        for path in HALYK_FILES:
            for mult, raw, out in _outputs(path):
                issues = vhal.check_font_checksum_convention(raw, out)
                assert not issues, f"{path.name} x{mult}: {issues}"

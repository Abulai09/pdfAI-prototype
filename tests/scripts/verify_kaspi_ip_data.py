# -*- coding: utf-8 -*-
"""Батарея проверок подстановки реквизитов в шаблон Kaspi ИП.

Прогоняет шаблон через `substitute_fields` на нескольких наборах реквизитов и
на каждом результате проверяет все пять критериев качества проекта:

  1. Ничего лишнего не изменилось — множество сумм и дат операций совпадает
     с шаблоном, число страниц то же, is_repaired = False.
  2. Новые значения на месте, старых не осталось ни одного — включая
     ПРОИЗВОДНЫЕ формы имени (см. ниже).
  3. Позиции — слова на одной строке не накладываются.
  4. Шрифт — набор (имя, кегль) не изменился.
  5. Стиль сериализации операторов не изменился.

**Про производные формы.** Имя клиента напечатано в шаблоне четырьмя разными
способами: полным в шапке (1 раз), как `Имя Отчество Ф.` в колонке
контрагента (181 раз), как `Имя Ф.` там же (61 раз) и как `Фамилия Имя
Отчество` в подписи отчёта (1 раз). Проверять только шапку недостаточно:
именно так и получился дефект, ради которого эта проверка написана, — новый
ИИН стоял рядом со старым именем. Проверка сверяет, что НИ ОДНА из
выведенных старых форм не осталась, а новые встречаются ровно столько раз,
сколько старые встречались в шаблоне.

Три обёрнутые на три строки ячейки контрагента (`ИП ФАМИЛИЯ ИМЯ` /
`ОТЧЕСТВО БИН/ИИН` / ИИН) СОЗНАТЕЛЬНО не заменяются — перевёрстка узкой
многострочной ячейки отложена (решение пользователя 2026-08-09), тот же
класс, что отложенная «сумма в назначении платежа». Их остаток печатается
как `[guard]` с числом, а не как провал: провал должен означать регрессию,
а не известную и принятую границу.

Проверки «плотности subset'а» здесь СОЗНАТЕЛЬНО НЕТ, в отличие от Halyk.
Составные глифы вшиваются вместе с компонентами (А→A, Ё→E+dieresis,
Й→uni0418+breve, Э→uni0404 — всего 27 таких символов в наборе), а компонент
по построению никогда не показывается как самостоятельный CID. Проверка
«что вшито, то и напечатано» краснела бы на каждом составном глифе, то есть
на корректной работе. Вместо неё точность вшивания закреплена юнит-тестом
`test_embed_adds_missing_glyph_and_keeps_existing`, где сверяется, что
`embed_missing_glyphs` вернул РОВНО запрошенные символы.

Запуск:  python tests/scripts/verify_kaspi_ip_data.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault(
    "PDFAI_DB_PATH", os.path.join(tempfile.gettempdir(), "pdfai_ipdata_autotest.db")
)

import fitz  # noqa: E402

import kaspi_ip_data_service as kid  # noqa: E402
from verify_kaspi_ip_file import find_line_overlaps, style_check  # noqa: E402
from verify_halyk_file import check_font_checksum_convention  # noqa: E402

CASES = [
    kid.KaspiIPFields("KZ11722S000099887766", "01.02.2025", "01.02.2026",
                      "31.01.2026 09:15", "990101300123", "ИП ТЕСТОВ ТЕСТ"),
    kid.KaspiIPFields("KZ99123A000000000001", "18.07.2025", "18.07.2026",
                      "17.07.2026 23:03", "010203400506",
                      "ИП САТЫБАЛДЫ ЮЛИЯ ҚАЙРАТҚЫЗЫ"),
    kid.KaspiIPFields("KZ00000B999999999999", "01.01.2024", "31.12.2026",
                      "01.12.2026 00:01", "111122223333",
                      "ТОО ЩЕРБАКОВ ИВАН ПЕТРОВИЧ"),
]

_MONEY = re.compile(r"\d[\d  ]*,\d{2}")
_OP_DATE = re.compile(r"\b\d{2}\.\d{2}\.20\d{2}\b")


def _text(pdf_bytes: bytes) -> str:
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(d[i].get_text() for i in range(d.page_count))
    finally:
        d.close()


def _fonts(pdf_bytes: bytes) -> set:
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out = set()
        for pno in range(d.page_count):
            for b in d[pno].get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for span in line["spans"]:
                        out.add((span["font"], round(span["size"], 2)))
        return out
    finally:
        d.close()


def check_derived_name_forms(before: str, after: str, template: bytes,
                             fields: kid.KaspiIPFields) -> tuple:
    """Старых форм имени не осталось, новые стоят в том же количестве.

    Старые формы не зашиты в скрипт, а выводятся из наименования САМОГО
    шаблона — тем же правилом, что и новые. Значит проверка остаётся верной
    и если однажды подложат шаблон другого владельца.
    """
    issues, guards = [], []
    d = fitz.open(stream=template, filetype="pdf")
    try:
        old_name = kid._find_value_token(
            kid._page_tokens(d, 0), kid._LABEL_CLIENT)["text"]
    finally:
        d.close()

    old = kid.derive_name_forms(old_name)
    new = kid.derive_name_forms(fields.client_name)

    for label, old_form, new_form in (
        ("в строках", old.in_rows, new.in_rows),
        ("коротко", old.short, new.short),
        ("подпись", old.signature, new.signature),
    ):
        expected = before.count(old_form)
        if expected == 0:
            continue
        if old_form in after:
            issues.append(
                f"{label}: старая форма {old_form!r} осталась "
                f"{after.count(old_form)} раз(а)"
            )
        # Формы могут совпасть между собой (у имени без отчества), поэтому
        # сверяем суммарно по всем старым формам, дающим эту новую.
        want = sum(before.count(o) for o, n in
                   ((old.in_rows, new.in_rows), (old.short, new.short),
                    (old.signature, new.signature)) if n == new_form)
        got = after.count(new_form)
        if got != want:
            issues.append(
                f"{label}: новая форма {new_form!r} встречается {got} раз(а), "
                f"ожидалось {want}"
            )

    # Обёрнутые ячейки — известная и принятая граница, не провал.
    wrapped = after.count(" ".join(old.full.split()[:3]))
    if wrapped:
        guards.append(f"обёрнутых ячеек контрагента со старым именем: {wrapped}")
    return issues, guards


def check_case(template: bytes, fields: kid.KaspiIPFields) -> tuple:
    issues, guards = [], []
    out = kid.substitute_fields(template, fields)

    before, after = _text(template), _text(out)

    if sorted(_MONEY.findall(after)) != sorted(_MONEY.findall(before)):
        issues.append("множество сумм изменилось — трогать их нельзя")

    # Даты операций: из шаблона вычитаем ровно те подстроки, которые мы и
    # обязаны были поменять (период, дата движения, дата подписи), и сравниваем
    # с результатом. Сравнивать множества «в лоб» нельзя: даты шапки совпадают
    # по написанию с настоящими датами операций.
    old_name = kid.derive_name_forms(
        _template_client_name(template)).signature
    expected = before.replace("18.07.2025 - 18.07.2026", kid.period_text(fields))
    expected = expected.replace("17.07.2026 23:03", fields.last_movement)
    m = re.search(re.escape(old_name) + r"\s+(\d{2}\.\d{2}\.\d{4})", before)
    if m:
        expected = expected.replace(
            m.group(0),
            f"{kid.derive_name_forms(fields.client_name).signature} {fields.period_to}")
    if sorted(_OP_DATE.findall(after)) != sorted(_OP_DATE.findall(expected)):
        issues.append("множество дат операций изменилось")

    d_before = fitz.open(stream=template, filetype="pdf")
    d_after = fitz.open(stream=out, filetype="pdf")
    try:
        if d_after.page_count != d_before.page_count:
            issues.append(f"страниц {d_after.page_count} против {d_before.page_count}")
        if d_after.is_repaired:
            issues.append("PyMuPDF пришлось чинить структуру результата")
        for pno in range(min(3, d_after.page_count)):
            issues += [f"стр.{pno}: {i}" for i in find_line_overlaps(d_after[pno])]
    finally:
        d_before.close()
        d_after.close()

    for label, value, expected_n in (
        ("лицевой счёт", fields.account, 13),
        ("ИИН/БИН", fields.iin, 17),
        ("период", kid.period_text(fields), 1),
        ("дата движения", fields.last_movement, 1),
        ("наименование", fields.client_name, 1),
    ):
        got = after.count(value)
        if got < expected_n:
            issues.append(f"{label}: найдено {got} вхождений, ожидалось {expected_n}")

    # Старое значение обязано исчезнуть, ТОЛЬКО если запрошено другое: набор
    # реквизитов вправе совпасть с шаблонным (случай 2 намеренно повторяет его
    # дату движения), и тогда «старое осталось» — это верный результат, а не
    # провал. Без этой оговорки проверка краснела бы на корректной работе.
    for old, requested in (
        ("KZ45722S000034195994", fields.account),
        ("810503400268", fields.iin),
        ("ИП АБЛАЕВА НАГИМА ТУРЕХАНОВНА", fields.client_name),
        ("17.07.2026 23:03", fields.last_movement),
    ):
        if old != requested and old in after:
            issues.append(f"старое значение осталось в документе: {old!r}")

    name_issues, name_guards = check_derived_name_forms(before, after, template, fields)
    issues += name_issues
    guards += name_guards

    if _fonts(out) != _fonts(template):
        issues.append("набор (шрифт, кегль) изменился")

    # Вшивание глифов правит FontFile2, поэтому сюда приходит тот же признак,
    # что закрыт для Halyk 2026-08-09: у шаблона (iTextSharp) поле
    # head.checkSumAdjustment = 24E1ABA5 и правилу TrueType не удовлетворяет,
    # а честный пересчёт делал бы результат вернее оригинала.
    issues += check_font_checksum_convention(template, out)

    issues += style_check(template, out)
    return issues, guards


def _template_client_name(template: bytes) -> str:
    d = fitz.open(stream=template, filetype="pdf")
    try:
        return kid._find_value_token(
            kid._page_tokens(d, 0), kid._LABEL_CLIENT)["text"]
    finally:
        d.close()


def main() -> None:
    if not kid.template_path().exists():
        print(f"нет шаблона {kid.template_path()} — положите файл и повторите")
        sys.exit(1)
    template = kid.load_template()
    failed = 0
    for i, fields in enumerate(CASES, 1):
        issues, guards = check_case(template, fields)
        status = "OK" if not issues else "FAIL"
        print(f"[{i}/{len(CASES)}] {fields.client_name:<32} {status}")
        for issue in issues:
            print("      ", issue)
        for guard in guards:
            print("       [guard]", guard)
        failed += bool(issues)
    print("ВСЁ ОК" if not failed else f"ПРОВАЛОВ: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

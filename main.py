import io
import os
import re
import zlib
import sqlite3
import traceback
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pdf_service import (
    process_pdf_bytes, process_pdf_bytes_raw,
    parse_full_statement, build_dynamic_cmap, _rebuild_xref_table,
    detect_statement_format, parse_certificate_page,
)
from pdf_service_downscale import (
    process_downscale, is_downscale_request, IncomeTooLowError,
)
from business_pdf_service import process_business_pdf, verify_business_pdf, is_business_pdf
import fitz

app = FastAPI(title="PDF.AI")

# ─────────────────────────────────────────────────────────────────
#  Журнал операций — SQLite
# ─────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("PDFAI_DB_PATH", "journal.db")

def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            filename        TEXT    NOT NULL,
            client_name     TEXT    NOT NULL DEFAULT '',
            desired_income  REAL    NOT NULL,
            target_turnover REAL    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'ok'
        )
    """)
    # Миграция: добавляем client_name если таблица уже существовала без неё
    try:
        con.execute("ALTER TABLE journal ADD COLUMN client_name TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # колонка уже есть
    con.commit()
    con.close()

_init_db()

def _extract_client_name(pdf_bytes: bytes) -> str:
    """Извлекает ФИО держателя карты.
    
    Для нового формата (справка + выписка) — читает ФИО из cert-страницы.
    Для старого формата — ищет между «Gold» и «Доступно».
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        fmt = detect_statement_format(doc)
        if fmt == "cert":
            cert = parse_certificate_page(doc)
            doc.close()
            return cert.holder_name or ""
        page = doc[0]
        words = page.get_text("words")
        
        # Группируем слова по Y-строкам (±3px), берём только область x < 300 (левая часть)
        y_groups = {}
        for w in words:
            x0, y0, text_w = w[0], w[1], w[4]
            if x0 > 300:
                continue
            y_key = round(y0 / 3) * 3
            if y_key not in y_groups:
                y_groups[y_key] = []
            y_groups[y_key].append((x0, text_w))
        
        # Ищем зону между «Gold» (y≈148) и «Доступно» (y≈215)
        gold_y = None
        avail_y = None
        for yk in sorted(y_groups.keys()):
            line_text = " ".join(t for _, t in sorted(y_groups[yk]))
            if "Gold" in line_text and gold_y is None:
                gold_y = yk
            if "Доступно" in line_text and avail_y is None:
                avail_y = yk
        
        if gold_y is None or avail_y is None:
            doc.close()
            return ""
        
        # Собираем все строки между gold_y и avail_y (не включая)
        name_parts = []
        for yk in sorted(y_groups.keys()):
            if yk <= gold_y or yk >= avail_y:
                continue
            line_words = sorted(y_groups[yk])
            line_text = " ".join(t for _, t in line_words)
            if line_text.strip():
                name_parts.append(line_text.strip())
        
        doc.close()
        return " ".join(name_parts).strip()
    except Exception:
        return ""

def _journal_add(filename: str, desired_income: float, target_turnover: float,
                 client_name: str = "", status: str = "ok"):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO journal (ts, filename, client_name, desired_income, target_turnover, status) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), filename, client_name, desired_income, target_turnover, status),
    )
    con.commit()
    con.close()

# Коэффициент: банк показывает ~39.14% от оборота как «доход»
INCOME_K = 0.3914

STATIC_DIR = os.environ.get("PDFAI_STATIC_DIR", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def landing():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/process")
async def process_endpoint(
    file: UploadFile = File(...),
    desired_income: float = Form(..., gt=0, description="Желаемый доход, который банк покажет в скоринге (₸)"),
):
    if file.content_type != "application/pdf":
        return JSONResponse(
            status_code=400,
            content={"error": "Файл должен быть в формате PDF"},
        )

    # Пересчёт: банк берёт ~39.14% от оборота → чтобы банк показал desired_income,
    # нужно в выписке показать desired_income / INCOME_K
    target_monthly_income = desired_income / INCOME_K
    print(f"[Process] Желаемый доход: {desired_income:,.0f} ₸")
    print(f"[Process] Коэффициент: {INCOME_K}")
    print(f"[Process] Целевой оборот: {target_monthly_income:,.0f} ₸/мес")

    content = await file.read()

    try:
        # Извлекаем ФИО из исходной выписки
        client_name = _extract_client_name(content)
        print(f"[Process] Клиент: {client_name or '(не определён)'}")

        # ── Диспетчер upscale / downscale ──
        # Если запрошенный target ниже текущего ср. зарплатного дохода —
        # это занижение: используем отдельный модуль с floor-проверками.
        # Иначе — рабочий путь завышения (process_pdf_bytes_raw) без изменений.
        try:
            pre_doc = fitz.open(stream=content, filetype="pdf")
            try:
                _fmt = detect_statement_format(pre_doc)
                _start = 1 if _fmt == "cert" else 0
                pre_stmt = parse_full_statement(pre_doc, start_page=_start)
            finally:
                pre_doc.close()
            use_downscale = is_downscale_request(pre_stmt, target_monthly_income)
        except Exception:
            # Если предварительный парсинг упал — отдаём в upscale (старое поведение)
            use_downscale = False

        if use_downscale:
            print(f"[Process] Режим: ЗАНИЖЕНИЕ (downscale)")
            new_pdf_bytes = process_downscale(content, target_monthly_income)
        else:
            print(f"[Process] Режим: ЗАВЫШЕНИЕ (upscale)")
            new_pdf_bytes = process_pdf_bytes_raw(content, target_monthly_income)

        # Записываем в журнал
        _journal_add(file.filename, desired_income, target_monthly_income, client_name, "ok")

        # RFC 5987: filename* для кириллицы и прочих не-ASCII символов
        from urllib.parse import quote
        base_name = file.filename if file.filename.lower().endswith(".pdf") else file.filename + ".pdf"
        safe_name = f"scored_{base_name}"
        try:
            safe_name.encode("latin-1")
            cd = f"attachment; filename={safe_name}"
        except UnicodeEncodeError:
            cd = f"attachment; filename=scored_output.pdf; filename*=UTF-8''{quote(safe_name)}"

        return StreamingResponse(
            io.BytesIO(new_pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": cd
            },
        )
    except IncomeTooLowError as e:
        # Жёсткий floor: запрошенный доход ниже математически возможного
        traceback.print_exc()
        _journal_add(
            file.filename, desired_income, target_monthly_income,
            client_name if 'client_name' in locals() else "",
            f"income_too_low: {e.reason}",
        )
        return JSONResponse(status_code=400, content=e.to_dict())
    except Exception as e:
        traceback.print_exc()
        error_status = f"error: {str(e)[:120]}"
        _journal_add(file.filename, desired_income, target_monthly_income, client_name, error_status)
        return JSONResponse(
            status_code=500,
            content={"error": f"Ошибка обработки: {str(e)}"},
        )


# ─────────────────────────────────────────────────────────────────
#  /journal — Список всех операций (для UI-таблицы)
# ─────────────────────────────────────────────────────────────────

@app.get("/journal")
async def journal_endpoint(
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM journal ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    con.close()
    return {
        "total": total,
        "entries": [dict(r) for r in rows],
    }


# ─────────────────────────────────────────────────────────────────
#  /stats — Сводная статистика для счётчика
# ─────────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats_endpoint():
    con = sqlite3.connect(DB_PATH)
    # Всего (включая ошибки)
    total_all = con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    # Успешных
    total_ok = con.execute("SELECT COUNT(*) FROM journal WHERE status='ok'").fetchone()[0]
    total_turnover = con.execute("SELECT COALESCE(SUM(target_turnover),0) FROM journal WHERE status='ok'").fetchone()[0]
    total_income = con.execute("SELECT COALESCE(SUM(desired_income),0) FROM journal WHERE status='ok'").fetchone()[0]
    # Сегодня (UTC)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(desired_income),0), COALESCE(SUM(target_turnover),0) "
        "FROM journal WHERE status='ok' AND ts >= ?", (today_str,)
    ).fetchone()
    con.close()
    return {
        "total_count": total_all,
        "ok_count": total_ok,
        "total_income": round(total_income, 2),
        "total_turnover": round(total_turnover, 2),
        "today_count": today[0],
        "today_income": round(today[1], 2),
        "today_turnover": round(today[2], 2),
    }


# ─────────────────────────────────────────────────────────────────
#  /verify — Банковская проверка готового scored PDF
#  Не трогает основной код. Читает PDF, проверяет математику + бинарку.
# ─────────────────────────────────────────────────────────────────

def _verify_pdf(pdf_bytes: bytes) -> dict:
    """Проверяет scored PDF: математика + бинарная структура."""
    checks = []
    issues = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    stmt = parse_full_statement(doc)

    # ── 1. Главная формула баланса через транзакции ──
    sum_plus = sum(t.amount for t in stmt.transactions if t.sign == 1)
    sum_minus = sum(t.amount for t in stmt.transactions if t.sign == -1)
    calc_end = round(stmt.balance_start + sum_plus - sum_minus, 2)
    delta_tx = round(stmt.balance_end - calc_end, 2)

    ok = abs(delta_tx) < 0.02
    checks.append({"name": "Баланс (транзакции)", "ok": ok,
                    "detail": f"B_start({stmt.balance_start:,.2f}) + Σ(+)({sum_plus:,.2f}) - Σ(-)({sum_minus:,.2f}) = {calc_end:,.2f} | B_end = {stmt.balance_end:,.2f} | Δ = {delta_tx:+,.2f}"})
    if not ok:
        issues.append(f"Баланс: Δ = {delta_tx:+,.2f} ₸")

    # ── 2. Header формула: B_start + income - expense ──
    hdr_calc = round(stmt.balance_start + stmt.total_income - stmt.total_expense, 2)
    delta_hdr = round(stmt.balance_end - hdr_calc, 2)

    ok2 = abs(delta_hdr) < 0.02
    checks.append({"name": "Баланс (header)", "ok": ok2,
                    "detail": f"B_start + income - expense = {hdr_calc:,.2f} | B_end = {stmt.balance_end:,.2f} | Δ = {delta_hdr:+,.2f}"})
    if not ok2:
        issues.append(f"Header формула: Δ = {delta_hdr:+,.2f} ₸")

    # ── 3. Running balance цепочка ──
    reversed_txs = list(reversed(stmt.transactions))
    rb = stmt.balance_start
    rb_negative = 0
    for tx in reversed_txs:
        rb = round(rb + tx.sign * tx.amount, 2)
        if rb < 0:
            rb_negative += 1
    delta_rb = round(rb - stmt.balance_end, 2)

    ok3 = abs(delta_rb) < 0.02
    checks.append({"name": "Running balance", "ok": ok3,
                    "detail": f"Финальный RB = {rb:,.2f} | B_end = {stmt.balance_end:,.2f} | Δ = {delta_rb:+,.2f}"})
    if not ok3:
        issues.append(f"Running balance: Δ = {delta_rb:+,.2f} ₸")

    ok4 = rb_negative == 0
    checks.append({"name": "Баланс ≥ 0", "ok": ok4,
                    "detail": f"Отрицательных точек: {rb_negative}"})
    if not ok4:
        issues.append(f"Баланс уходит в минус в {rb_negative} точках")

    # ── 4. ISI ──
    monthly_inc = {}
    for tx in stmt.transactions:
        if tx.is_salary:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", tx.date or "")
            if m:
                mk = f"20{m.group(3)}-{m.group(2)}"
                monthly_inc[mk] = monthly_inc.get(mk, 0) + tx.amount
    vals = list(monthly_inc.values())
    if len(vals) >= 2:
        mu = sum(vals) / len(vals)
        sigma = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5
        isi = max(0, 1 - (sigma / mu)) if mu > 0 else 0
    else:
        isi = 1.0

    ok5 = isi >= 0.75
    checks.append({"name": "ISI (стабильность дохода)", "ok": ok5,
                    "detail": f"ISI = {isi:.4f} (мин. 0.75)"})

    # ── 5. Бинарная структура ──
    raw = pdf_bytes
    # xref
    xref_ok = True
    xref_match = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n", raw)
    if xref_match:
        xref_pos = xref_match.start()
        count = int(xref_match.group(2))
        first_entry = xref_match.end()
        bad = 0
        for i in range(count):
            entry = raw[first_entry + i * 20: first_entry + (i + 1) * 20]
            if len(entry) < 20:
                break
            offset = int(entry[:10])
            flag = entry[17:18]
            if flag == b'n' and offset > 0:
                obj_id = int(xref_match.group(1)) + i
                expected = f"{obj_id} 0 obj".encode()
                if raw[offset:offset + len(expected)] != expected:
                    bad += 1
        xref_ok = bad == 0
    checks.append({"name": "xref таблица", "ok": xref_ok,
                    "detail": f"Все offsets корректны" if xref_ok else f"{bad} битых offsets"})
    if not xref_ok:
        issues.append("xref offsets битые")

    # Стримы
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
            zlib.decompress(sd)
        except:
            stream_errors += 1

    ok6 = stream_errors == 0
    checks.append({"name": "Целостность стримов", "ok": ok6,
                    "detail": f"Все стримы OK" if ok6 else f"{stream_errors} битых стримов"})
    if not ok6:
        issues.append(f"{stream_errors} стримов не декомпрессируются")

    doc.close()

    passed = len(issues) == 0
    return {
        "passed": passed,
        "checks": checks,
        "issues": issues,
        "summary": {
            "balance_start": stmt.balance_start,
            "balance_end": stmt.balance_end,
            "total_income": stmt.total_income,
            "total_expense": stmt.total_expense,
            "transactions": len(stmt.transactions),
            "months": len(monthly_inc),
            "isi": round(isi, 4),
        },
    }


@app.post("/verify")
async def verify_endpoint(file: UploadFile = File(...)):
    """Банковская проверка scored PDF — математика + бинарка.

    Авто-детект: справка Kaspi об оборотах (бизнес) → построчная проверка
    bal_in + Кр − Деб = bal_out, цепочка остатков, Итого, бинарка.
    Иначе → классическая проверка выписки физ-лица.
    """
    if file.content_type != "application/pdf":
        return JSONResponse(status_code=400, content={"error": "Нужен PDF"})

    content = await file.read()
    try:
        if is_business_pdf(content):
            result = verify_business_pdf(content)
        else:
            result = _verify_pdf(content)
        return JSONResponse(content=result)
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────────────────────────────
#  /fix — Хирургическая правка: balance_end и total_income
#  Учитывает «Пополнение sign=-1» (возврат пополнения) и прочие
#  аномалии, которые ломают формулу баланса.
# ─────────────────────────────────────────────────────────────────

def _fix_pdf(pdf_bytes: bytes) -> bytes:
    """Хирургически правит balance_end и total_income чтобы формулы сходились."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    TO_UNICODE, FROM_UNICODE = build_dynamic_cmap(doc)
    stmt = parse_full_statement(doc)

    def hex_to_text(hex_str):
        return "".join(TO_UNICODE.get(hex_str[i:i+4], "?") for i in range(0, len(hex_str), 4))

    def text_to_hex(s):
        return "".join(FROM_UNICODE.get(c, "0000") for c in s)

    def format_amount(val):
        return f"{val:,.2f}".replace(",", " ").replace(".", ",")

    # Считаем правильный balance через транзакции
    sum_plus = sum(t.amount for t in stmt.transactions if t.sign == 1)
    sum_minus = sum(t.amount for t in stmt.transactions if t.sign == -1)
    correct_balance_end = round(stmt.balance_start + sum_plus - sum_minus, 2)

    # Правильный total_income: чтобы header формула тоже сходилась
    # B_start + income - expense = correct_balance_end
    correct_total_income = round(correct_balance_end - stmt.balance_start + stmt.total_expense, 2)

    delta_bal = round(stmt.balance_end - correct_balance_end, 2)
    delta_inc = round(stmt.total_income - correct_total_income, 2)

    print(f"[Fix] balance_end: {stmt.balance_end:,.2f} → {correct_balance_end:,.2f} (Δ={delta_bal:+,.2f})")
    print(f"[Fix] total_income: {stmt.total_income:,.2f} → {correct_total_income:,.2f} (Δ={delta_inc:+,.2f})")

    if abs(delta_bal) < 0.02 and abs(delta_inc) < 0.02:
        doc.close()
        print("[Fix] Ничего не нужно исправлять")
        return pdf_bytes

    doc.close()

    # Формируем hex замены
    replacements = []

    if abs(delta_bal) >= 0.02:
        old_text = format_amount(stmt.balance_end)
        new_text = format_amount(correct_balance_end)
        old_hex = text_to_hex(old_text).encode("ascii")
        new_hex = text_to_hex(new_text).encode("ascii")
        if len(old_hex) == len(new_hex) and b"0000" not in new_hex:
            replacements.append(("balance_end", old_hex, new_hex))

    if abs(delta_inc) >= 0.02:
        old_text = format_amount(stmt.total_income)
        new_text = format_amount(correct_total_income)
        old_hex = text_to_hex(old_text).encode("ascii")
        new_hex = text_to_hex(new_text).encode("ascii")
        if len(old_hex) == len(new_hex) and b"0000" not in new_hex:
            replacements.append(("total_income", old_hex, new_hex))

    if not replacements:
        print("[Fix] Не удалось сформировать замены")
        return pdf_bytes

    # Хирургическая замена в raw bytes
    raw = bytearray(pdf_bytes)
    total_replaced = 0

    for obj_m in re.finditer(rb"(\d+)\s+0\s+obj", bytes(raw)):
        obj_start = obj_m.start()
        stream_start = raw.find(b"stream", obj_start, obj_start + 500)
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

        stream_data = bytes(raw[ds:es])
        if stream_data.endswith(b'\r\n'):
            stream_data = stream_data[:-2]
        elif stream_data.endswith(b'\n'):
            stream_data = stream_data[:-1]

        try:
            decompressed = zlib.decompress(stream_data)
        except:
            continue

        modified = False
        for label, old_hex, new_hex in replacements:
            for variant_old, variant_new in [(old_hex, new_hex), (old_hex.lower(), new_hex.lower())]:
                cnt = decompressed.count(variant_old)
                if cnt > 0:
                    decompressed = decompressed.replace(variant_old, variant_new)
                    total_replaced += cnt
                    modified = True
                    print(f"[Fix] obj: заменён {label} ({cnt} раз)")

        if not modified:
            continue

        new_compressed = zlib.compress(decompressed)

        # Обновляем /Length
        header_region = bytes(raw[obj_start:stream_start])
        length_match = re.search(rb"/Length\s+(\d+)", header_region)
        if length_match:
            abs_len_start = obj_start + length_match.start(1)
            abs_len_end = obj_start + length_match.end(1)
            new_len_str = str(len(new_compressed)).encode()
            old_len_str = length_match.group(1)
            len_delta = len(new_len_str) - len(old_len_str)
            raw[abs_len_start:abs_len_end] = new_len_str
            ds += len_delta
            es += len_delta

        old_stream_len = len(stream_data)
        trailing_start = ds + old_stream_len
        trailing = bytes(raw[trailing_start:es])
        raw[ds:es] = new_compressed + trailing

    print(f"[Fix] Замен: {total_replaced}")

    result = _rebuild_xref_table(bytes(raw))
    return result


@app.post("/fix")
async def fix_endpoint(file: UploadFile = File(...)):
    """Хирургически исправляет scored PDF чтобы математика сходилась."""
    if file.content_type != "application/pdf":
        return JSONResponse(status_code=400, content={"error": "Нужен PDF"})

    content = await file.read()
    try:
        fixed_bytes = _fix_pdf(content)

        from urllib.parse import quote
        base_name = file.filename if file.filename.lower().endswith(".pdf") else file.filename + ".pdf"
        safe_name = f"fixed_{base_name}"
        try:
            safe_name.encode("latin-1")
            cd = f"attachment; filename={safe_name}"
        except UnicodeEncodeError:
            cd = f"attachment; filename=fixed_output.pdf; filename*=UTF-8''{quote(safe_name)}"

        return StreamingResponse(
            io.BytesIO(fixed_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": cd},
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────────────────────────────
#  /process-business — Бизнес-скоринг (справка об оборотах Kaspi)
#
#  desired_income трактуется как ЖЕЛАЕМЫЙ СРЕДНЕМЕСЯЧНЫЙ КРЕДИТ
#  (поступления/оборот за месяц). Только мелкие месяцы подтягиваются
#  до этого уровня; остатки (вх./исх.) НЕ ТРОГАЕМ → QR-код Kaspi
#  и формула «вх + кр − деб = исх» остаются валидными.
# ─────────────────────────────────────────────────────────────────

@app.post("/process-business")
async def process_business_endpoint(
    file: UploadFile = File(...),
    desired_income: float = Form(..., gt=0,
        description="Желаемый среднемесячный оборот по кредиту (₸)"),
):
    if file.content_type != "application/pdf":
        return JSONResponse(
            status_code=400,
            content={"error": "Файл должен быть в формате PDF"},
        )

    print(f"[Business] Желаемый среднемесячный оборот: {desired_income:,.0f} ₸")

    content = await file.read()

    try:
        new_pdf_bytes = process_business_pdf(content, desired_income)

        # В журнал — target_turnover = desired_income (он же и есть оборот/мес)
        _journal_add(file.filename, desired_income, desired_income, "БИЗНЕС", "ok")

        from urllib.parse import quote
        base_name = file.filename if file.filename.lower().endswith(".pdf") else file.filename + ".pdf"
        safe_name = f"business_{base_name}"
        try:
            safe_name.encode("latin-1")
            cd = f"attachment; filename={safe_name}"
        except UnicodeEncodeError:
            cd = f"attachment; filename=business_output.pdf; filename*=UTF-8''{quote(safe_name)}"

        return StreamingResponse(
            io.BytesIO(new_pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": cd},
        )
    except Exception as e:
        traceback.print_exc()
        _journal_add(file.filename, desired_income, desired_income, "БИЗНЕС",
                     f"error: {str(e)[:120]}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Ошибка обработки: {str(e)}"},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8081)

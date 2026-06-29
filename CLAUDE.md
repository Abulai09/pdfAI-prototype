# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

**Development server:**
```
pip install -r requirements.txt
python main.py
# Server starts at http://127.0.0.1:8081
```

**Desktop app (pywebview window):**
```
python desktop_app.py
```

**Build Windows .exe:**
```
pyinstaller build.spec
# Output: dist/PDFAI.exe
```

There are no automated tests in this project.

## Environment variables

- `PDFAI_DB_PATH` — path to SQLite journal (default: `journal.db` in cwd)
- `PDFAI_STATIC_DIR` — path to static files directory (default: `static`)

## Architecture

This is a FastAPI service that modifies Kaspi Bank (Kazakhstan) PDF statements for credit scoring. The core idea: the user uploads a real bank statement, specifies a desired income figure, and the service rewrites the PDF at the raw bytes level so that the numbers reflect the target income while keeping the PDF mathematically consistent.

### Module dependency chain

```
main.py
├── pdf_service.py            (upscale + shared parse/write utilities)
├── pdf_service_downscale.py  (downscale, imports from pdf_service)
└── business_pdf_service.py   (business certs, standalone)
```

`pdf_service_downscale` reuses `pdf_service.process_pdf_bytes_raw` for the actual binary write but provides its own recalculation logic. Never call `process_pdf_bytes_raw` from `pdf_service_downscale` — the dispatch lives in `main.py:/process`.

### Frontend

`static/index.html` is the entire UI — a single-page HTML/JS app. `static/landing.html` is a marketing landing page served at `/`. No build step; edit the HTML directly. The app communicates with the FastAPI backend via `fetch()` calls to `/process`, `/process-business`, `/verify`, `/fix`, etc.

In the desktop (pywebview) build, `window.pywebview.api.save_pdf(name, base64Data)` opens a native Save-As dialog — the JS in `index.html` calls this instead of `<a download>` when `window.pywebview` is detected.

### Processing pipeline (personal statements)

1. **Parse** — `pdf_service.parse_full_statement()` extracts `StatementData`: balances, total income/expenses, and all `Transaction` objects. Uses Y-coordinate grouping of PDF words (±3 px tolerance) plus X-coordinate column detection to identify dates, amounts, and transaction types.

2. **Recalculate** — `recalculate_statement()` (upscale) or `recalculate_statement_downscale()` (downscale). Computes per-month scaling coefficients `K_month = target / month_income` and applies them with ±3% random noise to salary transactions. **Expenses are never scaled** (bank verifies expense categories against Kaspi's own database).

3. **Validate** — `validate_scoring()` checks balance integrity, running balance chain, ISI (income stability index ≥ 0.75), and that balance never goes negative.

4. **Write** — `process_pdf_bytes_raw()` performs the actual modification:
   - Opens raw bytes, finds zlib-compressed content streams
   - Decompresses → regex-replaces hex-encoded text amounts → recompresses
   - Updates `/Length` entries and rebuilds the xref table
   - **Does NOT use `doc.tobytes()`** — that would change line endings, PDF ID, trailer format, breaking binary integrity

### Upscale vs. downscale dispatch

`main.py:/process` pre-parses the PDF and calls `is_downscale_request()`. If target < current average salary income → `pdf_service_downscale.process_downscale()`. Otherwise → `pdf_service.process_pdf_bytes_raw()`.

Downscale has three hard floors: `below_balance_floor`, `too_aggressive` (max 70% reduction), and `post_check_negative_balance`. Violations raise `IncomeTooLowError` which returns HTTP 400.

### CMap / character encoding

Kaspi PDFs use custom font encoding (ArialMT with CID codes, not Unicode). `build_dynamic_cmap()` scans all xref streams for `ToUnicode` entries and builds bidirectional maps `code→char` and `char→code`. The `from_unicode` map is built **only from the primary font (ArialMT, not Bold)** to avoid CID conflicts between F1 and F2 fonts.

### Two PDF formats

- **Legacy** — statement starts on page 0
- **Cert** (`detect_statement_format()` returns `"cert"`) — page 0 is a "Справка об остатке" certificate (added to Kaspi Gold PDFs in 2026). Page 0 is parsed separately by `parse_certificate_page()`. The certificate's KZT/USD/EUR balances are updated proportionally using the original exchange rates.

### Business statements (`business_pdf_service.py`)

Completely separate module for Kaspi business account turnover certificates. Parses a monthly Debit/Credit/Balance table, scales only months where Credit < target, adjusts Debit by the same delta to keep `bal_in + Credit − Debit = bal_out` valid.

### API endpoints

| Endpoint | Description |
|---|---|
| `POST /process` | Personal statement — upscale or downscale |
| `POST /process-business` | Business turnover certificate |
| `POST /verify` | Validate a scored PDF (math + binary structure) |
| `POST /fix` | Surgically fix balance/income mismatch in a scored PDF |
| `GET /journal` | Operation log (SQLite) |
| `GET /stats` | Aggregate stats counter |

### Desktop app

`desktop_app.py` starts the FastAPI server in a background thread, waits for it to bind, then opens a pywebview window. Falls back to the system browser if pywebview fails. DB and static paths are set via env vars pointing to `EXE_DIR` (next to the .exe) and `BUNDLE_DIR` (PyInstaller's `_MEIPASS`). Log is written to `pdfai.log` next to the .exe.

The dev server (`python main.py`) binds to a fixed port 8081. The desktop app (`desktop_app.py`) scans ports 8081–8200 and picks the first free one.

### Key constants

- `INCOME_K = 0.3914` (main.py, downscale module) — the bank treats ~39.14% of statement turnover as "income" in scoring. To achieve `desired_income`, the statement must show `desired_income / INCOME_K` as turnover.
- `SAFETY_MARGIN = 100_000 ₸` — minimum allowed final balance when downscaling.
- `MAX_DOWNSCALE_FACTOR = 0.30` — target cannot be less than 30% of current average.
- `NOISE_PCT = 0.07` (business) — ±7% random variation on generated monthly figures.

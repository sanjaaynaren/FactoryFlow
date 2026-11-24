# Code guide (read this before the interview)

## Files
| File | What it does |
|---|---|
| `db.py` | Schema (one SQL string) and the connection. `DECTEXT` columns hold exact decimals. |
| `engine.py` | **All business rules.** Ledger (`post`, `balance`, `avg_cost`), purchases, runs (`start_run`, `complete_run`, `run_summary`), GST (`compute_tax`), orders, invoices, dispatch, payments, audit chain, lock. |
| `reports.py` | One `r_<name>` function per report. Returns title, headers, rows. |
| `exports.py` | CSV, Excel, PDF writers and the invoice PDF. |
| `app.py` | Flask routes. Read form, call `engine`, commit or roll back, render. `need("admin")` guards routes by role. |
| `seed.py` / `benchmark.py` | Section 5 master data / the scripted scenario with expected numbers. |
| `templates/`, `static/style.css` | Screens and the dark theme. |
| `tests/` | Engine tests (stock, units, tax, audit, lock) and web tests for every role. |

## One request, end to end (dispatch)
`POST /invoices/1/dispatch` → `app.invoice_dispatch` → `engine.dispatch` → for each invoice line `post(...)` inserts a **negative ledger row**
(refused if it would go below zero) → invoice and order status updated → `audit(...)` → `commit`. A `BizError` rolls everything back and shows a message.

## Likely live changes
- **Add a 500 g pack sharing the same BOM:** Masters → "+ Product / pack size": same recipe, pack factor 2.5, carton size, price. Then Masters → "+ Packaging line" for its pouch. `expected_output` already divides by the factor (test: `test_pack_size_shares_bom`).
- **Change the tolerance:** `settings` row `tolerance` (a UI field is the same pattern as `lock`).
- **Use FIFO instead of weighted average:** replace `avg_cost()` in `engine.py`. Only that function decides issue rates.
- **New report:** add `r_myreport(db, s, e, _=None)` in `reports.py` and an entry in `REPORTS`. It appears with filters and exports automatically.
- **Change GST rate:** edit the item's `gst_rate`. `compute_tax` reads it.

## Questions to be ready for
Why a ledger and not a stock column? (traceability, audit, no overwrites.) Why Decimal? (no float drift.) Why weighted average? (see design note.)
Why is invoicing blocked on shortage? (no invoice for goods that cannot ship.) How does the hash chain work? (`_hash` of content + previous hash.)

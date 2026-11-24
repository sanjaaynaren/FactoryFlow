# FactoryFlow: Order-to-Audit system for a small food manufacturer

Prototype for the NIT Tiruchirappalli (Centre for Digital Infrastructure) internship problem. A salesman's order
flows to a GST invoice, dispatch and payment; raw material flows through a recipe (BOM) into production and finished
goods; both loops meet in one stock ledger and feed the auditor's monthly reports. Every transaction is entered once.

## Run it (one command)

Needs Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python seed.py                  # loads the section 5 master data (optional, app.py does it on first run)
python app.py                   # open http://127.0.0.1:5000
```

Test logins (password for all: `demo123`):

| Role | Username |
|---|---|
| Salesman | `salesA`, `salesB` |
| Factory admin | `admin` |
| Production supervisor | `prod` |
| Accountant / auditor (read-only) | `accountant` |
| Owner | `owner` |

Reset to a clean benchmark start any time with `python seed.py`.
Run the tests with `python -m unittest discover -s tests`. They include the full benchmark and a walk through every page for every role.
Salesmen can open the same site on a phone (same Wi-Fi: `http://<your-pc-ip>:5000`).

## Stack and why

**Flask + SQLite + plain sqlite3, Jinja templates, ReportLab (PDF), openpyxl (Excel).** One small codebase and one command to run,
with no server to install. SQLite is a real relational database. Money and quantities are Python `Decimal`, stored as exact text, never floats.
The UI is a responsive web app (no install), so the salesman uses a phone browser and the admin a desktop.

## What is built (against section 6)

**Must have: all done.** Login with 5 roles and per-role access; product, customer, raw-material, recipe and packaging masters; mobile order entry and admin
approval; GST invoice PDF; production run that updates raw and finished stock automatically; planned vs actual consumption and yield with variance;
stock summary and stock ledger; sales register and stock report as CSV, Excel and PDF.

**Should have: all done.** Purchases and suppliers; payments, outstanding and ageing; cost per packet with overheads; GST summary (GSTR-1 / 3B shape);
variance alerts beyond a tolerance (setting, 3%); owner dashboard.

**Beyond the brief (my additions):**

1. **Benchmark self-test** (More → Self-test): runs the entire section 5 scenario on a scratch database and shows pass/fail for every number, plus a live stock-integrity check.
2. **Cost variance breakdown** on every run: price variance, usage variance and yield shortfall in rupees.
3. **"Explain this number"**: click any stock item to see the ledger lines and source documents that make its balance.
4. **Tamper-evident audit log**: each entry carries a hash of itself and the previous one. "Verify chain" detects any edit.
5. **Packing slip with carton conversion**: 150 packets = 7 cartons + 10 loose.
6. **WhatsApp message to draft order** (rule-based parser, no paid API) on the New order screen.
7. **Month-end lock**: a locked month rejects any dated posting; fixes go in as stock adjustments in an open month. Only the owner can unlock.
8. **Customer profitability ranking**: revenue minus cost per packet, per customer.
9. **Shortage-to-production suggestion**: a short order shows "make 1 batch; raw stock is sufficient" or what to buy first. Credit-limit warning at approval.

## Benchmark results (section 5)

All 16 checks pass (`python benchmark.py`, or the Self-test page).

| Check | Expected | Got |
|---|---|---|
| Flour / oil / pouches closing | 120 kg / 19.5 L / 306 | 120 / 19.5 / 306 |
| Finished goods after run / after Retailer A dispatch | 238 / 88 | 238 / 88 |
| Yield variance | 188 vs 200, −6%, flagged | −6.00%, flagged |
| Oil variance | +5%, flagged | +5.00%, flagged |
| Retailer A invoice | 7,500 + 187.50 + 187.50 = 7,875 | same |
| Retailer A outstanding | 3,875 | 3,875 |
| Distributor B | shortage of 32, IGST | shortage 32, IGST 5% = ₹300 on ₹6,000 |
| **Cost per packet** | my figure | **₹31.14**, margin ₹18.86 (37.7%) |

Cost per packet: materials ₹4,055 (flour 40 kg × weighted-average ₹43.125 = 1,725; oil 10.5 L = 1,470; spice 240; 194 pouches = 485; 9 cartons = 135)
+ overheads ₹1,800 (labour 1,200 + gas/power 600) = ₹5,855 ÷ 188 good packets.

**Differences to note (all reasoned):**
- Cartons used (9) against 10 planned is −10%, so it is also flagged. Pouches 194 vs 200 is −3.00%, which is not *beyond* the 3% tolerance, so it is not flagged.
- Flour is valued at weighted average (₹43.125), not at ₹45, because the 100 kg opening stock at ₹42 and the 60 kg purchase at ₹45 are mixed.

## Method and assumptions

- **Valuation: weighted-average (moving) cost**, replayed from the ledger. Simple, stable, and right for bulk ingredients bought at different prices. FIFO would need batch layers.
- **Overheads:** labour, gas and power are entered against a run and charged fully to that run's good output. Wastage cost is absorbed by the good packets, so cost per packet reflects the real yield.
- **Standard cost** (for variances) is the standard rate on each item (the rate column in the problem's BOM table).
- Opening finished goods (50 packets) are valued at standard material cost ₹19.85 because the problem gives no figure.
- An order can be approved with a shortage, but **an invoice cannot be raised** until stock covers it (so no invoice exists for goods that cannot ship). The admin can reduce the quantity or produce more.
- Packaging rule in a run: a primary pack (1 per packet, the pouch) = good packets + damaged; a carton-like item uses the counted figure.
- GST is split into CGST and SGST when the customer's state code equals the factory's (33), otherwise IGST. Invoice numbers `INV/2026-27/0001` restart each financial year (April to March). Credit notes are `CN/2026-27/0001`.
- Dummy data only. GSTINs are invented.

## Known gaps (honest list)

- No offline order capture or PWA yet, no batch or expiry tracking, no reorder alerts, no GPS visit log, no Tally or e-invoice export, no Tamil UI.
- Adaptive variance baseline (learning "normal" variance per product) is not built. The fixed tolerance is.
- Partial dispatch and returns (other than credit notes) are not supported. Monthly overheads that are not tied to a run are not allocated.
- Database is SQLite only (MySQL would need a driver change in `db.py`). No Docker file. Run with the commands above.
- Edits to a **posted** invoice are not allowed at all (cancel with a credit note); order edits and run steps are in the audit log.

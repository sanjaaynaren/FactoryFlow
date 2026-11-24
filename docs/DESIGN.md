# Design note

## Data model (ER sketch)

```
users            items (RAW | PACK | FG)  ──< recipe_lines >── recipes        customers      suppliers
                   │ FG: price, hsn, gst, carton_size, recipe_id, pack_factor
                   ├──< pack_lines (packaging per packet, per FG)
                   │
                   └──< ledger  (item, date, qty ±, rate, doc_type, doc_id, doc_ref)  <── THE SOURCE OF TRUTH FOR STOCK
                           ▲ doc_type: OPENING | PUR (purchases) | RUN (runs) | INV (dispatch) | CN (credit note) | ADJ

purchases(supplier, item, qty, rate)        runs ──< run_lines(planned, actual, rate, std_rate, cost)
orders ──< order_lines                            └──< run_overheads(kind, amount)
invoices ──< invoice_lines (hsn, taxable, cgst, sgst, igst)   payments(invoice, amount)
audit (hash chain)     locks (month)     settings (company, GSTIN, state code, tolerance)
```

## How stock works

Stock is never a stored number. The `ledger` table holds one signed row per movement and `balance = SUM(qty)`.
Every row names its source document (`PUR-0001`, `RUN-0001`, `INV/2026-27/0001`), so any balance can be explained line by line
(click an item in Stock). Writes that would take a balance below zero are refused. Corrections are new `ADJ` rows.
Reports compute `opening + receipts − issues ± adjustments = closing` from the same rows.

## How BOM consumption is computed

A **recipe** gives ingredient quantities per batch and the batch yield in base packs (40 kg flour, 10 L oil, 1.2 kg spice → 200 packs).
A **product** points at a recipe and has a `pack_factor` (a 500 g pack = 2.5 of the 200 g base pack), so `expected = yield ÷ pack_factor × batches`.
Packaging is per product (`pack_lines`: pouch 1, carton 0.05 per packet), so `planned = qty per packet × expected`.
Starting a run issues raw materials (ledger −). Completing it consumes packaging (ledger −) and adds good packets to finished goods (ledger +).
Variance % = (actual − planned) ÷ planned, flagged when its absolute value exceeds the tolerance (3%). Units convert through `convert()` (kg↔g, L↔ml, carton↔packet).
Each action runs in one database transaction, so a failed step leaves no half-posted stock.

## Costing and valuation

- **Weighted-average cost** per item, replayed from the ledger (receipts re-average, issues use the current average).
- Run material cost = Σ actual quantity × average rate at issue time. Overheads are added per run.
- **Cost per packet = (material + overheads) ÷ good packets.** Margin = selling price − cost per packet.
- Standard-cost analysis per line: price variance = (actual rate − standard rate) × actual qty; usage variance = (actual − planned) × standard rate.
  Their sum equals actual − standard material cost. Yield shortfall is shown separately in rupees at standard material cost per packet.

## GST and money

Tax is computed per line: `taxable = qty × rate` (2 dp, half-up), CGST = SGST = `taxable × rate ÷ 200`, or IGST = `taxable × rate ÷ 100`. All money is `Decimal`, stored as text.

## Audit and control

Orders, invoices, runs, payments, purchases, adjustments, master changes and locks write to `audit`. Each row's hash covers its content and the previous hash,
so editing history breaks the chain ("Verify chain"). Posted invoices are never deleted: cancel creates a credit note and returns dispatched stock to the ledger.
A locked month rejects new dated postings. Role checks are enforced on the server for every route.

"""All monthly reports. Each returns dict(title, headers, rows, links?). Money/qty stay Decimal."""
from decimal import Decimal
from datetime import date as _date
import engine as E
from engine import D, q2, fmt

REPORTS = {
    "sales_register": "Sales register", "customer_sales": "Customer-wise sales", "product_sales": "Product-wise sales",
    "gst_summary": "GST summary (GSTR-1 / 3B shape)", "purchase_register": "Purchase register",
    "stock_summary": "Stock summary", "stock_ledger": "Stock ledger", "production": "Production: planned vs actual",
    "cost_per_packet": "Cost per packet and margin", "payments": "Payments and collections",
    "outstanding": "Outstanding with ageing", "profitability": "Customer profitability ranking",
}


def month_range():
    t = _date.today()
    return t.replace(day=1).isoformat(), t.isoformat()


def build(db, name, start, end, item_id=None):
    return globals()["r_" + name](db, start, end, item_id)


def r_sales_register(db, s, e, _=None):
    rows, links, tot = [], [], [Decimal(0)] * 5
    for i in db.execute("SELECT i.*, c.name cname, c.gstin, c.state FROM invoices i JOIN customers c ON c.id=i.customer_id "
                        "WHERE i.date BETWEEN ? AND ? ORDER BY i.id", (s, e)):
        st = i["status"] if i["status"] == "POSTED" else f"CANCELLED ({i['cn_no']})"
        rows.append([i["inv_no"], i["date"], i["cname"], i["gstin"], i["state"], "CGST+SGST" if i["supply"] == "INTRA" else "IGST",
                     i["taxable"], i["cgst"], i["sgst"], i["igst"], i["total"], st])
        links.append(f"/invoices/{i['id']}")
        if i["status"] == "POSTED":
            tot = [a + b for a, b in zip(tot, [i["taxable"], i["cgst"], i["sgst"], i["igst"], i["total"]])]
    if rows:
        rows.append(["Total (posted)", "", "", "", "", ""] + tot + [""]); links.append(None)
    return dict(title="Sales register", headers=["Invoice", "Date", "Customer", "GSTIN", "Place of supply", "Tax type", "Taxable", "CGST", "SGST", "IGST", "Total", "Status"],
                rows=rows, links=links)


def r_customer_sales(db, s, e, _=None):
    rows = []
    for c in db.execute("SELECT * FROM customers ORDER BY name"):
        inv = db.execute("SELECT * FROM invoices WHERE customer_id=? AND status='POSTED' AND date BETWEEN ? AND ?", (c["id"], s, e)).fetchall()
        if inv:
            rows.append([c["name"], len(inv), sum((i["taxable"] for i in inv), Decimal(0)), sum((i["total"] for i in inv), Decimal(0)),
                         E.customer_outstanding(db, c["id"])])
    return dict(title="Customer-wise sales", headers=["Customer", "Invoices", "Taxable sales", "Invoice value", "Outstanding now"], rows=rows)


def r_product_sales(db, s, e, _=None):
    agg = {}
    for l in db.execute("SELECT l.*, i.name FROM invoice_lines l JOIN invoices v ON v.id=l.invoice_id JOIN items i ON i.id=l.item_id "
                        "WHERE v.status='POSTED' AND v.date BETWEEN ? AND ?", (s, e)):
        a = agg.setdefault(l["name"], [Decimal(0), Decimal(0)])
        a[0] += l["qty"]; a[1] += l["taxable"]
    return dict(title="Product-wise sales", headers=["Product", "Quantity", "Taxable sales"], rows=[[k, v[0], v[1]] for k, v in agg.items()])


def r_gst_summary(db, s, e, _=None):
    agg = {}
    for l in db.execute("SELECT l.*, v.supply, c.gstin, c.state FROM invoice_lines l JOIN invoices v ON v.id=l.invoice_id "
                        "JOIN customers c ON c.id=v.customer_id WHERE v.status='POSTED' AND v.date BETWEEN ? AND ?", (s, e)):
        key = ("B2B" if l["gstin"] else "B2C", l["state"], l["gst_rate"])
        a = agg.setdefault(key, [Decimal(0)] * 4)
        for n, f in enumerate(["taxable", "cgst", "sgst", "igst"]):
            a[n] += l[f]
    rows = [[k[0], k[1], k[2], *v, sum(v[1:], Decimal(0))] for k, v in sorted(agg.items(), key=lambda x: str(x[0]))]
    if rows:
        tot = [sum((r[i] for r in rows), Decimal(0)) for i in range(3, 8)]
        rows.append(["Total outward supplies", "", ""] + tot)
    for i in db.execute("SELECT * FROM invoices WHERE status='CANCELLED' AND cn_date BETWEEN ? AND ?", (s, e)):
        rows.append([f"Credit note {i['cn_no']}", f"against {i['inv_no']}", "", -i["taxable"], -i["cgst"], -i["sgst"], -i["igst"], -(i["total"] - i["taxable"])])
    return dict(title="GST summary of outward supplies", headers=["Type", "Place of supply", "GST %", "Taxable", "CGST", "SGST", "IGST", "Total tax"], rows=rows)


def r_purchase_register(db, s, e, _=None):
    rows = [[p["id"], p["date"], p["sname"], p["iname"], p["qty"], p["unit"], p["rate"], p["amount"], p["bill_no"]] for p in
            db.execute("SELECT p.*, s.name sname, i.name iname, i.unit unit FROM purchases p LEFT JOIN suppliers s ON s.id=p.supplier_id "
                       "JOIN items i ON i.id=p.item_id WHERE p.date BETWEEN ? AND ? ORDER BY p.id", (s, e))]
    if rows:
        rows.append(["Total", "", "", "", "", "", "", sum((r[7] for r in rows), Decimal(0)), ""])
    return dict(title="Purchase register", headers=["Ref", "Date", "Supplier", "Item", "Qty", "Unit", "Rate / unit", "Amount", "Bill no"], rows=rows,
                actions=[("+ Record purchase", "/purchases/new")])


def r_stock_summary(db, s, e, _=None):
    rows, links = [], []
    for it in db.execute("SELECT * FROM items ORDER BY CASE kind WHEN 'RAW' THEN 1 WHEN 'PACK' THEN 2 ELSE 3 END, name"):
        opening = receipts = issues = adj = Decimal(0)
        for r in db.execute("SELECT * FROM ledger WHERE item_id=?", (it["id"],)):
            if r["doc_type"] == "OPENING" or r["date"] < s:
                opening += r["qty"]
            elif r["date"] <= e:
                if r["doc_type"] == "ADJ":
                    adj += r["qty"]
                elif r["qty"] > 0:
                    receipts += r["qty"]
                else:
                    issues += r["qty"]
        closing = opening + receipts + issues + adj
        rows.append([{"RAW": "Raw material", "PACK": "Packaging", "FG": "Finished goods"}[it["kind"]], it["name"], it["unit"],
                     opening, receipts, -issues, adj, closing, q2(E.avg_cost(db, it["id"])), q2(closing * E.avg_cost(db, it["id"]))])
        links.append(f"/stock/{it['id']}")
    return dict(title="Stock summary (opening + receipts - issues ± adjustments = closing)",
                headers=["Group", "Item", "Unit", "Opening", "Receipts", "Issues / sales", "Adjustments", "Closing", "Avg cost", "Value"], rows=rows, links=links)


def r_stock_ledger(db, s, e, item_id=None):
    rows = []
    items = [E.get_item(db, item_id)] if item_id else db.execute("SELECT * FROM items ORDER BY kind,name").fetchall()
    for it in items:
        bal = Decimal(0)
        for r in db.execute("SELECT * FROM ledger WHERE item_id=? ORDER BY id", (it["id"],)):
            bal += r["qty"]
            if item_id or s <= r["date"] <= e:
                rows.append([r["date"], it["name"], r["doc_ref"], r["doc_type"], r["qty"] if r["qty"] > 0 else "", -r["qty"] if r["qty"] < 0 else "",
                             bal, q2(r["rate"]), r["note"]])
    return dict(title="Stock ledger" + (f": {items[0]['name']}" if item_id else ""),
                headers=["Date", "Item", "Source document", "Type", "In", "Out", "Balance", "Rate", "Note"], rows=rows)


def r_production(db, s, e, _=None):
    rows, links = [], []
    for r in db.execute("SELECT id FROM runs WHERE status='DONE' AND date BETWEEN ? AND ? ORDER BY id", (s, e)):
        m = E.run_summary(db, r["id"])
        run = m["run"]
        rows.append([run["run_no"], run["date"], "Output (packets)", run["expected"], run["actual"], m["yield_var"], "FLAG" if m["yield_flag"] else "ok"])
        links.append(f"/runs/{run['id']}")
        for l in m["lines"]:
            rows.append(["", "", f"{l['name']} ({l['unit']})", l["planned"], l["actual"], l["var"], "FLAG" if l["flag"] else "ok"])
            links.append(None)
    return dict(title="Production: planned vs actual", headers=["Run", "Date", "Item", "Planned / expected", "Actual", "Variance %", "Status"], rows=rows, links=links)


def r_cost_per_packet(db, s, e, _=None):
    rows = []
    for r in db.execute("SELECT id FROM runs WHERE status='DONE' AND date BETWEEN ? AND ? ORDER BY id", (s, e)):
        m = E.run_summary(db, r["id"])
        run = m["run"]
        rows.append([run["run_no"], run["product"], m["material"], m["overhead_total"], m["total_cost"], run["actual"],
                     q2(m["cpp"]) if m["cpp"] is not None else "", run["price"], q2(m["margin"]) if m["margin"] is not None else "",
                     m["margin_pct"] if m["margin_pct"] is not None else ""])
    return dict(title="Cost per packet and margin", headers=["Run", "Product", "Material", "Overheads", "Total cost", "Good packets", "Cost / packet", "Selling price", "Margin / packet", "Margin %"], rows=rows)


def r_payments(db, s, e, _=None):
    rows = [[p["date"], p["cname"], p["inv_no"], p["amount"], p["mode"], p["ref"]] for p in
            db.execute("SELECT p.*, c.name cname, i.inv_no FROM payments p JOIN customers c ON c.id=p.customer_id JOIN invoices i ON i.id=p.invoice_id "
                       "WHERE p.date BETWEEN ? AND ? ORDER BY p.id", (s, e))]
    if rows:
        rows.append(["Total", "", "", sum((r[3] for r in rows), Decimal(0)), "", ""])
    return dict(title="Payments and collections", headers=["Date", "Customer", "Invoice", "Amount", "Mode", "Reference"], rows=rows)


def r_outstanding(db, s, e, _=None):
    rows, links = [], []
    asof = _date.fromisoformat(e)
    for i in db.execute("SELECT i.*, c.name cname FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.status='POSTED' ORDER BY c.name, i.date"):
        due, paid = E.invoice_due(db, i["id"])
        if due <= 0:
            continue
        days = (asof - _date.fromisoformat(i["date"])).days
        b = [Decimal(0)] * 4
        b[0 if days <= 30 else 1 if days <= 60 else 2 if days <= 90 else 3] = due
        rows.append([i["cname"], i["inv_no"], i["date"], i["total"], paid, due, days, *b])
        links.append(f"/invoices/{i['id']}")
    if rows:
        rows.append(["Total outstanding", "", "", "", "", sum((r[5] for r in rows), Decimal(0)), ""] + [sum((r[7 + k] for r in rows), Decimal(0)) for k in range(4)])
        links.append(None)
    return dict(title="Outstanding receivables with ageing (as of end date)", headers=["Customer", "Invoice", "Date", "Invoice total", "Paid", "Due", "Days", "0-30", "31-60", "61-90", "90+"], rows=rows, links=links)


def r_profitability(db, s, e, _=None):
    cpp = {i["id"]: E.latest_cpp(db, i["id"]) for i in db.execute("SELECT id FROM items WHERE kind='FG'")}
    rows = []
    for c in db.execute("SELECT * FROM customers"):
        rev = cost = qty = Decimal(0)
        for l in db.execute("SELECT l.* FROM invoice_lines l JOIN invoices v ON v.id=l.invoice_id WHERE v.customer_id=? AND v.status='POSTED' AND v.date BETWEEN ? AND ?", (c["id"], s, e)):
            rev += l["taxable"]; qty += l["qty"]
            cost += l["qty"] * (cpp.get(l["item_id"]) or Decimal(0))
        if qty:
            rows.append([c["name"], qty, rev, q2(cost), q2(rev - cost), q2((rev - cost) / rev * 100), E.customer_outstanding(db, c["id"])])
    rows.sort(key=lambda r: -r[4])
    rows = [[n + 1] + r for n, r in enumerate(rows)]
    return dict(title="Customer profitability ranking (revenue minus latest cost per packet)",
                headers=["Rank", "Customer", "Packets", "Revenue", "Cost", "Profit", "Margin %", "Outstanding"], rows=rows)

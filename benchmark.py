"""Runs the section-5 scenario end to end through the same engine functions the web app uses."""
import os
import tempfile
from decimal import Decimal as D
import db as dbm
import seed
import engine as E


def run_benchmark(path=None, keep=False):
    tmp = None
    if path is None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "bench.db")
    db = seed.reset(path)
    ids = {r["name"]: r["id"] for r in db.execute("SELECT id,name FROM items")}
    uid = {r["username"]: r["id"] for r in db.execute("SELECT id,username FROM users")}
    cust = {r["name"]: r["id"] for r in db.execute("SELECT id,name FROM customers")}
    d, fg = E.today(), ids["Murukku 200 g"]

    E.record_purchase(db, "prod", 1, ids["Flour"], 60, "kg", 45, d, "BILL-1")                         # step 1
    rid = E.start_run(db, "prod", fg, 1, {ids["Flour"]: (40, "kg"), ids["Cooking oil"]: (D("10.5"), "L"),
                                          ids["Salt and spice mix"]: (D("1.2"), "kg")}, d)            # step 2
    E.complete_run(db, "prod", rid, 188, 6, 9, "Fryer temperature too low", d)                        # step 3
    after_run_fg = E.balance(db, fg)
    oa = E.create_order(db, uid["salesA"], cust["Retailer A"], [(fg, 150)])                           # step 4
    E.set_order_status(db, "admin", oa, "APPROVED", ("SUBMITTED",))                                   # step 5
    inv_a = E.create_invoice(db, "admin", oa, d)
    E.dispatch(db, "admin", inv_a, d)
    after_dispatch_fg = E.balance(db, fg)
    ob = E.create_order(db, uid["salesB"], cust["Distributor B"], [(fg, 120)])                        # step 6
    ov_b = E.order_overview(db, ob)
    E.set_order_status(db, "admin", ob, "APPROVED", ("SUBMITTED",))
    try:
        E.create_invoice(db, "admin", ob, d)
        blocked = False
    except E.BizError:
        blocked = True
    E.record_payment(db, "admin", inv_a, 4000, d, "UPI", "UTR123")                                    # step 7
    E.add_overhead(db, "prod", rid, "Labour", 1200, d)                                                # step 8
    E.add_overhead(db, "prod", rid, "Gas / power", 600, d)
    db.commit()

    s = E.run_summary(db, rid)
    ia = db.execute("SELECT * FROM invoices WHERE id=?", (inv_a,)).fetchone()
    short = [c for c in ov_b["check"] if not c["ok"]]
    lines = {l["name"]: l for l in s["lines"]}
    checks = [
        ("Flour closing balance (kg)", "120", E.fmt(E.balance(db, ids["Flour"]))),
        ("Oil closing balance (L)", "19.5", E.fmt(E.balance(db, ids["Cooking oil"]))),
        ("Pouches closing balance", "306", E.fmt(E.balance(db, ids["200 g pouch"]))),
        ("Finished goods after run", "238", E.fmt(after_run_fg)),
        ("Finished goods after Retailer A dispatch", "88", E.fmt(after_dispatch_fg)),
        ("Yield variance (%) and flagged", "-6.00 flagged", f"{s['yield_var']} {'flagged' if s['yield_flag'] else 'not flagged'}"),
        ("Oil consumption variance (%) and flagged", "5.00 flagged",
         f"{lines['Cooking oil']['var']} {'flagged' if lines['Cooking oil']['flag'] else 'not flagged'}"),
        ("Retailer A taxable", "7500.00", str(ia["taxable"])),
        ("Retailer A CGST", "187.50", str(ia["cgst"])),
        ("Retailer A SGST", "187.50", str(ia["sgst"])),
        ("Retailer A invoice total", "7875.00", str(ia["total"])),
        ("Retailer A outstanding", "3875.00", str(E.q2(E.customer_outstanding(db, cust["Retailer A"])))),
        ("Distributor B shortage (packets)", "32", E.fmt(short[0]["short"]) if short else "0"),
        ("Distributor B tax type", "IGST", "IGST" if ov_b["tax"]["supply"] == "INTER" and ov_b["tax"]["igst"] > 0 else "CGST+SGST"),
        ("Distributor B invoice blocked on shortage", "yes", "yes" if blocked else "no"),
        ("Cost per packet (Rs, weighted avg + overheads)", "31.14", str(E.q2(s["cpp"]))),
    ]
    results = [(n, e, a, e == a) for n, e, a in checks]
    return dict(results=results, ok=all(r[3] for r in results), summary=s, path=path, db=db if keep else None)


if __name__ == "__main__":
    out = run_benchmark()
    for n, e, a, ok in out["results"]:
        print(("PASS" if ok else "FAIL"), f"{n:50s} expected {e:14s} got {a}")
    print("ALL PASS" if out["ok"] else "SOME FAILED")

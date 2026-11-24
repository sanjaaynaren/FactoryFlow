"""Business logic. Every function takes an open sqlite3 connection `db` and does NOT commit
(the web layer commits on success and rolls back on BizError, so each action is all-or-nothing)."""
import hashlib
import json
from datetime import date as _date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING


class BizError(Exception):
    """A business-rule violation that is shown to the user (not a crash)."""


# ---------------------------------------------------------------- helpers
def D(x):
    if isinstance(x, Decimal):
        return x
    if x is None or str(x).strip() == "":
        return Decimal(0)
    return Decimal(str(x).strip())


def q2(x):  # money: 2 decimals, half-up
    return D(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def fmt(x):  # quantity without trailing zeros
    return format(D(x).normalize(), "f")


def inr(x):
    return "₹" + format(q2(x), ",.2f")


def pct(actual, planned):
    planned = D(planned)
    return Decimal(0) if planned == 0 else q2((D(actual) - planned) / planned * 100)


def today():
    return _date.today().isoformat()


def now():
    return datetime.now().isoformat(timespec="seconds")


def fy_label(date_str):
    y, m = int(date_str[:4]), int(date_str[5:7])
    s = y if m >= 4 else y - 1
    return f"{s}-{(s + 1) % 100:02d}"


def setting(db, key, default=None):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def get_item(db, item_id):
    r = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not r:
        raise BizError("Item not found")
    return r


def item_by_name(db, name):
    return db.execute("SELECT * FROM items WHERE name=?", (name,)).fetchone()


# ---------------------------------------------------------------- units
UNITS = {"kg": ("mass", 1000), "g": ("mass", 1), "L": ("vol", 1000), "ml": ("vol", 1),
         "pcs": ("count", 1), "packet": ("count", 1)}


def convert(qty, from_unit, to_unit, carton_size=None):
    """Convert qty between units (kg<->g, L<->ml, carton<->packet)."""
    qty = D(qty)
    if from_unit == to_unit:
        return qty
    if {from_unit, to_unit} == {"carton", "packet"}:
        if not carton_size:
            raise BizError("Carton size is not set for this product")
        return qty * carton_size if from_unit == "carton" else qty / carton_size
    if from_unit not in UNITS or to_unit not in UNITS or UNITS[from_unit][0] != UNITS[to_unit][0]:
        raise BizError(f"Cannot convert {from_unit} to {to_unit}")
    return qty * UNITS[from_unit][1] / UNITS[to_unit][1]


def to_base(item, qty, unit):
    return convert(qty, unit or item["unit"], item["unit"], item["carton_size"])


def carton_split(qty, carton_size):
    qty = int(D(qty))
    return (qty // carton_size, qty % carton_size) if carton_size else (0, qty)


# ---------------------------------------------------------------- audit (hash chain)
def _hash(ts, user, action, entity, eid, detail, prev):
    return hashlib.sha256(json.dumps([ts, user, action, entity, str(eid), detail, prev]).encode()).hexdigest()


def audit(db, user, action, entity, eid, detail=""):
    last = db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else "GENESIS"
    ts = now()
    h = _hash(ts, user, action, entity, eid, detail, prev)
    db.execute("INSERT INTO audit(ts,user,action,entity,entity_id,detail,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?)",
               (ts, user, action, entity, str(eid), detail, prev, h))


def verify_audit(db):
    """Returns (ok, first_bad_id). Recomputes the whole chain."""
    prev = "GENESIS"
    for r in db.execute("SELECT * FROM audit ORDER BY id"):
        if r["prev_hash"] != prev or r["hash"] != _hash(r["ts"], r["user"], r["action"], r["entity"],
                                                        r["entity_id"], r["detail"], r["prev_hash"]):
            return False, r["id"]
        prev = r["hash"]
    return True, None


# ---------------------------------------------------------------- month lock
def check_lock(db, date_str):
    if db.execute("SELECT 1 FROM locks WHERE month=?", (date_str[:7],)).fetchone():
        raise BizError(f"{date_str[:7]} is locked by the auditor. Post an adjustment in an open month instead.")


# ---------------------------------------------------------------- ledger / stock
def balance(db, item_id):
    return sum((r["qty"] for r in db.execute("SELECT qty FROM ledger WHERE item_id=?", (item_id,))), Decimal(0))


def avg_cost(db, item_id):
    """Weighted-average (moving) cost, replayed from the ledger."""
    bal = val = avg = Decimal(0)
    for r in db.execute("SELECT qty,rate FROM ledger WHERE item_id=? ORDER BY id", (item_id,)):
        if r["qty"] > 0:
            val += r["qty"] * r["rate"]
            bal += r["qty"]
            avg = val / bal
        else:
            bal += r["qty"]
            val = bal * avg
    return avg


def post(db, item_id, qty, rate, doc_type, doc_id, doc_ref, date_str, note=""):
    qty = D(qty)
    if qty < 0:
        have = balance(db, item_id)
        if have + qty < 0:
            it = get_item(db, item_id)
            raise BizError(f"Not enough {it['name']}: have {fmt(have)} {it['unit']}, need {fmt(-qty)}")
    db.execute("INSERT INTO ledger(item_id,date,qty,rate,doc_type,doc_id,doc_ref,note,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (item_id, date_str, qty, D(rate), doc_type, doc_id, doc_ref, note, now()))


def adjust_stock(db, user, item_id, qty, unit, reason, date_str):
    check_lock(db, date_str)
    it = get_item(db, item_id)
    if not reason.strip():
        raise BizError("Give a reason for the adjustment")
    q = to_base(it, qty, unit)
    post(db, item_id, q, avg_cost(db, item_id), "ADJ", 0, "ADJ", date_str, reason)
    audit(db, user, "ADJUST", "item", item_id, f"{fmt(q)} {it['unit']}: {reason}")


def check_integrity(db):
    """Re-derive every balance from the ledger and compare with a second independent computation."""
    out = []
    for it in db.execute("SELECT * FROM items ORDER BY kind,name"):
        rows = db.execute("SELECT qty FROM ledger WHERE item_id=?", (it["id"],)).fetchall()
        a = sum((r["qty"] for r in rows), Decimal(0))
        ins = sum((r["qty"] for r in rows if r["qty"] > 0), Decimal(0))
        outs = sum((r["qty"] for r in rows if r["qty"] < 0), Decimal(0))
        out.append((it["name"], a, ins + outs, a == ins + outs and a >= 0))
    return out


# ---------------------------------------------------------------- purchases
def record_purchase(db, user, supplier_id, item_id, qty, unit, rate, date_str, bill_no=""):
    check_lock(db, date_str)
    it = get_item(db, item_id)
    qty, rate = D(qty), D(rate)
    if qty <= 0 or rate <= 0:
        raise BizError("Quantity and rate must be positive")
    base_qty = to_base(it, qty, unit)
    amount = q2(qty * rate)
    base_rate = amount / base_qty  # cost per base unit
    cur = db.execute("INSERT INTO purchases(supplier_id,item_id,date,qty,rate,amount,bill_no) VALUES (?,?,?,?,?,?,?)",
                     (supplier_id, item_id, date_str, base_qty, base_rate, amount, bill_no))
    pid = cur.lastrowid
    post(db, item_id, base_qty, base_rate, "PUR", pid, f"PUR-{pid:04d}", date_str, f"Purchase {bill_no}")
    audit(db, user, "PURCHASE", "purchase", pid, f"{it['name']} {fmt(base_qty)} {it['unit']} = {amount}")
    return pid


# ---------------------------------------------------------------- BOM / production
def recipe_lines(db, product):
    return db.execute("SELECT l.*, i.name, i.unit, i.std_rate FROM recipe_lines l JOIN items i ON i.id=l.item_id "
                      "WHERE l.recipe_id=? ORDER BY l.id", (product["recipe_id"],)).fetchall()


def pack_lines(db, product_id):
    return db.execute("SELECT l.*, i.name, i.unit, i.std_rate FROM pack_lines l JOIN items i ON i.id=l.item_id "
                      "WHERE l.product_id=? ORDER BY l.id", (product_id,)).fetchall()


def expected_output(db, product, batches):
    r = db.execute("SELECT batch_yield FROM recipes WHERE id=?", (product["recipe_id"],)).fetchone()
    if not r:
        raise BizError("This product has no BOM (recipe) yet")
    return r["batch_yield"] / product["pack_factor"] * batches


def planned_for(db, product, batches):
    """Planned consumption for `batches` batches: [(item_id, name, unit, qty, phase)]."""
    exp = expected_output(db, product, batches)
    out = [(l["item_id"], l["name"], l["unit"], l["qty"] * batches, "ISSUE") for l in recipe_lines(db, product)]
    out += [(l["item_id"], l["name"], l["unit"], l["qty_per_pack"] * exp, "PACK") for l in pack_lines(db, product["id"])]
    return out


def start_run(db, user, product_id, batches, issued, date_str):
    """issued: {item_id: (qty, unit)} for recipe (raw) items. Missing items default to the BOM quantity."""
    check_lock(db, date_str)
    product = get_item(db, product_id)
    batches = int(batches)
    if batches < 1:
        raise BizError("Batches must be at least 1")
    exp = expected_output(db, product, batches)
    cur = db.execute("INSERT INTO runs(run_no,product_id,batches,date,status,expected,user) VALUES ('',?,?,?, 'OPEN',?,?)",
                     (product_id, batches, date_str, exp, user))
    rid = cur.lastrowid
    run_no = f"RUN-{rid:04d}"
    db.execute("UPDATE runs SET run_no=? WHERE id=?", (run_no, rid))
    for item_id, name, unit, planned, phase in planned_for(db, product, batches):
        if phase != "ISSUE":
            continue
        it = get_item(db, item_id)
        qty, u = issued.get(item_id, (planned, it["unit"]))
        actual = to_base(it, qty, u)
        rate = avg_cost(db, item_id)
        post(db, item_id, -actual, rate, "RUN", rid, run_no, date_str, "Issued to production")
        db.execute("INSERT INTO run_lines(run_id,item_id,planned,actual,rate,std_rate,cost,phase) VALUES (?,?,?,?,?,?,?,?)",
                   (rid, item_id, planned, actual, rate, it["std_rate"], actual * rate, "ISSUE"))
    audit(db, user, "RUN_START", "run", rid, f"{run_no} {product['name']} x{batches}")
    return rid


def complete_run(db, user, run_id, good, damaged, cartons_used, reason, date_str):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["status"] != "OPEN":
        raise BizError("Run is not open")
    check_lock(db, date_str)
    good, damaged = D(good), D(damaged)
    if good < 0 or damaged < 0:
        raise BizError("Quantities cannot be negative")
    product = get_item(db, run["product_id"])
    exp = run["expected"]
    for pl in pack_lines(db, product["id"]):
        per = pl["qty_per_pack"]
        planned = per * exp
        if per >= 1:      # primary pack (pouch): one per packet, plus damaged ones
            actual = per * good + damaged
        elif cartons_used not in (None, ""):   # carton-like: operator counts them
            actual = D(cartons_used)
        else:
            actual = (per * good).to_integral_value(ROUND_CEILING)
        rate = avg_cost(db, pl["item_id"])
        post(db, pl["item_id"], -actual, rate, "RUN", run_id, run["run_no"], date_str, "Packaging used")
        db.execute("INSERT INTO run_lines(run_id,item_id,planned,actual,rate,std_rate,cost,phase) VALUES (?,?,?,?,?,?,?,?)",
                   (run_id, pl["item_id"], planned, actual, rate, pl["std_rate"], actual * rate, "PACK"))
    material = sum((r["cost"] for r in db.execute("SELECT cost FROM run_lines WHERE run_id=?", (run_id,))), Decimal(0))
    if good > 0:
        post(db, product["id"], good, material / good, "RUN", run_id, run["run_no"], date_str, "Finished goods produced")
    db.execute("UPDATE runs SET status='DONE', actual=?, damaged=?, reason=?, completed_at=? WHERE id=?",
               (good, damaged, reason, now(), run_id))
    audit(db, user, "RUN_DONE", "run", run_id, f"{run['run_no']} output {fmt(good)} of {fmt(exp)}, damaged pouches {fmt(damaged)}")


def add_overhead(db, user, run_id, kind, amount, date_str):
    check_lock(db, date_str)
    amount = q2(amount)
    if amount <= 0:
        raise BizError("Amount must be positive")
    db.execute("INSERT INTO run_overheads(run_id,kind,amount,date) VALUES (?,?,?,?)", (run_id, kind, amount, date_str))
    audit(db, user, "OVERHEAD", "run", run_id, f"{kind} {amount}")


def run_summary(db, run_id):
    """Everything about one run: planned vs actual, variance flags, cost split, cost per packet."""
    run = db.execute("SELECT r.*, i.name AS product, i.price, i.unit AS punit FROM runs r JOIN items i ON i.id=r.product_id "
                     "WHERE r.id=?", (run_id,)).fetchone()
    tol = D(setting(db, "tolerance", "3"))
    lines, price_v, usage_v, material, std_material = [], Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    for l in db.execute("SELECT l.*, i.name, i.unit FROM run_lines l JOIN items i ON i.id=l.item_id WHERE run_id=? ORDER BY l.id", (run_id,)):
        v = pct(l["actual"], l["planned"])
        pv = (l["rate"] - l["std_rate"]) * l["actual"]
        uv = (l["actual"] - l["planned"]) * l["std_rate"]
        price_v += pv
        usage_v += uv
        material += l["cost"]
        std_material += l["planned"] * l["std_rate"]
        lines.append(dict(name=l["name"], unit=l["unit"], planned=l["planned"], actual=l["actual"], var=v,
                          flag=abs(v) > tol, rate=l["rate"], std_rate=l["std_rate"], cost=l["cost"],
                          price_var=q2(pv), usage_var=q2(uv)))
    overheads = db.execute("SELECT * FROM run_overheads WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    oh = sum((o["amount"] for o in overheads), Decimal(0))
    s = dict(run=run, lines=lines, tol=tol, material=q2(material), overheads=overheads, overhead_total=q2(oh),
             price_var=q2(price_v), usage_var=q2(usage_v), done=run["status"] == "DONE")
    if run["status"] == "DONE":
        s["yield_var"] = pct(run["actual"], run["expected"])
        s["yield_flag"] = abs(s["yield_var"]) > tol
        s["total_cost"] = q2(material + oh)
        s["cpp"] = (material + oh) / run["actual"] if run["actual"] > 0 else None
        s["margin"] = (run["price"] - s["cpp"]) if s["cpp"] is not None else None
        s["margin_pct"] = q2(s["margin"] / run["price"] * 100) if s["margin"] is not None and run["price"] else None
        std_cpp = std_material / run["expected"] if run["expected"] else Decimal(0)
        s["std_cpp"] = std_cpp
        s["yield_loss_value"] = q2((run["expected"] - run["actual"]) * std_cpp)
    return s


def latest_cpp(db, item_id):
    r = db.execute("SELECT id FROM runs WHERE product_id=? AND status='DONE' ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    return run_summary(db, r["id"])["cpp"] if r else None


# ---------------------------------------------------------------- GST
def compute_tax(db, customer, lines):
    """lines: [(item_row, qty, rate)] -> dict with per-line and total tax. CGST+SGST if same state, else IGST."""
    intra = customer["state_code"] == setting(db, "state_code")
    out, tot = [], dict(taxable=Decimal(0), cgst=Decimal(0), sgst=Decimal(0), igst=Decimal(0))
    for it, qty, rate in lines:
        taxable = q2(D(qty) * D(rate))
        r = it["gst_rate"]
        c = s = i = Decimal(0)
        if intra:
            c = s = q2(taxable * r / 200)
        else:
            i = q2(taxable * r / 100)
        out.append(dict(item=it, qty=D(qty), rate=D(rate), taxable=taxable, gst_rate=r, cgst=c, sgst=s, igst=i))
        tot["taxable"] += taxable
        tot["cgst"] += c
        tot["sgst"] += s
        tot["igst"] += i
    tot["total"] = tot["taxable"] + tot["cgst"] + tot["sgst"] + tot["igst"]
    return dict(supply="INTRA" if intra else "INTER", lines=out, **tot)


# ---------------------------------------------------------------- orders
def create_order(db, user_id, customer_id, lines, remarks=""):
    merged = {}
    for item_id, qty in lines:
        if D(qty) > 0:
            merged[item_id] = merged.get(item_id, Decimal(0)) + D(qty)
    if not merged:
        raise BizError("Add at least one product with a quantity")
    cur = db.execute("INSERT INTO orders(order_no,customer_id,salesman_id,status,remarks,created_at) VALUES ('',?,?, 'SUBMITTED',?,?)",
                     (customer_id, user_id, remarks, now()))
    oid = cur.lastrowid
    db.execute("UPDATE orders SET order_no=? WHERE id=?", (f"ORD-{oid:04d}", oid))
    for item_id, qty in merged.items():
        db.execute("INSERT INTO order_lines(order_id,item_id,qty,rate) VALUES (?,?,?,?)",
                   (oid, item_id, qty, get_item(db, item_id)["price"]))
    u = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    audit(db, u["username"] if u else str(user_id), "ORDER_NEW", "order", oid, f"ORD-{oid:04d}")
    return oid


def order_lines(db, order_id):
    return db.execute("SELECT l.*, i.name, i.unit, i.gst_rate, i.carton_size FROM order_lines l JOIN items i ON i.id=l.item_id "
                      "WHERE order_id=? ORDER BY l.id", (order_id,)).fetchall()


def update_order(db, user, order_id, lines):
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if o["status"] != "SUBMITTED":
        raise BizError("Only submitted orders can be edited")
    before = [(l["item_id"], str(l["qty"]), str(l["rate"])) for l in order_lines(db, order_id)]
    db.execute("DELETE FROM order_lines WHERE order_id=?", (order_id,))
    after = []
    for item_id, qty, rate in lines:
        if D(qty) > 0:
            db.execute("INSERT INTO order_lines(order_id,item_id,qty,rate) VALUES (?,?,?,?)", (order_id, item_id, D(qty), D(rate)))
            after.append((item_id, str(D(qty)), str(D(rate))))
    if not after:
        raise BizError("An order needs at least one line (reject it instead)")
    audit(db, user, "ORDER_EDIT", "order", order_id, json.dumps({"before": before, "after": after}))


def set_order_status(db, user, order_id, new, allowed_from):
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if o["status"] not in allowed_from:
        raise BizError(f"Order is {o['status']}; cannot change to {new}")
    db.execute("UPDATE orders SET status=? WHERE id=?", (new, order_id))
    audit(db, user, f"ORDER_{new}", "order", order_id, o["order_no"])


def stock_check(db, order_id):
    rows = []
    for l in order_lines(db, order_id):
        have = balance(db, l["item_id"])
        rows.append(dict(item_id=l["item_id"], name=l["name"], need=l["qty"], have=have,
                         short=max(Decimal(0), l["qty"] - have), ok=have >= l["qty"]))
    return rows


def plan_production(db, item_id, short):
    """How many batches cover a shortage, and do we hold the raw/packaging stock for them?"""
    p = get_item(db, item_id)
    per = expected_output(db, p, 1)
    batches = int((D(short) / per).to_integral_value(ROUND_CEILING))
    needs = []
    for _id, name, unit, qty, _ph in planned_for(db, p, batches):
        have = balance(db, _id)
        needs.append(dict(name=name, unit=unit, need=qty, have=have, short=max(Decimal(0), qty - have)))
    return dict(batches=batches, output=per * batches, needs=needs, ok=all(n["short"] == 0 for n in needs))


def customer_outstanding(db, customer_id):
    inv = sum((r["total"] for r in db.execute("SELECT total FROM invoices WHERE customer_id=? AND status='POSTED'", (customer_id,))), Decimal(0))
    paid = sum((r["amount"] for r in db.execute(
        "SELECT p.amount FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE p.customer_id=? AND i.status='POSTED'", (customer_id,))), Decimal(0))
    return inv - paid


def order_overview(db, order_id):
    o = db.execute("SELECT o.*, c.name AS customer, c.state, c.state_code, c.credit_limit FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?", (order_id,)).fetchone()
    cust = db.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
    lines = order_lines(db, order_id)
    tax = compute_tax(db, cust, [(get_item(db, l["item_id"]), l["qty"], l["rate"]) for l in lines])
    check = stock_check(db, order_id)
    plans = {c["item_id"]: plan_production(db, c["item_id"], c["short"]) for c in check if not c["ok"]
             and get_item(db, c["item_id"])["recipe_id"]}
    due = customer_outstanding(db, cust["id"])
    warn = None
    if cust["credit_limit"] and cust["credit_limit"] > 0 and due + tax["total"] > cust["credit_limit"]:
        warn = f"Credit limit {inr(cust['credit_limit'])} would be exceeded (outstanding {inr(due)} + this order {inr(tax['total'])})"
    inv = db.execute("SELECT * FROM invoices WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
    return dict(order=o, lines=lines, tax=tax, check=check, plans=plans, credit_warning=warn, invoice=inv,
                all_ok=all(c["ok"] for c in check))


# ---------------------------------------------------------------- invoices, dispatch, payments
def create_invoice(db, user, order_id, date_str):
    check_lock(db, date_str)
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if o["status"] != "APPROVED":
        raise BizError("Approve the order before invoicing")
    for c in stock_check(db, order_id):
        if not c["ok"]:
            raise BizError(f"Cannot invoice: short by {fmt(c['short'])} packets of {c['name']}. "
                           f"Reduce the quantity or produce more first.")
    cust = db.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
    ls = order_lines(db, order_id)
    tax = compute_tax(db, cust, [(get_item(db, l["item_id"]), l["qty"], l["rate"]) for l in ls])
    fy = fy_label(date_str)
    n = db.execute("SELECT COUNT(*) c FROM invoices WHERE inv_no LIKE ?", (f"INV/{fy}/%",)).fetchone()["c"] + 1
    inv_no = f"INV/{fy}/{n:04d}"
    cur = db.execute("INSERT INTO invoices(inv_no,order_id,customer_id,date,supply,taxable,cgst,sgst,igst,total,status) VALUES (?,?,?,?,?,?,?,?,?,?,'POSTED')",
                     (inv_no, order_id, cust["id"], date_str, tax["supply"], tax["taxable"], tax["cgst"], tax["sgst"], tax["igst"], tax["total"]))
    iid = cur.lastrowid
    for l in tax["lines"]:
        db.execute("INSERT INTO invoice_lines(invoice_id,item_id,hsn,qty,rate,taxable,gst_rate,cgst,sgst,igst) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (iid, l["item"]["id"], l["item"]["hsn"], l["qty"], l["rate"], l["taxable"], l["gst_rate"], l["cgst"], l["sgst"], l["igst"]))
    db.execute("UPDATE orders SET status='INVOICED' WHERE id=?", (order_id,))
    audit(db, user, "INVOICE", "invoice", iid, f"{inv_no} total {tax['total']}")
    return iid


def dispatch(db, user, invoice_id, date_str):
    check_lock(db, date_str)
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv["status"] != "POSTED" or inv["dispatched_on"]:
        raise BizError("Invoice is cancelled or already dispatched")
    for l in db.execute("SELECT * FROM invoice_lines WHERE invoice_id=?", (invoice_id,)):
        post(db, l["item_id"], -l["qty"], avg_cost(db, l["item_id"]), "INV", invoice_id, inv["inv_no"], date_str, "Dispatched")
    db.execute("UPDATE invoices SET dispatched_on=? WHERE id=?", (date_str, invoice_id))
    db.execute("UPDATE orders SET status='DISPATCHED' WHERE id=?", (inv["order_id"],))
    audit(db, user, "DISPATCH", "invoice", invoice_id, inv["inv_no"])


def cancel_invoice(db, user, invoice_id, reason, date_str):
    """Posted invoices are never deleted: cancel with a credit note; stock returns if it was dispatched."""
    check_lock(db, date_str)
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv["status"] != "POSTED":
        raise BizError("Invoice is already cancelled")
    if not reason.strip():
        raise BizError("Give a reason for the credit note")
    if db.execute("SELECT 1 FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone():
        raise BizError("This invoice has payments. Reverse them before issuing a credit note.")
    fy = fy_label(date_str)
    n = db.execute("SELECT COUNT(*) c FROM invoices WHERE cn_no LIKE ?", (f"CN/{fy}/%",)).fetchone()["c"] + 1
    cn = f"CN/{fy}/{n:04d}"
    if inv["dispatched_on"]:
        for l in db.execute("SELECT * FROM invoice_lines WHERE invoice_id=?", (invoice_id,)):
            post(db, l["item_id"], l["qty"], avg_cost(db, l["item_id"]), "CN", invoice_id, cn, date_str, "Returned via credit note")
    db.execute("UPDATE invoices SET status='CANCELLED', cn_no=?, cn_date=?, cn_reason=? WHERE id=?", (cn, date_str, reason, invoice_id))
    db.execute("UPDATE orders SET status='APPROVED' WHERE id=?", (inv["order_id"],))
    audit(db, user, "CREDIT_NOTE", "invoice", invoice_id, f"{inv['inv_no']} -> {cn}: {reason}")


def invoice_due(db, invoice_id):
    inv = db.execute("SELECT total FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    paid = sum((r["amount"] for r in db.execute("SELECT amount FROM payments WHERE invoice_id=?", (invoice_id,))), Decimal(0))
    return inv["total"] - paid, paid


def record_payment(db, user, invoice_id, amount, date_str, mode, ref):
    check_lock(db, date_str)
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    amount = q2(amount)
    due, _ = invoice_due(db, invoice_id)
    if inv["status"] != "POSTED":
        raise BizError("Cannot take payment on a cancelled invoice")
    if amount <= 0 or amount > due:
        raise BizError(f"Amount must be between ₹0.01 and the amount due {inr(due)}")
    cur = db.execute("INSERT INTO payments(invoice_id,customer_id,date,amount,mode,ref) VALUES (?,?,?,?,?,?)",
                     (invoice_id, inv["customer_id"], date_str, amount, mode, ref))
    audit(db, user, "PAYMENT", "payment", cur.lastrowid, f"{inv['inv_no']} {amount} {mode}")


# ---------------------------------------------------------------- WhatsApp text -> draft order
def parse_whatsapp(db, text):
    """Rule-based: finds the customer by name and 'product qty' pairs. Output is only a DRAFT to review."""
    import re
    low = text.lower()
    cust = None
    for c in sorted(db.execute("SELECT * FROM customers").fetchall(), key=lambda r: -len(r["name"])):
        if c["name"].lower() in low or any(len(w) > 3 and w in low for w in [c["name"].lower().split()[0]]):
            cust = c["id"]
            break
    products = db.execute("SELECT * FROM items WHERE kind='FG'").fetchall()
    found = {}
    for part in re.split(r"[,\n;]+|\band\b", text, flags=re.I):
        cleaned = re.sub(r"\d+\s*(?:g|gm|gms|kg|ml|l)\b", " ", part, flags=re.I)  # drop pack sizes like 200g
        nums = re.findall(r"\d+", cleaned)
        for p in products:
            key = p["name"].lower().split()[0]
            if key in part.lower() and nums:
                found[p["id"]] = int(nums[-1] if part.lower().find(key) < part.find(nums[-1]) else nums[0])
    return dict(customer_id=cust, lines=found)


# ---------------------------------------------------------------- dashboard
def variance_alerts(db, days=30):
    since = (_date.today() - timedelta(days=days)).isoformat()
    out = []
    for r in db.execute("SELECT id FROM runs WHERE status='DONE' AND date>=? ORDER BY id DESC", (since,)):
        s = run_summary(db, r["id"])
        msgs = [f"{l['name']} {'+' if l['var'] > 0 else ''}{l['var']}%" for l in s["lines"] if l["flag"]]
        if s["yield_flag"]:
            msgs.insert(0, f"Yield {s['yield_var']}%")
        if msgs:
            out.append(dict(id=r["id"], run_no=s["run"]["run_no"], msgs=msgs))
    return out


def dashboard(db):
    t = today()
    d = dict(orders_today=db.execute("SELECT COUNT(*) c FROM orders WHERE created_at LIKE ?", (t + "%",)).fetchone()["c"],
             pending=db.execute("SELECT COUNT(*) c FROM orders WHERE status='SUBMITTED'").fetchone()["c"])
    d["fg"] = [(i, balance(db, i["id"])) for i in db.execute("SELECT * FROM items WHERE kind='FG'")]
    short = []
    for i, bal in d["fg"]:
        need = sum((r["qty"] for r in db.execute(
            "SELECT l.qty FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE l.item_id=? AND o.status IN ('SUBMITTED','APPROVED')", (i["id"],))), Decimal(0))
        if need > bal:
            short.append((i["name"], need - bal))
    d["short"] = short
    d["alerts"] = variance_alerts(db)
    d["outstanding"] = sum((customer_outstanding(db, c["id"]) for c in db.execute("SELECT id FROM customers")), Decimal(0))
    d["costs"] = []
    for i, _ in d["fg"]:
        cpp = latest_cpp(db, i["id"])
        if cpp is not None:
            d["costs"].append((i["name"], cpp, i["price"], i["price"] - cpp))
    return d

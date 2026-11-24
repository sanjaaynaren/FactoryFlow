"""Flask web app. Thin layer: reads the form, calls engine.py, commits or rolls back, renders a template."""
import os
import tempfile
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote

from flask import Flask, g, request, session, redirect, url_for, render_template, flash, abort, Response, jsonify
from werkzeug.security import check_password_hash

import db as dbm
import engine as E
import exports
import reports as R
from engine import BizError, D, fmt, inr

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.jinja_env.filters.update(fmt=fmt, inr=inr, q2=E.q2)

NAV = {
    "salesman": [("Home", "home"), ("My orders", "orders_list"), ("New order", "order_new")],
    "admin": [("Home", "home"), ("Orders", "orders_list"), ("Invoices", "invoices"), ("Masters", "masters"), ("Stock", "stock"), ("Reports", "reports_index")],
    "production": [("Home", "home"), ("Production runs", "runs"), ("Purchases", "purchases"), ("Stock", "stock")],
    "accountant": [("Home", "home"), ("Reports", "reports_index"), ("Invoices", "invoices"), ("Stock", "stock")],
    "owner": [("Home", "home"), ("Reports", "reports_index"), ("Orders", "orders_list"), ("Stock", "stock")],
}
MORE = {  # kept out of the main menu on purpose
    "admin": [("Audit log", "audit_log"), ("Month lock", "lock"), ("Self-test", "selftest"), ("Customer profitability", "profit")],
    "production": [("Adjust stock", "adjust")],
    "accountant": [("Audit log", "audit_log"), ("Month lock", "lock")],
    "owner": [("Audit log", "audit_log"), ("Month lock", "lock"), ("Self-test", "selftest"), ("Customer profitability", "profit")],
    "salesman": [],
}


# ---------------------------------------------------------------- plumbing
@app.before_request
def open_db():
    g.db = dbm.connect()
    g.user = None
    if session.get("uid"):
        g.user = g.db.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()


@app.teardown_request
def close_db(_):
    if getattr(g, "db", None):
        g.db.close()


@app.context_processor
def inject():
    role = g.user["role"] if g.user else None
    return dict(user=g.user, nav=NAV.get(role, []), more=MORE.get(role, []), company=E.setting(g.db, "company", "Factory"))


def need(*roles):
    def deco(f):
        @wraps(f)
        def wrap(*a, **k):
            if not g.user:
                return redirect(url_for("login"))
            if roles and g.user["role"] not in roles:
                abort(403)
            return f(*a, **k)
        return wrap
    return deco


@app.errorhandler(403)
def forbidden(_):
    return render_template("message.html", title="Not allowed", text="Your role does not have access to this page."), 403


def num(key, default=None):
    v = request.form.get(key, "").strip()
    if v == "":
        if default is not None:
            return default
        raise BizError("Please fill in all required numbers")
    try:
        return Decimal(v)
    except InvalidOperation:
        raise BizError("Enter valid numbers only")


def act(fn, ok):
    """Run an engine action: commit + success message, or roll back + error message."""
    try:
        res = fn()
        g.db.commit()
        flash(ok, "ok")
        return True, res
    except BizError as e:
        g.db.rollback()
        flash(str(e), "err")
    except (InvalidOperation, ValueError, TypeError):
        g.db.rollback()
        flash("Please check the numbers you entered", "err")
    return False, None


def form_page(title, fields, action, submit, back=None, note=None):
    return render_template("form.html", title=title, fields=fields, action=action, submit=submit, back=back, note=note, today=E.today())


def opts(rows, label="name"):
    return [(r["id"], r[label]) for r in rows]


# ---------------------------------------------------------------- auth + home
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = g.db.execute("SELECT * FROM users WHERE username=?", (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["uid"] = u["id"]
            return redirect(url_for("home"))
        flash("Wrong username or password", "err")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@need()
def home():
    role, d = g.user["role"], E.dashboard(g.db)
    extra = {}
    if role == "salesman":
        extra["recent"] = g.db.execute("SELECT o.*, c.name cname FROM orders o JOIN customers c ON c.id=o.customer_id WHERE salesman_id=? ORDER BY o.id DESC LIMIT 6", (g.user["id"],)).fetchall()
        extra["sales"] = salesman_sales(g.user["id"])
    elif role == "admin":
        extra["todo"] = g.db.execute("SELECT o.*, c.name cname FROM orders o JOIN customers c ON c.id=o.customer_id WHERE status IN ('SUBMITTED','APPROVED','INVOICED') ORDER BY o.id DESC LIMIT 6").fetchall()
    elif role == "production":
        extra["open_runs"] = g.db.execute("SELECT r.*, i.name pname FROM runs r JOIN items i ON i.id=r.product_id ORDER BY r.id DESC LIMIT 5").fetchall()
    return render_template("dashboard.html", d=d, **extra)


def salesman_sales(uid):
    out = {}
    for l in g.db.execute("SELECT c.name, l.qty, l.rate FROM order_lines l JOIN orders o ON o.id=l.order_id JOIN customers c ON c.id=o.customer_id "
                          "WHERE o.salesman_id=? AND o.status!='REJECTED'", (uid,)):
        a = out.setdefault(l["name"], [Decimal(0), Decimal(0)])
        a[0] += l["qty"]; a[1] += l["qty"] * l["rate"]
    return out


# ---------------------------------------------------------------- orders
@app.route("/orders")
@need("salesman", "admin", "owner")
def orders_list():
    q = "SELECT o.*, c.name cname, u.name uname FROM orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_id"
    args = ()
    if g.user["role"] == "salesman":
        q += " WHERE o.salesman_id=?"; args = (g.user["id"],)
    rows, links = [], []
    for o in g.db.execute(q + " ORDER BY o.id DESC", args):
        pk = sum((l["qty"] for l in E.order_lines(g.db, o["id"])), Decimal(0))
        rows.append([o["order_no"], o["created_at"][:16].replace("T", " "), o["cname"], o["uname"], pk, o["status"]])
        links.append(url_for("order_view", oid=o["id"]))
    acts = [("+ New order", url_for("order_new"))] if g.user["role"] in ("salesman", "admin") else []
    return render_template("report.html", rep=dict(title="Orders", headers=["Order", "Placed", "Customer", "Salesman", "Packets", "Status"], rows=rows, links=links, actions=acts), no_filter=True)


@app.route("/orders/new", methods=["GET", "POST"])
@need("salesman", "admin")
def order_new():
    products = g.db.execute("SELECT * FROM items WHERE kind='FG' ORDER BY name").fetchall()
    customers = g.db.execute("SELECT * FROM customers ORDER BY name").fetchall()
    if request.method == "POST":
        def go():
            lines = [(p["id"], num(f"qty_{p['id']}", Decimal(0))) for p in products]
            return E.create_order(g.db, g.user["id"], int(request.form["customer_id"]), lines, request.form.get("remarks", ""))
        ok, oid = act(go, "Order submitted to the factory")
        if ok:
            return redirect(url_for("order_view", oid=oid))
    return render_template("order_new.html", products=products, customers=customers)


@app.route("/orders/parse", methods=["POST"])
@need("salesman", "admin")
def order_parse():
    return jsonify(E.parse_whatsapp(g.db, request.form.get("text", "")))


@app.route("/orders/<int:oid>")
@need("salesman", "admin", "owner")
def order_view(oid):
    o = g.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() or abort(404)
    if g.user["role"] == "salesman" and o["salesman_id"] != g.user["id"]:
        abort(403)
    return render_template("order_view.html", ov=E.order_overview(g.db, oid), o=o)


@app.route("/orders/<int:oid>/<action>", methods=["POST"])
@need("admin")
def order_action(oid, action):
    if action == "edit":
        def go():
            lines = [(l["item_id"], num(f"qty_{l['item_id']}"), num(f"rate_{l['item_id']}")) for l in E.order_lines(g.db, oid)]
            E.update_order(g.db, g.user["username"], oid, lines)
        act(go, "Order updated")
    elif action == "approve":
        act(lambda: E.set_order_status(g.db, g.user["username"], oid, "APPROVED", ("SUBMITTED",)), "Order approved")
    elif action == "reject":
        act(lambda: E.set_order_status(g.db, g.user["username"], oid, "REJECTED", ("SUBMITTED", "APPROVED")), "Order rejected")
    elif action == "invoice":
        ok, iid = act(lambda: E.create_invoice(g.db, g.user["username"], oid, E.today()), "GST invoice raised")
        if ok:
            return redirect(url_for("invoice_view", iid=iid))
    return redirect(url_for("order_view", oid=oid))


# ---------------------------------------------------------------- invoices
@app.route("/invoices")
@need("admin", "accountant", "owner")
def invoices():
    s, e = request.args.get("start") or R.month_range()[0], request.args.get("end") or R.month_range()[1]
    return render_template("report.html", rep=R.build(g.db, "sales_register", s, e), start=s, end=e, name="sales_register", base="/invoices")


@app.route("/invoices/<int:iid>")
@need("admin", "accountant", "owner")
def invoice_view(iid):
    inv = g.db.execute("SELECT i.*, c.name cname, c.gstin cgstin, c.state cstate, c.phone, c.id cid FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (iid,)).fetchone() or abort(404)
    lines = g.db.execute("SELECT l.*, i.name, i.carton_size FROM invoice_lines l JOIN items i ON i.id=l.item_id WHERE invoice_id=?", (iid,)).fetchall()
    due, paid = E.invoice_due(g.db, iid)
    pays = g.db.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (iid,)).fetchall()
    wa = "https://wa.me/?text=" + quote(f"Invoice {inv['inv_no']} for {inr(inv['total'])} from {E.setting(g.db, 'company')}: {request.url_root}invoices/{iid}/pdf")
    return render_template("invoice_view.html", inv=inv, lines=lines, due=due, paid=paid, pays=pays, wa=wa)


@app.route("/invoices/<int:iid>/pdf")
@need("admin", "accountant", "owner")
def invoice_pdf(iid):
    inv = g.db.execute("SELECT inv_no FROM invoices WHERE id=?", (iid,)).fetchone() or abort(404)
    return Response(exports.invoice_pdf(g.db, iid), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={inv['inv_no'].replace('/', '-')}.pdf"})


@app.route("/invoices/<int:iid>/packing")
@need("admin", "owner")
def packing(iid):
    inv = g.db.execute("SELECT i.*, c.name cname FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (iid,)).fetchone() or abort(404)
    rows = []
    for l in g.db.execute("SELECT l.*, i.name, i.carton_size FROM invoice_lines l JOIN items i ON i.id=l.item_id WHERE invoice_id=?", (iid,)):
        c, loose = E.carton_split(l["qty"], l["carton_size"])
        rows.append((l["name"], l["qty"], c, loose, l["carton_size"]))
    return render_template("packing.html", inv=inv, rows=rows)


@app.route("/invoices/<int:iid>/dispatch", methods=["POST"])
@need("admin")
def invoice_dispatch(iid):
    act(lambda: E.dispatch(g.db, g.user["username"], iid, E.today()), "Dispatch recorded. Finished-goods stock reduced")
    return redirect(url_for("invoice_view", iid=iid))


@app.route("/invoices/<int:iid>/cancel", methods=["POST"])
@need("admin")
def invoice_cancel(iid):
    act(lambda: E.cancel_invoice(g.db, g.user["username"], iid, request.form.get("reason", ""), E.today()), "Invoice cancelled with a credit note")
    return redirect(url_for("invoice_view", iid=iid))


@app.route("/invoices/<int:iid>/pay", methods=["GET", "POST"])
@need("admin")
def invoice_pay(iid):
    due, _ = E.invoice_due(g.db, iid)
    if request.method == "POST":
        ok, _r = act(lambda: E.record_payment(g.db, g.user["username"], iid, num("amount"), request.form["date"], request.form["mode"], request.form.get("ref", "")), "Payment recorded")
        if ok:
            return redirect(url_for("invoice_view", iid=iid))
    return form_page("Record payment", [
        dict(name="amount", label=f"Amount (due {inr(due)})", type="number", value=fmt(due), step="0.01"),
        dict(name="date", label="Date", type="date", value=E.today()),
        dict(name="mode", label="Mode", type="select", options=[(m, m) for m in ("UPI", "Cash", "Bank transfer", "Cheque")]),
        dict(name="ref", label="Reference (optional)", type="text")], request.path, "Save payment", back=url_for("invoice_view", iid=iid))


# ---------------------------------------------------------------- production + purchases + stock
@app.route("/purchases")
@need("production", "admin", "owner", "accountant")
def purchases():
    s, e = request.args.get("start") or R.month_range()[0], request.args.get("end") or R.month_range()[1]
    rep = R.build(g.db, "purchase_register", s, e)
    if g.user["role"] not in ("production", "admin"):
        rep["actions"] = []
    return render_template("report.html", rep=rep, start=s, end=e, name="purchase_register", base="/purchases")


@app.route("/purchases/new", methods=["GET", "POST"])
@need("production", "admin")
def purchase_new():
    if request.method == "POST":
        ok, _r = act(lambda: E.record_purchase(g.db, g.user["username"], int(request.form["supplier_id"]), int(request.form["item_id"]), num("qty"),
                                               request.form["unit"], num("rate"), request.form["date"], request.form.get("bill_no", "")), "Purchase recorded. Stock increased")
        if ok:
            return redirect(url_for("purchases"))
    items = g.db.execute("SELECT * FROM items WHERE kind IN ('RAW','PACK') ORDER BY name").fetchall()
    return form_page("Record purchase", [
        dict(name="supplier_id", label="Supplier", type="select", options=opts(g.db.execute("SELECT * FROM suppliers ORDER BY name"))),
        dict(name="item_id", label="Item", type="select", options=[(i["id"], f"{i['name']} ({i['unit']})") for i in items]),
        dict(name="qty", label="Quantity", type="number", step="any"),
        dict(name="unit", label="Unit", type="select", options=[(u, u) for u in ("kg", "g", "L", "ml", "pcs")]),
        dict(name="rate", label="Rate per unit above (₹)", type="number", step="any"),
        dict(name="date", label="Date", type="date", value=E.today()), dict(name="bill_no", label="Supplier bill no", type="text")],
        request.path, "Save purchase", back=url_for("purchases"), note="Quantity and unit are converted to the item's base unit automatically.")


@app.route("/runs")
@need("production", "admin", "owner", "accountant")
def runs():
    rows, links = [], []
    for r in g.db.execute("SELECT r.*, i.name pname FROM runs r JOIN items i ON i.id=r.product_id ORDER BY r.id DESC"):
        yv = E.pct(r["actual"], r["expected"]) if r["status"] == "DONE" else ""
        rows.append([r["run_no"], r["date"], r["pname"], r["batches"], r["expected"], r["actual"] if r["actual"] is not None else "", yv, r["status"]])
        links.append(url_for("run_view", rid=r["id"]))
    acts = [("+ New production run", url_for("run_new"))] if g.user["role"] == "production" else []
    return render_template("report.html", rep=dict(title="Production runs", headers=["Run", "Date", "Product", "Batches", "Expected", "Actual", "Yield variance %", "Status"], rows=rows, links=links, actions=acts), no_filter=True)


@app.route("/runs/new", methods=["GET", "POST"])
@need("production")
def run_new():
    products = g.db.execute("SELECT * FROM items WHERE kind='FG' AND recipe_id IS NOT NULL ORDER BY name").fetchall()
    pid = int(request.values.get("product") or products[0]["id"])
    batches = int(request.values.get("batches") or 1)
    product = E.get_item(g.db, pid)
    plan = [(i, n, u, q) for i, n, u, q, ph in E.planned_for(g.db, product, batches) if ph == "ISSUE"]
    if request.method == "POST" and request.form.get("go"):
        def go():
            issued = {i: (num(f"qty_{i}"), request.form[f"unit_{i}"]) for i, *_ in plan}
            return E.start_run(g.db, g.user["username"], pid, batches, issued, request.form["date"])
        ok, rid = act(go, "Production run started. Raw materials issued from stock")
        if ok:
            return redirect(url_for("run_view", rid=rid))
    stock = {i: E.balance(g.db, i) for i, *_ in plan}
    return render_template("run_new.html", products=products, product=product, batches=batches, plan=plan, stock=stock,
                           expected=E.expected_output(g.db, product, batches), today=E.today(), units=("kg", "g", "L", "ml", "pcs"))


@app.route("/runs/<int:rid>")
@need("production", "admin", "owner", "accountant")
def run_view(rid):
    s = E.run_summary(g.db, rid) if g.db.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone() else abort(404)
    packs = E.pack_lines(g.db, s["run"]["product_id"])
    return render_template("run_view.html", s=s, packs=packs, today=E.today())


@app.route("/runs/<int:rid>/complete", methods=["POST"])
@need("production")
def run_complete(rid):
    act(lambda: E.complete_run(g.db, g.user["username"], rid, num("good"), num("damaged", Decimal(0)), request.form.get("cartons", ""),
                               request.form.get("reason", ""), E.today()), "Run completed. Finished goods stock increased")
    return redirect(url_for("run_view", rid=rid))


@app.route("/runs/<int:rid>/overhead", methods=["POST"])
@need("production", "admin")
def run_overhead(rid):
    act(lambda: E.add_overhead(g.db, g.user["username"], rid, request.form["kind"], num("amount"), E.today()), "Overhead added")
    return redirect(url_for("run_view", rid=rid))


@app.route("/stock")
@need("production", "admin", "owner", "accountant")
def stock():
    s, e = request.args.get("start") or R.month_range()[0], request.args.get("end") or R.month_range()[1]
    return render_template("report.html", rep=R.build(g.db, "stock_summary", s, e), start=s, end=e, name="stock_summary", base="/stock",
                           hint="Click an item to see exactly how its balance is made up.")


@app.route("/stock/<int:item_id>")
@need("production", "admin", "owner", "accountant")
def stock_item(item_id):
    it = E.get_item(g.db, item_id)
    rep = R.build(g.db, "stock_ledger", "0000-00-00", "9999-12-31", item_id)
    ins = sum((r[4] for r in rep["rows"] if r[4] != ""), Decimal(0))
    outs = sum((r[5] for r in rep["rows"] if r[5] != ""), Decimal(0))
    names = {"OPENING": "opening", "PUR": "purchases", "RUN": "production", "INV": "dispatches", "CN": "credit-note returns", "ADJ": "adjustments"}
    groups = {}
    for r in rep["rows"]:
        groups[r[3]] = groups.get(r[3], Decimal(0)) + (r[4] if r[4] != "" else -r[5])
    return render_template("ledger.html", it=it, rep=rep, groups=[(names.get(k, k), v) for k, v in groups.items()], bal=E.balance(g.db, item_id), avg=E.avg_cost(g.db, item_id))


@app.route("/stock/adjust", methods=["GET", "POST"])
@need("production", "admin")
def adjust():
    if request.method == "POST":
        ok, _r = act(lambda: E.adjust_stock(g.db, g.user["username"], int(request.form["item_id"]), num("qty"), request.form["unit"], request.form["reason"], request.form["date"]), "Adjustment posted to the ledger")
        if ok:
            return redirect(url_for("stock"))
    items = g.db.execute("SELECT * FROM items ORDER BY kind,name").fetchall()
    return form_page("Adjust stock", [
        dict(name="item_id", label="Item", type="select", options=[(i["id"], f"{i['name']} ({i['unit']})") for i in items]),
        dict(name="qty", label="Quantity (use minus to reduce)", type="number", step="any"),
        dict(name="unit", label="Unit", type="select", options=[(u, u) for u in ("kg", "g", "L", "ml", "pcs", "packet", "carton")]),
        dict(name="reason", label="Reason", type="text"), dict(name="date", label="Date", type="date", value=E.today())],
        request.path, "Post adjustment", back=url_for("stock"), note="Adjustments are new ledger lines. Nothing is overwritten.")


# ---------------------------------------------------------------- masters
def master_fields(kind):
    q = lambda sql: g.db.execute(sql).fetchall()
    return {
        "customers": ("Customer", "customers", [dict(name="name", label="Name", type="text"), dict(name="kind", label="Type", type="select", options=[("Retailer", "Retailer"), ("Distributor", "Distributor")]),
                      dict(name="gstin", label="GSTIN", type="text"), dict(name="state", label="State", type="text"), dict(name="state_code", label="State code (e.g. 33)", type="text"),
                      dict(name="credit_limit", label="Credit limit (₹)", type="number", step="0.01"), dict(name="phone", label="Phone", type="text")], {}),
        "suppliers": ("Supplier", "suppliers", [dict(name="name", label="Name", type="text"), dict(name="gstin", label="GSTIN", type="text"), dict(name="phone", label="Phone", type="text")], {}),
        "items": ("Raw material / packaging", "items", [dict(name="name", label="Name", type="text"), dict(name="kind", label="Type", type="select", options=[("RAW", "Raw material"), ("PACK", "Packaging")]),
                  dict(name="unit", label="Base unit", type="select", options=[(u, u) for u in ("kg", "L", "pcs")]), dict(name="std_rate", label="Standard rate per unit (₹)", type="number", step="any")], {}),
        "products": ("Product / pack size", "items", [dict(name="name", label="Name (e.g. Murukku 500 g)", type="text"), dict(name="price", label="Selling price (₹)", type="number", step="0.01"),
                     dict(name="hsn", label="HSN code", type="text", value="1905"), dict(name="gst_rate", label="GST %", type="number", step="0.01", value="5"),
                     dict(name="carton_size", label="Packets per carton", type="number", value="20"), dict(name="recipe_id", label="BOM (recipe)", type="select", options=opts(q("SELECT * FROM recipes"))),
                     dict(name="pack_factor", label="Pack size vs base pack (500 g vs 200 g = 2.5)", type="number", step="any", value="1"), dict(name="std_rate", label="Standard material cost per packet (₹)", type="number", step="any", value="0")],
                    dict(kind="FG", unit="packet")),
        "recipes": ("Recipe", "recipes", [dict(name="name", label="Recipe name", type="text"), dict(name="batch_yield", label="Packs per batch (in base packs)", type="number", step="any")], {}),
        "recipe_lines": ("Recipe ingredient", "recipe_lines", [dict(name="recipe_id", label="Recipe", type="select", options=opts(q("SELECT * FROM recipes"))),
                         dict(name="item_id", label="Raw material", type="select", options=opts(q("SELECT * FROM items WHERE kind='RAW'"))), dict(name="qty", label="Quantity per batch (item's base unit)", type="number", step="any")], {}),
        "pack_lines": ("Packaging line", "pack_lines", [dict(name="product_id", label="Product", type="select", options=opts(q("SELECT * FROM items WHERE kind='FG'"))),
                       dict(name="item_id", label="Packaging item", type="select", options=opts(q("SELECT * FROM items WHERE kind='PACK'"))), dict(name="qty_per_pack", label="Quantity per packet", type="number", step="any")], {}),
    }[kind]


@app.route("/masters")
@need("admin", "owner")
def masters():
    q = lambda sql: g.db.execute(sql).fetchall()
    boms = []
    for p in q("SELECT * FROM items WHERE kind='FG'"):
        if p["recipe_id"]:
            boms.append(dict(p=p, rec=E.recipe_lines(g.db, p), pack=E.pack_lines(g.db, p["id"]), out=E.expected_output(g.db, p, 1)))
    mats = [(i, E.balance(g.db, i["id"])) for i in q("SELECT * FROM items WHERE kind IN ('RAW','PACK') ORDER BY kind,name")]
    return render_template("masters.html", boms=boms, mats=mats, customers=q("SELECT * FROM customers ORDER BY name"), suppliers=q("SELECT * FROM suppliers ORDER BY name"),
                           products=q("SELECT * FROM items WHERE kind='FG'"), can_edit=g.user["role"] == "admin")


@app.route("/masters/add/<kind>", methods=["GET", "POST"])
@need("admin")
def master_add(kind):
    title, table, fields, fixed = master_fields(kind)
    if request.method == "POST":
        def go():
            vals = dict(fixed)
            for f in fields:
                v = request.form.get(f["name"], "").strip()
                vals[f["name"]] = (D(v) if f["type"] == "number" and v != "" else (int(v) if f["type"] == "select" and v.isdigit() and f["name"].endswith("_id") else v))
            if not vals.get("name", "x"):
                raise BizError("Name is required")
            try:
                cur = g.db.execute(f"INSERT INTO {table}({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
            except Exception as ex:
                raise BizError(f"Could not save: {ex}")
            E.audit(g.db, g.user["username"], "MASTER_ADD", table, cur.lastrowid, str({k: str(v) for k, v in vals.items()}))
        ok, _r = act(go, f"{title} added")
        if ok:
            return redirect(url_for("masters"))
    return form_page(f"Add {title.lower()}", fields, request.path, "Save", back=url_for("masters"))


# ---------------------------------------------------------------- reports
@app.route("/reports")
@need("admin", "accountant", "owner")
def reports_index():
    return render_template("reports_index.html", reports=R.REPORTS)


@app.route("/reports/<name>")
@need("admin", "accountant", "owner")
def report(name):
    if name not in R.REPORTS:
        abort(404)
    s, e = request.args.get("start") or R.month_range()[0], request.args.get("end") or R.month_range()[1]
    rep = R.build(g.db, name, s, e)
    fmt_ = request.args.get("fmt")
    fname = f"{name}_{s}_to_{e}"
    if fmt_ == "csv":
        return Response(exports.to_csv(rep), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={fname}.csv"})
    if fmt_ == "xlsx":
        return Response(exports.to_xlsx(rep), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={fname}.xlsx"})
    if fmt_ == "pdf":
        return Response(exports.to_pdf(rep, f"{s} to {e}"), mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={fname}.pdf"})
    return render_template("report.html", rep=rep, start=s, end=e, name=name, base=f"/reports/{name}", export=True)


@app.route("/profitability")
@need("admin", "owner")
def profit():
    return redirect(url_for("report", name="profitability"))


# ---------------------------------------------------------------- audit, lock, self-test
@app.route("/audit")
@need("admin", "accountant", "owner")
def audit_log():
    ver = E.verify_audit(g.db) if request.args.get("verify") else None
    rows = g.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("audit.html", rows=rows, ver=ver)


@app.route("/lock", methods=["GET", "POST"])
@need("admin", "accountant", "owner")
def lock():
    if request.method == "POST":
        m, what = request.form.get("month", "")[:7], request.form.get("what")
        def go():
            if what == "unlock":
                if g.user["role"] != "owner":
                    raise BizError("Only the owner can unlock a month")
                g.db.execute("DELETE FROM locks WHERE month=?", (m,))
            else:
                if len(m) != 7:
                    raise BizError("Pick a month")
                g.db.execute("INSERT OR IGNORE INTO locks VALUES (?,?,?)", (m, g.user["username"], E.now()))
            E.audit(g.db, g.user["username"], what.upper(), "month", m, "")
        act(go, f"Month {m} {'unlocked' if what == 'unlock' else 'locked'}")
        return redirect(url_for("lock"))
    return render_template("lock.html", locks=g.db.execute("SELECT * FROM locks ORDER BY month DESC").fetchall(), this_month=E.today()[:7])


@app.route("/selftest")
@need("admin", "owner")
def selftest():
    import benchmark
    out = benchmark.run_benchmark(os.path.join(tempfile.mkdtemp(), "selftest.db"))
    return render_template("selftest.html", out=out, integrity=E.check_integrity(g.db))


if __name__ == "__main__":
    if not os.path.exists(dbm.DB_PATH):
        import seed
        seed.reset()
        print("Created a fresh database with the benchmark master data.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

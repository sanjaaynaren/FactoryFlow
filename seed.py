"""Loads the section-5 master data. Run `python seed.py` to reset the database to a clean benchmark start."""
import os
from decimal import Decimal as D
from werkzeug.security import generate_password_hash
import db as dbm
from engine import today, audit, post

USERS = [("salesA", "Salesman A", "salesman"), ("salesB", "Salesman B", "salesman"), ("admin", "Factory Admin", "admin"),
         ("prod", "Production Supervisor", "production"), ("accountant", "Accountant", "accountant"), ("owner", "Owner", "owner")]
PASSWORD = "demo123"


def seed(db):
    for k, v in [("company", "Sri Murukku Foods (demo)"), ("gstin", "33ZZZZZ9999Z1Z5"), ("state", "Tamil Nadu"),
                 ("state_code", "33"), ("address", "Coimbatore, Tamil Nadu"), ("tolerance", "3")]:
        db.execute("INSERT INTO settings VALUES (?,?)", (k, v))
    for u, n, r in USERS:
        db.execute("INSERT INTO users(username,name,role,password_hash) VALUES (?,?,?,?)", (u, n, r, generate_password_hash(PASSWORD)))

    raw = [("Flour", "RAW", "kg", "42", 100), ("Cooking oil", "RAW", "L", "140", 30), ("Salt and spice mix", "RAW", "kg", "200", 5),
           ("200 g pouch", "PACK", "pcs", "2.50", 500), ("Carton (20 packets)", "PACK", "pcs", "15", 30)]
    ids = {}
    d = today()
    for name, kind, unit, rate, opening in raw:
        cur = db.execute("INSERT INTO items(name,kind,unit,std_rate) VALUES (?,?,?,?)", (name, kind, unit, D(rate)))
        ids[name] = cur.lastrowid
        post(db, cur.lastrowid, D(opening), D(rate), "OPENING", 0, "OPENING", d, "Opening stock")

    cur = db.execute("INSERT INTO recipes(name,batch_yield) VALUES ('Murukku base recipe', ?)", (D(200),))
    rid = cur.lastrowid
    for name, qty in [("Flour", "40"), ("Cooking oil", "10"), ("Salt and spice mix", "1.2")]:
        db.execute("INSERT INTO recipe_lines(recipe_id,item_id,qty) VALUES (?,?,?)", (rid, ids[name], D(qty)))
    cur = db.execute("INSERT INTO items(name,kind,unit,price,hsn,gst_rate,carton_size,recipe_id,pack_factor,std_rate) "
                     "VALUES ('Murukku 200 g','FG','packet',?,'1905',?,20,?,?,?)", (D(50), D(5), rid, D(1), D("19.85")))
    fg = cur.lastrowid
    ids["Murukku 200 g"] = fg
    for name, per in [("200 g pouch", "1"), ("Carton (20 packets)", "0.05")]:
        db.execute("INSERT INTO pack_lines(product_id,item_id,qty_per_pack) VALUES (?,?,?)", (fg, ids[name], D(per)))
    # Opening FG valued at standard material cost per packet (3970 / 200 = 19.85): an assumption stated in README
    post(db, fg, D(50), D("19.85"), "OPENING", 0, "OPENING", d, "Opening stock")

    db.execute("INSERT INTO customers(name,kind,gstin,state,state_code,credit_limit,phone) VALUES ('Retailer A','Retailer','33AAAAA1111A1Z5','Tamil Nadu','33',?,'9000000001')", (D(50000),))
    db.execute("INSERT INTO customers(name,kind,gstin,state,state_code,credit_limit,phone) VALUES ('Distributor B','Distributor','29BBBBB2222B1Z5','Karnataka','29',?,'9000000002')", (D(100000),))
    db.execute("INSERT INTO suppliers(name,gstin,phone) VALUES ('Sri Flour Mills','33CCCCC3333C1Z5','9000000003')")
    db.execute("INSERT INTO suppliers(name,gstin,phone) VALUES ('Oil & Spice Traders','33DDDDD4444D1Z5','9000000004')")
    audit(db, "system", "SEED", "system", 0, "Loaded section 5 master data")
    db.commit()
    return ids


def reset(path=None):
    path = path or dbm.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    db = dbm.connect(path)
    dbm.init_db(db)
    seed(db)
    return db


def init_if_empty(path=None):
    """Initialise schema and seed only if the database is brand-new.

    Safe to call on every startup; it is a no-op when data already exists.
    This is used by the Vercel cold-start path in app.py.
    """
    db = dbm.connect(path)
    try:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            seed(db)
    except Exception:
        # Schema missing → create it then seed
        dbm.init_db(db)
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    reset()
    print("Database reset with benchmark master data.")
    print("Logins (password demo123): " + ", ".join(u for u, _, _ in USERS))

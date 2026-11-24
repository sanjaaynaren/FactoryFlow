"""Database layer: plain sqlite3. Decimals are stored as TEXT so nothing is ever rounded by a float."""
import os
import sqlite3
from decimal import Decimal

# Store Decimal as exact text; read columns declared DECTEXT back as Decimal.
sqlite3.register_adapter(Decimal, str)
sqlite3.register_converter("DECTEXT", lambda b: Decimal(b.decode()))

DB_PATH = os.environ.get("FACTORY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "factory.db"))

SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, role TEXT, password_hash TEXT);

-- One table for raw materials (RAW), packaging (PACK) and finished products (FG)
CREATE TABLE items(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, unit TEXT,
  std_rate DECTEXT DEFAULT '0',            -- standard rate used for variance analysis
  price DECTEXT, hsn TEXT, gst_rate DECTEXT, -- FG only
  carton_size INTEGER,                      -- FG only: packets per carton
  recipe_id INTEGER, pack_factor DECTEXT DEFAULT '1'  -- FG only: which BOM, and pack size vs base pack
);
-- BOM, part 1: the shared recipe (ingredients per batch). batch_yield is in base packs.
CREATE TABLE recipes(id INTEGER PRIMARY KEY, name TEXT, batch_yield DECTEXT);
CREATE TABLE recipe_lines(id INTEGER PRIMARY KEY, recipe_id INTEGER, item_id INTEGER, qty DECTEXT);
-- BOM, part 2: packaging that belongs to one pack size (qty per packet)
CREATE TABLE pack_lines(id INTEGER PRIMARY KEY, product_id INTEGER, item_id INTEGER, qty_per_pack DECTEXT);

CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, gstin TEXT, state TEXT,
  state_code TEXT, credit_limit DECTEXT DEFAULT '0', phone TEXT);
CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT UNIQUE, gstin TEXT, phone TEXT);

-- THE LEDGER: every stock movement. Balance = SUM(qty). Never overwritten.
CREATE TABLE ledger(id INTEGER PRIMARY KEY, item_id INTEGER, date TEXT, qty DECTEXT, rate DECTEXT,
  doc_type TEXT, doc_id INTEGER, doc_ref TEXT, note TEXT, created_at TEXT);

CREATE TABLE purchases(id INTEGER PRIMARY KEY, supplier_id INTEGER, item_id INTEGER, date TEXT,
  qty DECTEXT, rate DECTEXT, amount DECTEXT, bill_no TEXT);
CREATE TABLE runs(id INTEGER PRIMARY KEY, run_no TEXT, product_id INTEGER, batches INTEGER, date TEXT,
  status TEXT, expected DECTEXT, actual DECTEXT, damaged DECTEXT, reason TEXT, user TEXT, completed_at TEXT);
CREATE TABLE run_lines(id INTEGER PRIMARY KEY, run_id INTEGER, item_id INTEGER, planned DECTEXT, actual DECTEXT,
  rate DECTEXT, std_rate DECTEXT, cost DECTEXT, phase TEXT);
CREATE TABLE run_overheads(id INTEGER PRIMARY KEY, run_id INTEGER, kind TEXT, amount DECTEXT, date TEXT);

CREATE TABLE orders(id INTEGER PRIMARY KEY, order_no TEXT, customer_id INTEGER, salesman_id INTEGER,
  status TEXT, remarks TEXT, created_at TEXT);
CREATE TABLE order_lines(id INTEGER PRIMARY KEY, order_id INTEGER, item_id INTEGER, qty DECTEXT, rate DECTEXT);
CREATE TABLE invoices(id INTEGER PRIMARY KEY, inv_no TEXT, order_id INTEGER, customer_id INTEGER, date TEXT,
  supply TEXT, taxable DECTEXT, cgst DECTEXT, sgst DECTEXT, igst DECTEXT, total DECTEXT, status TEXT,
  dispatched_on TEXT, cn_no TEXT, cn_date TEXT, cn_reason TEXT);
CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY, invoice_id INTEGER, item_id INTEGER, hsn TEXT, qty DECTEXT,
  rate DECTEXT, taxable DECTEXT, gst_rate DECTEXT, cgst DECTEXT, sgst DECTEXT, igst DECTEXT);
CREATE TABLE payments(id INTEGER PRIMARY KEY, invoice_id INTEGER, customer_id INTEGER, date TEXT,
  amount DECTEXT, mode TEXT, ref TEXT);

-- Tamper-evident audit trail: each row stores a hash of itself + the previous row
CREATE TABLE audit(id INTEGER PRIMARY KEY, ts TEXT, user TEXT, action TEXT, entity TEXT, entity_id TEXT,
  detail TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE locks(month TEXT PRIMARY KEY, locked_by TEXT, locked_at TEXT);
"""


def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(db):
    db.executescript(SCHEMA)
    db.commit()

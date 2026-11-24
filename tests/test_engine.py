"""Tests for stock, unit conversion, tax maths, ledger integrity, audit chain, month lock. Run: python -m unittest discover -s tests"""
import os, sys, tempfile, unittest
from decimal import Decimal as D
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import benchmark, seed, engine as E


class Bench(unittest.TestCase):
    def test_every_benchmark_number(self):
        out = benchmark.run_benchmark()
        for name, exp, got, ok in out["results"]:
            self.assertTrue(ok, f"{name}: expected {exp}, got {got}")


class Fresh(unittest.TestCase):
    def setUp(self):
        self.db = seed.reset(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.ids = {r["name"]: r["id"] for r in self.db.execute("SELECT id,name FROM items")}
        self.cust = {r["name"]: r for r in self.db.execute("SELECT * FROM customers")}

    def test_unit_conversion(self):
        self.assertEqual(E.convert(500, "g", "kg"), D("0.5"))
        self.assertEqual(E.convert(D("1.5"), "L", "ml"), D(1500))
        self.assertEqual(E.convert(3, "carton", "packet", 20), D(60))
        with self.assertRaises(E.BizError):
            E.convert(1, "kg", "L")

    def test_weighted_average_cost(self):
        E.record_purchase(self.db, "t", 1, self.ids["Flour"], 60, "kg", 45, E.today())
        self.assertEqual(E.avg_cost(self.db, self.ids["Flour"]), D("43.125"))

    def test_purchase_in_grams(self):
        E.record_purchase(self.db, "t", 1, self.ids["Flour"], 5000, "g", D("0.045"), E.today())
        self.assertEqual(E.balance(self.db, self.ids["Flour"]), D(105))

    def test_cannot_go_negative(self):
        with self.assertRaises(E.BizError):
            E.post(self.db, self.ids["Flour"], -101, 42, "ADJ", 0, "x", E.today())

    def test_gst_intra_and_inter(self):
        fg = E.get_item(self.db, self.ids["Murukku 200 g"])
        a = E.compute_tax(self.db, self.cust["Retailer A"], [(fg, 150, 50)])
        self.assertEqual((a["taxable"], a["cgst"], a["sgst"], a["igst"], a["total"]), (D("7500"), D("187.50"), D("187.50"), D(0), D("7875")))
        b = E.compute_tax(self.db, self.cust["Distributor B"], [(fg, 120, 50)])
        self.assertEqual((b["cgst"], b["igst"], b["total"]), (D(0), D("300"), D("6300")))

    def test_money_never_drifts(self):
        fg = E.get_item(self.db, self.ids["Murukku 200 g"])
        t = E.compute_tax(self.db, self.cust["Retailer A"], [(fg, 3, D("33.33"))] * 3)
        self.assertEqual(t["total"], t["taxable"] + t["cgst"] + t["sgst"])
        self.assertEqual(t["total"], E.q2(t["total"]))

    def test_invoice_numbering_and_credit_note(self):
        d, fg = E.today(), self.ids["Murukku 200 g"]
        uid = self.db.execute("SELECT id FROM users WHERE username='salesA'").fetchone()["id"]
        o = E.create_order(self.db, uid, self.cust["Retailer A"]["id"], [(fg, 10)])
        E.set_order_status(self.db, "admin", o, "APPROVED", ("SUBMITTED",))
        i = E.create_invoice(self.db, "admin", o, d)
        self.assertEqual(self.db.execute("SELECT inv_no FROM invoices WHERE id=?", (i,)).fetchone()[0], f"INV/{E.fy_label(d)}/0001")
        E.dispatch(self.db, "admin", i, d)
        self.assertEqual(E.balance(self.db, fg), 40)
        E.cancel_invoice(self.db, "admin", i, "wrong qty", d)
        self.assertEqual(E.balance(self.db, fg), 50)                      # stock returned
        self.assertEqual(self.db.execute("SELECT status FROM invoices WHERE id=?", (i,)).fetchone()[0], "CANCELLED")
        self.assertEqual(E.customer_outstanding(self.db, self.cust["Retailer A"]["id"]), 0)

    def test_overpayment_blocked(self):
        d, fg = E.today(), self.ids["Murukku 200 g"]
        o = E.create_order(self.db, 1, self.cust["Retailer A"]["id"], [(fg, 10)])
        E.set_order_status(self.db, "admin", o, "APPROVED", ("SUBMITTED",))
        i = E.create_invoice(self.db, "admin", o, d)
        with self.assertRaises(E.BizError):
            E.record_payment(self.db, "admin", i, 99999, d, "Cash", "")

    def test_run_blocked_without_stock(self):
        with self.assertRaises(E.BizError):
            E.start_run(self.db, "p", self.ids["Murukku 200 g"], 3, {}, E.today())   # needs 120 kg flour, have 100

    def test_pack_size_shares_bom(self):
        """Interview change: a 500 g pack sharing the same recipe needs only data, no code."""
        r = self.db.execute("SELECT recipe_id FROM items WHERE name='Murukku 200 g'").fetchone()[0]
        self.db.execute("INSERT INTO items(name,kind,unit,price,hsn,gst_rate,carton_size,recipe_id,pack_factor) VALUES ('Murukku 500 g','FG','packet',?,'1905',?,10,?,?)", (D(120), D(5), r, D("2.5")))
        p = E.get_item(self.db, self.db.execute("SELECT id FROM items WHERE name='Murukku 500 g'").fetchone()[0])
        self.assertEqual(E.expected_output(self.db, p, 1), D(80))

    def test_ledger_integrity_and_lock(self):
        self.assertTrue(all(ok for *_x, ok in E.check_integrity(self.db)))
        self.db.execute("INSERT INTO locks VALUES (?,?,?)", (E.today()[:7], "owner", E.now()))
        with self.assertRaises(E.BizError):
            E.record_purchase(self.db, "t", 1, self.ids["Flour"], 1, "kg", 40, E.today())

    def test_audit_chain_detects_tampering(self):
        E.audit(self.db, "a", "X", "e", 1, "one"); E.audit(self.db, "a", "Y", "e", 2, "two")
        self.assertTrue(E.verify_audit(self.db)[0])
        self.db.execute("UPDATE audit SET detail='edited' WHERE action='X'")
        self.assertFalse(E.verify_audit(self.db)[0])

    def test_whatsapp_parser(self):
        r = E.parse_whatsapp(self.db, "Retailer A murukku 200g 150")
        self.assertEqual(r["customer_id"], self.cust["Retailer A"]["id"])
        self.assertEqual(r["lines"], {self.ids["Murukku 200 g"]: 150})

    def test_carton_split(self):
        self.assertEqual(E.carton_split(150, 20), (7, 10))


if __name__ == "__main__":
    unittest.main()

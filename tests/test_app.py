"""Walks the real web app for every role, so broken templates or permissions show up."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = os.path.join(tempfile.mkdtemp(), "app.db")
os.environ["FACTORY_DB"] = TMP
import benchmark
import app as webapp


def client(user):
    c = webapp.app.test_client()
    r = c.post("/login", data={"username": user, "password": "demo123"})
    assert r.status_code == 302, user
    return c


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        benchmark.run_benchmark(TMP)

    def ok(self, c, path, code=200):
        r = c.get(path)
        self.assertEqual(r.status_code, code, f"{path} -> {r.status_code}")
        return r

    def test_login_page_and_redirect(self):
        c = webapp.app.test_client()
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(c.get("/login").status_code, 200)
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "bad"}).status_code, 200)

    def test_every_role_home(self):
        for u in ("salesA", "admin", "prod", "accountant", "owner"):
            self.ok(client(u), "/")

    def test_admin_pages(self):
        c = client("admin")
        for p in ["/orders", "/orders/1", "/orders/2", "/orders/new", "/invoices", "/invoices/1", "/invoices/1/packing", "/invoices/1/pay", "/masters",
                  "/masters/add/customers", "/masters/add/products", "/masters/add/recipe_lines", "/stock", "/stock/1", "/stock/adjust", "/runs", "/runs/1",
                  "/purchases", "/purchases/new", "/reports", "/audit?verify=1", "/lock", "/selftest", "/profitability"]:
            self.ok(c, p) if p != "/profitability" else self.assertEqual(c.get(p).status_code, 302)
        r = self.ok(c, "/orders/2")
        self.assertIn(b"short by 32", r.data)
        self.assertIn(b"IGST", r.data)
        self.assertIn(b"Chain intact", self.ok(c, "/audit?verify=1").data)
        self.assertIn(b"All checks passed", self.ok(c, "/selftest").data)

    def test_invoice_pdf_and_exports(self):
        c = client("accountant")
        r = self.ok(c, "/invoices/1/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))
        from reports import REPORTS
        for name in REPORTS:
            self.ok(c, f"/reports/{name}")
            for f, magic in (("csv", b""), ("xlsx", b"PK"), ("pdf", b"%PDF")):
                r = self.ok(c, f"/reports/{name}?fmt={f}")
                self.assertTrue(r.data.startswith(magic), f"{name}.{f}")
        self.assertIn(b"7875", self.ok(c, "/reports/sales_register?fmt=csv").data)

    def test_production_pages(self):
        c = client("prod")
        for p in ["/runs", "/runs/new", "/runs/1", "/purchases", "/purchases/new", "/stock", "/stock/adjust"]:
            self.ok(c, p)
        self.assertIn(b"31.14", self.ok(c, "/runs/1").data)
        self.ok(c, "/masters", 403)
        self.ok(c, "/invoices", 403)

    def test_salesman_isolated(self):
        a, b = client("salesA"), client("salesB")
        self.ok(a, "/orders/1")
        self.ok(a, "/orders/2", 403)            # B's order
        self.ok(b, "/orders/2")
        self.ok(a, "/reports", 403)
        self.ok(a, "/stock", 403)
        self.assertEqual(a.post("/orders/1/approve").status_code, 403)

    def test_web_order_flow_and_parse(self):
        a = client("salesA")
        r = a.post("/orders/parse", data={"text": "Retailer A murukku 5"})
        
        self.assertTrue(r.get_json()["lines"])
        r = a.post("/orders/new", data={"customer_id": 1, "qty_6": 5}, follow_redirects=True)
        self.assertIn(b"Order submitted", r.data)
        r = a.post("/orders/new", data={"customer_id": 1, "qty_6": 0}, follow_redirects=True)
        self.assertIn(b"at least one product", r.data)

    def test_locked_month_blocks_web_purchase(self):
        o, p = client("owner"), client("prod")
        from engine import today
        o.post("/lock", data={"month": today()[:7], "what": "lock"})
        r = p.post("/purchases/new", data=dict(supplier_id=1, item_id=1, qty=1, unit="kg", rate=40, date=today()), follow_redirects=True)
        self.assertIn(b"locked", r.data)
        o.post("/lock", data={"month": today()[:7], "what": "unlock"})


if __name__ == "__main__":
    unittest.main()

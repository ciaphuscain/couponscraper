"""API + dashboard tests through App.handle, plus one real-socket smoke test."""

import json
import threading
import unittest
import urllib.request
from urllib.parse import urlencode

from couponhunter import ingest, repo
from couponhunter.db import open_db
from couponhunter.web.app import App, Request
from couponhunter.web.server import make_handler, serve
from tests.test_alerts import NOW

TOKEN = "t" * 43
PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.app = App(self.db, TOKEN, PORT, clock=lambda: NOW)
        self.cookie = f"ch_session={self.app.session}"

    def api(self, method, path, body=None, token=TOKEN, headers=None):
        h = {**HOST, "x-couponhunter-token": token, **(headers or {})}
        r = self.app.handle(Request(method, path, {}, h, json.dumps(body).encode() if body is not None else b""))
        return r.status, (json.loads(r.body) if r.body else None), r

    def page(self, path, cookie=True):
        h = {**HOST, **({"cookie": self.cookie} if cookie else {})}
        return self.app.handle(Request("GET", path, {}, h))

    def post_form(self, path, data, csrf=True, headers=None):
        d = dict(data)
        if csrf:
            d["csrf"] = self.app.csrf
        h = {**HOST, "cookie": self.cookie, **(headers or {})}
        return self.app.handle(Request("POST", path, {}, h, urlencode(d).encode()))

    # -- security -----------------------------------------------------------------

    def test_api_requires_token(self):
        self.assertEqual(self.api("GET", "/api/v1/status", token="wrong")[0], 401)
        self.assertEqual(self.api("GET", "/api/v1/status", token="")[0], 401)
        self.assertEqual(self.api("GET", "/api/v1/status")[0], 200)

    def test_dns_rebinding_host_check(self):
        r = self.app.handle(Request("GET", "/api/v1/status", {}, {"host": "evil.example:8765",
                                                                  "x-couponhunter-token": TOKEN}))
        self.assertEqual(r.status, 421)

    def test_dashboard_requires_session(self):
        r = self.page("/", cookie=False)
        self.assertEqual((r.status, r.headers["Location"]), (303, "/login"))
        self.assertEqual(self.page("/").status, 200)

    def test_login_sets_strict_httponly_cookie(self):
        r = self.app.handle(Request("POST", "/login", {}, HOST, urlencode({"token": TOKEN}).encode()))
        self.assertEqual(r.status, 303)
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
        self.assertNotIn(TOKEN, r.headers["Set-Cookie"])
        bad = self.app.handle(Request("POST", "/login", {}, HOST, urlencode({"token": "nope"}).encode()))
        self.assertEqual(bad.status, 401)

    def test_csrf_required(self):
        r = self.post_form("/actions/watch", {"url": "B0TESTASIN"}, csrf=False)
        self.assertEqual(r.status, 403)
        r = self.post_form("/actions/watch", {"url": "B0TESTASIN"}, headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.post_form("/actions/watch", {"url": "B0TESTASIN"}).status, 303)

    def test_security_headers(self):
        r = self.page("/")
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_body_limit(self):
        r = self.app.handle(Request("POST", "/api/v1/captures", {}, {**HOST, "x-couponhunter-token": TOKEN},
                                    b"x" * (65 * 1024)))
        self.assertEqual(r.status, 413)

    def test_stored_xss_is_escaped(self):
        evil = "<script>alert('x')</script>"
        self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": 10_000_00, "title": evil,
                                              "offers": [f"Save ₹500 with coupon {evil}"], "watch": True})
        for path in ("/", "/watchlist", "/offers", "/product/B0TESTASIN"):
            body = self.page(path).body.decode()
            self.assertNotIn("<script>alert", body, path)
            self.assertIn("&lt;script&gt;", body, path)

    def test_markup_not_double_escaped(self):
        repo.set_setting(self.db, "wallet", [{"id": "h", "label": "HDFC CC", "type": "credit_card", "bank": "hdfc"}])
        self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": 10_000_00, "watch": True, "offers": [
            "Save ₹500 with coupon", "10% Instant Discount up to INR 750 on HDFC Bank Credit Card"]})
        body = self.page("/product/B0TESTASIN").body.decode()
        self.assertIn("<ol><li>", body)
        self.assertNotIn("&lt;li&gt;", body)
        self.assertNotIn("&amp;#x27;", body)
        self.assertNotIn("&lt;input", body)

    def test_malformed_api_input(self):
        self.assertEqual(self.api("POST", "/api/v1/captures", None, headers={})[0], 400)  # empty -> missing fields
        r = self.app.handle(Request("POST", "/api/v1/captures", {}, {**HOST, "x-couponhunter-token": TOKEN}, b"{not json"))
        self.assertEqual(r.status, 400)
        self.assertEqual(self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": "₹100"})[0], 400)
        self.assertEqual(self.api("POST", "/api/v1/captures", {"asin": "../../etc", "price": 100})[0], 400)
        self.assertEqual(self.api("POST", "/api/v1/products", {"url": "https://evil.example/dp/B0TESTASIN"})[0], 400)
        self.assertEqual(self.api("GET", "/api/v2/status")[0], 404)

    # -- behaviour --------------------------------------------------------------

    def test_capture_flow_end_to_end(self):
        st, out, _ = self.api("POST", "/api/v1/captures", {
            "asin": "B0TESTASIN", "title": "Headphones", "price": 10_000_00, "list_price": 14_990_00,
            "offers": ["Save ₹500 with coupon",
                       "10% Instant Discount up to INR 750 on HDFC Bank Credit Card Transactions. Minimum purchase value INR 5000",
                       "Some unrelated marketing text"],
            "watch": True})
        self.assertEqual(st, 201)
        self.assertEqual(out["capture"]["offers_created"], 2)
        self.assertEqual(out["capture"]["offers_unparsed"], 1)
        # no wallet configured: bank offer can't be matched, coupon alone is the confirmed best
        self.assertEqual(out["best_confirmed"]["cash_cost"], 9_500_00)
        repo.set_setting(self.db, "wallet", [{"id": "h", "label": "HDFC CC", "type": "credit_card", "bank": "hdfc"}])
        st, ev, _ = self.api("GET", "/api/v1/products/B0TESTASIN")
        self.assertTrue(ev["tracked"])
        # stacking coupon+bank unknown -> confirmed best is bank alone (₹750), possible is both
        self.assertEqual(ev["best_confirmed"]["cash_cost"], 9_250_00)
        self.assertEqual(ev["best_possible"]["cash_cost"], 8_750_00)

        # Page re-captured without the coupon -> coupon withdrawn for this ASIN
        st, out, _ = self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": 10_000_00, "offers": []})
        self.assertEqual(out["best_confirmed"]["cash_cost"], 10_000_00)

    def test_unknown_product(self):
        st, out, _ = self.api("GET", "/api/v1/products/B0UNKNOWN1")
        self.assertEqual((st, out["tracked"]), (404, False))

    def test_redemption_feedback_changes_confidence(self):
        self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": 10_000_00, "offers": ["Save ₹500 with coupon"]})
        oid = self.db.one("SELECT id FROM offers")[0]
        before = self.api("GET", "/api/v1/products/B0TESTASIN")[1]["best_confirmed"]["success_probability_bp"]
        for k in ("a", "b", "c"):
            self.assertEqual(self.api("POST", "/api/v1/redemptions",
                                      {"offer_id": oid, "asin": "B0TESTASIN", "outcome": "failed", "idempotency_key": k})[0], 201)
        self.api("POST", "/api/v1/redemptions", {"offer_id": oid, "outcome": "failed", "idempotency_key": "c"})  # replay
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM redemption_attempts")[0], 3)
        ev = self.api("GET", "/api/v1/products/B0TESTASIN")[1]
        # three failures: offer flagged and no longer part of a 'rules known' recommendation
        self.assertIsNone(ev["best_confirmed"]["applied"] or None)
        self.assertLess(ev["scenarios"][0]["success_probability_bp"], before)

    def test_all_pages_render(self):
        ingest.register_source(self.db, "x", "file_import", "x", {}, NOW)
        self.api("POST", "/api/v1/captures", {"asin": "B0TESTASIN", "price": 10_000_00,
                                              "offers": ["Save ₹500 with coupon"], "watch": True})
        self.post_form("/actions/purchase", {"asin": "B0TESTASIN", "reference_price": "10000", "checkout_paid": "9500",
                                             "cashback": "100", "cashback_provider": "ExamplePortal"})
        for path in ("/", "/watchlist", "/offers", "/offers?expired=1", "/product/B0TESTASIN", "/history", "/health",
                     "/settings"):
            r = self.page(path.split("?")[0]) if "?" not in path else self.app.handle(
                Request("GET", "/offers", {"expired": ["1"]}, {**HOST, "cookie": self.cookie}))
            self.assertEqual(r.status, 200, path)
        self.assertIn("₹500", self.page("/history").body.decode())

    def test_settings_form_validation(self):
        r = self.post_form("/actions/settings", {"wallet": '[{"id":"x","type":"credit_card","label":"4111111111111111"}]'})
        self.assertEqual(r.status, 400)
        r = self.post_form("/actions/settings", {"wallet": "[]", "stacking_defaults": '{"bank_instant+product_coupon":"yes"}',
                                                 "is_prime": "yes", "min_savings": "150"})
        self.assertEqual(r.status, 303)
        s = repo.get_settings(self.db)
        self.assertEqual((s["profile"]["is_prime"], s["min_savings_paise"]), (True, 150_00))

    def test_manual_offer_text(self):
        self.post_form("/actions/watch", {"url": "B0TESTASIN", "price": "10000"})
        r = self.post_form("/actions/offer-text", {"text": "Save ₹300 with coupon", "asin": "B0TESTASIN",
                                                   "evidence": "official_page", "url": "https://phish.example/x"})
        self.assertEqual(r.status, 303)
        row = self.db.one("SELECT * FROM offers")
        self.assertEqual((row["value"], row["authenticity"], row["redemption_url"]), (300_00, "official", None))


class SocketSmokeTest(unittest.TestCase):
    def test_real_http(self):
        db = open_db(":memory:")
        self.addCleanup(db.close)
        # Bind first (port 0 = any free port), then build the App for that port.
        httpd = serve(App(db, TOKEN, 1), "127.0.0.1", 0)
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = make_handler(App(db, TOKEN, port))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/status", headers={"X-CouponHunter-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["api_version"], 1)


if __name__ == "__main__":
    unittest.main()

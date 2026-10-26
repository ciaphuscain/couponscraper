import io
import unittest
from datetime import datetime, timedelta, timezone

from couponhunter import alerts, ingest, repo
from couponhunter.db import open_db
from couponhunter.notify import NotifyError
from couponhunter.sources import FetchResult, SourceRecord
from couponhunter.domain import Authenticity

# 14:00 IST - outside default quiet hours (22:30-07:30 IST)
NOW = datetime(2026, 10, 25, 8, 30, tzinfo=timezone.utc)
ASIN = "B0TESTASIN"
COUPON = {"kind": "product_coupon", "title": "₹500 coupon", "discount": "₹500", "asins": [ASIN], "basis": "price",
          "valid_until": "2026-11-30"}


class Adapter:
    adapter, requires_terms_review = "static", False

    def __init__(self, recs):
        self.recs = recs

    def fetch(self, config):
        return FetchResult([SourceRecord(dict(r), "official_page", Authenticity.OFFICIAL) for r in self.recs],
                           snapshot=True, data_origin="synthetic")


class Recorder:
    name = "recorder"

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise self.fail
        self.sent.append(text)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        ingest.register_source(self.db, "s", "file_import", "s", {}, NOW, min_interval_s=0)
        ingest.run_source(self.db, "s", NOW, Adapter([COUPON]))
        self.pid = repo.upsert_product(self.db, ASIN, NOW, title="Test Headphones")
        repo.watch(self.db, self.pid, NOW)
        repo.record_price(self.db, self.pid, 10_000_00, NOW, "ext", "synthetic")

    def test_alert_sent_once_for_unchanged_state(self):
        n = Recorder()
        r1 = alerts.run_alerts(self.db, n, NOW)
        r2 = alerts.run_alerts(self.db, n, NOW + timedelta(hours=1))
        self.assertEqual((r1["sent"], r2["sent"], r2["duplicate"]), (1, 0, 1))
        text = n.sent[0]
        self.assertIn("₹500", text)
        self.assertIn("https://www.amazon.in/dp/B0TESTASIN", text)
        self.assertIn("SYNTHETIC", text)

    def test_below_threshold_no_alert(self):
        repo.set_setting(self.db, "min_savings_paise", 1_000_00)
        n = Recorder()
        self.assertEqual(alerts.run_alerts(self.db, n, NOW)["decided"], 0)

    def test_target_price_alert(self):
        repo.set_setting(self.db, "min_savings_paise", 5_000_00)
        repo.watch(self.db, self.pid, NOW, target_price=9_600_00)
        n = Recorder()
        alerts.run_alerts(self.db, n, NOW)
        self.assertIn("target", n.sent[0])

    def test_stale_price_never_alerts(self):
        n = Recorder()
        self.assertEqual(alerts.run_alerts(self.db, n, NOW + timedelta(hours=13))["decided"], 0)

    def test_cooldown_suppresses_marginal_change(self):
        n = Recorder()
        alerts.run_alerts(self.db, n, NOW)
        repo.record_price(self.db, self.pid, 9_990_00, NOW + timedelta(hours=1), "ext", "synthetic")
        r = alerts.run_alerts(self.db, n, NOW + timedelta(hours=1))
        self.assertEqual((r["suppressed"], len(n.sent)), (1, 1))

    def test_downgrade_not_alerted(self):
        n = Recorder()
        alerts.run_alerts(self.db, n, NOW)  # ₹500 coupon alert
        with self.db.tx() as c:
            c.execute("UPDATE offers SET valid_until=?", ("2026-10-25T20:00:00Z",))  # coupon expires
        ingest.run_source(self.db, "s", NOW + timedelta(hours=13), Adapter([
            {"kind": "product_coupon", "title": "₹300 coupon", "discount": "₹300", "asins": [ASIN], "basis": "price",
             "valid_until": "2026-11-30"}]))
        repo.record_price(self.db, self.pid, 10_000_00, NOW + timedelta(hours=13), "ext", "synthetic")
        r = alerts.run_alerts(self.db, n, NOW + timedelta(hours=13))  # past cooldown, but worse than last week's alert
        self.assertEqual((r["suppressed"], len(n.sent)), (1, 1))

    def test_quiet_hours_defer_then_deliver(self):
        night = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
        repo.record_price(self.db, self.pid, 10_000_00, night, "ext", "synthetic")
        n = Recorder()
        r = alerts.run_alerts(self.db, n, night)
        self.assertEqual((r["deferred"], len(n.sent)), (1, 0))
        morning = datetime(2026, 10, 26, 2, 5, tzinfo=timezone.utc)  # 07:35 IST
        alerts.deliver(self.db, n, morning)
        self.assertEqual(len(n.sent), 1)

    def test_delivery_failure_retries_then_fails(self):
        n = Recorder(fail=NotifyError("down"))
        alerts.run_alerts(self.db, n, NOW)
        row = self.db.one("SELECT * FROM alerts")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        t = NOW
        for _ in range(alerts.MAX_ATTEMPTS):
            t += timedelta(hours=2)
            alerts.deliver(self.db, n, t)
        self.assertEqual(self.db.one("SELECT status FROM alerts")[0], "failed")

    def test_permanent_failure(self):
        n = Recorder(fail=NotifyError("bad token", permanent=True))
        alerts.run_alerts(self.db, n, NOW)
        self.assertEqual(self.db.one("SELECT status FROM alerts")[0], "failed")

    def test_grouping_into_digest(self):
        for i in range(3):
            asin = f"B0TEST000{i}"
            pid = repo.upsert_product(self.db, asin, NOW, title=f"P{i}")
            repo.watch(self.db, pid, NOW, target_price=20_000_00)
            repo.record_price(self.db, pid, 10_000_00, NOW, "ext", "synthetic")
        n = Recorder()
        r = alerts.run_alerts(self.db, n, NOW)
        self.assertEqual(r["sent"], 4)
        self.assertEqual(len(n.sent), 1)
        self.assertTrue(n.sent[0].startswith("4 savings opportunities"))

    def test_overdue_cashback_reminder_once_per_fortnight(self):
        from datetime import timedelta as td
        n = Recorder()
        alerts.run_alerts(self.db, n, NOW)  # deal alert for the product (cooldown now active)
        purchase = repo.record_purchase(self.db, self.pid, NOW, 10_000_00, 9_500_00)
        rid = repo.create_cashback(self.db, "ExamplePortal", NOW, advertised=300_00, purchase_id=purchase)
        self.assertEqual(alerts.cashback_reminders(self.db, NOW + td(days=89)), [])
        t = NOW + td(days=91)
        repo.record_price(self.db, self.pid, 10_000_00, t, "ext", "synthetic")
        alerts.run_alerts(self.db, n, t)
        reminders = [m for m in n.sent if "Cashback overdue" in m]
        self.assertEqual(len(reminders), 1)
        self.assertIn("₹300 from ExamplePortal", reminders[0])
        self.assertIn("missing-cashback claim", reminders[0])
        alerts.run_alerts(self.db, n, t + td(days=1))  # same fortnight: no repeat
        self.assertEqual(len([m for m in n.sent if "Cashback overdue" in m]), 1)
        alerts.run_alerts(self.db, n, t + td(days=15))  # next fortnight: one more nudge
        self.assertEqual(len([m for m in n.sent if "Cashback overdue" in m]), 2)
        repo.transition_cashback(self.db, rid, "pending", 300_00, t)
        repo.transition_cashback(self.db, rid, "confirmed", 300_00, t)
        repo.transition_cashback(self.db, rid, "paid", 300_00, t)
        self.assertEqual(alerts.cashback_reminders(self.db, t + td(days=60)), [])  # paid: silence

    def test_alert_precision(self):
        from datetime import timedelta as td
        alerts.run_alerts(self.db, Recorder(), NOW)
        self.assertEqual((repo.alert_precision(self.db, NOW).sent, repo.alert_precision(self.db, NOW).acted), (1, 0))
        oid = self.db.one("SELECT id FROM offers")[0]
        repo.record_redemption(self.db, oid, None, "applied", NOW + td(days=2))  # e.g. Telegram /applied, no product
        self.assertEqual(repo.alert_precision(self.db, NOW + td(days=3)).acted, 1)
        self.assertEqual(repo.alert_precision(self.db, NOW + td(days=3)).precision_bp, 10_000)
        self.assertEqual(repo.alert_precision(self.db, NOW + td(days=200)).sent, 0)  # outside the window

    def test_quiet_until(self):
        s = {"quiet_hours": {"start": "22:30", "end": "07:30", "utc_offset_minutes": 330}}
        self.assertIsNone(alerts.quiet_until(NOW, s))
        q = alerts.quiet_until(datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc), s)  # 01:30 IST
        self.assertEqual(q, datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc))


class TelegramTests(unittest.TestCase):
    def test_token_never_in_error_or_repr(self):
        import urllib.error

        from couponhunter.notify import TelegramNotifier

        class Opener:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))

        token = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        t = TelegramNotifier(token, "42", opener=Opener())
        with self.assertRaises(NotifyError) as cm:
            t.send("hi")
        self.assertTrue(cm.exception.permanent)
        self.assertNotIn(token, str(cm.exception))
        self.assertNotIn(token, repr(t))
        self.assertIsNone(cm.exception.__cause__)


if __name__ == "__main__":
    unittest.main()

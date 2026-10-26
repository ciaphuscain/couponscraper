"""Promotional-email import. All messages are synthetic."""

import mailbox
import tempfile
import unittest
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from couponhunter import ingest
from couponhunter.db import open_db
from couponhunter.sources.email_import import EmailImportAdapter, candidate_lines, html_to_text
from tests.test_alerts import NOW


def make(subject, body, sent=NOW, html=False, sender="offers@bank.example"):
    m = EmailMessage()
    m["From"] = f"Bank Offers <{sender}>"
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = format_datetime(sent)
    if html:
        m.set_content("This email needs HTML")
        m.add_alternative(body, subtype="html")
    else:
        m.set_content(body)
    return m


class EmailImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def fetch(self, path, **cfg):
        return EmailImportAdapter(now=NOW).fetch({"path": str(path), **cfg})

    def test_plain_email_with_tc_expiry(self):
        (self.dir / "a.eml").write_bytes(bytes(make("Festive offers", (
            "Dear customer,\n"
            "Get 10% Instant Discount up to INR 1500 on HDFC Bank Credit Card. Minimum purchase value INR 5000\n"
            "Shop the biggest sale of the year!\n"
            "T&C: Offer valid till 15th Nov 2026.\n"))))
        r = self.fetch(self.dir / "a.eml")
        self.assertEqual(len(r.records), 1)
        rec = r.records[0]
        self.assertEqual(rec.data["valid_until"], "2026-11-15")  # taken from the T&C line
        self.assertIn("From: offers@bank.example", rec.raw_text)
        self.assertEqual(rec.authenticity.value, "unverified")

    def test_html_email_ignores_script_and_links(self):
        body = ("<html><head><style>.x{}</style><script>var x='Save ₹999 with coupon';</script></head><body>"
                "<p>Use code FEST250 to get ₹250 off on orders above ₹1999</p>"
                "<a href='https://phish.example/claim'>Claim 50% cashback now</a></body></html>")
        (self.dir / "b.eml").write_bytes(bytes(make("Codes", body, html=True)))
        r = self.fetch(self.dir / "b.eml")
        texts = [x.data["text"] for x in r.records]
        self.assertEqual(len(texts), 1, texts)
        self.assertIn("FEST250", texts[0])
        self.assertNotIn("redemption_url", r.records[0].data)
        self.assertNotIn("script", html_to_text(body))

    def test_old_email_skipped_unless_future_expiry(self):
        old = NOW - timedelta(days=90)
        mb = mailbox.mbox(str(self.dir / "takeout.mbox"))
        mb.add(make("Old", "Save ₹500 with coupon on headphones", sent=old))
        mb.add(make("Old but valid", "Flat ₹300 cashback on Amazon Pay UPI. Valid till 31/12/2026", sent=old))
        mb.add(make("New", "Apply 5% coupon on kitchen items"))
        mb.flush()
        mb.close()
        r = self.fetch(self.dir / "takeout.mbox")
        texts = sorted(x.data["text"] for x in r.records)
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(any("skipped" in w for w in r.warnings))

    def test_folder_and_ingest_end_to_end(self):
        (self.dir / "1.eml").write_bytes(bytes(make("x", "Save ₹500 with coupon")))
        (self.dir / "2.eml").write_bytes(bytes(make("y", "Save ₹500 with coupon")))  # same offer, two emails
        (self.dir / "notes.txt").write_text("ignored")
        db = open_db(":memory:")
        self.addCleanup(db.close)
        ingest.register_source(db, "mail", "email_import", "mail", {"path": str(self.dir)}, NOW, min_interval_s=0)
        st = ingest.run_source(db, "mail", NOW, EmailImportAdapter(now=NOW))
        self.assertEqual((st.created, st.duplicates, st.rejected), (1, 1, 0))
        self.assertEqual(db.one("SELECT authenticity FROM offers")[0], "unverified")

    def test_candidate_lines_filter(self):
        lines = candidate_lines("Hello!\nUnsubscribe here\nGet 5% back on all orders\nPrice ₹199 only\n")
        self.assertEqual(lines, ["Get 5% back on all orders"])

    def test_missing_path(self):
        from couponhunter.sources import SourceError
        with self.assertRaises(SourceError):
            self.fetch(self.dir / "nope.mbox")


if __name__ == "__main__":
    unittest.main()

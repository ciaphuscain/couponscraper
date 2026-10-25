import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from couponhunter import ingest, repo
from couponhunter.db import open_db
from couponhunter.domain import Lifecycle
from couponhunter.sources import FetchResult, SourceError, SourceRecord
from couponhunter.domain import Authenticity
from tests.factories import NOW

HDFC = {"kind": "bank_instant", "title": "10% off HDFC", "provider": "HDFC Bank", "discount": "10%",
        "max_discount": "₹750", "payment": {"banks": ["HDFC"], "types": ["credit_card"]},
        "valid_until": "2026-11-01T23:59:59+05:30"}
COUPON = {"kind": "product_coupon", "title": "₹500 coupon", "discount": "₹500", "asins": ["B0TESTASIN"],
          "basis": "price"}


class StaticAdapter:
    adapter = "static"
    requires_terms_review = False

    def __init__(self, records, snapshot=True, fail=None, origin="synthetic"):
        self.records, self.snapshot, self.fail, self.origin = records, snapshot, fail, origin

    def fetch(self, config):
        if self.fail:
            raise self.fail
        recs = [SourceRecord(data=dict(r), evidence_type="fixture", authenticity=Authenticity.SOURCE_LISTED)
                for r in self.records]
        return FetchResult(recs, snapshot=self.snapshot, data_origin=self.origin, schema_fingerprint="x")


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        for sid in ("a", "b"):
            ingest.register_source(self.db, sid, "file_import", sid, {}, NOW, min_interval_s=0)

    def offers(self):
        return self.db.query("SELECT * FROM offers")

    def test_idempotent_replay(self):
        ad = StaticAdapter([HDFC, COUPON])
        s1 = ingest.run_source(self.db, "a", NOW, ad)
        s2 = ingest.run_source(self.db, "a", NOW + timedelta(minutes=5), ad)
        self.assertEqual((s1.created, s2.created), (2, 0))
        self.assertEqual(s2.duplicates, 2)
        self.assertEqual(len(self.offers()), 2)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM offer_sources")[0], 2)

    def test_cross_source_dedup_keeps_provenance(self):
        reworded = {**HDFC, "title": "HDFC Bank: 10% instant discount", "provider": "HDFC"}
        ingest.run_source(self.db, "a", NOW, StaticAdapter([HDFC]))
        ingest.run_source(self.db, "b", NOW, StaticAdapter([reworded]))
        self.assertEqual(len(self.offers()), 1)
        self.assertEqual(len(repo.offer_provenance(self.db, self.offers()[0]["id"])), 2)

    def test_synthetic_never_merges_with_real(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([COUPON], origin="synthetic"))
        ingest.run_source(self.db, "b", NOW, StaticAdapter([COUPON], origin="live"))
        self.assertEqual(sorted(r["data_origin"] for r in self.offers()), ["live", "synthetic"])

    def test_duplicate_within_one_listing(self):
        s = ingest.run_source(self.db, "a", NOW, StaticAdapter([HDFC, HDFC]))
        self.assertEqual((s.created, s.duplicates), (1, 1))

    def test_expiry_disagreement_earliest_wins(self):
        later = {**HDFC, "valid_until": "2026-11-05T23:59:59+05:30"}
        ingest.run_source(self.db, "a", NOW, StaticAdapter([later]))
        ingest.run_source(self.db, "b", NOW, StaticAdapter([HDFC]))
        self.assertEqual(self.offers()[0]["valid_until"], "2026-11-01T18:29:59Z")

    def test_snapshot_withdrawal(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([HDFC, COUPON]))
        s = ingest.run_source(self.db, "a", NOW + timedelta(hours=1), StaticAdapter([COUPON]))
        self.assertEqual(s.missing_marked, 1)
        o = {r["kind"]: r for r in self.offers()}
        self.assertIsNotNone(o["bank_instant"]["withdrawn_at"])
        self.assertIsNone(o["product_coupon"]["withdrawn_at"])
        # still listed elsewhere -> not withdrawn
        ingest.run_source(self.db, "b", NOW + timedelta(hours=2), StaticAdapter([HDFC], snapshot=False))
        self.assertIsNone({r["kind"]: r for r in self.offers()}["bank_instant"]["withdrawn_at"])

    def test_high_reject_rate_does_not_withdraw(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([HDFC, COUPON]))
        broken = [{"kind": "nonsense"}, {"kind": "nonsense"}, {"kind": "nonsense"}, COUPON]
        s = ingest.run_source(self.db, "a", NOW + timedelta(hours=1), StaticAdapter(broken))
        self.assertEqual(s.status, "partial")
        self.assertEqual(s.missing_marked, 0)
        self.assertTrue(all(r["withdrawn_at"] is None for r in self.offers()))

    def test_failure_does_not_refresh_and_offers_go_stale(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([COUPON]))
        later = NOW + timedelta(hours=72)
        s = ingest.run_source(self.db, "a", later, StaticAdapter([], fail=SourceError("down")), force=True)
        self.assertEqual(s.status, "failed")
        offers = repo.offers_for_asin(self.db, "B0TESTASIN", later)
        self.assertEqual(offers[0].lifecycle, Lifecycle.STALE)

    def test_auth_error_disables_source(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([], fail=SourceError("403", kind="auth")))
        row = self.db.one("SELECT * FROM sources WHERE id='a'")
        self.assertEqual(row["enabled"], 0)
        self.assertIn("access refused", row["disabled_reason"])
        s = ingest.run_source(self.db, "a", NOW + timedelta(days=1), StaticAdapter([COUPON]))
        self.assertEqual(s.status, "skipped")

    def test_repeated_transient_failures_disable_with_backoff(self):
        for i in range(ingest.DISABLE_AFTER_FAILURES):
            ingest.run_source(self.db, "a", NOW + timedelta(days=i), StaticAdapter([], fail=SourceError("x")), force=True)
        row = self.db.one("SELECT * FROM sources WHERE id='a'")
        self.assertEqual(row["enabled"], 0)
        self.assertIsNotNone(row["next_fetch_at"])

    def test_rate_limit_backoff_respected(self):
        ingest.run_source(self.db, "a", NOW, StaticAdapter([], fail=SourceError("429", kind="rate_limited", retry_after_s=7200)))
        s = ingest.run_source(self.db, "a", NOW + timedelta(hours=1), StaticAdapter([COUPON]))
        self.assertEqual(s.status, "skipped")
        s = ingest.run_source(self.db, "a", NOW + timedelta(hours=3), StaticAdapter([COUPON]))
        self.assertEqual(s.status, "ok")

    def test_adapter_crash_contained(self):
        s = ingest.run_source(self.db, "a", NOW, StaticAdapter([], fail=RuntimeError("bug")))
        self.assertEqual(s.status, "failed")

    def test_terms_review_required_for_network_sources(self):
        ingest.register_source(self.db, "feed", "rss", "Feed", {"url": "https://example.com/feed"}, NOW)
        s = ingest.run_source(self.db, "feed", NOW)
        self.assertEqual(s.status, "skipped")
        self.assertIn("terms", s.warnings[0])

    def test_crash_mid_ingest_rolls_back(self):
        class Boom(Exception):
            pass

        ad = StaticAdapter([HDFC, COUPON])
        orig = ingest.upsert_offer
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Boom()
            return orig(*a, **k)

        ingest.upsert_offer = flaky
        try:
            with self.assertRaises(Boom):
                ingest.run_source(self.db, "a", NOW, ad)
        finally:
            ingest.upsert_offer = orig
        self.assertEqual(len(self.offers()), 0)  # first offer's insert rolled back too

    def test_less_authoritative_source_does_not_overwrite_title(self):
        official = StaticAdapter([HDFC])
        ingest.run_source(self.db, "a", NOW, official)

        class Weak(StaticAdapter):
            def fetch(self, config):
                r = super().fetch(config)
                for rec in r.records:
                    rec.authenticity = Authenticity.UNVERIFIED
                return r

        ingest.run_source(self.db, "b", NOW, Weak([{**HDFC, "title": "CLICK HERE!!! HDFC"}]))
        self.assertEqual(self.offers()[0]["title"], "10% off HDFC")
        self.assertEqual(self.offers()[0]["authenticity"], "source_listed")


class FileImportTests(unittest.TestCase):
    def test_json_and_csv_import(self):
        db = open_db(":memory:")
        self.addCleanup(db.close)
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "o.json"
            j.write_text(json.dumps({"synthetic": True, "offers": [HDFC, COUPON, "garbage"]}), encoding="utf-8")
            c = Path(d) / "o.csv"
            c.write_text("kind,title,discount,banks,types,asins\n"
                         "bank_instant,SBI 5%,5%,SBI,debit_card,\n"
                         "product_coupon,Coupon,₹200,,,B0TESTASIN|B0OTHERVAR\n", encoding="utf-8")
            ingest.register_source(db, "j", "file_import", "j", {"path": str(j)}, NOW, min_interval_s=0)
            ingest.register_source(db, "c", "file_import", "c", {"path": str(c)}, NOW, min_interval_s=0)
            sj = ingest.run_source(db, "j", NOW)
            sc = ingest.run_source(db, "c", NOW)
        self.assertEqual((sj.created, sj.rejected), (2, 1))
        self.assertEqual(sc.created, 2)
        self.assertEqual(db.one("SELECT COUNT(*) FROM offer_products")[0], 3)
        origins = {r[0] for r in db.query("SELECT data_origin FROM offers")}
        self.assertEqual(origins, {"synthetic", "imported"})

    def test_missing_file_disables(self):
        db = open_db(":memory:")
        self.addCleanup(db.close)
        ingest.register_source(db, "j", "file_import", "j", {"path": "/nonexistent.json"}, NOW, min_interval_s=0)
        s = ingest.run_source(db, "j", NOW)
        self.assertEqual(s.status, "failed")
        self.assertEqual(db.one("SELECT enabled FROM sources WHERE id='j'")[0], 0)


if __name__ == "__main__":
    unittest.main()

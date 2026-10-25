"""Database integrity, migrations, crash recovery and backup."""

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import timedelta
from pathlib import Path

from couponhunter import repo
from couponhunter.db import backup, integrity_check, migrate, open_db
from tests.factories import NOW

ROOT = Path(__file__).resolve().parents[1]


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)

    def test_migrations_idempotent(self):
        self.assertEqual(migrate(self.db), [])

    def test_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as c:
                c.execute("INSERT INTO products(asin, created_at) VALUES ('SHORT', 'x')")
        pid = repo.upsert_product(self.db, "B0TESTASIN", NOW)
        with self.assertRaises(ValueError):
            repo.record_price(self.db, pid, 0, NOW, "manual", "manual")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as c:
                c.execute("INSERT INTO price_observations(product_id, price, observed_at, source, data_origin) "
                          "VALUES (999, 100, 'x', 's', 'manual')")  # FK violation

    def test_price_observation_idempotent(self):
        pid = repo.upsert_product(self.db, "B0TESTASIN", NOW)
        self.assertTrue(repo.record_price(self.db, pid, 100_00, NOW, "ext", "live"))
        self.assertFalse(repo.record_price(self.db, pid, 100_00, NOW, "ext", "live"))

    def test_cashback_state_machine(self):
        rid = repo.create_cashback(self.db, "ExamplePortal", NOW, advertised=300_00)
        repo.transition_cashback(self.db, rid, "pending", 300_00, NOW)
        with self.assertRaises(repo.InvalidTransition):
            repo.transition_cashback(self.db, rid, "paid", 300_00, NOW)  # must be confirmed first
        repo.transition_cashback(self.db, rid, "confirmed", 280_00, NOW)
        with self.assertRaises(repo.InvalidTransition):
            repo.transition_cashback(self.db, rid, "paid", 280_00, NOW, expected_version_status="pending")
        repo.transition_cashback(self.db, rid, "paid", 280_00, NOW)
        s = repo.savings_summary(self.db)
        self.assertEqual(s.cashback_paid, 280_00)
        self.assertEqual(s.cashback_pending, 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM cashback_events WHERE record_id=?", (rid,))[0], 4)

    def test_pending_cashback_not_counted_as_realized(self):
        pid = repo.upsert_product(self.db, "B0TESTASIN", NOW)
        purchase = repo.record_purchase(self.db, pid, NOW, 10_000_00, 8_750_00)
        repo.create_cashback(self.db, "ExamplePortal", NOW, advertised=300_00, purchase_id=purchase)
        s = repo.savings_summary(self.db)
        self.assertEqual(s.realized_total, 1_250_00)
        self.assertEqual(s.cashback_pending, 300_00)

    def test_wallet_rejects_card_numbers(self):
        with self.assertRaises(ValueError):
            repo.set_setting(self.db, "wallet", [{"id": "x", "type": "credit_card", "label": "4111 1111 1111 1111"}])

    def test_redemption_idempotency(self):
        with self.db.tx() as c:
            c.execute("""INSERT INTO offers(fingerprint, kind, title, provider, discount_type, value, basis,
                         first_seen_at, data_origin) VALUES ('f','product_coupon','t','p','flat',1,'price','x','manual')""")
        a = repo.record_redemption(self.db, 1, None, "applied", NOW, idempotency_key="k1")
        b = repo.record_redemption(self.db, 1, None, "applied", NOW, idempotency_key="k1")
        self.assertEqual(a, b)


class CrashRecoveryTests(unittest.TestCase):
    def test_kill_during_write_leaves_consistent_db(self):
        """SIGKILL a process mid-way through a large transaction, then check
        the database opens, passes integrity_check and contains no partial batch."""
        if os.name == "nt":
            self.skipTest("uses POSIX signals")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            db = open_db(path)
            pid = repo.upsert_product(db, "B0TESTASIN", NOW)
            db.close()
            script = textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(ROOT)!r})
                from couponhunter.db import open_db
                db = open_db({str(path)!r})
                with db.tx() as c:
                    for i in range(1_000_000):
                        c.execute("INSERT INTO price_observations(product_id, price, observed_at, source, data_origin)"
                                  " VALUES (?, ?, ?, 'crash', 'synthetic')", ({pid}, 100 + i, f"t{{i:09d}}"))
                        if i == 20_000:
                            print("ready", flush=True)
            """)
            proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            time.sleep(0.05)
            proc.send_signal(signal.SIGKILL)
            proc.wait()
            proc.stdout.close()
            db = open_db(path)
            self.addCleanup(db.close)
            self.assertEqual(integrity_check(db), "ok")
            self.assertEqual(db.one("SELECT COUNT(*) FROM price_observations")[0], 0)
            self.assertEqual(db.one("SELECT COUNT(*) FROM products")[0], 1)


class EvaluationCacheTests(unittest.TestCase):
    def test_cache_invalidated_by_other_connection(self):
        from couponhunter import service

        with tempfile.TemporaryDirectory() as d:
            a = open_db(Path(d) / "c.db")
            b = open_db(Path(d) / "c.db")  # e.g. the CLI while the server runs
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            pid = repo.upsert_product(a, "B0TESTASIN", NOW)
            repo.record_price(a, pid, 100_00, NOW, "m", "manual")
            self.assertEqual(service.evaluate(a, "B0TESTASIN", NOW).result.product.price, 100_00)
            self.assertIs(service.evaluate(a, "B0TESTASIN", NOW), service.evaluate(a, "B0TESTASIN", NOW))
            repo.record_price(b, pid, 90_00, NOW + timedelta(minutes=1), "m", "manual")
            self.assertEqual(service.evaluate(a, "B0TESTASIN", NOW).result.product.price, 90_00)


class BackupTests(unittest.TestCase):
    def test_backup_restore_roundtrip_and_pruning(self):
        with tempfile.TemporaryDirectory() as d:
            db = open_db(Path(d) / "live.db")
            self.addCleanup(db.close)
            repo.upsert_product(db, "B0TESTASIN", NOW, title="Thing")
            dest = backup(db, Path(d) / "b", keep=2)
            restored = sqlite3.connect(dest)
            self.assertEqual(restored.execute("SELECT title FROM products").fetchone()[0], "Thing")
            restored.close()
            for i in range(3):
                (Path(d) / "b" / f"couponhunter-2000010{i}T000000Z.db").write_bytes(b"")
            backup(db, Path(d) / "b", keep=2)
            self.assertEqual(len(list((Path(d) / "b").glob("couponhunter-*.db"))), 2)


if __name__ == "__main__":
    unittest.main()

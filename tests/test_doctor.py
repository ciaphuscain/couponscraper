import socket
import tempfile
import unittest
from pathlib import Path

from couponhunter import ingest, repo
from couponhunter.config import Config
from couponhunter.db import open_db
from couponhunter.doctor import FAIL, PASS, WARN, run_checks
from tests.factories import NOW


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        with socket.socket() as s:  # a port nobody listens on
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.cfg = Config(Path(self.tmp.name), port=free)

    def by_name(self, checks):
        return {c.name: c for c in checks}

    def test_uninitialised(self):
        c = self.by_name(run_checks(self.cfg, online=False, now=NOW))
        self.assertEqual(c["Database"].status, FAIL)
        self.assertIn("couponhunter init", c["Database"].fix)

    def test_fresh_install_has_no_failures_and_actionable_warnings(self):
        db = open_db(self.cfg.db_path)
        ingest.ensure_builtin_sources(db, NOW)
        db.close()
        self.cfg.api_token()
        c = self.by_name(run_checks(self.cfg, online=False, now=NOW))
        self.assertFalse([x for x in c.values() if x.status == FAIL], c)
        self.assertEqual(c["Server"].status, WARN)
        self.assertEqual(c["Payment methods"].status, WARN)
        self.assertTrue(all(x.fix for x in c.values() if x.status == WARN))

    def test_port_taken_by_other_program(self):
        db = open_db(self.cfg.db_path)
        db.close()
        self.cfg.api_token()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            cfg = Config(self.cfg.home, port=s.getsockname()[1])
            c = self.by_name(run_checks(cfg, online=False, now=NOW))
        self.assertEqual(c["Server"].status, FAIL)
        self.assertIn("--port", c["Server"].fix)

    def test_stale_watchlist_warns(self):
        db = open_db(self.cfg.db_path)
        pid = repo.upsert_product(db, "B0TESTASIN", NOW)
        repo.watch(db, pid, NOW)
        repo.record_price(db, pid, 100_00, NOW, "m", "manual")
        db.close()
        self.cfg.api_token()
        from datetime import timedelta
        c = self.by_name(run_checks(self.cfg, online=False, now=NOW + timedelta(days=2)))
        self.assertEqual(c["Watchlist"].status, WARN)
        self.assertIn("1 with stale", c["Watchlist"].detail)
        c = self.by_name(run_checks(self.cfg, online=False, now=NOW))
        self.assertEqual(c["Watchlist"].status, PASS)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import timedelta

from couponhunter.db import open_db
from couponhunter.scheduler import LEASE_TTL, Job, Scheduler, acquire_lease, run_job
from tests.factories import NOW


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)

    def test_lease_exclusive_until_expiry(self):
        self.assertTrue(acquire_lease(self.db, "j", "A", NOW))
        self.assertFalse(acquire_lease(self.db, "j", "B", NOW + timedelta(minutes=1)))
        # holder A crashed without releasing: B takes over after expiry
        self.assertTrue(acquire_lease(self.db, "j", "B", NOW + LEASE_TTL + timedelta(seconds=1)))

    def test_locked_job_does_not_run(self):
        calls = []
        job = Job("j", timedelta(minutes=5), lambda now: calls.append(now) or "ran")
        acquire_lease(self.db, "j", "other", NOW)
        self.assertEqual(run_job(self.db, job, "me", NOW), "locked")
        self.assertEqual(calls, [])

    def test_catch_up_runs_once_after_downtime(self):
        calls = []
        clock = {"t": NOW}
        s = Scheduler(self.db, [Job("j", timedelta(minutes=10), lambda now: calls.append(now) or "")],
                      clock=lambda: clock["t"])
        s.run_pending()
        clock["t"] = NOW + timedelta(days=3)  # machine was off for 3 days
        s.run_pending()
        s.run_pending()
        self.assertEqual(len(calls), 2)

    def test_failing_job_is_contained_and_recorded(self):
        def boom(now):
            raise RuntimeError("source exploded")

        s = Scheduler(self.db, [Job("bad", timedelta(minutes=1), boom), Job("good", timedelta(minutes=1), lambda n: "ok")],
                      clock=lambda: NOW)
        self.assertEqual(s.run_pending(), {"bad": "error", "good": "ok"})
        row = self.db.one("SELECT status, detail FROM job_runs WHERE job='bad'")
        self.assertEqual(row["status"], "error")
        self.assertIn("source exploded", row["detail"])
        self.assertIsNone(self.db.one("SELECT * FROM job_leases WHERE job='bad'"))  # lease released

    def test_thread_start_stop(self):
        s = Scheduler(self.db, [Job("j", timedelta(hours=1), lambda n: "")], tick=0.01)
        s.start()
        s.stop()
        self.assertFalse(s._thread.is_alive())


if __name__ == "__main__":
    unittest.main()

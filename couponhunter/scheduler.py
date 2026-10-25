"""In-process scheduler: a single thread, a few periodic jobs, durable leases.

Why not APScheduler or Windows Task Scheduler for each job: there are four
jobs on fixed intervals. A 100-line loop with a database lease gives
restart-safety, no-overlap and an audit trail (job_runs) without a
dependency. Windows Task Scheduler is still used, once, to start the process
at logon (see docs/deployment.md).

Reliability properties:
  * A job never runs concurrently with itself, even across two processes
    (lease row with expiry; a crashed holder's lease simply expires).
  * Missed runs after downtime are caught up once, not replayed N times.
  * A failing job is logged and retried at its next interval; it cannot kill
    the loop.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import alerts, ingest, logs
from .db import Database, backup, iso, parse_iso, utcnow
from .notify import Notifier

log = logs.get("scheduler")
LEASE_TTL = timedelta(minutes=15)


@dataclass
class Job:
    name: str
    interval: timedelta
    fn: Callable[[datetime], str]


def acquire_lease(db: Database, job: str, holder: str, now: datetime) -> bool:
    with db.tx() as c:
        c.execute(
            """INSERT INTO job_leases(job, holder, expires_at) VALUES (?,?,?)
               ON CONFLICT(job) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at
               WHERE job_leases.expires_at <= ? OR job_leases.holder = excluded.holder""",
            (job, holder, iso(now + LEASE_TTL), iso(now)),
        )
        row = c.execute("SELECT holder FROM job_leases WHERE job=?", (job,)).fetchone()
        return row is not None and row["holder"] == holder


def release_lease(db: Database, job: str, holder: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM job_leases WHERE job=? AND holder=?", (job, holder))


def last_run(db: Database, job: str) -> datetime | None:
    r = db.one("SELECT MAX(started_at) FROM job_runs WHERE job=? AND status IN ('ok','error')", (job,))
    return parse_iso(r[0]) if r and r[0] else None


def run_job(db: Database, job: Job, holder: str, now: datetime) -> str:
    if not acquire_lease(db, job.name, holder, now):
        return "locked"
    with db.tx() as c:
        run_id = c.execute("INSERT INTO job_runs(job, started_at, status) VALUES (?,?, 'running')",
                           (job.name, iso(now))).lastrowid
    status, detail = "ok", ""
    try:
        detail = job.fn(now)
    except Exception as e:  # contain: one bad job must not stop the scheduler
        status, detail = "error", f"{type(e).__name__}: {e}"
        log.exception("job failed", extra={"fields": {"job": job.name}})
    finally:
        with db.tx() as c:
            c.execute("UPDATE job_runs SET finished_at=?, status=?, detail=? WHERE id=?",
                      (iso(utcnow()), status, detail[:1000], run_id))
        release_lease(db, job.name, holder)
    return status


def default_jobs(db: Database, notifier: Notifier, backup_dir) -> list[Job]:
    def sources(now: datetime) -> str:
        out = []
        for s in db.query("SELECT id FROM sources WHERE adapter NOT IN ('extension','manual')"):
            st = ingest.run_source(db, s["id"], now)
            if st.status != "skipped":
                out.append(f"{s['id']}:{st.status}+{st.created}/{st.updated}/-{st.rejected}")
        return ", ".join(out) or "nothing due"

    def alert(now: datetime) -> str:
        return str(alerts.run_alerts(db, notifier, now))

    def nightly(now: datetime) -> str:
        path = backup(db, backup_dir)
        with db.tx() as c:
            cutoff = iso(now - timedelta(days=90))
            n1 = c.execute("DELETE FROM source_runs WHERE started_at < ?", (cutoff,)).rowcount
            n2 = c.execute("DELETE FROM job_runs WHERE started_at < ?", (cutoff,)).rowcount
            n3 = c.execute("DELETE FROM alerts WHERE status IN ('sent','suppressed','failed') AND created_at < ?",
                           (iso(now - timedelta(days=180)),)).rowcount
        return f"backup={path.name} pruned source_runs={n1} job_runs={n2} alerts={n3}"

    return [
        Job("sources", timedelta(minutes=10), sources),
        Job("alerts", timedelta(minutes=15), alert),
        Job("nightly", timedelta(hours=24), nightly),
    ]


class Scheduler:
    def __init__(self, db: Database, jobs: list[Job], tick: float = 30.0, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.jobs = jobs
        self.tick = tick
        self.clock = clock
        self.holder = f"proc-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def due(self, now: datetime) -> list[Job]:
        out = []
        for j in self.jobs:
            last = last_run(self.db, j.name)
            if last is None or now - last >= j.interval:
                out.append(j)  # however long we were down, this runs once
        return out

    def run_pending(self) -> dict[str, str]:
        now = self.clock()
        return {j.name: run_job(self.db, j, self.holder, now) for j in self.due(now)}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="couponhunter-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)


__all__ = ["Scheduler", "Job", "default_jobs", "acquire_lease", "run_job"]

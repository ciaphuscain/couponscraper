"""SQLite connection management, migrations and backups.

SQLite is the right database here: one user, one machine, a few thousand rows
a day at most. WAL mode lets the dashboard read while the scheduler writes;
busy_timeout absorbs short lock contention; foreign keys are enforced.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all times must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(_ISO)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Database:
    """Thread-safe wrapper: one connection, serialised writes.

    A single process owns the database. Writes go through `tx()`, which holds
    a process-local lock and an IMMEDIATE transaction so a write either fully
    commits or fully rolls back (verified by tests/test_db.py crash tests).
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = connect(path)
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self.conn.in_transaction:  # nested: join the outer transaction
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def migrate(db: Database) -> list[str]:
    """Apply pending migrations in order, each in its own transaction."""
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in db.query("SELECT version FROM schema_migrations")}
    done = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = f.stem
        if version in applied:
            continue
        sql = f.read_text(encoding="utf-8")
        with db._lock:
            # executescript would auto-commit; run statements inside one explicit transaction.
            db.conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    db.conn.execute(stmt)
                db.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, iso(utcnow()))
                )
                db.conn.execute("COMMIT")
            except BaseException:
                db.conn.execute("ROLLBACK")
                raise
        done.append(version)
    return done


def _split_sql(script: str) -> list[str]:
    stmts, buf = [], []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0]
        buf.append(stripped)
        if stripped.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if sqlite3.complete_statement(stmt):
                stmts.append(stmt)
                buf = []
    rest = "\n".join(buf).strip()
    if rest:
        stmts.append(rest)
    return stmts


def open_db(path: str | Path) -> Database:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = Database(path)
    migrate(db)
    return db


def backup(db: Database, dest_dir: str | Path, keep: int = 14) -> Path:
    """Online, consistent backup via the SQLite backup API, then integrity check.

    Old backups beyond `keep` are pruned (only files this function created,
    matched by name prefix).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"couponhunter-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.db"
    target = sqlite3.connect(str(dest))
    try:
        with db._lock:
            db.conn.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    finally:
        target.close()
    backups = sorted(dest_dir.glob("couponhunter-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    return dest


def integrity_check(db: Database) -> str:
    return db.one("PRAGMA integrity_check")[0]

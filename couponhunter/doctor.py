"""`couponhunter doctor`: check every moving part and say exactly how to fix it."""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import repo
from .config import Config
from .db import integrity_check, open_db, parse_iso, utcnow
from .notify import NotifyError, TelegramNotifier

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _ago(ts: str | None, now: datetime) -> str:
    dt = parse_iso(ts)
    if dt is None:
        return "never"
    h = (now - dt).total_seconds() / 3600
    return f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def run_checks(cfg: Config, online: bool = True, now: datetime | None = None) -> list[Check]:
    now = now or utcnow()
    out: list[Check] = []
    add = out.append

    v = sys.version_info
    add(Check(PASS if v >= (3, 11) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}",
              "" if v >= (3, 11) else "Install Python 3.11+: winget install --id Python.Python.3.13 -e"))

    try:
        cfg.home.mkdir(parents=True, exist_ok=True)
        probe = cfg.home / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        add(Check(PASS, "Data folder", str(cfg.home)))
    except OSError as e:
        add(Check(FAIL, "Data folder", f"not writable: {e}", "Set COUPONHUNTER_HOME to a folder you own"))
        return out

    if not cfg.db_path.exists():
        add(Check(FAIL, "Database", "not created yet", "Run: couponhunter init"))
        return out
    db = open_db(cfg.db_path)
    try:
        ic = integrity_check(db)
        add(Check(PASS if ic == "ok" else FAIL, "Database integrity", ic,
                  "" if ic == "ok" else "Restore the latest backup: couponhunter restore <file> --yes"))

        tok_ok = cfg.token_path.exists() and len(cfg.token_path.read_text(encoding="utf-8").strip()) >= 32
        detail = "present"
        status = PASS if tok_ok else FAIL
        if tok_ok and os.name != "nt" and cfg.token_path.stat().st_mode & 0o077:
            status, detail = WARN, "readable by other users"
        add(Check(status, "API token", detail, "" if status == PASS else
                  ("Run: couponhunter init" if not tok_ok else f"chmod 600 {cfg.token_path}")))

        add(_server_check(cfg))

        ext = db.one("SELECT last_success_at FROM sources WHERE id='extension'")
        listing = db.one("SELECT MAX(observed_at) FROM price_observations WHERE source='extension_listing'")[0]
        last_ext = max(filter(None, [ext["last_success_at"] if ext else None, listing]), default=None)
        add(Check(PASS if last_ext else WARN, "Browser extension", f"last contact {_ago(last_ext, now)}",
                  "" if last_ext else "Load extension/ unpacked, paste `couponhunter token` in its options, "
                                      "then press Save on any amazon.in product page"))

        s = repo.get_settings(db)
        add(Check(PASS if s["wallet"] else WARN, "Payment methods", f"{len(s['wallet'])} in wallet",
                  "" if s["wallet"] else "Settings → add your cards (bank + type only) so bank offers can match"))
        prof = s["profile"]
        unset = [k for k in ("is_prime", "is_new_customer") if prof.get(k) is None]
        add(Check(WARN if unset else PASS, "Profile", "complete" if not unset else f"not set: {', '.join(unset)}",
                  "Settings → set Prime / new-customer status (otherwise those offers stay unconfirmed)" if unset else ""))

        rows = repo.watchlist_rows(db)
        fresh = timedelta(hours=s["price_fresh_hours"])
        stale = [r["asin"] for r in rows if not r["last_observed_at"] or now - parse_iso(r["last_observed_at"]) > fresh]
        if not rows:
            add(Check(WARN, "Watchlist", "empty", "Track a product: extension Track button, Telegram, or couponhunter add <url>"))
        else:
            add(Check(PASS if not stale else WARN, "Watchlist", f"{len(rows)} watched, {len(stale)} with stale prices",
                      "" if not stale else "Stale prices never trigger alerts. Enable passive observation in the "
                                           "extension options, or visit those products"))

        bad = db.query("SELECT id, disabled_reason, last_error, consecutive_failures, enabled FROM sources "
                       "WHERE adapter NOT IN ('extension','manual') AND (enabled=0 OR consecutive_failures>0)")
        n_src = db.one("SELECT COUNT(*) FROM sources WHERE adapter NOT IN ('extension','manual')")[0]
        add(Check(PASS if not bad else WARN, "Sources",
                  f"{n_src} configured" + (f"; problems: {', '.join(b['id'] for b in bad)}" if bad else ""),
                  "; ".join(f"{b['id']}: {b['disabled_reason'] or b['last_error']}" for b in bad)))

        jobs = {r["job"]: r["t"] for r in db.query("SELECT job, MAX(started_at) t FROM job_runs GROUP BY job")}
        late = [j for j, lim in (("alerts", 1), ("sources", 1), ("nightly", 30)) if
                not jobs.get(j) or now - parse_iso(jobs[j]) > timedelta(hours=lim)]
        add(Check(PASS if not late else WARN, "Scheduler",
                  ", ".join(f"{j} {_ago(jobs.get(j), now)}" for j in ("sources", "alerts", "nightly")),
                  "" if not late else "Keep `couponhunter serve` running (see docs/09 §3 for auto-start at logon)"))

        backups = sorted(cfg.backup_dir.glob("couponhunter-*.db")) if cfg.backup_dir.exists() else []
        newest = datetime.fromtimestamp(backups[-1].stat().st_mtime, now.tzinfo) if backups else None
        ok = newest is not None and now - newest < timedelta(days=2)
        add(Check(PASS if ok else WARN, "Backups",
                  f"{len(backups)} kept, newest {(now - newest).days}d old" if newest else "none yet",
                  "" if ok else "Run: couponhunter backup (the nightly job does this while serve runs)"))

        failed = db.one("SELECT COUNT(*) FROM alerts WHERE status='failed'")[0]
        add(Check(PASS if not failed else WARN, "Alert delivery", f"{failed} failed",
                  "" if not failed else "Health page → Alert outbox shows the error"))
    finally:
        db.close()

    add(_telegram_check(online))
    return out


def _server_check(cfg: Config) -> Check:
    try:
        with urllib.request.urlopen(f"http://{cfg.host}:{cfg.port}/healthz", timeout=2) as r:
            if json.loads(r.read()).get("ok"):
                return Check(PASS, "Server", f"running on http://{cfg.host}:{cfg.port}")
    except Exception:
        pass
    with socket.socket() as s:
        busy = s.connect_ex((cfg.host, cfg.port)) == 0
    if busy:
        return Check(FAIL, "Server", f"port {cfg.port} is used by another program",
                     f"Use another port: couponhunter --port {cfg.port + 1} serve (and update the extension options)")
    return Check(WARN, "Server", "not running", "Start it: couponhunter serve")


def _telegram_check(online: bool) -> Check:
    tg = TelegramNotifier.from_env()
    if tg is None:
        return Check(WARN, "Telegram", "not configured (alerts go to alerts.jsonl)",
                     "Optional: see docs/09 §4 to set COUPONHUNTER_TELEGRAM_TOKEN and _CHAT_ID")
    if not online:
        return Check(PASS, "Telegram", "configured (not contacted: --offline)")
    try:
        me = tg.call("getMe", {}, timeout=8)
        return Check(PASS, "Telegram", f"bot @{me['result'].get('username')} reachable")
    except NotifyError as e:
        return Check(FAIL if e.permanent else WARN, "Telegram", str(e),
                     "Check the token with @BotFather" if e.permanent else "Network problem; retry later")


def render(checks: list[Check]) -> str:
    icon = {PASS: "✔", WARN: "!", FAIL: "✘"}
    lines = []
    for c in checks:
        lines.append(f"[{icon[c.status]}] {c.name:<18} {c.detail}")
        if c.fix and c.status != PASS:
            lines.append(f"    → {c.fix}")
    n = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    lines.append(f"\n{n[PASS]} ok, {n[WARN]} warnings, {n[FAIL]} problems")
    return "\n".join(lines)

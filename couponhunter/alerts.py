"""Alert policy and delivery.

Pipeline: evaluate watched products -> decide (thresholds) -> dedupe ->
cooldown -> quiet hours -> outbox -> deliver (grouped, with retries).

Every alert is a row with a UNIQUE dedup_key derived from *what the user
would be told* (product, offer set, instrument, price). The same offer seen
on three websites, or the same state re-evaluated every hour, produces one
key and therefore at most one alert.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from . import repo, service
from .db import Database, iso, parse_iso
from .money import fmt
from .notify import Notifier, NotifyError
from .optimizer import Scenario

MAX_ATTEMPTS = 5
MIN_SUCCESS_BP = 5_000
DOWNGRADE_WINDOW = timedelta(days=7)  # don't alert on combinations more likely to fail than work


@dataclass
class AlertDecision:
    product_id: int
    reason: str
    dedup_key: str
    savings: int
    text: str


def _quiet_window(settings: dict[str, Any]) -> tuple[time, time, timezone]:
    q = settings.get("quiet_hours") or {}
    tz = timezone(timedelta(minutes=int(q.get("utc_offset_minutes", 330))))
    start = time.fromisoformat(q.get("start", "22:30"))
    end = time.fromisoformat(q.get("end", "07:30"))
    return start, end, tz


def quiet_until(now: datetime, settings: dict[str, Any]) -> datetime | None:
    """If now is inside quiet hours, the moment they end; else None."""
    q = settings.get("quiet_hours")
    if not q:
        return None
    start, end, tz = _quiet_window(settings)
    local = now.astimezone(tz)
    t = local.time()
    inside = (start <= t or t < end) if start > end else (start <= t < end)
    if not inside:
        return None
    end_dt = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_dt <= local:
        end_dt += timedelta(days=1)
    return end_dt.astimezone(timezone.utc)


def _dedup_key(product_id: int, s: Scenario, reason_kind: str) -> str:
    raw = json.dumps([product_id, reason_kind, list(s.offer_ids), s.instrument.id if s.instrument else None,
                      s.checkout_payable])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def render(ev: service.Evaluation, s: Scenario, reason: str, by_id: dict[str, Any], now: datetime) -> str:
    r = ev.result
    assert r is not None
    lines = [f"{ev.title or ev.asin}", f"Why: {reason}", f"Price now: {fmt(r.product.price)}"]
    if s.instant_savings or s.funding_savings:
        lines.append(f"Confirmed-at-checkout savings: {fmt(s.instant_savings + s.funding_savings)} "
                     f"→ you pay {fmt(s.cash_cost)}")
    if s.expected_deferred:
        lines.append(f"Possible later cashback: up to {fmt(s.expected_deferred)} "
                     f"(not guaranteed; est. {fmt(s.risk_adjusted_deferred)})")
    lines.append(f"Chance every offer applies: {s.success_probability_bp // 100}%")
    for a in s.applied:
        o = by_id.get(a.offer_id)
        if o is not None and o.valid_until is not None:
            hours = (o.valid_until - now).total_seconds() / 3600
            if hours < 48:
                lines.append(f"⏳ {a.title} ends in {max(hours, 0):.0f}h")
    if s.instrument is not None and any(by_id[a.offer_id].payment for a in s.applied if a.offer_id in by_id):
        lines.append(f"Pay with: {s.instrument.label}")
    for a in s.assumptions[:3]:
        lines.append(f"⚠ {a}")
    if s.steps:
        lines.append("Steps: " + " → ".join(s.steps[:4]))
    if ev.advice and ev.advice.action in ("buy_now", "wait"):
        lines.append(f"Verdict: {'Buy now' if ev.advice.action == 'buy_now' else 'Wait'}: {ev.advice.headline}")
    if ev.verdict and ev.verdict.caveats:
        lines.append("Note: " + ev.verdict.caveats[0])
    if any(by_id[a.offer_id].is_synthetic for a in s.applied if a.offer_id in by_id):
        lines.append("[SYNTHETIC TEST DATA]")
    lines.append(repo.product_url(ev.asin))
    return "\n".join(lines)


def decide(db: Database, now: datetime, settings: dict[str, Any] | None = None) -> list[AlertDecision]:
    settings = settings or repo.get_settings(db)
    out: list[AlertDecision] = []
    for w in repo.watchlist_rows(db):
        if not w["alerts_enabled"]:
            continue
        ev = service.evaluate(db, w["asin"], now, settings)
        if ev.result is None or ev.result.product.price_is_stale:
            continue  # never alert on a price we can't vouch for
        best = ev.result.best_confirmed
        if best is None or best.success_probability_bp < MIN_SUCCESS_BP:
            continue
        by_id = {o.id: o for o in ev.offers}
        price = ev.result.product.price
        immediate = best.instant_savings + best.funding_savings
        min_abs = w["min_savings"] if w["min_savings"] is not None else settings["min_savings_paise"]
        min_pct = price * settings["min_savings_pct_bp"] // 10_000
        reason = None
        kind = None
        if w["target_price"] and best.cash_cost <= w["target_price"]:
            reason, kind = f"at or below your target {fmt(w['target_price'])}", "target"
        elif best.applied and immediate >= max(min_abs, min_pct):
            reason, kind = f"{fmt(immediate)} off with offers you're eligible for", "savings"
        elif ev.verdict and ev.verdict.is_good_deal:
            reason, kind = "price drop: " + ev.verdict.reasons[0], "price"
        if reason is None:
            continue
        out.append(AlertDecision(w["id"], reason, _dedup_key(w["id"], best, kind), immediate,
                                 render(ev, best, reason, by_id, now)))
    return out


def enqueue(db: Database, decisions: list[AlertDecision], now: datetime, settings: dict[str, Any] | None = None) -> dict[str, int]:
    settings = settings or repo.get_settings(db)
    counts = {"new": 0, "duplicate": 0, "suppressed": 0, "deferred": 0}
    cooldown = timedelta(hours=settings["cooldown_hours"])
    quiet = quiet_until(now, settings)
    with db.tx() as c:
        for d in decisions:
            if c.execute("SELECT 1 FROM alerts WHERE dedup_key=?", (d.dedup_key,)).fetchone():
                counts["duplicate"] += 1
                continue
            status, next_at = "pending", iso(now)
            recent = c.execute(
                """SELECT savings FROM alerts WHERE product_id=? AND status IN ('sent','pending','deferred')
                   AND created_at>=? ORDER BY created_at DESC LIMIT 1""",
                (d.product_id, iso(now - cooldown)),
            ).fetchone()
            # A worse deal than one we told the user about this week is not news
            # (typically the better offer just expired or went stale).
            better_sent = c.execute(
                """SELECT 1 FROM alerts WHERE product_id=? AND status IN ('sent','pending','deferred')
                   AND created_at>=? AND savings>=? AND reason NOT LIKE 'at or below your target%' LIMIT 1""",
                (d.product_id, iso(now - DOWNGRADE_WINDOW), d.savings),
            ).fetchone()
            # Within cooldown, only a materially better deal gets through.
            if (recent is not None and d.savings < recent["savings"] + max(10_000, recent["savings"] // 10)) or (
                    better_sent is not None and not d.reason.startswith("at or below your target")):
                status, next_at = "suppressed", None
                counts["suppressed"] += 1
            elif quiet is not None:
                status, next_at = "deferred", iso(quiet)
                counts["deferred"] += 1
            else:
                counts["new"] += 1
            c.execute(
                """INSERT INTO alerts(product_id, reason, dedup_key, payload, savings, created_at, status, next_attempt_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (d.product_id, d.reason, d.dedup_key, json.dumps({"text": d.text}), d.savings, iso(now), status, next_at),
            )
    return counts


def deliver(db: Database, notifier: Notifier, now: datetime, settings: dict[str, Any] | None = None) -> dict[str, int]:
    settings = settings or repo.get_settings(db)
    if quiet_until(now, settings) is not None:
        return {"sent": 0, "failed": 0, "retrying": 0}
    due = db.query(
        """SELECT * FROM alerts WHERE status IN ('pending','deferred') AND (next_attempt_at IS NULL OR next_attempt_at<=?)
           ORDER BY created_at LIMIT 50""", (iso(now),))
    counts = {"sent": 0, "failed": 0, "retrying": 0}
    if not due:
        return counts
    texts = [json.loads(r["payload"])["text"] for r in due]
    if len(due) >= settings["alert_digest_threshold"]:
        batches = [(due, f"{len(due)} savings opportunities\n\n" + "\n\n— — —\n\n".join(texts))]
    else:
        batches = [([r], t) for r, t in zip(due, texts)]
    for rows, text in batches:
        try:
            notifier.send(text)
        except NotifyError as e:
            with db.tx() as c:
                for r in rows:
                    attempts = r["attempts"] + 1
                    if e.permanent or attempts >= MAX_ATTEMPTS:
                        c.execute("UPDATE alerts SET status='failed', attempts=?, last_error=?, channel=? WHERE id=?",
                                  (attempts, str(e)[:300], notifier.name, r["id"]))
                        counts["failed"] += 1
                    else:
                        delay = timedelta(seconds=e.retry_after_s or 60 * 2 ** attempts)
                        c.execute("UPDATE alerts SET attempts=?, last_error=?, next_attempt_at=?, channel=? WHERE id=?",
                                  (attempts, str(e)[:300], iso(now + delay), notifier.name, r["id"]))
                        counts["retrying"] += 1
            continue
        with db.tx() as c:
            for r in rows:
                c.execute("UPDATE alerts SET status='sent', sent_at=?, attempts=attempts+1, channel=?, last_error=NULL "
                          "WHERE id=?", (iso(now), notifier.name, r["id"]))
                counts["sent"] += 1
    return counts


def run_alerts(db: Database, notifier: Notifier, now: datetime) -> dict[str, Any]:
    settings = repo.get_settings(db)
    decisions = decide(db, now, settings)
    q = enqueue(db, decisions, now, settings)
    d = deliver(db, notifier, now, settings)
    return {"decided": len(decisions), **q, **d}


__all__ = ["decide", "enqueue", "deliver", "run_alerts", "quiet_until", "parse_iso"]

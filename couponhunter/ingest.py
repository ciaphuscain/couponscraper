"""Source run pipeline: fetch -> normalise -> dedupe -> upsert with provenance.

Correctness properties (each covered by tests/test_ingest.py):
  * Idempotent: replaying the same fetch creates no new offers or sightings.
  * Cross-source dedup: the same offer from two sources is one row with two
    provenance rows (UNIQUE fingerprint + INSERT .. ON CONFLICT).
  * Atomic: all writes for one run happen in one transaction; a crash
    mid-run leaves the previous state intact.
  * Conservative withdrawal: offers absent from a *complete* snapshot are
    marked missing for that source; a run with a high reject rate (likely a
    schema change) never marks anything missing.
  * Failures never refresh offers: a failed source run touches no offer, so
    its offers age into STALE instead of looking confirmed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .db import Database, iso, parse_iso
from .domain import Authenticity
from .normalize import NormalizationError, NormalizedOffer, normalize_record
from .sources import ADAPTERS, FetchResult, SourceError, SourceRecord

DISABLE_AFTER_FAILURES = 5
MAX_REJECT_RATIO_FOR_SNAPSHOT = 0.5
MAX_BACKOFF = timedelta(hours=24)
AUTH_RANK = {
    Authenticity.INVALID.value: -1,
    Authenticity.DISPUTED.value: 0,
    Authenticity.UNVERIFIED.value: 1,
    Authenticity.SOURCE_LISTED.value: 2,
    Authenticity.OFFICIAL.value: 3,
}


@dataclass
class RunStats:
    source_id: str
    status: str = "ok"
    fetched: int = 0
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    rejected: int = 0
    missing_marked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def register_source(
    db: Database,
    source_id: str,
    adapter: str,
    name: str,
    config: dict[str, Any],
    now: datetime,
    *,
    terms_reviewed: bool = False,
    terms_note: str = "",
    min_interval_s: int = 3600,
    stale_after_h: int = 48,
    enabled: bool = True,
) -> None:
    if adapter not in ADAPTERS and adapter not in ("extension", "manual"):
        raise ValueError(f"unknown adapter {adapter}")
    with db.tx() as c:
        c.execute(
            """INSERT INTO sources(id, adapter, name, config, enabled, terms_reviewed, terms_note,
                                   min_interval_s, stale_after_h, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET adapter=excluded.adapter, name=excluded.name,
                   config=excluded.config, terms_reviewed=excluded.terms_reviewed,
                   terms_note=excluded.terms_note, min_interval_s=excluded.min_interval_s,
                   stale_after_h=excluded.stale_after_h""",
            (source_id, adapter, name, json.dumps(config), int(enabled), int(terms_reviewed), terms_note,
             min_interval_s, stale_after_h, iso(now)),
        )


def ensure_builtin_sources(db: Database, now: datetime) -> None:
    for sid, name, stale in (
        ("extension", "Browser extension (pages you viewed)", 24),
        ("manual", "Entered by you", 24 * 14),
    ):
        if not db.one("SELECT 1 FROM sources WHERE id=?", (sid,)):
            register_source(db, sid, sid, name, {}, now, terms_reviewed=True, min_interval_s=0, stale_after_h=stale)


def source_due(row: sqlite3.Row, now: datetime, ignore_timing: bool = False) -> tuple[bool, str]:
    if not row["enabled"]:
        return False, f"disabled: {row['disabled_reason'] or 'by owner'}"
    adapter = ADAPTERS.get(row["adapter"])
    if adapter is None:
        return False, "not a scheduled source"
    if adapter.requires_terms_review and not row["terms_reviewed"]:
        return False, "terms not reviewed by owner"
    if ignore_timing:
        return True, "forced"
    nxt = parse_iso(row["next_fetch_at"])
    if nxt and now < nxt:
        return False, f"backing off until {row['next_fetch_at']}"
    last = parse_iso(row["last_fetch_at"])
    if last and now - last < timedelta(seconds=row["min_interval_s"]):
        return False, "fetched recently"
    return True, "due"


def run_source(db: Database, source_id: str, now: datetime, adapter_obj: Any = None,
               force: bool = False) -> RunStats:
    row = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if row is None:
        raise KeyError(source_id)
    stats = RunStats(source_id)
    # --force only skips interval/backoff timing; it never overrides a disabled source
    # (e.g. after an access refusal) or a missing terms review.
    ok, why = source_due(row, now, ignore_timing=force)
    if not ok:
        stats.status = "skipped"
        stats.warnings.append(why)
        return stats
    adapter_obj = adapter_obj or ADAPTERS[row["adapter"]]()
    with db.tx() as c:
        run_id = c.execute(
            "INSERT INTO source_runs(source_id, started_at, status) VALUES (?,?, 'running')", (source_id, iso(now))
        ).lastrowid
        c.execute("UPDATE sources SET last_fetch_at=? WHERE id=?", (iso(now), source_id))
    try:
        result = adapter_obj.fetch(json.loads(row["config"]))
    except SourceError as e:
        _record_failure(db, row, run_id, now, e, stats)
        return stats
    except Exception as e:  # adapter bug: contain it, record it, never crash the scheduler
        _record_failure(db, row, run_id, now, SourceError(f"adapter crashed: {e!r}", kind="transient"), stats)
        return stats

    stats.warnings.extend(result.warnings)
    if row["schema_fingerprint"] and result.schema_fingerprint and row["schema_fingerprint"] != result.schema_fingerprint:
        stats.warnings.append("source record shape changed since last run")
    from .repo import extra_domains, get_settings  # local import: repo depends on nothing in ingest

    with db.tx() as c:
        ingest_result(c, source_id, result, now, stats, extra_domains(get_settings(db)))
        c.execute(
            """UPDATE sources SET last_success_at=?, consecutive_failures=0, last_error=NULL,
                   next_fetch_at=NULL, schema_fingerprint=COALESCE(?, schema_fingerprint) WHERE id=?""",
            (iso(now), result.schema_fingerprint, source_id),
        )
        _finish_run(c, run_id, now, stats)
    return stats


def _record_failure(db: Database, row: sqlite3.Row, run_id: int, now: datetime, e: SourceError, stats: RunStats) -> None:
    stats.status = "failed"
    stats.errors.append(f"{e.kind}: {e}")
    failures = row["consecutive_failures"] + 1
    disable_reason = None
    if e.kind == "auth":
        disable_reason = f"access refused ({e}); not retrying automatically"
    elif e.kind == "config":
        disable_reason = f"configuration problem: {e}"
    elif failures >= DISABLE_AFTER_FAILURES:
        disable_reason = f"{failures} consecutive failures; last: {e}"
    if e.kind == "rate_limited":
        delay = timedelta(seconds=max(e.retry_after_s or 3600, row["min_interval_s"]))
    else:
        delay = min(timedelta(seconds=max(row["min_interval_s"], 60)) * (2 ** (failures - 1)), MAX_BACKOFF)
    with db.tx() as c:
        c.execute(
            """UPDATE sources SET consecutive_failures=?, last_error=?, last_error_at=?, next_fetch_at=?,
                   enabled=CASE WHEN ? IS NULL THEN enabled ELSE 0 END,
                   disabled_reason=COALESCE(?, disabled_reason) WHERE id=?""",
            (failures, str(e)[:500], iso(now), iso(now + delay), disable_reason, disable_reason, row["id"]),
        )
        _finish_run(c, run_id, now, stats)
    if disable_reason:
        stats.warnings.append(f"source disabled: {disable_reason}")


def _finish_run(c: sqlite3.Connection, run_id: int, now: datetime, s: RunStats) -> None:
    c.execute(
        """UPDATE source_runs SET finished_at=?, status=?, fetched=?, created=?, updated=?, duplicates=?,
               rejected=?, error=? WHERE id=?""",
        (iso(now), s.status, s.fetched, s.created, s.updated, s.duplicates, s.rejected,
         "; ".join(s.errors + s.warnings)[:1000] or None, run_id),
    )


def ingest_result(c: sqlite3.Connection, source_id: str, result: FetchResult, now: datetime, stats: RunStats,
                  extra_domains: frozenset[str] = frozenset()) -> list[int]:
    """Write one fetch result. Must be called inside a transaction."""
    seen: list[int] = []
    stats.fetched += len(result.records)
    seen_in_run: set[str] = set()
    for rec in result.records:
        try:
            n = normalize_record(dict(rec.data), extra_domains)
        except (NormalizationError, TypeError, ValueError) as e:
            stats.rejected += 1
            if len(stats.errors) < 20:
                stats.errors.append(f"rejected record: {e}")
            continue
        fp = n.fingerprint()  # in-run dedup only; origin is constant within one result
        if fp in seen_in_run:
            stats.duplicates += 1  # same offer twice in one listing
            continue
        seen_in_run.add(fp)
        offer_id, outcome = upsert_offer(c, n, source_id, rec, result.data_origin, now)
        setattr(stats, outcome, getattr(stats, outcome) + 1)
        seen.append(offer_id)
        stats.warnings.extend(f"{n.title[:60]}: {w}" for w in n.warnings[:3] if len(stats.warnings) < 50)

    reject_ratio = stats.rejected / stats.fetched if stats.fetched else 0
    if stats.rejected:
        stats.status = "partial"
    if result.snapshot and not result.records and not result.empty_is_authoritative:
        stats.warnings.append("empty listing: not withdrawing any offers (could be a partial page or feed)")
    elif result.snapshot and reject_ratio <= MAX_REJECT_RATIO_FOR_SNAPSHOT:
        stats.missing_marked = _mark_missing(c, source_id, seen, now, result.snapshot_asin)
    elif result.snapshot:
        stats.warnings.append(
            f"{stats.rejected}/{stats.fetched} records rejected: possible format change; "
            "not withdrawing any offers from this run"
        )
    return seen


def upsert_offer(c: sqlite3.Connection, n: NormalizedOffer, source_id: str, rec: SourceRecord, data_origin: str,
                 now: datetime) -> tuple[int, str]:
    """Insert or merge one normalised offer. Returns (offer_id, 'created'|'updated'|'duplicates')."""
    fp = n.fingerprint()
    if data_origin == "synthetic":
        fp = "syn-" + fp[:28]  # synthetic data never merges with real observations
    t = iso(now)
    pay = None
    if n.payment:
        pay = json.dumps({"banks": sorted(n.payment.banks), "types": sorted(n.payment.instrument_types),
                          "networks": sorted(n.payment.networks)})
    cur = c.execute(
        """INSERT INTO offers(fingerprint, kind, title, provider, discount_type, value, max_discount, min_order,
               basis, payment, eligibility, new_customer_only, prime_only, account_specific, one_time_use,
               valid_from, valid_until, code, redemption_url, redemption_steps, stacks_with,
               authenticity, settlement_probability_bp, source_text, first_seen_at, last_confirmed_at, data_origin)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fingerprint) DO NOTHING""",
        (fp, n.kind.value, n.title, n.provider, n.discount_type.value, n.value, n.max_discount, n.min_order,
         n.basis.value, pay, json.dumps(n.eligibility) if n.eligibility else None, int(n.new_customer_only),
         int(n.prime_only), int(n.account_specific), int(n.one_time_use), iso(n.valid_from), iso(n.valid_until),
         n.code, n.redemption_url, json.dumps(n.redemption_steps), json.dumps({k: v.value for k, v in n.stacks_with.items()}),
         rec.authenticity.value, n.settlement_probability_bp, n.source_text, t, t, data_origin),
    )
    created = cur.rowcount == 1
    offer_id = c.execute("SELECT id FROM offers WHERE fingerprint=?", (fp,)).fetchone()[0]
    for asin in n.asins:
        c.execute("INSERT OR IGNORE INTO offer_products(offer_id, asin) VALUES (?,?)", (offer_id, asin))

    prev = c.execute("SELECT raw_text, valid_until, missing_since FROM offer_sources WHERE offer_id=? AND source_id=?",
                     (offer_id, source_id)).fetchone()
    c.execute(
        """INSERT INTO offer_sources(offer_id, source_id, source_url, evidence_type, raw_text, authenticity,
               valid_until, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(offer_id, source_id) DO UPDATE SET source_url=excluded.source_url,
               evidence_type=excluded.evidence_type, raw_text=excluded.raw_text,
               authenticity=excluded.authenticity, valid_until=excluded.valid_until,
               last_seen_at=excluded.last_seen_at, missing_since=NULL""",
        (offer_id, source_id, rec.source_url, rec.evidence_type, rec.raw_text[:2000], rec.authenticity.value,
         iso(n.valid_until), t, t),
    )
    if not created and AUTH_RANK[rec.authenticity.value] >= AUTH_RANK[_current_auth(c, offer_id)]:
        # Prefer the wording and rules from the most authoritative source; never
        # overwrite with a less trustworthy source's version.
        c.execute(
            """UPDATE offers SET title=?, source_text=?, basis=CASE WHEN ?='unknown' THEN basis ELSE ? END,
                   redemption_url=COALESCE(?, redemption_url), stacks_with=CASE WHEN ?='{}' THEN stacks_with ELSE ? END,
                   version=version+1 WHERE id=?""",
            (n.title, n.source_text, n.basis.value, n.basis.value, n.redemption_url,
             json.dumps({k: v.value for k, v in n.stacks_with.items()}),
             json.dumps({k: v.value for k, v in n.stacks_with.items()}), offer_id),
        )
    refresh_offer_aggregates(c, offer_id, now)
    if created:
        return offer_id, "created"
    if prev is not None and prev["missing_since"] is None and prev["valid_until"] == iso(n.valid_until):
        return offer_id, "duplicates"  # re-confirmation of an unchanged sighting
    return offer_id, "updated"


def _current_auth(c: sqlite3.Connection, offer_id: int) -> str:
    return c.execute("SELECT authenticity FROM offers WHERE id=?", (offer_id,)).fetchone()[0]


def refresh_offer_aggregates(c: sqlite3.Connection, offer_id: int, now: datetime) -> None:
    rows = c.execute(
        "SELECT authenticity, valid_until, last_seen_at, missing_since FROM offer_sources WHERE offer_id=?",
        (offer_id,),
    ).fetchall()
    if not rows:
        return
    live = [r for r in rows if r["missing_since"] is None]
    basis_rows = live or rows
    expiries = [r["valid_until"] for r in basis_rows if r["valid_until"]]
    auth = max((r["authenticity"] for r in basis_rows), key=lambda a: AUTH_RANK[a])
    # When live sources disagree on expiry, the earliest stated date wins.
    last_confirmed = max(r["last_seen_at"] for r in live) if live else max(r["last_seen_at"] for r in rows)
    c.execute(
        """UPDATE offers SET valid_until=?, authenticity=?, last_confirmed_at=?,
               withdrawn_at=CASE WHEN ? THEN COALESCE(withdrawn_at, ?) ELSE NULL END WHERE id=?""",
        (min(expiries) if expiries else None, auth, last_confirmed, int(not live), iso(now), offer_id),
    )


def _mark_missing(c: sqlite3.Connection, source_id: str, seen: list[int], now: datetime,
                  scope_asin: str | None) -> int:
    where = "source_id=? AND missing_since IS NULL"
    params: list[Any] = [source_id]
    if seen:
        where += f" AND offer_id NOT IN ({','.join('?' * len(seen))})"
        params += seen
    if scope_asin:
        where += " AND offer_id IN (SELECT offer_id FROM offer_products WHERE asin=?)"
        params.append(scope_asin)
    ids = [r[0] for r in c.execute(f"SELECT offer_id FROM offer_sources WHERE {where}", params).fetchall()]
    c.execute(f"UPDATE offer_sources SET missing_since=? WHERE {where}", [iso(now), *params])
    for oid in ids:
        refresh_offer_aggregates(c, oid, now)
    return len(ids)

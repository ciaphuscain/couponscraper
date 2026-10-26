"""Database <-> domain mapping and owner-facing record keeping.

Business rules live in optimizer/eligibility/verification; this module only
loads, stores and enforces state-transition integrity.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .amazon import canonical_product_url, normalize_asin
from .db import Database, iso, parse_iso
from .domain import (
    Authenticity,
    Basis,
    DiscountType,
    Offer,
    OfferKind,
    PaymentInstrument,
    PaymentRequirement,
    ProductContext,
    Tri,
    UserProfile,
)
from .verification import derive_lifecycle

DEFAULT_SETTINGS: dict[str, Any] = {
    "profile": {"is_prime": None, "is_new_customer": None},
    "wallet": [],  # [{id, label, type, bank, network, cost_ratio_bp}] - categories only, never card numbers
    "min_savings_paise": 20_000,  # ₹200
    "min_savings_pct_bp": 300,  # 3%
    "cooldown_hours": 12,
    "quiet_hours": {"start": "22:30", "end": "07:30", "utc_offset_minutes": 330},
    "price_fresh_hours": 12,
    "alert_digest_threshold": 3,
    "stacking_defaults": {},  # {"bank_instant+product_coupon": "yes"} - owner-confirmed rules
    "extra_redemption_domains": [],
    "include_hypothetical_instruments": False,
}

CASHBACK_TRANSITIONS = {
    "expected": {"tracked", "pending", "rejected"},
    "tracked": {"pending", "confirmed", "rejected"},
    "pending": {"confirmed", "rejected"},
    "confirmed": {"paid", "rejected"},
    "paid": set(),
    "rejected": {"pending"},  # providers do reverse rejections after a claim
}


class NotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


# --- settings ---------------------------------------------------------------

def get_settings(db: Database) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for r in db.query("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def set_setting(db: Database, key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(f"unknown setting {key}")
    validate_setting(key, value)
    with db.tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


def validate_setting(key: str, value: Any) -> None:
    if key == "wallet":
        if not isinstance(value, list):
            raise ValueError("wallet must be a list")
        for w in value:
            if not isinstance(w, dict) or not w.get("id") or not w.get("type"):
                raise ValueError("each wallet entry needs id and type")
            for k, v in w.items():
                if isinstance(v, str) and sum(ch.isdigit() for ch in v) >= 12:
                    raise ValueError("wallet entries must not contain card numbers")
            if not 1 <= int(w.get("cost_ratio_bp", 10_000)) <= 10_000:
                raise ValueError("cost_ratio_bp must be 1..10000")
    elif key in ("min_savings_paise", "min_savings_pct_bp", "cooldown_hours", "price_fresh_hours",
                 "alert_digest_threshold"):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")


def user_profile(settings: dict[str, Any]) -> UserProfile:
    prof = settings.get("profile", {})
    wallet = tuple(
        PaymentInstrument(
            id=str(w["id"]), label=str(w.get("label") or w["id"]), type=str(w["type"]).lower(),
            bank=(str(w["bank"]).lower() if w.get("bank") else None),
            network=(str(w["network"]).lower() if w.get("network") else None),
            cost_ratio_bp=int(w.get("cost_ratio_bp", 10_000)),
        )
        for w in settings.get("wallet", [])
    )
    return UserProfile(is_prime=prof.get("is_prime"), is_new_customer=prof.get("is_new_customer"), instruments=wallet)


def stacking_defaults(settings: dict[str, Any]) -> dict[frozenset[str], Tri]:
    out = {}
    for k, v in (settings.get("stacking_defaults") or {}).items():
        a, _, b = k.partition("+")
        out[frozenset({a, b})] = Tri(v)
    return out


# --- products, watchlist, prices -------------------------------------------

def upsert_product(db: Database, asin: str, now: datetime, title: str = "", data_origin: str = "manual",
                   **attrs: Any) -> int:
    asin = normalize_asin(asin)
    allowed = {"brand", "category", "seller", "variant", "availability"}
    attrs = {k: v for k, v in attrs.items() if k in allowed and v}
    with db.tx() as c:
        c.execute(
            "INSERT INTO products(asin, title, created_at, data_origin) VALUES (?,?,?,?) ON CONFLICT(asin) DO NOTHING",
            (asin, title[:300], iso(now), data_origin),
        )
        if title:
            c.execute("UPDATE products SET title=? WHERE asin=?", (title[:300], asin))
        for k, v in attrs.items():
            c.execute(f"UPDATE products SET {k}=? WHERE asin=?", (str(v)[:200], asin))
        return c.execute("SELECT id FROM products WHERE asin=?", (asin,)).fetchone()[0]


def watch(db: Database, product_id: int, now: datetime, target_price: int | None = None,
          min_savings: int | None = None) -> None:
    with db.tx() as c:
        c.execute(
            """INSERT INTO watchlist(product_id, target_price, min_savings, added_at) VALUES (?,?,?,?)
               ON CONFLICT(product_id) DO UPDATE SET target_price=excluded.target_price,
                   min_savings=excluded.min_savings""",
            (product_id, target_price, min_savings, iso(now)),
        )


def unwatch(db: Database, product_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM watchlist WHERE product_id=?", (product_id,))


def get_product(db: Database, asin: str) -> sqlite3.Row:
    r = db.one("SELECT * FROM products WHERE asin=?", (normalize_asin(asin),))
    if r is None:
        raise NotFound(asin)
    return r


def record_price(db: Database, product_id: int, price: int, observed_at: datetime, source: str, data_origin: str,
                 list_price: int | None = None, seller: str | None = None, in_stock: bool | None = None) -> bool:
    if price <= 0:
        raise ValueError("price must be positive")
    if list_price is not None and list_price <= 0:
        list_price = None
    with db.tx() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO price_observations(product_id, price, list_price, seller, in_stock, observed_at,
                   source, data_origin) VALUES (?,?,?,?,?,?,?,?)""",
            (product_id, price, list_price, seller, None if in_stock is None else int(in_stock), iso(observed_at),
             source, data_origin),
        )
        return cur.rowcount == 1


def latest_price(db: Database, product_id: int) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM price_observations WHERE product_id=? ORDER BY observed_at DESC, id DESC LIMIT 1", (product_id,)
    )


def product_context(db: Database, asin: str, now: datetime, settings: dict[str, Any] | None = None) -> ProductContext | None:
    settings = settings or get_settings(db)
    p = get_product(db, asin)
    obs = latest_price(db, p["id"])
    if obs is None:
        return None
    observed = parse_iso(obs["observed_at"])
    stale = now - observed > timedelta(hours=settings["price_fresh_hours"])
    return ProductContext(
        asin=p["asin"], price=obs["price"], title=p["title"], category=p["category"],
        seller=obs["seller"] or p["seller"], brand=p["brand"], price_observed_at=observed, price_is_stale=stale,
    )


# --- offers -------------------------------------------------------------------

def _row_to_offer(r: sqlite3.Row, now: datetime) -> Offer:
    pay = None
    if r["payment"]:
        p = json.loads(r["payment"])
        pay = PaymentRequirement(frozenset(p.get("banks", [])), frozenset(p.get("types", [])),
                                 frozenset(p.get("networks", [])))
    authenticity = Authenticity(r["authenticity"])
    if r["user_flag"] in ("invalid", "disputed"):
        authenticity = Authenticity(r["user_flag"])
    lifecycle = derive_lifecycle(
        now, parse_iso(r["valid_from"]), parse_iso(r["valid_until"]), parse_iso(r["last_confirmed_at"]),
        withdrawn=r["withdrawn_at"] is not None, stale_after=timedelta(hours=r["stale_after_h"] or 48),
    )
    return Offer(
        id=str(r["id"]), kind=OfferKind(r["kind"]), title=r["title"], provider=r["provider"],
        discount_type=DiscountType(r["discount_type"]), value=r["value"], max_discount=r["max_discount"],
        min_order=r["min_order"], basis=Basis(r["basis"]), payment=pay,
        eligibility=json.loads(r["eligibility"]) if r["eligibility"] else None,
        new_customer_only=bool(r["new_customer_only"]), prime_only=bool(r["prime_only"]),
        account_specific=bool(r["account_specific"]), one_time_use=bool(r["one_time_use"]),
        valid_from=parse_iso(r["valid_from"]), valid_until=parse_iso(r["valid_until"]), code=r["code"],
        redemption_url=r["redemption_url"], redemption_steps=json.loads(r["redemption_steps"]),
        stacks_with={k: Tri(v) for k, v in json.loads(r["stacks_with"]).items()},
        excludes=frozenset(json.loads(r["excludes"])), authenticity=authenticity, lifecycle=lifecycle,
        redeemed_ok=r["ok_count"] or 0, redeemed_failed=r["fail_count"] or 0,
        settlement_probability_bp=r["settlement_probability_bp"], source_text=r["source_text"],
        is_synthetic=r["data_origin"] == "synthetic",
    )


_OFFER_SELECT = """
SELECT o.*,
       (SELECT MAX(s.stale_after_h) FROM offer_sources os JOIN sources s ON s.id = os.source_id
         WHERE os.offer_id = o.id) AS stale_after_h,
       (SELECT COUNT(*) FROM redemption_attempts ra WHERE ra.offer_id = o.id AND ra.outcome = 'applied') AS ok_count,
       (SELECT COUNT(*) FROM redemption_attempts ra WHERE ra.offer_id = o.id
         AND ra.outcome IN ('failed', 'not_shown')) AS fail_count
FROM offers o
"""


def offers_for_asin(db: Database, asin: str, now: datetime) -> list[Offer]:
    """Product-scoped offers for this ASIN plus every offer not scoped to products."""
    rows = db.query(
        _OFFER_SELECT + """
        WHERE (o.id IN (SELECT offer_id FROM offer_products WHERE asin = ?)
               OR NOT EXISTS (SELECT 1 FROM offer_products op WHERE op.offer_id = o.id))
          AND (o.valid_until IS NULL OR o.valid_until > ?)
        """,
        (normalize_asin(asin), iso(now - timedelta(days=1))),
    )
    offers = [_row_to_offer(r, now) for r in rows]
    return offers + inferred_offers(db, normalize_asin(asin), now, offers)


INFERABLE_KINDS = ("bank_instant", "bank_cashback", "amazon_pay_cashback")
INFER_FROM_DAYS = 7
INFER_SUPPRESS_HOURS = 24


def _economics_key(o: Offer) -> tuple:
    pay = (tuple(sorted(o.payment.banks)), tuple(sorted(o.payment.instrument_types)),
           tuple(sorted(o.payment.networks))) if o.payment else None
    return (o.kind.value, o.discount_type.value, o.value, o.max_discount, o.min_order, pay)


def inferred_offers(db: Database, asin: str, now: datetime, direct: list[Offer]) -> list[Offer]:
    """Bank / Amazon Pay offers seen on *other* product pages, as 'possibly applies here'.

    Issuer offers on amazon.in usually cover many products, so an offer shown on
    three other pages is worth surfacing, but never as confirmed: eligibility is
    UNKNOWN, so it only appears in 'possible' answers. Skipped when this product's
    own page was captured recently (the page itself is better evidence) and never
    crosses between synthetic and real data.
    """
    prod = db.one("SELECT id, data_origin FROM products WHERE asin=?", (asin,))
    if prod is None:
        return []
    recent_capture = db.one(
        "SELECT 1 FROM price_observations WHERE product_id=? AND source='extension' AND observed_at>=? LIMIT 1",
        (prod["id"], iso(now - timedelta(hours=INFER_SUPPRESS_HOURS))))
    if recent_capture:
        return []
    synthetic_target = prod["data_origin"] == "synthetic"
    rows = db.query(
        _OFFER_SELECT + f"""
        WHERE o.kind IN ({",".join("?" * len(INFERABLE_KINDS))})
          AND o.withdrawn_at IS NULL AND o.user_flag IS NULL
          AND (o.valid_until IS NULL OR o.valid_until > ?)
          AND o.last_confirmed_at >= ?
          AND EXISTS (SELECT 1 FROM offer_products op WHERE op.offer_id = o.id)
          AND NOT EXISTS (SELECT 1 FROM offer_products op WHERE op.offer_id = o.id AND op.asin = ?)
          AND EXISTS (SELECT 1 FROM offer_sources os WHERE os.offer_id = o.id AND os.evidence_type = 'user_observed')
          AND (o.data_origin = 'synthetic') = ?
        ORDER BY o.last_confirmed_at DESC""",
        (*INFERABLE_KINDS, iso(now), iso(now - timedelta(days=INFER_FROM_DAYS)), asin, int(synthetic_target)))
    have = {_economics_key(o) for o in direct}
    groups: dict[tuple, tuple[Offer, set[str]]] = {}
    for r in rows:
        o = _row_to_offer(r, now)
        k = _economics_key(o)
        if k in have:
            continue
        asins = {x["asin"] for x in db.query("SELECT asin FROM offer_products WHERE offer_id=?", (r["id"],))}
        if k in groups:
            groups[k][1].update(asins)
        else:
            groups[k] = (o, asins)
    out = []
    for o, asins in groups.values():
        o.seen_on_other_products = len(asins)
        out.append(o)
    return out


def all_offers(db: Database, now: datetime, include_expired: bool = False, limit: int = 500) -> list[tuple[Offer, list[str]]]:
    where = "" if include_expired else "WHERE (o.valid_until IS NULL OR o.valid_until > ?)"
    params: tuple = () if include_expired else (iso(now),)
    rows = db.query(_OFFER_SELECT + where + " ORDER BY o.last_confirmed_at DESC LIMIT ?", (*params, limit))
    out = []
    for r in rows:
        asins = [x["asin"] for x in db.query("SELECT asin FROM offer_products WHERE offer_id=?", (r["id"],))]
        out.append((_row_to_offer(r, now), asins))
    return out


def offer_provenance(db: Database, offer_id: int) -> list[sqlite3.Row]:
    return db.query(
        """SELECT os.*, s.name AS source_name, s.adapter FROM offer_sources os JOIN sources s ON s.id = os.source_id
           WHERE os.offer_id=? ORDER BY os.last_seen_at DESC""", (offer_id,))


def flag_offer(db: Database, offer_id: int, flag: str | None) -> None:
    if flag not in (None, "invalid", "disputed"):
        raise ValueError("flag must be invalid, disputed or null")
    with db.tx() as c:
        if c.execute("UPDATE offers SET user_flag=?, version=version+1 WHERE id=?", (flag, offer_id)).rowcount == 0:
            raise NotFound(offer_id)


# --- redemption, purchases, cashback --------------------------------------

def record_redemption(db: Database, offer_id: int, product_id: int | None, outcome: str, now: datetime, *,
                      failure_reason: str | None = None, observed_savings: int | None = None,
                      snapshot: dict | None = None, channel: str = "dashboard",
                      idempotency_key: str | None = None) -> int:
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
            raise NotFound(offer_id)
        if idempotency_key:
            r = c.execute("SELECT id FROM redemption_attempts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if r:
                return r[0]
        return c.execute(
            """INSERT INTO redemption_attempts(offer_id, product_id, attempted_at, outcome, failure_reason,
                   observed_savings, eligibility_snapshot, channel, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)""",
            (offer_id, product_id, iso(now), outcome, (failure_reason or "")[:300] or None, observed_savings,
             json.dumps(snapshot or {}), channel, idempotency_key),
        ).lastrowid


def record_purchase(db: Database, product_id: int | None, now: datetime, reference_price: int, checkout_paid: int,
                    cash_paid: int | None = None, offers_used: list[int] | None = None, notes: str = "",
                    idempotency_key: str | None = None) -> int:
    with db.tx() as c:
        if idempotency_key:
            r = c.execute("SELECT id FROM purchases WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if r:
                return r[0]
        return c.execute(
            """INSERT INTO purchases(product_id, purchased_at, reference_price, checkout_paid, cash_paid, offers_used,
                   notes, idempotency_key) VALUES (?,?,?,?,?,?,?,?)""",
            (product_id, iso(now), reference_price, checkout_paid,
             checkout_paid if cash_paid is None else cash_paid, json.dumps(offers_used or []), notes[:500],
             idempotency_key),
        ).lastrowid


def create_cashback(db: Database, provider: str, now: datetime, advertised: int, expected: int | None = None,
                    purchase_id: int | None = None, offer_id: int | None = None, order_ref: str | None = None,
                    expected_by: datetime | None = None, status: str = "expected") -> int:
    if status not in ("expected", "tracked"):
        raise InvalidTransition("new cashback records start as expected or tracked")
    with db.tx() as c:
        rid = c.execute(
            """INSERT INTO cashback_records(provider, purchase_id, offer_id, order_ref, advertised, expected, status,
                   amount, expected_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (provider[:80], purchase_id, offer_id, (order_ref or "")[:80] or None, advertised,
             advertised if expected is None else expected, status, 0, iso(expected_by), iso(now), iso(now)),
        ).lastrowid
        c.execute("INSERT INTO cashback_events(record_id, from_status, to_status, amount, at) VALUES (?,?,?,?,?)",
                  (rid, None, status, 0, iso(now)))
        return rid


def transition_cashback(db: Database, record_id: int, to_status: str, amount: int, now: datetime,
                        note: str = "", expected_version_status: str | None = None) -> None:
    """Move a cashback record along its lifecycle with an audit event.

    The UPDATE is conditional on the status we read, so two concurrent updates
    cannot both succeed from the same starting state (compare-and-set).
    """
    with db.tx() as c:
        r = c.execute("SELECT status FROM cashback_records WHERE id=?", (record_id,)).fetchone()
        if r is None:
            raise NotFound(record_id)
        cur = r["status"]
        if expected_version_status and cur != expected_version_status:
            raise InvalidTransition(f"record is {cur}, expected {expected_version_status}")
        if to_status not in CASHBACK_TRANSITIONS[cur]:
            raise InvalidTransition(f"cannot go from {cur} to {to_status}")
        if amount < 0:
            raise ValueError("amount must be >= 0")
        n = c.execute("UPDATE cashback_records SET status=?, amount=?, updated_at=? WHERE id=? AND status=?",
                      (to_status, amount, iso(now), record_id, cur)).rowcount
        if n != 1:
            raise InvalidTransition("concurrent update")
        c.execute("INSERT INTO cashback_events(record_id, from_status, to_status, amount, at, note) VALUES (?,?,?,?,?,?)",
                  (record_id, cur, to_status, amount, iso(now), note[:300]))


@dataclass
class SavingsSummary:
    purchases: int
    realized_instant: int  # reference price - checkout paid, over recorded purchases
    realized_funding: int  # checkout paid - cash paid (discounted balance)
    cashback_paid: int  # only status = paid
    cashback_confirmed_unpaid: int
    cashback_pending: int  # expected + tracked + pending
    cashback_rejected: int
    confirmation_rate_bp: int | None  # settled (paid/confirmed) / (settled + rejected)

    @property
    def realized_total(self) -> int:
        return self.realized_instant + self.realized_funding + self.cashback_paid


def savings_summary(db: Database) -> SavingsSummary:
    p = db.one("""SELECT COUNT(*) n, COALESCE(SUM(reference_price - checkout_paid), 0) inst,
                         COALESCE(SUM(checkout_paid - cash_paid), 0) fund FROM purchases""")
    cb = {r["status"]: (r["amt"], r["exp"], r["n"]) for r in db.query(
        "SELECT status, SUM(amount) amt, SUM(expected) exp, COUNT(*) n FROM cashback_records GROUP BY status")}

    def amt(s): return cb.get(s, (0, 0, 0))[0] or 0
    def exp(s): return cb.get(s, (0, 0, 0))[1] or 0
    def n(s): return cb.get(s, (0, 0, 0))[2] or 0

    settled = n("paid") + n("confirmed")
    rate = settled * 10_000 // (settled + n("rejected")) if settled + n("rejected") else None
    return SavingsSummary(
        purchases=p["n"], realized_instant=p["inst"], realized_funding=p["fund"], cashback_paid=amt("paid"),
        cashback_confirmed_unpaid=amt("confirmed"),
        cashback_pending=exp("expected") + exp("tracked") + amt("pending"),
        cashback_rejected=n("rejected"), confirmation_rate_bp=rate,
    )


def provider_confirmation_rates(db: Database, min_records: int = 3) -> dict[str, int]:
    """Measured settlement rate per provider, used to replace priors once enough data exists."""
    out = {}
    for r in db.query("""SELECT provider, SUM(status IN ('paid','confirmed')) ok, SUM(status='rejected') bad
                         FROM cashback_records GROUP BY provider"""):
        total = (r["ok"] or 0) + (r["bad"] or 0)
        if total >= min_records:
            out[r["provider"].lower()] = (r["ok"] or 0) * 10_000 // total
    return out


# --- price history ------------------------------------------------------------

def price_history(db: Database, product_id: int, since: datetime | None = None) -> list[sqlite3.Row]:
    if since is None:
        return db.query("SELECT * FROM price_observations WHERE product_id=? ORDER BY observed_at", (product_id,))
    return db.query("SELECT * FROM price_observations WHERE product_id=? AND observed_at>=? ORDER BY observed_at",
                    (product_id, iso(since)))


def watchlist_rows(db: Database) -> list[sqlite3.Row]:
    return db.query(
        """SELECT p.*, w.target_price, w.min_savings, w.alerts_enabled, w.added_at,
                  (SELECT price FROM price_observations po WHERE po.product_id=p.id
                    ORDER BY observed_at DESC, id DESC LIMIT 1) AS last_price,
                  (SELECT observed_at FROM price_observations po WHERE po.product_id=p.id
                    ORDER BY observed_at DESC, id DESC LIMIT 1) AS last_observed_at,
                  (SELECT data_origin FROM price_observations po WHERE po.product_id=p.id
                    ORDER BY observed_at DESC, id DESC LIMIT 1) AS last_price_origin
           FROM watchlist w JOIN products p ON p.id = w.product_id ORDER BY w.added_at DESC""")


def product_url(asin: str) -> str:
    return canonical_product_url(asin)

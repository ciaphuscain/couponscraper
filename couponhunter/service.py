"""Application service: answers "what is the lowest realistic price for this
product right now, how sure are we, and how do I get it?"

Everything the API, CLI, alerting and extension show comes from evaluate().
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import repo
from .db import Database
from .domain import OfferKind
from .eligibility import describe_payment
from .money import fmt
from .optimizer import OptimizationResult, Scenario, optimize
from .pricing import DealVerdict, Observation, PriceStats, compute_stats, judge_price
from .verification import offer_confidence


@dataclass
class Evaluation:
    asin: str
    title: str
    has_price: bool
    result: OptimizationResult | None
    stats: PriceStats | None
    verdict: DealVerdict | None
    price_origin: str | None
    list_price: int | None
    offers: list = field(default_factory=list)


_CACHE: OrderedDict[tuple, Evaluation] = OrderedDict()
_CACHE_SIZE = 512
_CACHE_BUCKET_S = 300  # lifecycle/staleness are time-dependent; re-evaluate at least every 5 minutes


def evaluate(db: Database, asin: str, now: datetime, settings: dict[str, Any] | None = None) -> Evaluation:
    """Memoised on (database version, 5-minute time bucket, asin).

    Any commit, by this process or another (e.g. the CLI), invalidates every
    entry, so a cached answer is never staler than the data plus 5 minutes.
    """
    key = (db.instance_id, db.version(), int(now.timestamp()) // _CACHE_BUCKET_S, asin.upper())
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    ev = _evaluate(db, asin, now, settings)
    _CACHE[key] = ev
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return ev


def _evaluate(db: Database, asin: str, now: datetime, settings: dict[str, Any] | None = None) -> Evaluation:
    settings = settings or repo.get_settings(db)
    p = repo.get_product(db, asin)
    ctx = repo.product_context(db, asin, now, settings)
    if ctx is None:
        return Evaluation(p["asin"], p["title"], False, None, None, None, None, None)
    offers = repo.offers_for_asin(db, asin, now)
    measured = repo.provider_confirmation_rates(db)
    for o in offers:
        if o.settlement_probability_bp is None and o.provider.lower() in measured:
            o.settlement_probability_bp = measured[o.provider.lower()]
    result = optimize(
        ctx, offers, repo.user_profile(settings), now,
        include_hypothetical=bool(settings.get("include_hypothetical_instruments")),
        stacking_defaults=repo.stacking_defaults(settings),
    )
    hist = repo.price_history(db, p["id"])
    obs = [Observation(r["price"], repo.parse_iso(r["observed_at"]), r["list_price"]) for r in hist]
    stats = compute_stats(obs, now)
    last = hist[-1] if hist else None
    best = result.best_confirmed or result.baseline
    verdict = judge_price(stats, best.cash_cost, last["list_price"] if last else None)
    return Evaluation(p["asin"], p["title"], True, result, stats, verdict,
                      last["data_origin"] if last else None, last["list_price"] if last else None, offers)


def scenario_json(s: Scenario, offers_by_id: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "offer_ids": [int(i) for i in s.offer_ids],
        "instrument": None if s.instrument is None else {
            "id": s.instrument.id, "label": s.instrument.label, "hypothetical": s.instrument.hypothetical},
        "price": s.price,
        "instant_savings": s.instant_savings,
        "checkout_payable": s.checkout_payable,
        "cash_cost": s.cash_cost,
        "funding_savings": s.funding_savings,
        "expected_deferred": s.expected_deferred,
        "risk_adjusted_deferred": s.risk_adjusted_deferred,
        "effective_if_all_paid": s.effective_if_all_paid,
        "risk_adjusted_cost": s.risk_adjusted_cost,
        "optimistic_payable": s.optimistic_payable,
        "success_probability_bp": s.success_probability_bp,
        "rules_confirmed": s.rules_confirmed,
        "assumptions": s.assumptions,
        "steps": s.steps,
        "applied": [
            {"offer_id": int(a.offer_id), "kind": a.kind.value, "title": a.title, "amount": a.amount,
             "deferred": a.deferred, "basis": a.basis.value, "confidence_bp": a.confidence_bp,
             "settle_bp": a.settle_bp, "code": a.code, "redemption_url": a.redemption_url}
            for a in s.applied
        ],
    }


def evaluation_json(ev: Evaluation, db: Database, now: datetime, max_scenarios: int = 8) -> dict[str, Any]:
    out: dict[str, Any] = {"asin": ev.asin, "title": ev.title, "has_price": ev.has_price,
                           "product_url": repo.product_url(ev.asin)}
    if not ev.has_price or ev.result is None:
        out["message"] = "No price observation yet. Open the product page with the extension, or record a price."
        return out
    r = ev.result
    best_c = r.best_confirmed
    out.update({
        "price": r.product.price,
        "price_observed_at": repo.iso(r.product.price_observed_at),
        "price_is_stale": r.product.price_is_stale,
        "price_origin": ev.price_origin,
        "list_price": ev.list_price,
        "baseline": scenario_json(r.baseline),
        "best_confirmed": scenario_json(best_c) if best_c else None,
        "best_possible": scenario_json(r.best_possible),
        "scenarios": [scenario_json(s) for s in r.scenarios[:max_scenarios]],
        "excluded": [{"offer_id": int(e.offer_id), "title": e.title, "reason": e.reason} for e in r.excluded],
        "notes": r.notes,
        "price_stats": asdict(ev.stats) if ev.stats else None,
        "deal_verdict": asdict(ev.verdict) if ev.verdict else None,
    })
    out["offers"] = [offer_json(o) for o in ev.offers]
    return out


def offer_json(o, asins: list[str] | None = None) -> dict[str, Any]:
    c = offer_confidence(o)
    return {
        "id": int(o.id), "kind": o.kind.value, "title": o.title, "provider": o.provider,
        "discount_type": o.discount_type.value, "value": o.value, "max_discount": o.max_discount,
        "min_order": o.min_order, "basis": o.basis.value, "payment": describe_payment(o.payment) if o.payment else None,
        "code": o.code, "redemption_url": o.redemption_url,
        "valid_until": repo.iso(o.valid_until), "lifecycle": o.lifecycle.value, "authenticity": o.authenticity.value,
        "confidence_bp": c.bp, "confidence_note": c.note, "new_customer_only": o.new_customer_only,
        "prime_only": o.prime_only, "account_specific": o.account_specific, "is_synthetic": o.is_synthetic,
        "asins": asins,
    }


def describe_offer_value(o) -> str:
    if o.kind is OfferKind.DEAL_PRICE:
        return "in price"
    if o.discount_type.value == "percent":
        s = f"{o.value / 100:g}%"
        if o.max_discount:
            s += f" up to {fmt(o.max_discount)}"
        return s
    return fmt(o.value)

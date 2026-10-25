"""Offer verification: lifecycle derivation and confidence estimation.

Lifecycle is *derived* from timestamps every time it is needed rather than
stored as a sticky flag, so an offer can never remain "active" just because
nothing came along to update it. A source going quiet makes its offers STALE,
never more trustworthy.

Confidence numbers here are documented heuristics (docs/verification.md), not
learned probabilities. They are exposed with their components so the UI can
show *why* an offer is rated as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import Authenticity, Lifecycle, Offer, OfferKind

DEFAULT_STALE_AFTER = timedelta(hours=48)

AUTHENTICITY_BASE_BP = {
    Authenticity.OFFICIAL: 9_500,
    Authenticity.SOURCE_LISTED: 7_500,
    Authenticity.UNVERIFIED: 5_000,
    Authenticity.DISPUTED: 2_000,
    Authenticity.INVALID: 0,
}

# Prior probability that an advertised deferred benefit is eventually paid.
# Assumptions until the user's own cashback records give a measured rate.
SETTLEMENT_PRIOR_BP = {
    OfferKind.BANK_CASHBACK: 8_500,
    OfferKind.AMAZON_PAY_CASHBACK: 9_000,
    OfferKind.PORTAL_CASHBACK: 6_500,  # tracking failures are common with portals
}


def derive_lifecycle(
    now: datetime,
    valid_from: datetime | None,
    valid_until: datetime | None,
    last_confirmed_at: datetime | None,
    withdrawn: bool = False,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> Lifecycle:
    if valid_until is not None and now >= valid_until:
        return Lifecycle.EXPIRED
    if withdrawn:
        return Lifecycle.WITHDRAWN
    if valid_from is not None and now < valid_from:
        return Lifecycle.NOT_STARTED
    if last_confirmed_at is None or now - last_confirmed_at > stale_after:
        return Lifecycle.STALE
    return Lifecycle.ACTIVE


@dataclass(frozen=True)
class Confidence:
    bp: int
    base_bp: int
    attempts: int
    successes: int
    note: str


def offer_confidence(offer: Offer) -> Confidence:
    """Probability-like score that the offer applies when the user tries it.

    Starts from an authenticity prior and is pulled toward the user's own
    observed success rate as redemption attempts accumulate, using a
    pseudo-count of 2 for the prior (a Beta-style update).
    """
    base = AUTHENTICITY_BASE_BP[offer.authenticity]
    ok, bad = offer.redeemed_ok, offer.redeemed_failed
    n = ok + bad
    prior_weight = 2
    bp = (ok * 10_000 + base * prior_weight) // (n + prior_weight)
    if offer.lifecycle is Lifecycle.STALE:
        bp = bp * 6 // 10
        note = "not re-confirmed recently"
    elif n:
        note = f"{ok}/{n} of your attempts succeeded"
    else:
        note = f"no redemption attempts yet; prior from '{offer.authenticity.value}' source"
    return Confidence(bp=bp, base_bp=base, attempts=n, successes=ok, note=note)


def settlement_probability_bp(offer: Offer) -> int:
    if offer.settlement_probability_bp is not None:
        return offer.settlement_probability_bp
    return SETTLEMENT_PRIOR_BP.get(offer.kind, 10_000)

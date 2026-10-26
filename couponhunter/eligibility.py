"""Per-offer eligibility for a given product, user and moment in time.

Returns a three-valued status with human-readable reasons. Payment-instrument
matching is separate (match_payment) because the optimizer evaluates the same
offer against each instrument in the user's wallet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import rules
from .domain import (
    Authenticity,
    Lifecycle,
    Offer,
    PaymentInstrument,
    PaymentRequirement,
    ProductContext,
    Tri,
    UserProfile,
)

# Instrument types that by definition have no issuing bank. A bank-restricted
# offer can never apply to them (as opposed to "bank unknown").
BANKLESS_TYPES = frozenset({"amazon_pay_balance", "gift_card_balance", "cod"})


@dataclass
class EligibilityResult:
    status: Tri
    reasons: list[str] = field(default_factory=list)  # why NO / UNKNOWN
    notes: list[str] = field(default_factory=list)  # informational caveats


def rule_context(product: ProductContext, user: UserProfile) -> dict:
    return {
        "product": {
            "asin": product.asin,
            "category": product.category,
            "seller": product.seller,
            "brand": product.brand,
            "price": product.price,
        },
        "user": {"is_prime": user.is_prime, "is_new_customer": user.is_new_customer},
    }


def check_offer(offer: Offer, product: ProductContext, user: UserProfile, now: datetime) -> EligibilityResult:
    res = EligibilityResult(Tri.YES)

    def no(reason: str) -> EligibilityResult:
        res.status = Tri.NO
        res.reasons.append(reason)
        return res

    def unknown(reason: str) -> None:
        if res.status is Tri.YES:
            res.status = Tri.UNKNOWN
        res.reasons.append(reason)

    lc = offer.lifecycle
    if lc is Lifecycle.EXPIRED:
        return no("offer has expired")
    if lc is Lifecycle.NOT_STARTED:
        return no("offer has not started yet")
    if lc is Lifecycle.WITHDRAWN:
        return no("source no longer lists this offer")
    if offer.authenticity is Authenticity.INVALID:
        return no("offer marked invalid")
    if lc is Lifecycle.STALE:
        unknown("not re-confirmed by any source recently")
    if offer.authenticity is Authenticity.DISPUTED:
        unknown("sources disagree or offer was reported as not working")
    if offer.valid_until is None:
        res.notes.append("expiry date unknown")

    if offer.new_customer_only:
        if user.is_new_customer is False:
            return no("new-customer-only offer")
        if user.is_new_customer is None:
            unknown("new-customer-only; your new-customer status is not set")
    if offer.prime_only:
        if user.is_prime is False:
            return no("Prime-only offer")
        if user.is_prime is None:
            unknown("Prime-only; your Prime status is not set")
    if offer.seen_on_other_products:
        unknown(f"shown on {offer.seen_on_other_products} other product page(s); not confirmed for this one")
    if offer.account_specific:
        unknown("targeted offer: applies only if Amazon shows it in your own account")
    if offer.one_time_use:
        res.notes.append("one use per customer")

    r = rules.evaluate(offer.eligibility, rule_context(product, user))
    if r is False:
        return no(f"product/seller restriction not met: {rules.explain(offer.eligibility)}")
    if r is None:
        unknown(f"cannot confirm restriction: {rules.explain(offer.eligibility)}")

    if offer.redeemed_failed >= 2 and offer.redeemed_failed > offer.redeemed_ok:
        unknown(f"failed in {offer.redeemed_failed} of your attempts")
    return res


def match_payment(req: PaymentRequirement | None, inst: PaymentInstrument | None) -> Tri:
    if req is None:
        return Tri.YES
    if inst is None:
        return Tri.NO
    status = Tri.YES

    def check(allowed: frozenset[str], actual: str | None, bankless: bool = False) -> Tri:
        if not allowed:
            return Tri.YES
        if actual is None:
            return Tri.NO if bankless else Tri.UNKNOWN
        return Tri.YES if actual.lower() in allowed else Tri.NO

    for t in (
        check(req.instrument_types, inst.type),
        check(req.banks, inst.bank, bankless=inst.type in BANKLESS_TYPES),
        check(req.networks, inst.network, bankless=inst.type in BANKLESS_TYPES),
    ):
        if t is Tri.NO:
            return Tri.NO
        if t is Tri.UNKNOWN:
            status = Tri.UNKNOWN
    return status


def describe_payment(req: PaymentRequirement | None) -> str:
    if req is None:
        return "any payment method"
    parts = []
    if req.banks:
        parts.append("/".join(sorted(b.upper() for b in req.banks)))
    if req.networks:
        parts.append("/".join(sorted(n.title() for n in req.networks)))
    if req.instrument_types:
        parts.append("/".join(sorted(t.replace("_", " ") for t in req.instrument_types)))
    return " ".join(parts) or "any payment method"

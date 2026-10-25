"""Deterministic savings optimizer.

Given a product price, candidate offers and the user's payment instruments,
enumerate every feasible combination (at most one offer per kind, one payment
instrument per checkout), compute exact paise amounts and rank them.

Why exhaustive enumeration rather than a solver: a single amazon.in product
rarely carries more than a handful of offers per kind, so the search space is
tiny (see benchmarks/bench.py for measured timings). Enumeration is exact,
explainable and trivially testable; a CP/ILP solver would add a dependency and
opacity for no gain at this size. A pruning step bounds the worst case.

Uncertainty is never resolved by guessing:
  * unknown stacking between two offers -> the combination is kept but
    flagged, and the subsets without that pair are also enumerated;
  * unknown basis (does the bank % apply before or after the coupon?) -> all
    plausible bases are computed and reported as a range; the headline uses
    the least favourable one;
  * deferred benefits (cashback) are never subtracted from the checkout price;
    they are reported separately and in a probability-weighted estimate.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime

from .domain import (
    APPLICATION_ORDER,
    Basis,
    DEFERRED_KINDS,
    DiscountType,
    INSTANT_KINDS,
    Lifecycle,
    Offer,
    OfferKind,
    PaymentInstrument,
    ProductContext,
    Tri,
    UserProfile,
)
from .eligibility import check_offer, describe_payment, match_payment
from .money import fmt, percent_of
from .verification import offer_confidence, settlement_probability_bp

_ORDER = {k: i for i, k in enumerate(APPLICATION_ORDER)}

# Bases that are plausible readings of an UNKNOWN basis, per kind.
_UNKNOWN_BASES = {
    OfferKind.PRODUCT_COUPON: [Basis.PRICE],
    OfferKind.PROMO_CODE: [Basis.PRICE, Basis.AFTER_COUPONS],
    OfferKind.BANK_INSTANT: [Basis.PRICE, Basis.AFTER_COUPONS],
    OfferKind.BANK_CASHBACK: [Basis.PRICE, Basis.PAYABLE],
    OfferKind.AMAZON_PAY_CASHBACK: [Basis.PRICE, Basis.PAYABLE],
    OfferKind.PORTAL_CASHBACK: [Basis.PRICE, Basis.PAYABLE],
}
MAX_UNKNOWN_BASIS_OFFERS = 4  # beyond this, only the pessimistic reading is computed
DEFAULT_MAX_COMBINATIONS = 20_000


@dataclass
class AppliedOffer:
    offer_id: str
    kind: OfferKind
    title: str
    amount: int  # paise
    deferred: bool
    basis: Basis
    confidence_bp: int
    settle_bp: int  # 10_000 for instant offers
    code: str | None
    redemption_url: str | None


@dataclass
class Scenario:
    instrument: PaymentInstrument | None
    applied: list[AppliedOffer]
    price: int
    instant_savings: int
    checkout_payable: int
    cash_cost: int  # what leaves your bank account (differs from payable for discounted gift-card balance)
    funding_savings: int
    expected_deferred: int  # advertised deferred benefits, if all pay out
    risk_adjusted_deferred: int
    effective_if_all_paid: int
    risk_adjusted_cost: int
    optimistic_payable: int  # best reading of any unknown basis
    success_probability_bp: int  # chance every instant offer applies
    assumptions: list[str] = field(default_factory=list)
    rules_confirmed: bool = True
    steps: list[str] = field(default_factory=list)

    @property
    def offer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(a.offer_id for a in self.applied))

    @property
    def advertised_discount(self) -> int:
        return self.instant_savings + self.expected_deferred + self.funding_savings


@dataclass
class Exclusion:
    offer_id: str
    title: str
    reason: str


@dataclass
class OptimizationResult:
    product: ProductContext
    scenarios: list[Scenario]
    excluded: list[Exclusion]
    notes: list[str]

    @property
    def baseline(self) -> Scenario:
        return min(
            (s for s in self.scenarios if not s.applied),
            key=lambda s: s.cash_cost,
        )

    @property
    def best_confirmed(self) -> Scenario | None:
        """Best scenario whose rules are fully known. May still carry
        offers whose *redemption* is unproven; see success_probability_bp."""
        return next((s for s in self.scenarios if s.rules_confirmed), None)

    @property
    def best_possible(self) -> Scenario:
        return self.scenarios[0]


def stack_status(a: Offer, b: Offer, defaults: dict[frozenset[str], Tri] | None = None) -> Tri:
    if a.id in b.excludes or b.id in a.excludes:
        return Tri.NO
    if a.kind == b.kind:
        return Tri.NO
    if OfferKind.DEAL_PRICE in (a.kind, b.kind):
        return Tri.YES  # already part of the price, not a separate benefit
    declared = {x for x in (a.stacks_with.get(b.kind.value), b.stacks_with.get(a.kind.value)) if x is not None}
    if Tri.NO in declared:
        return Tri.NO
    if declared == {Tri.YES}:
        return Tri.YES
    if Tri.UNKNOWN in declared:
        return Tri.UNKNOWN
    if defaults:
        return defaults.get(frozenset({a.kind.value, b.kind.value}), Tri.UNKNOWN)
    return Tri.UNKNOWN


def _benefit(offer: Offer, basis_amount: int) -> int:
    if offer.discount_type is DiscountType.FLAT:
        amt = offer.value
    elif offer.discount_type is DiscountType.PERCENT:
        amt = percent_of(basis_amount, offer.value)
    else:
        return 0
    if offer.max_discount is not None:
        amt = min(amt, offer.max_discount)
    return max(0, amt)


def _hypothetical_instruments(offers: list[Offer], wallet: tuple[PaymentInstrument, ...]) -> list[PaymentInstrument]:
    out: dict[str, PaymentInstrument] = {}
    for o in offers:
        req = o.payment
        if req is None or not req.banks:
            continue
        if any(match_payment(req, w) is Tri.YES for w in wallet):
            continue
        bank = sorted(req.banks)[0]
        itype = sorted(req.instrument_types)[0] if req.instrument_types else "credit_card"
        network = sorted(req.networks)[0] if req.networks else None
        key = f"{bank}:{itype}:{network}"
        out.setdefault(
            key,
            PaymentInstrument(
                id=f"hypothetical:{key}",
                label=f"{describe_payment(req)} (not in your wallet)",
                type=itype,
                bank=bank,
                network=network,
                hypothetical=True,
            ),
        )
    return list(out.values())


def _evaluate(
    price: int,
    chosen: list[Offer],
    bases: dict[str, Basis],
    inst: PaymentInstrument | None,
    conf: dict[str, int],
) -> tuple[list[AppliedOffer], int, int] | str:
    """Compute one interpretation. Returns (applied, payable, risk_payable) or a failure reason."""
    applied: list[AppliedOffer] = []
    instant_total = 0
    coupon_total = 0
    expected_instant = 0
    pending_deferred: list[Offer] = []
    for o in sorted(chosen, key=lambda o: _ORDER[o.kind]):
        if o.kind in DEFERRED_KINDS:
            pending_deferred.append(o)
            continue
        if o.kind not in INSTANT_KINDS:
            continue
        basis = bases[o.id]
        if basis is Basis.PRICE:
            base_amt = price
        elif basis is Basis.AFTER_COUPONS:
            base_amt = price - coupon_total
        else:  # PAYABLE
            base_amt = price - instant_total
        if o.min_order is not None and base_amt < o.min_order:
            return f"{o.title}: minimum order {fmt(o.min_order)} not met on {basis.value} ({fmt(base_amt)})"
        amt = min(_benefit(o, base_amt), price - instant_total)
        instant_total += amt
        expected_instant += amt * conf[o.id] // 10_000
        if o.kind in (OfferKind.PRODUCT_COUPON, OfferKind.PROMO_CODE):
            coupon_total += amt
        applied.append(
            AppliedOffer(o.id, o.kind, o.title, amt, False, basis, conf[o.id], 10_000, o.code, o.redemption_url)
        )
    payable = price - instant_total
    for o in pending_deferred:
        basis = bases[o.id]
        base_amt = price if basis is Basis.PRICE else payable
        if o.min_order is not None and base_amt < o.min_order:
            return f"{o.title}: minimum order {fmt(o.min_order)} not met on {basis.value} ({fmt(base_amt)})"
        amt = _benefit(o, base_amt)
        applied.append(
            AppliedOffer(
                o.id, o.kind, o.title, amt, True, basis, conf[o.id], settlement_probability_bp(o), o.code, o.redemption_url
            )
        )
    return applied, payable, price - expected_instant


def _cash(payable: int, inst: PaymentInstrument | None) -> int:
    ratio = inst.cost_ratio_bp if inst else 10_000
    return (payable * ratio + 9_999) // 10_000  # round up: conservative


def _steps(applied: list[AppliedOffer], offers: dict[str, Offer], inst: PaymentInstrument | None) -> list[str]:
    steps: list[str] = []
    by_kind = {a.kind: a for a in applied}
    if OfferKind.PORTAL_CASHBACK in by_kind:
        a = by_kind[OfferKind.PORTAL_CASHBACK]
        steps.append(
            f"FIRST open Amazon through the {offers[a.offer_id].provider} cashback link (before adding to cart), "
            "with ad-blockers paused, so the portal can track the order"
        )
    if OfferKind.PRODUCT_COUPON in by_kind:
        steps.append("Tick the coupon checkbox on the product page")
    if OfferKind.PROMO_CODE in by_kind:
        steps.append(f"At checkout, enter code {by_kind[OfferKind.PROMO_CODE].code or '(see offer)'}")
    if inst is not None and any(o.payment is not None for o in (offers[a.offer_id] for a in applied)):
        steps.append(f"Pay with {inst.label}; confirm the offer line appears before paying")
    elif inst is not None and inst.cost_ratio_bp != 10_000:
        steps.append(f"Pay with {inst.label}")
    for o in (offers[a.offer_id] for a in applied):
        steps.extend(o.redemption_steps)
    return steps


def optimize(
    product: ProductContext,
    offers: list[Offer],
    user: UserProfile,
    now: datetime,
    *,
    include_hypothetical: bool = False,
    stacking_defaults: dict[frozenset[str], Tri] | None = None,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
) -> OptimizationResult:
    excluded: list[Exclusion] = []
    notes: list[str] = []
    usable: list[Offer] = []
    elig_unknown: dict[str, list[str]] = {}
    for o in offers:
        res = check_offer(o, product, user, now)
        if res.status is Tri.NO:
            excluded.append(Exclusion(o.id, o.title, "; ".join(res.reasons)))
            continue
        if o.kind is OfferKind.DEAL_PRICE:
            notes.append(f"Observed price already includes: {o.title}")
            continue
        if res.status is Tri.UNKNOWN:
            elig_unknown[o.id] = res.reasons
        usable.append(o)
    if product.price_is_stale:
        notes.append("Price observation is stale; open the product page to refresh it")

    by_id = {o.id: o for o in usable}
    conf = {o.id: offer_confidence(o).bp for o in usable}
    instruments: list[PaymentInstrument | None] = list(user.instruments)
    if include_hypothetical:
        instruments += _hypothetical_instruments(usable, user.instruments)
    if not instruments:
        instruments = [None]
        notes.append("No payment methods in your wallet: card/bank offers cannot be matched")

    for o in usable:
        if o.payment is not None and all(match_payment(o.payment, i) is Tri.NO for i in instruments):
            excluded.append(Exclusion(o.id, o.title, f"requires {describe_payment(o.payment)}; none in your wallet"))

    scenarios: list[Scenario] = []
    seen: set[tuple] = set()
    for inst in instruments:
        cands = [o for o in usable if match_payment(o.payment, inst) is not Tri.NO]
        groups: dict[OfferKind, list[Offer]] = {}
        for o in cands:
            groups.setdefault(o.kind, []).append(o)
        _prune(groups, product.price, max_combinations, notes)
        options = [[None, *g] for g in groups.values()]
        for combo in itertools.product(*options):
            chosen = [o for o in combo if o is not None]
            sc = _scenario(product, chosen, inst, conf, by_id, elig_unknown, stacking_defaults)
            if sc is None:
                continue
            key = (inst.id if inst else None, sc.offer_ids)
            if key not in seen:
                seen.add(key)
                scenarios.append(sc)

    scenarios.sort(key=lambda s: (s.risk_adjusted_cost, s.cash_cost, len(s.applied), not s.rules_confirmed))
    return OptimizationResult(product, scenarios, excluded, notes)


def _prune(groups: dict[OfferKind, list[Offer]], price: int, limit: int, notes: list[str]) -> None:
    def size() -> int:
        n = 1
        for g in groups.values():
            n *= len(g) + 1
        return n

    for g in groups.values():
        g.sort(key=lambda o: _benefit(o, price), reverse=True)
    pruned = 0
    while size() > limit:
        largest = max(groups.values(), key=len)
        largest.pop()
        pruned += 1
    if pruned:
        notes.append(f"Search pruned: {pruned} lowest-value offers skipped to bound computation")


def _scenario(
    product: ProductContext,
    chosen: list[Offer],
    inst: PaymentInstrument | None,
    conf: dict[str, int],
    by_id: dict[str, Offer],
    elig_unknown: dict[str, list[str]],
    stacking_defaults: dict[frozenset[str], Tri] | None,
) -> Scenario | None:
    assumptions: list[str] = []
    for a, b in itertools.combinations(chosen, 2):
        st = stack_status(a, b, stacking_defaults)
        if st is Tri.NO:
            return None
        if st is Tri.UNKNOWN:
            assumptions.append(f"Assumes '{a.title}' and '{b.title}' can be combined (not confirmed)")
    for o in chosen:
        for r in elig_unknown.get(o.id, []):
            assumptions.append(f"{o.title}: {r}")
        if o.payment is not None and match_payment(o.payment, inst) is Tri.UNKNOWN:
            assumptions.append(f"{o.title}: cannot confirm your payment method qualifies")

    unknown_ids = [o.id for o in chosen if o.basis is Basis.UNKNOWN]
    fixed = {o.id: o.basis for o in chosen if o.basis is not Basis.UNKNOWN}
    readings: list[dict[str, Basis]] = []
    if len(unknown_ids) <= MAX_UNKNOWN_BASIS_OFFERS:
        for choice in itertools.product(*(_UNKNOWN_BASES[by_id[i].kind] for i in unknown_ids)):
            readings.append({**fixed, **dict(zip(unknown_ids, choice))})
    else:
        # Too many unknowns to enumerate; take the least favourable reading per offer.
        pess = {
            i: (Basis.AFTER_COUPONS if by_id[i].kind in INSTANT_KINDS and by_id[i].kind is not OfferKind.PRODUCT_COUPON
                else Basis.PAYABLE if by_id[i].kind in DEFERRED_KINDS else Basis.PRICE)
            for i in unknown_ids
        }
        readings.append({**fixed, **pess})

    results = []
    for bases in readings:
        r = _evaluate(product.price, chosen, bases, inst, conf)
        if isinstance(r, str):
            return None  # some reading fails a minimum spend: don't present it as feasible
        results.append((bases, r))

    def deferred_sum(applied: list[AppliedOffer]) -> int:
        return sum(a.amount for a in applied if a.deferred)

    # Headline = least favourable reading (highest payable, then lowest deferred).
    worst = max(results, key=lambda x: (x[1][1], -deferred_sum(x[1][0])))
    best = min(results, key=lambda x: (x[1][1], -deferred_sum(x[1][0])))
    if len(results) > 1 and (worst[1][1] != best[1][1] or deferred_sum(worst[1][0]) != deferred_sum(best[1][0])):
        assumptions.append(
            "Terms don't say whether percentages apply before or after other discounts; "
            f"payable could be {fmt(best[1][1])} to {fmt(worst[1][1])}. Showing the higher."
        )
    applied, payable, risk_payable = worst[1]
    for a in applied:
        if by_id[a.offer_id].lifecycle is Lifecycle.STALE:
            assumptions.append(f"{a.title}: not re-confirmed recently")

    cash = _cash(payable, inst)
    expected_def = deferred_sum(applied)
    risk_def = sum(a.amount * a.settle_bp // 10_000 * a.confidence_bp // 10_000 for a in applied if a.deferred)
    success = 10_000
    for a in applied:
        if not a.deferred:
            success = success * a.confidence_bp // 10_000
    rules_confirmed = not assumptions
    if inst is not None and inst.hypothetical:
        assumptions.append(f"Requires a payment method you have not added: {inst.label}")
        rules_confirmed = False
    return Scenario(
        instrument=inst,
        applied=applied,
        price=product.price,
        instant_savings=product.price - payable,
        checkout_payable=payable,
        cash_cost=cash,
        funding_savings=payable - cash,
        expected_deferred=expected_def,
        risk_adjusted_deferred=risk_def,
        effective_if_all_paid=cash - expected_def,
        risk_adjusted_cost=_cash(risk_payable, inst) - risk_def,
        optimistic_payable=best[1][1],
        success_probability_bp=success,
        assumptions=assumptions,
        rules_confirmed=rules_confirmed,
        steps=_steps(applied, by_id, inst),
    )


# --- Alternative ranking strategies (used by Experiment C) -------------------

def rank_by(result: OptimizationResult, strategy: str) -> Scenario:
    s = result.scenarios
    if strategy == "highest_advertised_discount":
        return max(s, key=lambda x: x.advertised_discount)
    if strategy == "lowest_checkout_price":
        return min(s, key=lambda x: x.checkout_payable)
    if strategy == "lowest_effective_if_all_paid":
        return min(s, key=lambda x: x.effective_if_all_paid)
    if strategy == "risk_adjusted":
        return s[0]
    raise ValueError(strategy)

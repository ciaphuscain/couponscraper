"""Cross-product insights: which payment method to use, and which you lack.

Uses only confirmed scenarios for cards you hold. For cards you don't hold,
a scenario counts only if the missing card is its *sole* assumption, so the
"you'd save ₹X with an ICICI card" figure never rests on unconfirmed stacking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import repo
from .db import Database
from .optimizer import optimize

HYPOTHETICAL_PREFIX = "Requires a payment method you have not added"


@dataclass
class CardStat:
    label: str
    products_best: int = 0  # products where this card gives the lowest confirmed cash cost
    savings_where_best: int = 0  # paise, summed over those products
    products: list[str] = field(default_factory=list)


@dataclass
class MissingCard:
    label: str
    products: int = 0
    extra_savings: int = 0  # vs. the best card you hold, summed
    examples: list[str] = field(default_factory=list)


@dataclass
class CardReport:
    products_evaluated: int
    owned: list[CardStat]
    missing: list[MissingCard]


def card_report(db: Database, now: datetime) -> CardReport:
    settings = repo.get_settings(db)
    user = repo.user_profile(settings)
    stacking = repo.stacking_defaults(settings)
    owned: dict[str, CardStat] = {i.id: CardStat(i.label) for i in user.instruments}
    missing: dict[str, MissingCard] = {}
    n = 0
    for w in repo.watchlist_rows(db):
        ctx = repo.product_context(db, w["asin"], now, settings)
        if ctx is None or ctx.price_is_stale:
            continue
        n += 1
        offers = repo.offers_for_asin(db, w["asin"], now)
        r = optimize(ctx, offers, user, now, include_hypothetical=True, stacking_defaults=stacking)
        name = w["title"] or w["asin"]
        confirmed = [s for s in r.scenarios if s.rules_confirmed]
        best_owned = min(confirmed, key=lambda s: s.cash_cost) if confirmed else None
        if best_owned is not None and best_owned.instrument is not None and best_owned.cash_cost < ctx.price:
            # Credit the card only if it beats paying with any other held card.
            others = [s for s in confirmed if s.instrument is None or s.instrument.id != best_owned.instrument.id]
            if not others or best_owned.cash_cost < min(s.cash_cost for s in others):
                st = owned.get(best_owned.instrument.id)
                if st is not None:
                    st.products_best += 1
                    st.savings_where_best += ctx.price - best_owned.cash_cost
                    st.products.append(name)
        floor = best_owned.cash_cost if best_owned else ctx.price
        per_missing: dict[str, int] = {}
        for s in r.scenarios:
            inst = s.instrument
            if inst is None or not inst.hypothetical:
                continue
            if any(not a.startswith(HYPOTHETICAL_PREFIX) for a in s.assumptions):
                continue
            gain = floor - s.cash_cost
            if gain > 0 and gain > per_missing.get(inst.label, 0):
                per_missing[inst.label] = gain
        for label, gain in per_missing.items():
            m = missing.setdefault(label, MissingCard(label.replace(" (not in your wallet)", "")))
            m.products += 1
            m.extra_savings += gain
            if len(m.examples) < 3:
                m.examples.append(name)
    return CardReport(
        n,
        sorted(owned.values(), key=lambda c: (-c.savings_where_best, c.label)),
        sorted(missing.values(), key=lambda m: -m.extra_savings),
    )

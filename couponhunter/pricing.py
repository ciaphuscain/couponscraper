"""Price-history statistics and deal-quality judgement.

Claims are bounded by coverage: we say "lowest price in the 41 days we have
observed (23 observations)", never "all-time low". An advertised "% off" a
list/MRP reference price is compared against our own observed median, because
reference prices are set by sellers and are frequently inflated.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Observation:
    price: int
    observed_at: datetime
    list_price: int | None = None


@dataclass
class PriceStats:
    count: int
    coverage_days: float
    current: int | None
    minimum: int | None
    median: int | None
    p25: int | None
    maximum: int | None
    trend_7d_bp: int | None  # change of current vs. median of the previous 7 days, basis points
    volatility_bp: int | None  # coefficient of variation
    raised_recently: bool  # price went up >=10% within the last 14 days (pre-sale inflation pattern)


@dataclass
class DealVerdict:
    is_good_deal: bool
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


MIN_OBS_FOR_CLAIMS = 5
MIN_DAYS_FOR_LOWEST_CLAIM = 14


def compute_stats(obs: list[Observation], now: datetime) -> PriceStats:
    obs = sorted(obs, key=lambda o: o.observed_at)
    if not obs:
        return PriceStats(0, 0.0, None, None, None, None, None, None, None, False)
    prices = [o.price for o in obs]
    cov = (obs[-1].observed_at - obs[0].observed_at).total_seconds() / 86_400
    cur = prices[-1]
    prior7 = [o.price for o in obs[:-1] if o.observed_at >= now - timedelta(days=7)]
    trend = (cur - statistics.median(prior7)) * 10_000 // statistics.median(prior7) if prior7 else None
    vol = None
    if len(prices) >= 3:
        mean = statistics.fmean(prices)
        vol = int(statistics.pstdev(prices) / mean * 10_000) if mean else None
    recent = [o for o in obs if o.observed_at >= now - timedelta(days=14)]
    raised = False
    lowest_so_far = None
    for o in recent:
        if lowest_so_far is not None and o.price >= lowest_so_far * 11 // 10:
            raised = True
        lowest_so_far = o.price if lowest_so_far is None else min(lowest_so_far, o.price)
    q = statistics.quantiles(prices, n=4) if len(prices) >= 4 else None
    return PriceStats(
        count=len(prices), coverage_days=round(cov, 1), current=cur, minimum=min(prices),
        median=int(statistics.median(prices)), p25=int(q[0]) if q else None, maximum=max(prices),
        trend_7d_bp=int(trend) if trend is not None else None, volatility_bp=vol, raised_recently=raised,
    )


def judge_price(stats: PriceStats, effective_price: int, list_price: int | None = None) -> DealVerdict:
    """Is `effective_price` genuinely good relative to what we've observed?"""
    v = DealVerdict(False)
    if stats.count < MIN_OBS_FOR_CLAIMS:
        v.caveats.append(f"only {stats.count} price observation(s): too little history to judge")
        if list_price and effective_price < list_price:
            v.caveats.append("discount is relative to the seller's reference price, which is not independently verified")
        return v
    assert stats.median is not None and stats.minimum is not None
    below_median_bp = (stats.median - effective_price) * 10_000 // stats.median
    # A deal must clear normal price noise: at least 5% below the median, and
    # at least twice the observed volatility. A new "lowest" that is within
    # noise is reported as a caveat, not as a deal.
    threshold = max(500, 2 * (stats.volatility_bp or 0))
    if below_median_bp >= threshold:
        v.is_good_deal = True
        v.reasons.append(f"{below_median_bp / 100:.1f}% below the median observed price")
        if effective_price <= stats.minimum and stats.coverage_days >= MIN_DAYS_FOR_LOWEST_CLAIM:
            v.reasons.append(f"lowest in the {stats.coverage_days:.0f} days observed ({stats.count} observations)")
    elif effective_price <= stats.minimum:
        v.caveats.append("matches the lowest observed price, but within normal price variation")
    if v.is_good_deal and stats.coverage_days < MIN_DAYS_FOR_LOWEST_CLAIM:
        v.caveats.append(f"history covers only {stats.coverage_days:.0f} days")
    if stats.raised_recently:
        v.caveats.append("price was raised in the last 14 days; the 'discount' may just undo that rise")
    if list_price and stats.median and list_price > stats.median * 13 // 10:
        v.caveats.append("reference/list price is >30% above the typical observed price; advertised % off is inflated")
    if not v.is_good_deal and below_median_bp < 0:
        v.caveats.append("effective price is above the median observed price")
    return v

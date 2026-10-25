"""Synthetic test data. Everything here is fictional and flagged is_synthetic."""

from datetime import datetime, timedelta, timezone

from couponhunter.domain import (
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
from couponhunter.money import rupees

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ASIN = "B0TESTASIN"

HDFC_CC = PaymentInstrument("hdfc-cc", "HDFC Credit Card", "credit_card", bank="hdfc", network="visa")
SBI_DC = PaymentInstrument("sbi-dc", "SBI Debit Card", "debit_card", bank="sbi", network="rupay")


def product(price_rupees=10_000, **kw) -> ProductContext:
    return ProductContext(asin=kw.pop("asin", ASIN), price=rupees(price_rupees), title="Test Headphones", **kw)


def user(*instruments, **kw) -> UserProfile:
    return UserProfile(instruments=tuple(instruments), **kw)


def offer(id, kind, **kw) -> Offer:
    kw.setdefault("title", id)
    kw.setdefault("authenticity", Authenticity.OFFICIAL)
    kw.setdefault("valid_until", NOW + timedelta(days=3))
    kw.setdefault("is_synthetic", True)
    return Offer(id=id, kind=kind, **kw)


def _with(defaults: dict, kw: dict) -> dict:
    return {**defaults, **kw}


def coupon_500(**kw) -> Offer:
    return offer("coupon500", OfferKind.PRODUCT_COUPON, **_with(dict(
        discount_type=DiscountType.FLAT, value=rupees(500), basis=Basis.PRICE, title="₹500 coupon"), kw))


def hdfc_10pct(**kw) -> Offer:
    return offer("hdfc10", OfferKind.BANK_INSTANT, **_with(dict(
        discount_type=DiscountType.PERCENT, value=1_000, max_discount=rupees(750), basis=Basis.AFTER_COUPONS,
        title="10% HDFC up to ₹750",
        payment=PaymentRequirement(banks=frozenset({"hdfc"}), instrument_types=frozenset({"credit_card"}))), kw))


def cashback_5pct(**kw) -> Offer:
    return offer("cb5", OfferKind.PORTAL_CASHBACK, **_with(dict(
        discount_type=DiscountType.PERCENT, value=500, max_discount=rupees(300), basis=Basis.PAYABLE,
        provider="ExamplePortal", title="5% cashback up to ₹300", settlement_probability_bp=8_000), kw))


YES, NO, UNKNOWN = Tri.YES, Tri.NO, Tri.UNKNOWN

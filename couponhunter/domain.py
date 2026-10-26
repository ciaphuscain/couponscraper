"""Pure domain model. No I/O, no database, no HTTP.

Design note: an offer's *authenticity*, its *lifecycle*, a user's
*eligibility* and *redemption evidence* are independent dimensions. They are
kept as separate fields instead of being collapsed into one "confidence"
state, because "source says it exists" and "it applied at my checkout" are
different claims backed by different evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OfferKind(StrEnum):
    PRODUCT_COUPON = "product_coupon"  # clip-able coupon shown on the product page
    PROMO_CODE = "promo_code"  # public code typed at checkout
    DEAL_PRICE = "deal_price"  # lightning/limited-time deal; already in the observed price
    BANK_INSTANT = "bank_instant"  # instant discount tied to a payment instrument
    BANK_CASHBACK = "bank_cashback"  # deferred credit from a bank/card issuer
    AMAZON_PAY_CASHBACK = "amazon_pay_cashback"  # deferred Amazon Pay balance reward
    PORTAL_CASHBACK = "portal_cashback"  # third-party cashback site; needs a tracked journey


INSTANT_KINDS = frozenset({OfferKind.PRODUCT_COUPON, OfferKind.PROMO_CODE, OfferKind.BANK_INSTANT})
DEFERRED_KINDS = frozenset(
    {OfferKind.BANK_CASHBACK, OfferKind.AMAZON_PAY_CASHBACK, OfferKind.PORTAL_CASHBACK}
)
PAYMENT_KINDS = frozenset({OfferKind.BANK_INSTANT, OfferKind.BANK_CASHBACK})

# Order in which instant discounts are applied at checkout. This mirrors how
# amazon.in presents them (item coupon, then code, then payment offer) but is
# an assumption; the *basis* field on each offer decides what it is computed on.
APPLICATION_ORDER = [
    OfferKind.DEAL_PRICE,
    OfferKind.PRODUCT_COUPON,
    OfferKind.PROMO_CODE,
    OfferKind.BANK_INSTANT,
    OfferKind.BANK_CASHBACK,
    OfferKind.AMAZON_PAY_CASHBACK,
    OfferKind.PORTAL_CASHBACK,
]


class DiscountType(StrEnum):
    FLAT = "flat"  # value is paise
    PERCENT = "percent"  # value is basis points
    NONE = "none"  # informational (e.g. a deal price already reflected in price)


class Basis(StrEnum):
    """What amount a percentage, cap and minimum-spend are evaluated against."""

    PRICE = "price"  # item price before any offer
    AFTER_COUPONS = "after_coupons"  # after product coupon + promo code
    PAYABLE = "payable"  # final checkout amount after every instant discount
    UNKNOWN = "unknown"  # terms do not say; optimizer evaluates every option


class Authenticity(StrEnum):
    UNVERIFIED = "unverified"  # seen in a feed / pasted text only
    SOURCE_LISTED = "source_listed"  # listed by a source with a reliability record
    OFFICIAL = "official"  # observed on amazon.in itself or the issuer's own site
    DISPUTED = "disputed"  # sources disagree or user reported it fake
    INVALID = "invalid"


class Lifecycle(StrEnum):
    ACTIVE = "active"
    NOT_STARTED = "not_started"
    STALE = "stale"  # not re-confirmed recently; never treated as active for alerts
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"  # the source stopped listing it


class Tri(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PaymentRequirement:
    """Empty sets mean 'no restriction on this attribute'."""

    banks: frozenset[str] = frozenset()  # normalised lowercase, e.g. {"hdfc"}
    instrument_types: frozenset[str] = frozenset()  # credit_card, debit_card, upi, netbanking, emi, amazon_pay_balance
    networks: frozenset[str] = frozenset()  # visa, mastercard, rupay, amex


@dataclass(frozen=True)
class PaymentInstrument:
    """A payment method the user owns, described categorically.

    Never holds card numbers, CVVs or expiry dates: only the attributes that
    offer terms are written against.
    """

    id: str
    label: str
    type: str
    bank: str | None = None
    network: str | None = None
    # Cash cost per unit of face value. 10_000 = paying ₹1 costs ₹1. A gift card
    # bought at 3% off face value is 9_700. This is how discounted gift cards are
    # accounted for without double-counting their face value.
    cost_ratio_bp: int = 10_000
    hypothetical: bool = False  # suggested by the optimizer, not in the user's wallet


@dataclass
class Offer:
    id: str
    kind: OfferKind
    title: str
    provider: str = "amazon"
    discount_type: DiscountType = DiscountType.FLAT
    value: int = 0  # paise for FLAT, basis points for PERCENT
    max_discount: int | None = None  # paise cap
    min_order: int | None = None  # paise, compared against `basis`
    basis: Basis = Basis.UNKNOWN
    payment: PaymentRequirement | None = None
    eligibility: dict[str, Any] | None = None  # rule tree, see rules.py
    new_customer_only: bool = False
    prime_only: bool = False
    account_specific: bool = False  # targeted offers: never assumed to apply to the user
    one_time_use: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    code: str | None = None
    redemption_url: str | None = None
    redemption_steps: list[str] = field(default_factory=list)
    stacks_with: dict[str, Tri] = field(default_factory=dict)  # kind -> yes/no/unknown
    excludes: frozenset[str] = frozenset()  # offer ids this cannot combine with
    authenticity: Authenticity = Authenticity.UNVERIFIED
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    # Evidence from the user's own redemption attempts.
    redeemed_ok: int = 0
    redeemed_failed: int = 0
    # Probability (bp) that a deferred benefit actually settles. None -> kind default.
    settlement_probability_bp: int | None = None
    source_text: str = ""
    is_synthetic: bool = False  # test fixture / demo data; shown as such in the UI
    # >0 when this offer was observed on *other* product pages and is only
    # inferred to possibly apply here (always eligibility-unknown).
    seen_on_other_products: int = 0


@dataclass(frozen=True)
class ProductContext:
    asin: str
    price: int  # paise, the currently observed selling price
    title: str = ""
    category: str | None = None
    seller: str | None = None
    brand: str | None = None
    price_observed_at: datetime | None = None
    price_is_stale: bool = False


@dataclass(frozen=True)
class UserProfile:
    is_prime: bool | None = None
    is_new_customer: bool | None = None
    instruments: tuple[PaymentInstrument, ...] = ()

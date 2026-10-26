"""Turn untrusted source records into validated, normalised offers.

Two entry points:
  * normalize_record(dict)  - structured input (JSON/CSV import, extension capture)
  * parse_offer_text(str)   - deterministic parser for amazon.in-style offer text

Everything coming from a source is treated as hostile: lengths are bounded,
URLs are checked against an allowlist, codes are restricted to a safe
alphabet, and nothing is ever evaluated or rendered as HTML.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from . import rules
from .amazon import is_valid_asin
from .domain import Basis, DiscountType, OfferKind, PaymentRequirement, Tri
from .money import parse_inr, rupees

MAX_TEXT = 2_000
MAX_TITLE = 200
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")

# Domains a redemption link may point to. Anything else is dropped, so a
# poisoned feed cannot turn the dashboard into a phishing launcher.
REDEMPTION_DOMAINS = frozenset({
    "amazon.in", "www.amazon.in", "m.amazon.in",
    # Issuer offer pages (extend via settings `extra_redemption_domains`).
    "www.hdfcbank.com", "www.icicibank.com", "www.sbicard.com", "www.axisbank.com",
    "www.kotak.com", "www.idfcfirstbank.com",
})

BANKS = {
    "hdfc": r"hdfc",
    "icici": r"icici",
    "sbi": r"\bsbi\b|state bank",
    "axis": r"\baxis\b",
    "kotak": r"kotak",
    "idfc": r"idfc",
    "yes": r"\byes bank\b",
    "indusind": r"indusind",
    "au": r"\bau (?:small finance )?bank\b",
    "rbl": r"\brbl\b",
    "hsbc": r"\bhsbc\b",
    "sc": r"standard chartered",
    "bob": r"bank of baroda|bobcard",
    "federal": r"\bfederal bank\b",
    "onecard": r"onecard",
}
NETWORKS = {"rupay": r"rupay", "visa": r"\bvisa\b", "mastercard": r"mastercard", "amex": r"american express|\bamex\b"}


class NormalizationError(ValueError):
    pass


@dataclass
class NormalizedOffer:
    kind: OfferKind
    title: str
    provider: str
    discount_type: DiscountType
    value: int
    max_discount: int | None = None
    min_order: int | None = None
    basis: Basis = Basis.UNKNOWN
    payment: PaymentRequirement | None = None
    eligibility: dict | None = None
    asins: tuple[str, ...] = ()
    new_customer_only: bool = False
    prime_only: bool = False
    account_specific: bool = False
    one_time_use: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    code: str | None = None
    redemption_url: str | None = None
    redemption_steps: list[str] = field(default_factory=list)
    stacks_with: dict[str, Tri] = field(default_factory=dict)
    settlement_probability_bp: int | None = None
    source_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Identity of an offer across sources.

        Deliberately excludes title, wording and expiry: different sites word
        the same offer differently and often disagree on the end date. Two
        records with the same economics, scope, code and payment restriction
        are the same offer; expiry disagreements are resolved conservatively
        when aggregating (earliest stated expiry wins).
        """
        pay = None
        if self.payment:
            pay = [sorted(self.payment.banks), sorted(self.payment.instrument_types), sorted(self.payment.networks)]
        key = [
            self.kind.value,
            _slug(self.provider),
            self.discount_type.value,
            self.value,
            self.max_discount,
            self.min_order,
            (self.code or "").upper(),
            pay,
            sorted(self.asins),
            json.dumps(self.eligibility, sort_keys=True) if self.eligibility else None,
            self.new_customer_only,
            self.prime_only,
        ]
        return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()[:32]


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    s = re.sub(r"\b(bank|ltd|limited|india|pvt|credit|card|cards)\b", " ", s)
    return " ".join(s.split())


def clean_text(s: Any, limit: int) -> str:
    if s is None:
        return ""
    s = str(s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)  # control chars
    s = " ".join(s.split())
    return s[:limit]


def safe_redemption_url(url: str | None, extra_domains: frozenset[str] = frozenset()) -> str | None:
    if not url:
        return None
    url = str(url).strip()
    if len(url) > 2048:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        return None
    if host not in REDEMPTION_DOMAINS and host not in extra_domains:
        return None
    return url


def safe_code(code: Any) -> str | None:
    if code is None or code == "":
        return None
    c = str(code).strip().upper()
    if not CODE_RE.match(c):
        raise NormalizationError(f"coupon code has unexpected characters or length: {c[:40]!r}")
    return c


def parse_time(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            # Date-only expiry: assume end of that day in India (IST, UTC+5:30).
            s += "T23:59:59+05:30"
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise NormalizationError(f"unparseable date {s!r}") from e
    if dt.tzinfo is None:
        raise NormalizationError("dates must include a timezone offset (e.g. +05:30)")
    return dt.astimezone(timezone.utc)


def _amount(v: Any, what: str) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return rupees(v)
    try:
        return parse_inr(str(v))
    except ValueError as e:
        raise NormalizationError(f"bad {what}: {v!r}") from e


def _discount(v: Any) -> tuple[DiscountType, int]:
    if isinstance(v, dict):
        t = DiscountType(v.get("type", "flat"))
        raw = v.get("value")
        if t is DiscountType.PERCENT:
            return t, _percent_bp(raw)
        if t is DiscountType.NONE:
            return t, 0
        amt = _amount(raw, "discount")
        if amt is None:
            raise NormalizationError("discount value missing")
        return t, amt
    s = str(v or "").strip()
    if not s:
        raise NormalizationError("discount missing")
    if s.endswith("%"):
        return DiscountType.PERCENT, _percent_bp(s)
    return DiscountType.FLAT, _amount(s, "discount") or 0


def _percent_bp(raw: Any) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d{1,2})?)\s*%?\s*", str(raw))
    if not m:
        raise NormalizationError(f"bad percentage {raw!r}")
    whole, _, frac = m.group(1).partition(".")
    bp = int(whole) * 100 + int((frac + "00")[:2])
    if not 0 < bp <= 9_000:
        raise NormalizationError(f"implausible percentage {raw!r}")
    return bp


def normalize_record(rec: dict[str, Any], extra_domains: frozenset[str] = frozenset()) -> NormalizedOffer:
    """Validate one structured record. Raises NormalizationError on rejects."""
    if not isinstance(rec, dict):
        raise NormalizationError("record must be an object")
    text = clean_text(rec.get("text"), MAX_TEXT)
    if "kind" not in rec and text:
        parsed = parse_offer_text(text)
        merged = {**_as_record(parsed), **{k: v for k, v in rec.items() if k != "text"}}
        out = normalize_record(merged, extra_domains)
        out.source_text = text
        out.warnings = parsed.warnings + out.warnings
        return out
    try:
        kind = OfferKind(rec.get("kind"))
    except ValueError as e:
        raise NormalizationError(f"unknown kind {rec.get('kind')!r}") from e
    title = clean_text(rec.get("title") or text, MAX_TITLE)
    if not title:
        raise NormalizationError("title missing")
    provider = clean_text(rec.get("provider") or "Amazon", 80)
    if kind is OfferKind.DEAL_PRICE:
        dtype, value = DiscountType.NONE, 0
    else:
        dtype, value = _discount(rec.get("discount"))
    warnings: list[str] = []
    max_discount = _amount(rec.get("max_discount"), "max_discount")
    if dtype is DiscountType.PERCENT and max_discount is None and kind is not OfferKind.DEAL_PRICE:
        warnings.append("percentage offer with no stated cap")
    try:
        basis = Basis(rec.get("basis") or "unknown")
    except ValueError as e:
        raise NormalizationError(f"bad basis {rec.get('basis')!r}") from e

    payment = None
    pay = rec.get("payment")
    if pay:
        if not isinstance(pay, dict):
            raise NormalizationError("payment must be an object")
        payment = PaymentRequirement(
            banks=frozenset(_slug_bank(b) for b in pay.get("banks", [])),
            instrument_types=frozenset(str(t).lower() for t in pay.get("types", [])),
            networks=frozenset(str(n).lower() for n in pay.get("networks", [])),
        )
    elif kind in (OfferKind.BANK_INSTANT, OfferKind.BANK_CASHBACK):
        raise NormalizationError("bank offer without a payment restriction")

    asins = tuple(sorted({str(a).strip().upper() for a in rec.get("asins", []) or []}))
    bad = [a for a in asins if not is_valid_asin(a)]
    if bad:
        raise NormalizationError(f"invalid ASINs: {bad[:3]}")
    elig = rec.get("eligibility")
    if elig is not None:
        try:
            rules.validate(elig)
        except rules.RuleError as e:
            raise NormalizationError(f"bad eligibility rule: {e}") from e

    stacks = {}
    for k, v in (rec.get("stacks_with") or {}).items():
        try:
            stacks[OfferKind(k).value] = Tri(v)
        except ValueError as e:
            raise NormalizationError(f"bad stacks_with entry {k}={v}") from e

    url_in = rec.get("redemption_url")
    url = safe_redemption_url(url_in, extra_domains)
    if url_in and not url:
        warnings.append("redemption link dropped: not https on an allowlisted domain")

    settle = rec.get("settlement_probability_bp")
    if settle is not None and not (isinstance(settle, int) and 0 <= settle <= 10_000):
        raise NormalizationError("settlement_probability_bp must be 0..10000")

    valid_from, valid_until = parse_time(rec.get("valid_from")), parse_time(rec.get("valid_until"))
    if valid_from and valid_until and valid_until <= valid_from:
        raise NormalizationError("valid_until is not after valid_from")

    return NormalizedOffer(
        kind=kind,
        title=title,
        provider=provider,
        discount_type=dtype,
        value=value,
        max_discount=max_discount,
        min_order=_amount(rec.get("min_order"), "min_order"),
        basis=basis,
        payment=payment,
        eligibility=elig,
        asins=asins,
        new_customer_only=bool(rec.get("new_customer_only", False)),
        prime_only=bool(rec.get("prime_only", False)),
        account_specific=bool(rec.get("account_specific", False)),
        one_time_use=bool(rec.get("one_time_use", False)),
        valid_from=valid_from,
        valid_until=valid_until,
        code=safe_code(rec.get("code")),
        redemption_url=url,
        redemption_steps=[clean_text(s, 200) for s in (rec.get("redemption_steps") or [])][:5],
        stacks_with=stacks,
        settlement_probability_bp=settle,
        source_text=text,
        warnings=warnings,
    )


def _slug_bank(b: Any) -> str:
    s = str(b).lower()
    for key, pat in BANKS.items():
        if re.search(pat, s):
            return key
    return _slug(s)


# --- Deterministic text parser --------------------------------------------------

_PCT = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*%")
_FLAT = re.compile(r"(?:flat|save|get|instant discount of|discount of)?\s*(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)", re.I)
_CAP = re.compile(r"up\s*to\s*(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)", re.I)
_MIN = re.compile(
    r"(?:min(?:imum)?\.?\s*(?:purchase|order|cart|transaction|txn)\s*(?:value|amount)?\s*(?:of)?|on orders? (?:of|above)|above)\s*(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)",
    re.I,
)
_CODE = re.compile(r"\b(?:use|apply|with)?\s*(?:promo\s*)?code[:\s]+([A-Z0-9][A-Z0-9_-]{2,31})\b")


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_EXPIRY_LEAD = r"(?:valid\s+(?:till|until|up\s*to|upto|through)|offer\s+(?:ends|valid\s+till)(?:\s+on)?|ends\s+on|expires(?:\s+on)?|till|until)\s*:?\s*"
_D_MON_Y = re.compile(_EXPIRY_LEAD + r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})", re.I)
_MON_D_Y = re.compile(_EXPIRY_LEAD + r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I)
_NUMERIC = re.compile(_EXPIRY_LEAD + r"(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b", re.I)  # Indian dd/mm/yyyy
_ISO = re.compile(_EXPIRY_LEAD + r"(20\d{2})-(\d{2})-(\d{2})\b", re.I)


def extract_expiry(text: str) -> str | None:
    """'valid till 31st Oct 2026' -> '2026-10-31'. Only dates with an explicit year
    after an expiry phrase; anything ambiguous returns None rather than a guess."""
    import datetime as _dt

    for pat, order in ((_ISO, "ymd"), (_D_MON_Y, "dmy_name"), (_MON_D_Y, "mdy_name"), (_NUMERIC, "dmy")):
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if order == "ymd":
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif order == "dmy_name":
                d, mo, y = int(g[0]), _MONTHS.get(g[1][:3].lower(), 0), int(g[2])
            elif order == "mdy_name":
                mo, d, y = _MONTHS.get(g[0][:3].lower(), 0), int(g[1]), int(g[2])
            else:
                d, mo, y = int(g[0]), int(g[1]), int(g[2])
            return _dt.date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


@dataclass
class ParsedText:
    fields: dict[str, Any]
    warnings: list[str]
    confidence_bp: int  # how much of the text the parser could account for


def parse_offer_text(text: str) -> ParsedText:
    """Parse common amazon.in offer phrasing into structured fields.

    Conservative by design: anything not understood is reported as a warning
    and lowers parse confidence; nothing is inferred that the text doesn't say.
    """
    t = clean_text(text, MAX_TEXT)
    low = t.lower()
    f: dict[str, Any] = {"title": t[:MAX_TITLE], "text": t}
    warnings: list[str] = []
    score = 10_000

    banks = sorted(k for k, pat in BANKS.items() if re.search(pat, low))
    networks = sorted(k for k, pat in NETWORKS.items() if re.search(pat, low))
    types = []
    if "credit card" in low:
        types.append("credit_card")
    if "debit card" in low:
        types.append("debit_card")
    if re.search(r"\bupi\b", low):
        types.append("upi")
    if "net banking" in low or "netbanking" in low:
        types.append("netbanking")
    if re.search(r"(?<!non-)(?<!non )\bemi\b", low):
        types.append("emi")

    if "coupon" in low and not banks:
        kind = OfferKind.PRODUCT_COUPON
    elif _CODE.search(t):
        kind = OfferKind.PROMO_CODE
    elif "instant discount" in low or ("discount" in low and banks):
        kind = OfferKind.BANK_INSTANT
    elif ("cashback" in low or "back" in low) and ("amazon pay balance" in low or ("amazon pay" in low and not banks)):
        kind = OfferKind.AMAZON_PAY_CASHBACK
    elif ("cashback" in low or "% back" in low) and (banks or types):
        kind = OfferKind.BANK_CASHBACK
    elif "cashback" in low:
        kind = OfferKind.AMAZON_PAY_CASHBACK if "amazon pay" in low else OfferKind.PORTAL_CASHBACK
    else:
        raise NormalizationError("could not determine offer type from text")
    f["kind"] = kind.value

    cap = _CAP.search(t)
    pct = _PCT.search(t)
    if pct:
        f["discount"] = f"{pct.group(1)}%"
    else:
        flats = [m for m in _FLAT.finditer(t) if not (cap and cap.start() <= m.start(1) < cap.end())]
        mins = _MIN.search(t)
        flats = [m for m in flats if not (mins and mins.start() <= m.start() <= mins.end())]
        if flats:
            f["discount"] = f"₹{flats[0].group(1)}"
        elif cap:
            # "Up to ₹300 cashback" with no rate: the cap is the only stated
            # amount. Record it as the flat maximum, flag the uncertainty.
            f["discount"] = f"₹{cap.group(1)}"
            warnings.append("only an 'up to' amount is stated; actual benefit may be lower")
            score -= 3_000
            cap = None
        else:
            raise NormalizationError("no discount amount found in text")
    if cap:
        f["max_discount"] = f"₹{cap.group(1)}"
    mn = _MIN.search(t)
    if mn:
        f["min_order"] = f"₹{mn.group(1)}"

    if kind in (OfferKind.BANK_INSTANT, OfferKind.BANK_CASHBACK):
        if not banks and not types:
            raise NormalizationError("bank offer text names no bank or instrument")
        f["payment"] = {"banks": banks, "types": types, "networks": networks}
        f["provider"] = (banks[0].upper() + " Bank") if banks else "Bank offer"
    elif kind is OfferKind.AMAZON_PAY_CASHBACK:
        f["provider"] = "Amazon Pay"
        if types:
            f["payment"] = {"banks": banks, "types": types, "networks": networks}
    if kind is OfferKind.PROMO_CODE:
        m = _CODE.search(t)
        f["code"] = m.group(1) if m else None

    if "prime member" in low or "for prime" in low:
        f["prime_only"] = True
    if re.search(r"new (?:customer|user)s?|first (?:order|purchase|transaction)", low):
        f["new_customer_only"] = True
    if re.search(r"select(?:ed)? (?:customers|users)|eligible customers|targeted", low):
        f["account_specific"] = True
        warnings.append("offer appears targeted to selected accounts")
    if "once per" in low or "one time" in low or "once" in low and "customer" in low:
        f["one_time_use"] = True
    if "excluding" in low or "except" in low or "not applicable" in low:
        warnings.append("text mentions exclusions not captured as rules: review manually")
        score -= 2_000
    if "non-emi" in low or "non emi" in low:
        warnings.append("non-EMI transactions only")
    if f.get("discount", "").endswith("%") and "max_discount" not in f:
        score -= 1_000
    expiry = extract_expiry(t)
    if expiry:
        f["valid_until"] = expiry
    return ParsedText(fields=f, warnings=warnings, confidence_bp=max(score, 0))


def _as_record(p: ParsedText) -> dict[str, Any]:
    return {k: v for k, v in p.fields.items() if k != "text"}

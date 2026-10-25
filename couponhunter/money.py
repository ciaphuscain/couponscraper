"""Integer money helpers. Paise in, paise out; rounding is explicit."""

from __future__ import annotations

import re

PAISE_PER_RUPEE = 100
BP_PER_UNIT = 10_000  # 100% == 10_000 basis points

_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


def rupees(value: int | str) -> int:
    """Convert a rupee amount (int or decimal string) to paise exactly."""
    if isinstance(value, bool):
        raise TypeError("bool is not a money amount")
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    if isinstance(value, str):
        return parse_inr(value)
    raise TypeError(f"unsupported money type: {type(value).__name__}")


def parse_inr(text: str) -> int:
    """Parse '₹1,299.50', 'Rs. 500', 'INR 750', '10000' into paise.

    Raises ValueError rather than guessing when no amount is present.
    """
    m = _AMOUNT_RE.search(text.strip())
    if not m:
        raise ValueError(f"no INR amount in {text!r}")
    raw = m.group(1).replace(",", "")
    if "." in raw:
        whole, frac = raw.split(".", 1)
        frac = (frac + "0")[:2]
    else:
        whole, frac = raw, "00"
    return int(whole) * PAISE_PER_RUPEE + int(frac)


def percent_of(amount_paise: int, rate_bp: int) -> int:
    """rate_bp of amount, rounded down to the paisa.

    Rounding down is deliberate: issuers floor discounts in practice far more
    often than they round up, and under-promising is the safe direction.
    """
    if amount_paise < 0 or rate_bp < 0:
        raise ValueError("negative amount or rate")
    return amount_paise * rate_bp // BP_PER_UNIT


def fmt(paise: int) -> str:
    """Format paise as an Indian-grouped rupee string, e.g. ₹1,23,456.50."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    whole, frac = divmod(paise, PAISE_PER_RUPEE)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}₹{s}" + (f".{frac:02d}" if frac else "")

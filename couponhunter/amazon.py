"""Amazon India URL validation and ASIN extraction.

Nothing here performs network I/O. Submitted URLs are parsed, never fetched,
which removes the SSRF surface from "add a product URL" entirely.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

MARKETPLACE = "amazon.in"
ALLOWED_HOSTS = frozenset({"amazon.in", "www.amazon.in", "m.amazon.in", "smile.amazon.in"})
# Short links resolve via redirect; we refuse to follow them server-side.
SHORTLINK_HOSTS = frozenset({"amzn.in", "amzn.to", "a.co"})

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_PATH_PATTERNS = [
    re.compile(r"/dp/([A-Za-z0-9]{10})(?:[/?]|$)"),
    re.compile(r"/gp/product/([A-Za-z0-9]{10})(?:[/?]|$)"),
    re.compile(r"/gp/aw/d/([A-Za-z0-9]{10})(?:[/?]|$)"),
    re.compile(r"/exec/obidos/(?:ASIN|asin)/([A-Za-z0-9]{10})(?:[/?]|$)"),
    re.compile(r"/o/ASIN/([A-Za-z0-9]{10})(?:[/?]|$)"),
]


class InvalidAmazonURL(ValueError):
    pass


def is_valid_asin(asin: str) -> bool:
    return bool(ASIN_RE.match(asin or ""))


def normalize_asin(asin: str) -> str:
    a = (asin or "").strip().upper()
    if not is_valid_asin(a):
        raise InvalidAmazonURL(f"not a valid ASIN: {asin!r}")
    return a


def extract_asin(url: str) -> str:
    """Return the ASIN from an amazon.in product URL or raise InvalidAmazonURL.

    Only https/http amazon.in hosts are accepted; credentials, ports and
    look-alike hosts (amazon.in.evil.example) are rejected.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise InvalidAmazonURL("URL missing or too long")
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", url):
        return normalize_asin(url)
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise InvalidAmazonURL("URL must be http(s)")
    if parts.username or parts.password or parts.port:
        raise InvalidAmazonURL("URL must not contain credentials or a port")
    host = (parts.hostname or "").lower().rstrip(".")
    if host in SHORTLINK_HOSTS:
        raise InvalidAmazonURL(
            "short links are not expanded (that would need a network fetch); "
            "open it in your browser and paste the full amazon.in URL"
        )
    if host not in ALLOWED_HOSTS:
        raise InvalidAmazonURL(f"not an Amazon India URL: {host or '(no host)'}")
    for pat in _PATH_PATTERNS:
        m = pat.search(parts.path + ("?" if parts.query else ""))
        if m:
            return normalize_asin(m.group(1))
    raise InvalidAmazonURL("no ASIN found in URL path")


def canonical_product_url(asin: str) -> str:
    """Clean product URL with no tracking or affiliate parameters."""
    return urlunsplit(("https", "www.amazon.in", f"/dp/{normalize_asin(asin)}", "", ""))

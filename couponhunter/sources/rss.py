"""RSS/Atom feed adapter for deal publishers that syndicate via feeds.

A published feed is an explicit invitation to machine consumption, which is
why this is the one network adapter shipped. It is still disabled until the
owner records that they reviewed the publisher's terms (sources.terms_reviewed).

Feed items are free text written by third parties: every item goes through
the deterministic text parser, is marked UNVERIFIED, and links to the
publisher's own site are not used as redemption links.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ..domain import Authenticity
from .base import FetchResult, SourceError, SourceRecord
from .http import HttpClient

_TAG_RE = re.compile(r"<[^>]+>")
_ASIN_IN_LINK = re.compile(r"amazon\.in/(?:[^\s\"'<>]*/)?(?:dp|gp/product)/([A-Z0-9]{10})")


def _strip_html(s: str) -> str:
    return " ".join(_TAG_RE.sub(" ", s or "").split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class RssAdapter:
    adapter: ClassVar[str] = "rss"
    requires_terms_review: ClassVar[bool] = True

    def __init__(self, client_factory=HttpClient):
        self.client_factory = client_factory

    def fetch(self, config: dict[str, Any]) -> FetchResult:
        url = config.get("url", "")
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            raise SourceError("feed URL missing", kind="config")
        body = self.client_factory(host).get(url)
        return self.parse(body, url)

    def parse(self, body: bytes, url: str) -> FetchResult:
        head = body[:2048].lower()
        if b"<!doctype" in head or b"<!entity" in body.lower():
            # Feeds have no need for DTDs; refusing them closes entity-expansion attacks.
            raise SourceError("feed contains a DTD; refusing to parse", kind="schema")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise SourceError(f"malformed feed XML: {e}", kind="schema") from e
        items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
        if not items and _local(root.tag) not in ("rss", "feed", "RDF"):
            raise SourceError("document is not an RSS/Atom feed", kind="schema")
        tags: set[str] = set()
        records = []
        for it in items:
            fields: dict[str, str] = {}
            for child in it:
                name = _local(child.tag)
                tags.add(name)
                if name == "link" and child.get("href"):
                    fields["link"] = child.get("href", "")
                else:
                    fields.setdefault(name, "".join(child.itertext()))
            title = _strip_html(fields.get("title", ""))
            desc = _strip_html(fields.get("description") or fields.get("summary") or fields.get("content") or "")
            link = fields.get("link", "").strip()
            text = f"{title}. {desc}".strip(". ")[:2000]
            data: dict[str, Any] = {"text": text}
            m = _ASIN_IN_LINK.search(link) or _ASIN_IN_LINK.search(fields.get("description", ""))
            if m:
                data["asins"] = [m.group(1)]
            records.append(
                SourceRecord(data=data, evidence_type="feed_listing", authenticity=Authenticity.UNVERIFIED,
                             source_url=link[:2048] or url, raw_text=text)
            )
        fp = hashlib.sha256(",".join(sorted(tags)).encode()).hexdigest()[:16]
        return FetchResult(records=records, snapshot=True, data_origin="live", schema_fingerprint=fp)

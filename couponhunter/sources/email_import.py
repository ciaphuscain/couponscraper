"""Import offers from promotional emails you saved: .eml files, an .mbox export
(e.g. Google Takeout), or a folder of them.

No mailbox credentials are involved. You export or save the messages yourself.

Emails are untrusted (sender addresses are trivially spoofed), so:
  * every offer is UNVERIFIED and carries "From / Subject" as evidence;
  * links (including their text), attachments and images are ignored; HTML
    is reduced to text;
  * only lines that look like offers *and* pass the deterministic parser
    become records (so marketing fluff isn't counted as rejected offers);
  * messages older than `max_age_days` are skipped unless they state a
    future expiry, so an old promo can't be mistaken for a fresh one.
"""

from __future__ import annotations

import email
import email.policy
import html
import mailbox
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar, Iterator

from ..domain import Authenticity
from ..normalize import NormalizationError, extract_expiry, parse_offer_text
from .base import FetchResult, SourceError, SourceRecord

MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_MESSAGES = 5_000
MAX_BODY_CHARS = 200_000
_TAG = re.compile(r"<[^>]+>")
# Link text is dropped with the link: calls to action ("Claim 50% cashback now") are where phishing
# lives, and genuine offer terms are written in the body text.
_DROP = re.compile(r"<(script|style|head|a)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_BREAKS = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", re.I)
_MONEY = re.compile(r"(₹|\brs\.?|\binr\b|\d\s*%)", re.I)
_OFFERY = re.compile(r"\b(off|discount|cashback|coupon|code|save|back)\b", re.I)


def html_to_text(s: str) -> str:
    s = _DROP.sub(" ", s)
    s = _BREAKS.sub("\n", s)
    return html.unescape(_TAG.sub(" ", s))


def _body(msg: email.message.EmailMessage) -> str:
    plain, rich = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        (plain if ctype == "text/plain" else rich).append(content if isinstance(content, str) else "")
    # Use both: promotional mail often has a stub text/plain part ("view this email in HTML").
    # candidate_lines() drops the duplicates when both parts carry the same offer.
    text = "\n".join(plain + [html_to_text(r) for r in rich])
    return text[:MAX_BODY_CHARS]


def candidate_lines(text: str) -> list[str]:
    out, seen = [], set()
    for raw in re.split(r"[\n\r]+|(?<=[.!])\s+(?=[A-Z])", text):
        line = " ".join(raw.split())
        if not 12 <= len(line) <= 400 or not _MONEY.search(line) or not _OFFERY.search(line):
            continue
        if line.lower() in seen:
            continue
        seen.add(line.lower())
        out.append(line)
    return out


def iter_messages(path: Path) -> Iterator[email.message.EmailMessage]:
    files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".eml", ".mbox")) if path.is_dir() else [path]
    count = 0
    for f in files:
        if f.stat().st_size > MAX_FILE_BYTES:
            raise SourceError(f"{f.name} is larger than {MAX_FILE_BYTES // 2**20} MB", kind="config")
        if f.suffix.lower() == ".mbox":
            box = mailbox.mbox(str(f), factory=lambda fp: email.message_from_binary_file(fp, policy=email.policy.default),
                               create=False)
            try:
                for m in box:
                    count += 1
                    if count > MAX_MESSAGES:
                        return
                    yield m
            finally:
                box.close()
        else:
            count += 1
            yield email.message_from_bytes(f.read_bytes(), policy=email.policy.default)


class EmailImportAdapter:
    adapter: ClassVar[str] = "email_import"
    requires_terms_review: ClassVar[bool] = False

    def __init__(self, now: datetime | None = None):
        self.now = now

    def fetch(self, config: dict[str, Any]) -> FetchResult:
        path = Path(config.get("path", ""))
        if not path.exists():
            raise SourceError(f"not found: {path}", kind="config")
        now = self.now or datetime.now(timezone.utc)
        max_age = timedelta(days=int(config.get("max_age_days", 30)))
        records: list[SourceRecord] = []
        warnings: list[str] = []
        skipped_old = 0
        for msg in iter_messages(path):
            sender = parseaddr(str(msg.get("From", "")))[1].lower()[:120]
            subject = " ".join(str(msg.get("Subject", "")).split())[:160]
            try:
                sent = parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
                if sent is not None and sent.tzinfo is None:
                    sent = sent.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                sent = None
            body = _body(msg)
            body_expiry = {e for e in (extract_expiry(x) for x in re.split(r"[\n\r]+", body)) if e}
            shared_expiry = next(iter(body_expiry)) if len(body_expiry) == 1 else None
            for line in candidate_lines(body):
                try:
                    parsed = parse_offer_text(line)
                except NormalizationError:
                    continue
                data: dict[str, Any] = {"text": line}
                expiry = parsed.fields.get("valid_until") or shared_expiry
                if expiry:
                    data["valid_until"] = expiry
                expired_or_unknown_old = (
                    sent is not None and now - sent > max_age
                    and (not expiry or datetime.fromisoformat(expiry).date() < now.date())
                )
                if expired_or_unknown_old:
                    skipped_old += 1
                    continue
                records.append(SourceRecord(
                    data=data, evidence_type="email", authenticity=Authenticity.UNVERIFIED, source_url=None,
                    raw_text=f"From: {sender} | Subject: {subject} | {line}"[:2000]))
        if skipped_old:
            warnings.append(f"{skipped_old} offer line(s) skipped: email older than {max_age.days} days "
                            "without a future expiry date")
        return FetchResult(records=records, snapshot=False, data_origin="imported", schema_fingerprint=None,
                           warnings=warnings + ([] if records else ["no offers found in these emails"]))

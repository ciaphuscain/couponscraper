"""Import offers from a local JSON or CSV file the owner controls.

This is the always-available source: offers copied from an issuer's offer
page, a promotional email the user received, or a feed exported by an
authorised provider. No network access.

JSON: {"synthetic": false, "offers": [ {record}, ... ]}
CSV : header row with record field names; list fields (asins, banks, types,
      networks) are separated by '|'.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

from ..domain import Authenticity
from .base import FetchResult, SourceError, SourceRecord

MAX_FILE_BYTES = 5 * 1024 * 1024
EVIDENCE_AUTHENTICITY = {
    "official_page": Authenticity.OFFICIAL,  # owner copied it from amazon.in / the issuer
    "user_observed": Authenticity.OFFICIAL,
    "feed_listing": Authenticity.UNVERIFIED,
    "user_entered": Authenticity.UNVERIFIED,
    "fixture": Authenticity.SOURCE_LISTED,
}
LIST_FIELDS = ("asins", "banks", "types", "networks")


class FileImportAdapter:
    adapter: ClassVar[str] = "file_import"
    requires_terms_review: ClassVar[bool] = False

    def fetch(self, config: dict[str, Any]) -> FetchResult:
        path = Path(config.get("path", ""))
        if not path.is_file():
            raise SourceError(f"import file not found: {path}", kind="config")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError("import file too large", kind="config")
        raw = path.read_bytes()
        if path.suffix.lower() == ".csv":
            rows, synthetic = self._csv(raw), bool(config.get("synthetic", False))
        else:
            try:
                doc = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise SourceError(f"invalid JSON: {e}", kind="schema") from e
            if not isinstance(doc, dict) or not isinstance(doc.get("offers"), list):
                raise SourceError("JSON must be an object with an 'offers' list", kind="schema")
            rows, synthetic = doc["offers"], bool(doc.get("synthetic", False))
        records = []
        for r in rows:
            if not isinstance(r, dict):
                records.append(SourceRecord(data={"_invalid": True}, evidence_type="user_entered",
                                            authenticity=Authenticity.UNVERIFIED))
                continue
            evidence = r.pop("evidence", None) or config.get("evidence", "user_entered")
            if synthetic:
                evidence = "fixture"
            records.append(
                SourceRecord(
                    data=r,
                    evidence_type=evidence if evidence in EVIDENCE_AUTHENTICITY else "user_entered",
                    authenticity=EVIDENCE_AUTHENTICITY.get(evidence, Authenticity.UNVERIFIED),
                    source_url=None,
                    raw_text=str(r.get("text") or r.get("title") or "")[:2000],
                )
            )
        keys = sorted({k for r in rows if isinstance(r, dict) for k in r})
        return FetchResult(
            records=records,
            snapshot=bool(config.get("snapshot", True)),
            data_origin="synthetic" if synthetic else config.get("data_origin", "imported"),
            schema_fingerprint=hashlib.sha256(",".join(keys).encode()).hexdigest()[:16],
        )

    @staticmethod
    def _csv(raw: bytes) -> list[dict[str, Any]]:
        text = raw.decode("utf-8-sig")
        out = []
        for row in csv.DictReader(text.splitlines()):
            rec: dict[str, Any] = {}
            pay: dict[str, list[str]] = {}
            for k, v in row.items():
                if k is None or v is None or v == "":
                    continue
                k = k.strip()
                if k in ("banks", "types", "networks"):
                    pay[k] = [x.strip() for x in v.split("|") if x.strip()]
                elif k == "asins":
                    rec[k] = [x.strip() for x in v.split("|") if x.strip()]
                elif k in ("new_customer_only", "prime_only", "account_specific", "one_time_use"):
                    rec[k] = v.strip().lower() in ("1", "true", "yes")
                else:
                    rec[k] = v
            if pay:
                rec["payment"] = pay
            out.append(rec)
        return out

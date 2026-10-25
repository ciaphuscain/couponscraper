"""Source adapter contract.

An adapter only *fetches* and *shapes* records. It never writes to the
database, never decides eligibility, and never escalates when refused: an
authorization error disables the source, it does not trigger a workaround.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from ..domain import Authenticity


class SourceError(Exception):
    """kind: 'transient' (retry later), 'auth' (disable now), 'rate_limited', 'config', 'schema'."""

    def __init__(self, message: str, kind: str = "transient", retry_after_s: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after_s = retry_after_s


@dataclass
class SourceRecord:
    data: dict[str, Any]  # input to normalize.normalize_record
    evidence_type: str  # feed_listing | official_page | user_observed | user_entered | fixture
    authenticity: Authenticity
    source_url: str | None = None
    raw_text: str = ""


@dataclass
class FetchResult:
    records: list[SourceRecord]
    # True when the result is the source's complete current listing, so an
    # offer missing from it has been withdrawn by that source.
    snapshot: bool
    data_origin: str  # live | imported | manual | synthetic
    schema_fingerprint: str | None = None
    # Restrict snapshot semantics to offers for one product (extension page capture).
    snapshot_asin: str | None = None
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(Protocol):
    adapter: ClassVar[str]
    # Network adapters require the owner to confirm they reviewed the source's terms.
    requires_terms_review: ClassVar[bool]

    def fetch(self, config: dict[str, Any]) -> FetchResult: ...

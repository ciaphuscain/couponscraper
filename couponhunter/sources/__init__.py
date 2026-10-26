"""Registry of source adapters. Add an adapter here and nowhere else."""

from .base import FetchResult, SourceAdapter, SourceError, SourceRecord
from .email_import import EmailImportAdapter
from .file_import import FileImportAdapter
from .rss import RssAdapter

ADAPTERS: dict[str, type] = {
    FileImportAdapter.adapter: FileImportAdapter,
    EmailImportAdapter.adapter: EmailImportAdapter,
    RssAdapter.adapter: RssAdapter,
}

__all__ = ["ADAPTERS", "FetchResult", "SourceAdapter", "SourceError", "SourceRecord"]

"""Registry of source adapters. Add an adapter here and nowhere else."""

from .base import FetchResult, SourceAdapter, SourceError, SourceRecord
from .file_import import FileImportAdapter
from .rss import RssAdapter

ADAPTERS: dict[str, type] = {
    FileImportAdapter.adapter: FileImportAdapter,
    RssAdapter.adapter: RssAdapter,
}

__all__ = ["ADAPTERS", "FetchResult", "SourceAdapter", "SourceError", "SourceRecord"]

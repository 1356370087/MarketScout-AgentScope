"""Local document ingestion and retrieval for governed research runs."""

from .contracts import SourceMode, SourceSelection
from .settings import DocumentSettings, get_document_settings

__all__ = [
    "DocumentSettings",
    "SourceMode",
    "SourceSelection",
    "get_document_settings",
]

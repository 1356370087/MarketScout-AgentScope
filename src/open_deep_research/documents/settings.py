"""Environment configuration for the local-document subsystem."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class DocumentSettings:
    """Resolved deployment settings for uploads, storage and retrieval."""

    enabled: bool = field(default_factory=lambda: _flag("DOCUMENT_RESEARCH_ENABLED"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DOCUMENT_DATABASE_URL")
        or os.getenv("IAM_DATABASE_URL", "")
    )
    storage_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("DOCUMENT_STORAGE_DIR", ".documents")
        ).resolve()
    )
    max_file_bytes: int = field(
        default_factory=lambda: _int("DOCUMENT_MAX_FILE_BYTES", 50 * 1024 * 1024)
    )
    max_documents_per_user: int = field(
        default_factory=lambda: _int("DOCUMENT_MAX_COUNT_PER_USER", 500)
    )
    max_bytes_per_user: int = field(
        default_factory=lambda: _int("DOCUMENT_MAX_BYTES_PER_USER", 5 * 1024**3)
    )
    max_logical_units: int = field(
        default_factory=lambda: _int("DOCUMENT_MAX_LOGICAL_UNITS", 500)
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("DOCUMENT_EMBEDDING_MODEL", "if-embedding-v1")
    )
    embedding_dimensions: int = field(
        default_factory=lambda: _int("DOCUMENT_EMBEDDING_DIMENSIONS", 1536)
    )
    embedding_batch_size: int = field(
        default_factory=lambda: _int("DOCUMENT_EMBEDDING_BATCH_SIZE", 64)
    )
    trigram_min_score: float = field(
        default_factory=lambda: max(
            0.0, min(1.0, _float("DOCUMENT_TRIGRAM_MIN_SCORE", 0.08))
        )
    )
    worker_poll_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCUMENT_WORKER_POLL_SECONDS", "2"))
    )
    worker_lease_seconds: int = field(
        default_factory=lambda: _int("DOCUMENT_WORKER_LEASE_SECONDS", 300)
    )
    worker_heartbeat_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("DOCUMENT_WORKER_HEARTBEAT_SECONDS", "30")
        )
    )
    worker_max_attempts: int = field(
        default_factory=lambda: _int("DOCUMENT_WORKER_MAX_ATTEMPTS", 3)
    )
    worker_metrics_port: int = field(
        default_factory=lambda: _int("DOCUMENT_WORKER_METRICS_PORT", 9109)
    )
    ocr_enabled: bool = field(
        default_factory=lambda: _flag("DOCUMENT_OCR_ENABLED", "true")
    )
    ocr_mode: str = field(
        default_factory=lambda: os.getenv("DOCUMENT_OCR_MODE", "local").strip().lower()
    )
    ocr_url: str = field(default_factory=lambda: os.getenv("DOCUMENT_OCR_URL", "").strip())
    ocr_api_key: str = field(default_factory=lambda: os.getenv("DOCUMENT_OCR_API_KEY", ""))
    ocr_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("DOCUMENT_OCR_TIMEOUT_SECONDS", "120"))
    )
    ocr_file_type: int = field(
        default_factory=lambda: _int("DOCUMENT_OCR_FILE_TYPE", 0)
    )
    ocr_dpi: int = field(default_factory=lambda: _int("DOCUMENT_OCR_DPI", 300))
    docling_base_url: str = field(
        default_factory=lambda: os.getenv("DOCLING_SERVE_URL", "").strip().rstrip("/")
    )
    docling_api_key: str = field(
        default_factory=lambda: os.getenv("DOCLING_SERVE_API_KEY", "").strip()
    )
    docling_timeout_seconds: float = field(
        default_factory=lambda: _float("DOCLING_TIMEOUT_SECONDS", 600.0)
    )
    docling_poll_seconds: float = field(
        default_factory=lambda: _float("DOCLING_POLL_SECONDS", 2.0)
    )
    docling_max_concurrent_tasks: int = field(
        default_factory=lambda: _int("DOCLING_MAX_CONCURRENT_TASKS", 2)
    )
    docling_ocr_lang: str = field(
        default_factory=lambda: os.getenv("DOCLING_OCR_LANG", "").strip()
    )
    # Empty model keeps suggestions deterministic-only in the worker.
    metadata_suggestion_model: str = field(
        default_factory=lambda: os.getenv(
            "DOCUMENT_METADATA_SUGGESTION_MODEL", ""
        ).strip()
    )
    soffice_path: str = field(
        default_factory=lambda: os.getenv("DOCUMENT_SOFFICE_PATH", "soffice")
    )
    office_preview_timeout_seconds: float = field(
        default_factory=lambda: _float("DOCUMENT_OFFICE_PREVIEW_TIMEOUT_SECONDS", 180.0)
    )
    # Rows per table row-group segment; each group repeats header and footnotes.
    table_row_group_size: int = field(
        default_factory=lambda: _int("DOCUMENT_TABLE_ROW_GROUP_SIZE", 40)
    )

    @property
    def configured(self) -> bool:
        """Return whether required external dependencies are configured."""
        return self.enabled and bool(self.database_url)

    @property
    def docling_configured(self) -> bool:
        """Return whether the self-hosted Docling Serve endpoint is usable."""
        return self.docling_base_url.startswith(("http://", "https://"))

    @property
    def remote_ocr_configured(self) -> bool:
        """Return whether the selected remote OCR transport has an endpoint."""
        return (
            self.ocr_mode == "remote"
            and self.ocr_url.startswith(("http://", "https://"))
            and self.ocr_file_type in {0, 1}
        )


def get_document_settings() -> DocumentSettings:
    """Resolve fresh document settings so tests and deployments can change env."""
    return DocumentSettings()

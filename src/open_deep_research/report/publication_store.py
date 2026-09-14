"""Durable, run-scoped publication jobs and committed report files."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
import uuid
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, cast

import portalocker
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import PublisherTheme, RenderedArtifact
from .publishers import (
    DOCX_MEDIA_TYPE,
    JSON_MEDIA_TYPE,
    MARKDOWN_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    resolve_publication_format,
)

PUBLICATION_SCHEMA_VERSION = 1
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PublicationFormat(str, Enum):
    """Canonical file formats accepted by the publication API."""

    MARKDOWN = "markdown"
    JSON = "json"
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    ONE_PAGER = "one_pager"


class PublicationStatus(str, Enum):
    """Durable publication job states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float, *, minimum: float = 0.01) -> float:
    try:
        parsed = float(os.getenv(name, str(default)))
        return max(minimum, parsed) if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _normalize_error_code(value: object) -> str:
    """Return one bounded public error identifier."""
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]
    return candidate or "publication_failed"


def _reject_symlink(path: Path) -> None:
    """Reject a symlink at a publication storage boundary."""
    if path.is_symlink():
        raise ValueError("publication_path_symlink_not_allowed")


def _assert_no_symlink_components(path: Path, floor: Path) -> None:
    """Ensure existing path components stay inside the configured run tree."""
    current = path
    while True:
        _reject_symlink(current)
        if current == floor or current.parent == current:
            return
        current = current.parent


@dataclass(frozen=True, slots=True)
class PublisherSettings:
    """Administrator-owned publication queue and resource settings."""

    enabled: bool = field(default_factory=lambda: _flag("PUBLISHER_ENABLED"))
    runs_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RUNS_DIR", ".runs")).resolve()
    )
    poll_interval_seconds: float = field(
        default_factory=lambda: _float("PUBLISHER_POLL_INTERVAL_SECONDS", 1.0)
    )
    lease_seconds: float = field(
        default_factory=lambda: _float("PUBLISHER_LEASE_SECONDS", 180.0)
    )
    max_concurrent_jobs: int = field(
        default_factory=lambda: _int("PUBLISHER_MAX_CONCURRENT_JOBS", 2)
    )
    max_attempts: int = field(
        default_factory=lambda: _int("PUBLISHER_MAX_ATTEMPTS", 3)
    )
    max_input_chars: int = field(
        default_factory=lambda: _int("PUBLISHER_MAX_INPUT_CHARS", 500_000)
    )
    max_output_bytes: int = field(
        default_factory=lambda: _int(
            "PUBLISHER_MAX_OUTPUT_BYTES", 50 * 1024 * 1024
        )
    )
    max_pdf_pages: int = field(
        default_factory=lambda: _int("PUBLISHER_MAX_PDF_PAGES", 100)
    )
    max_pptx_slides: int = field(
        default_factory=lambda: _int(
            "PUBLISHER_MAX_PPTX_SLIDES",
            _int("PUBLISHER_MAX_PPTX_PAGES", 64),
        )
    )
    sse_idle_seconds: float = field(
        default_factory=lambda: _float("PUBLISHER_SSE_IDLE_SECONDS", 60.0)
    )
    heartbeat_interval_seconds: float = field(
        default_factory=lambda: _float(
            "PUBLISHER_HEARTBEAT_INTERVAL_SECONDS", 10.0
        )
    )
    heartbeat_stale_seconds: float = field(
        default_factory=lambda: _float("PUBLISHER_HEARTBEAT_STALE_SECONDS", 45.0)
    )

    @property
    def heartbeat_path(self) -> Path:
        """Return the worker heartbeat path shared with the API."""
        return self.runs_dir / ".publisher_worker" / "heartbeat.json"

    @property
    def max_pptx_pages(self) -> int:
        """Compatibility spelling for the maximum PPTX slide count."""
        return self.max_pptx_slides


def get_publisher_settings() -> PublisherSettings:
    """Resolve fresh publication settings for tests and deployments."""
    return PublisherSettings()


class PublicationArtifact(BaseModel):
    """Metadata for one validated file committed by the publisher worker."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    media_type: str
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: str
    page_count: int | None = Field(default=None, ge=1)
    slide_count: int | None = Field(default=None, ge=1)
    preview: dict[str, Any] | None = None

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        """Keep the download name a single safe path component."""
        candidate = str(value).strip()
        if (
            not candidate
            or "/" in candidate
            or "\\" in candidate
            or Path(candidate).name != candidate
            or any(
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or unicodedata.category(char) == "Cc"
                for char in candidate
            )
        ):
            raise ValueError("publication_filename_invalid")
        return candidate[:180]

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        """Reject response-header control characters in persisted metadata."""
        candidate = str(value).strip()
        if not candidate or any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or unicodedata.category(char) == "Cc"
            for char in candidate
        ):
            raise ValueError("publication_media_type_invalid")
        return candidate[:160]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """Reject absolute and parent-relative artifact paths at load time."""
        candidate = str(value).replace("\\", "/")
        path = Path(candidate)
        if path.is_absolute() or ".." in path.parts or not candidate:
            raise ValueError("publication_artifact_path_invalid")
        return candidate

    @field_validator("preview")
    @classmethod
    def validate_preview(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep preview metadata JSON-safe before it reaches an HTTP response."""
        if value is None:
            return None
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("publication_preview_invalid") from exc
        return value


class PublicationJob(BaseModel):
    """Durable state machine for one format/theme publication request."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = PUBLICATION_SCHEMA_VERSION
    publication_id: str
    run_id: str
    requested_format: str
    format: Literal["markdown", "json", "pdf", "docx", "pptx", "one_pager"]
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_schema_version: str = "1.0"
    theme: PublisherTheme = Field(default_factory=PublisherTheme)
    theme_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    retryable: bool = True
    error_code: str | None = None
    artifact: PublicationArtifact | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None

    @field_validator(
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "lease_expires_at",
    )
    @classmethod
    def validate_finite_timestamp(cls, value: float | None) -> float | None:
        """Reject corrupt non-finite timestamps at the Job persistence boundary."""
        if value is not None and not math.isfinite(value):
            raise ValueError("publication_timestamp_invalid")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        """Keep public error codes short and free of path/control data."""
        if value is None:
            return None
        return _normalize_error_code(value)

    @field_validator("requested_format")
    @classmethod
    def validate_requested_format(cls, value: str) -> str:
        """Keep the original client spelling bounded and header-safe."""
        candidate = str(value).strip()
        if (
            not candidate
            or len(candidate) > 40
            or any(
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or unicodedata.category(char) == "Cc"
                for char in candidate
            )
        ):
            raise ValueError("publication_requested_format_invalid")
        return candidate

    def public_dict(self) -> dict[str, Any]:
        """Return the owner-facing projection without local paths or leases."""
        artifact = self.artifact.model_dump(exclude={"relative_path"}) if self.artifact else None
        return {
            "publication_id": self.publication_id,
            "run_id": self.run_id,
            "requested_format": self.requested_format,
            "format": self.format,
            "status": self.status,
            "report_sha256": self.report_sha256,
            "theme": self.theme.model_dump(mode="json"),
            "theme_sha256": self.theme_sha256,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "retryable": self.retryable,
            "error_code": self.error_code,
            "artifact": artifact,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def _validate_component(value: str, kind: str) -> str:
    if not _COMPONENT_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"invalid_{kind}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        with suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _theme_digest(theme: PublisherTheme) -> str:
    encoded = json.dumps(
        theme.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def publication_id_for(
    run_id: str,
    report_sha256: str,
    publication_format: str,
    theme: PublisherTheme,
    *,
    canonical_schema_version: str = "1.0",
) -> tuple[str, str, str]:
    """Return deterministic publication id, canonical format and theme hash."""
    safe_run_id = _validate_component(run_id, "run_id")
    if not _SHA256_RE.fullmatch(report_sha256):
        raise ValueError("invalid_report_sha256")
    resolved_format = resolve_publication_format(publication_format)
    theme_sha256 = _theme_digest(theme)
    raw = "\0".join(
        (
            safe_run_id,
            report_sha256,
            resolved_format,
            theme_sha256,
            canonical_schema_version,
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"pub-{digest[:40]}", resolved_format, theme_sha256


def publication_filename(title: str, publication_format: str, extension: str) -> str:
    """Return a bounded display filename without using it as a storage path."""
    name = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]+", " ", str(title or "report"))
    name = " ".join(name.split()).strip(" .")[:120] or "report"
    if publication_format == "one_pager":
        name += "-one-pager"
    return f"{name}.{extension}"


def _expected_artifact_metadata(publication_format: str) -> tuple[str, str]:
    """Return the only extension/media type pair accepted for a format."""
    resolved = resolve_publication_format(publication_format)
    return {
        "markdown": ("md", MARKDOWN_MEDIA_TYPE),
        "json": ("json", JSON_MEDIA_TYPE),
        "pdf": ("pdf", PDF_MEDIA_TYPE),
        "docx": ("docx", DOCX_MEDIA_TYPE),
        "pptx": ("pptx", PPTX_MEDIA_TYPE),
        "one_pager": ("pdf", PDF_MEDIA_TYPE),
    }[resolved]


def _expected_artifact_relative_path(
    publication_id: str,
    publication_format: str,
) -> str:
    """Return the only relative file path a completed Job may reference."""
    extension, _media_type = _expected_artifact_metadata(publication_format)
    return (
        Path("publications")
        / "files"
        / f"{publication_id}.{extension}"
    ).as_posix()


class PublicationJobStore:
    """Persist publication jobs and outputs inside one run context."""

    def __init__(self, run_id: str, *, runs_dir: str | Path = ".runs") -> None:
        """Initialize paths for one validated run id."""
        self.run_id = _validate_component(run_id, "run_id")
        root = Path(runs_dir).resolve()
        raw_run_dir = root / self.run_id
        _reject_symlink(raw_run_dir)
        self.run_dir = raw_run_dir.resolve()
        if root not in self.run_dir.parents:
            raise ValueError("invalid_run_id")
        self.context_dir = self.run_dir / "context"
        self.root = self.context_dir / "publications"
        self.jobs_dir = self.root / "jobs"
        self.files_dir = self.root / "files"

    def _job_path(self, publication_id: str) -> Path:
        safe = _validate_component(publication_id, "publication_id")
        return self.jobs_dir / f"{safe}.json"

    def _lock_path(self, publication_id: str) -> Path:
        safe = _validate_component(publication_id, "publication_id")
        return self.jobs_dir / f"{safe}.lock"

    def _read_job(self, path: Path) -> PublicationJob:
        try:
            job = PublicationJob.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise ValueError("publication_job_corrupted") from exc
        if job.run_id != self.run_id or path.stem != job.publication_id:
            raise ValueError("publication_job_identity_mismatch")
        if (
            job.schema_version != PUBLICATION_SCHEMA_VERSION
            or job.canonical_schema_version != "1.0"
        ):
            raise ValueError("publication_job_schema_unsupported")
        try:
            requested_format = resolve_publication_format(job.requested_format)
        except ValueError as exc:
            raise ValueError("publication_job_corrupted") from exc
        if requested_format != job.format:
            raise ValueError("publication_job_identity_mismatch")
        if job.artifact is not None:
            expected_extension, expected_media_type = _expected_artifact_metadata(job.format)
            expected_relative = _expected_artifact_relative_path(
                job.publication_id,
                job.format,
            )
            if (
                job.artifact.media_type != expected_media_type
                or Path(job.artifact.relative_path).suffix.lower().lstrip(".")
                != expected_extension
                or job.artifact.relative_path.replace("\\", "/")
                != expected_relative
                or not job.artifact.filename.lower().endswith(
                    f".{expected_extension}"
                )
            ):
                raise ValueError("publication_job_corrupted")
        if (job.status == "completed") != (job.artifact is not None):
            raise ValueError("publication_job_corrupted")
        if job.status != "running" and (
            job.lease_owner is not None or job.lease_expires_at is not None
        ):
            raise ValueError("publication_job_corrupted")
        return job

    def enqueue(
        self,
        *,
        report_sha256: str,
        publication_format: str,
        theme: PublisherTheme,
        max_attempts: int,
        on_created: Callable[[PublicationJob], None] | None = None,
    ) -> tuple[PublicationJob, bool]:
        """Create or return an idempotent publication job."""
        publication_id, resolved_format, theme_sha256 = publication_id_for(
            self.run_id,
            report_sha256,
            publication_format,
            theme,
        )
        path = self._job_path(publication_id)
        lock = self._lock_path(publication_id)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(lock), mode="a+b", timeout=30):
            if path.exists():
                return self._read_job(path), False
            job = PublicationJob(
                publication_id=publication_id,
                run_id=self.run_id,
                requested_format=str(
                    publication_format.value
                    if isinstance(publication_format, Enum)
                    else publication_format
                ).strip().lower(),
                format=cast(
                    Literal["markdown", "json", "pdf", "docx", "pptx", "one_pager"],
                    resolved_format,
                ),
                report_sha256=report_sha256,
                theme=theme,
                theme_sha256=theme_sha256,
                max_attempts=max(1, max_attempts),
            )
            _atomic_write_json(path, job.model_dump(mode="json"))
            if on_created is not None:
                on_created(job)
            return job, True

    def get(self, publication_id: str) -> PublicationJob | None:
        """Return one atomically written job, or None when absent."""
        path = self._job_path(publication_id)
        if not path.exists():
            return None
        try:
            return self._read_job(path)
        except FileNotFoundError:
            return None

    def list(self) -> list[PublicationJob]:
        """Return atomically written jobs newest first."""
        if not self.jobs_dir.exists():
            return []
        jobs: list[PublicationJob] = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                job = self._read_job(path)
            except (FileNotFoundError, OSError, ValueError):
                continue
            jobs.append(job)
        return sorted(
            jobs,
            key=lambda job: (job.created_at, job.publication_id),
            reverse=True,
        )

    def claim(
        self,
        publication_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
        on_exhausted: Callable[[PublicationJob], None] | None = None,
    ) -> PublicationJob | None:
        """Claim a queued or expired-running job under its file lock."""
        _validate_component(worker_id, "worker_id")
        path = self._job_path(publication_id)
        if not path.exists():
            return None
        with portalocker.Lock(
            str(self._lock_path(publication_id)), mode="a+b", timeout=30
        ):
            job = self._read_job(path)
            now = time.time()
            expired = (
                job.status == "running"
                and (job.lease_expires_at or 0) <= now
            )
            if job.status != "queued" and not expired:
                return None
            if job.attempt >= job.max_attempts:
                job.status = "failed"
                job.retryable = True
                job.error_code = "publication_attempts_exhausted"
                job.updated_at = now
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                _atomic_write_json(path, job.model_dump(mode="json"))
                if on_exhausted is not None:
                    on_exhausted(job)
                return None
            job.status = "running"
            job.attempt += 1
            job.started_at = now
            job.updated_at = now
            job.error_code = None
            job.lease_owner = worker_id
            job.lease_expires_at = now + max(1.0, lease_seconds)
            _atomic_write_json(path, job.model_dump(mode="json"))
            return job

    @staticmethod
    def _validate_artifact_metadata(
        job: PublicationJob,
        artifact: PublicationArtifact,
    ) -> None:
        """Validate a completed artifact's format and fixed storage location."""
        expected_extension, expected_media_type = _expected_artifact_metadata(
            job.format
        )
        if (
            artifact.media_type != expected_media_type
            or artifact.relative_path.replace("\\", "/")
            != _expected_artifact_relative_path(job.publication_id, job.format)
            or not artifact.filename.lower().endswith(f".{expected_extension}")
        ):
            raise ValueError("publication_artifact_metadata_invalid")

    def renew(
        self,
        publication_id: str,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        """Extend an active lease, returning false after fencing."""
        path = self._job_path(publication_id)
        if not path.exists():
            return False
        with portalocker.Lock(
            str(self._lock_path(publication_id)), mode="a+b", timeout=30
        ):
            job = self._read_job(path)
            now = time.time()
            if (
                job.status != "running"
                or job.lease_owner != worker_id
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                return False
            job.lease_expires_at = now + max(1.0, lease_seconds)
            job.updated_at = now
            _atomic_write_json(path, job.model_dump(mode="json"))
            return True

    def complete(
        self,
        publication_id: str,
        *,
        worker_id: str,
        artifact: PublicationArtifact,
    ) -> PublicationJob:
        """Commit a completed state only for the current lease owner."""
        path = self._job_path(publication_id)
        with portalocker.Lock(
            str(self._lock_path(publication_id)), mode="a+b", timeout=30
        ):
            job = self._read_job(path)
            now = time.time()
            if (
                job.status != "running"
                or job.lease_owner != worker_id
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                raise RuntimeError("publication_lease_lost")
            self._validate_artifact_metadata(job, artifact)
            job.status = "completed"
            job.artifact = artifact
            job.retryable = False
            job.error_code = None
            job.completed_at = now
            job.updated_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            _atomic_write_json(path, job.model_dump(mode="json"))
            return job

    def fail(
        self,
        publication_id: str,
        *,
        worker_id: str,
        error_code: str,
        retryable: bool,
        on_updated: Callable[[PublicationJob, bool], None] | None = None,
    ) -> tuple[PublicationJob, bool]:
        """Fail or requeue a claimed job and report whether it was requeued."""
        path = self._job_path(publication_id)
        with portalocker.Lock(
            str(self._lock_path(publication_id)), mode="a+b", timeout=30
        ):
            job = self._read_job(path)
            now = time.time()
            if (
                job.status != "running"
                or job.lease_owner != worker_id
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                raise RuntimeError("publication_lease_lost")
            should_retry = retryable and job.attempt < job.max_attempts
            job.status = "queued" if should_retry else "failed"
            job.retryable = retryable
            job.error_code = _normalize_error_code(
                error_code or "publication_render_failed"
            )
            job.updated_at = now
            job.completed_at = None if should_retry else now
            job.lease_owner = None
            job.lease_expires_at = None
            _atomic_write_json(path, job.model_dump(mode="json"))
            if on_updated is not None:
                on_updated(job, should_retry)
            return job, should_retry

    def retry(
        self,
        publication_id: str,
        *,
        on_requeued: Callable[[PublicationJob], None] | None = None,
    ) -> PublicationJob:
        """Requeue a retryable failed job without changing its identity."""
        path = self._job_path(publication_id)
        if not path.exists():
            raise FileNotFoundError(publication_id)
        with portalocker.Lock(
            str(self._lock_path(publication_id)), mode="a+b", timeout=30
        ):
            job = self._read_job(path)
            if job.status != "failed":
                raise ValueError("publication_not_failed")
            if not job.retryable:
                raise ValueError("publication_not_retryable")
            if job.attempt >= job.max_attempts:
                job.max_attempts = job.attempt + 1
            job.status = "queued"
            job.error_code = None
            job.artifact = None
            job.started_at = None
            job.completed_at = None
            job.updated_at = time.time()
            _atomic_write_json(path, job.model_dump(mode="json"))
            if on_requeued is not None:
                on_requeued(job)
            return job

    def commit_file(
        self,
        job: PublicationJob,
        rendered: RenderedArtifact,
        *,
        report_title: str,
        max_output_bytes: int,
        worker_id: str | None = None,
    ) -> PublicationArtifact:
        """Atomically commit validated bytes and return their metadata.

        Worker commits are fenced by the Job lease while the fixed artifact
        target is replaced.  A late renderer therefore cannot overwrite the
        bytes belonging to a newer lease holder.
        """
        if worker_id is not None:
            _validate_component(worker_id, "worker_id")
        lock = (
            portalocker.Lock(
                str(self._lock_path(job.publication_id)),
                mode="a+b",
                timeout=30,
            )
            if worker_id is not None
            else nullcontext()
        )
        with lock:
            if worker_id is not None:
                current = self._read_job(self._job_path(job.publication_id))
                now = time.time()
                if (
                    current.status != "running"
                    or current.lease_owner != worker_id
                    or current.lease_expires_at is None
                    or current.lease_expires_at <= now
                ):
                    raise RuntimeError("publication_lease_lost")
            content = rendered.content
            if not content:
                raise ValueError("publication_output_empty")
            if len(content) > max_output_bytes:
                raise ValueError("publication_output_too_large")
            extension = re.sub(r"[^a-z0-9]", "", rendered.extension.lower())
            if not extension:
                raise ValueError("publication_extension_invalid")
            expected_extension, expected_media_type = _expected_artifact_metadata(
                job.format
            )
            if (
                extension != expected_extension
                or rendered.media_type != expected_media_type
            ):
                raise ValueError("publication_output_metadata_invalid")
            _assert_no_symlink_components(self.files_dir, self.run_dir)
            self.files_dir.mkdir(parents=True, exist_ok=True)
            raw_target = self.files_dir / f"{job.publication_id}.{extension}"
            _reject_symlink(raw_target)
            target = raw_target.resolve()
            if self.files_dir.resolve() not in target.parents:
                raise ValueError("publication_artifact_path_invalid")
            digest = hashlib.sha256(content).hexdigest()
            existing_matches = False
            try:
                if target.stat().st_size == len(content):
                    with target.open("rb") as existing:
                        existing_matches = (
                            hashlib.file_digest(existing, "sha256").hexdigest()
                            == digest
                        )
            except FileNotFoundError:
                pass
            if not existing_matches:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=self.files_dir
                )
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, target)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                with suppress(OSError):
                    os.chmod(target, 0o600)
            relative = target.relative_to(self.context_dir.resolve()).as_posix()
            return PublicationArtifact(
                filename=publication_filename(report_title, job.format, extension),
                media_type=rendered.media_type,
                size_bytes=len(content),
                sha256=digest,
                relative_path=relative,
                page_count=rendered.page_count,
                slide_count=rendered.slide_count,
                preview=rendered.preview,
            )

    def artifact_path(self, job: PublicationJob, *, verify: bool = True) -> Path:
        """Resolve and optionally hash-check a completed artifact."""
        if job.status != "completed" or job.artifact is None:
            raise FileNotFoundError("publication_not_ready")
        relative = Path(job.artifact.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("publication_artifact_path_invalid")
        expected_extension, _expected_media_type = _expected_artifact_metadata(job.format)
        expected_relative = (
            Path("publications")
            / "files"
            / f"{job.publication_id}.{expected_extension}"
        )
        if relative.as_posix() != expected_relative.as_posix():
            raise ValueError("publication_artifact_path_invalid")
        raw_target = self.context_dir / relative
        _assert_no_symlink_components(raw_target, self.run_dir)
        target = raw_target.resolve()
        if self.context_dir.resolve() not in target.parents or not target.is_file():
            raise FileNotFoundError("publication_artifact_missing")
        if verify:
            content = target.read_bytes()
            if len(content) != job.artifact.size_bytes:
                raise ValueError("publication_artifact_size_mismatch")
            if hashlib.sha256(content).hexdigest() != job.artifact.sha256:
                raise ValueError("publication_artifact_hash_mismatch")
        return target

    @property
    def canonical_report_path(self) -> Path:
        """Return the run's canonical report path."""
        return self.context_dir / "canonical_report.json"

    @property
    def final_report_path(self) -> Path:
        """Return the run's final Markdown path."""
        return self.context_dir / "final_report.md"

    def load_final_report(self) -> str:
        """Read the authoritative Markdown without newline translation."""
        _assert_no_symlink_components(self.final_report_path, self.run_dir)
        with self.final_report_path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as handle:
            return handle.read()

    def persist_canonical_report(self, payload: dict[str, Any]) -> None:
        """Atomically persist a canonical report, including legacy backfills."""
        _assert_no_symlink_components(self.canonical_report_path, self.run_dir)
        _atomic_write_json(self.canonical_report_path, payload)

    def load_canonical_report(self) -> str:
        """Read a persisted canonical bundle through the safe path boundary."""
        _assert_no_symlink_components(self.canonical_report_path, self.run_dir)
        with self.canonical_report_path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as handle:
            return handle.read()


def claim_available_jobs(
    settings: PublisherSettings,
    *,
    worker_id: str,
    limit: int,
    on_exhausted: Callable[[PublicationJobStore, PublicationJob], None]
    | None = None,
) -> list[tuple[PublicationJobStore, PublicationJob]]:
    """Claim up to ``limit`` jobs across the shared runs directory."""
    if limit <= 0 or not settings.runs_dir.exists():
        return []
    claimed: list[tuple[PublicationJobStore, PublicationJob]] = []
    for path in sorted(settings.runs_dir.glob("*/context/publications/jobs/*.json")):
        if len(claimed) >= limit:
            break
        run_id = path.parents[3].name
        try:
            store = PublicationJobStore(run_id, runs_dir=settings.runs_dir)
            exhausted_callback: Callable[[PublicationJob], None] | None = None
            if on_exhausted is not None:
                def exhausted_callback(
                    exhausted: PublicationJob,
                    *,
                    owner: PublicationJobStore = store,
                    callback: Callable[[PublicationJobStore, PublicationJob], None]
                    = on_exhausted,
                ) -> None:
                    callback(owner, exhausted)

            job = store.claim(
                path.stem,
                worker_id=worker_id,
                lease_seconds=settings.lease_seconds,
                on_exhausted=exhausted_callback,
            )
        except (OSError, RuntimeError, ValueError, portalocker.exceptions.LockException):
            continue
        if job is not None:
            claimed.append((store, job))
    return claimed


def worker_available(settings: PublisherSettings | None = None) -> bool:
    """Return whether a recent publisher-worker heartbeat is visible."""
    resolved = settings or get_publisher_settings()
    if not resolved.enabled:
        return False
    try:
        payload = json.loads(resolved.heartbeat_path.read_text(encoding="utf-8"))
        timestamp = float(payload.get("timestamp", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    age = time.time() - timestamp
    return 0 <= age <= resolved.heartbeat_stale_seconds


def new_worker_id() -> str:
    """Return a path-safe publisher worker identity."""
    return f"publisher-{uuid.uuid4().hex}"

"""Durable public events for post-run report publication jobs."""

from __future__ import annotations

import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import portalocker
from pydantic import BaseModel, ConfigDict, Field, field_validator

PUBLICATION_EVENT_SCHEMA_VERSION = 1
_ALLOWED_FORMATS = {"markdown", "json", "pdf", "docx", "pptx", "one_pager"}
_ALLOWED_STATUSES = {"queued", "running", "completed", "failed"}
_MEDIA_TYPES = {
    "text/markdown",
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DOWNLOAD_URL_RE = re.compile(
    r"^/runs/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/publications/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/download$"
)
_TAIL_READ_BYTES = 64 * 1024
_ALLOWED_KEYS: dict[str, set[str]] = {
    "publication.queued": {"format", "status", "attempt"},
    "publication.started": {"format", "status", "attempt"},
    "publication.completed": {
        "format", "status", "attempt", "filename", "media_type", "size_bytes",
        "sha256", "page_count", "slide_count", "download_url",
    },
    "publication.failed": {
        "format", "status", "attempt", "error_code", "retryable",
    },
    "publication.requeued": {
        "format", "status", "attempt", "error_code", "retryable",
    },
}


class PublicationEvent(BaseModel):
    """One public, owner-scoped publication lifecycle event."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = PUBLICATION_EVENT_SCHEMA_VERSION
    event_id: str
    sequence: int = Field(ge=1)
    run_id: str
    publication_id: str
    type: str
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float) -> float:
        """Reject non-finite timestamps before they reach the SSE stream."""
        if not math.isfinite(value):
            raise ValueError("publication_event_timestamp_invalid")
        return value

    @field_validator("event_id", "run_id", "publication_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """Keep identifiers exposed through SSE free of path/control data."""
        candidate = str(value)
        if (
            not candidate
            or len(candidate) > 128
            or "/" in candidate
            or "\\" in candidate
            or any(
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or ord(char) == 127
                for char in candidate
            )
        ):
            raise ValueError("publication_event_identifier_invalid")
        return candidate

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        """Restrict persisted event names to the public lifecycle contract."""
        candidate = str(value)
        if candidate not in _ALLOWED_KEYS:
            raise ValueError("unsupported_publication_event")
        return candidate

    @field_validator("dedupe_key")
    @classmethod
    def validate_dedupe_key(cls, value: str) -> str:
        """Bound the private idempotency key and reject control characters."""
        candidate = str(value)
        if len(candidate) > 300 or any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or ord(char) == 127
            for char in candidate
        ):
            raise ValueError("publication_event_dedupe_key_invalid")
        return candidate

    def public_dict(self) -> dict[str, Any]:
        """Return the SSE wire payload without persistence metadata."""
        return self.model_dump(exclude={"dedupe_key"})


def _validate_component(value: str, kind: str) -> str:
    if not _COMPONENT_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"invalid_{kind}")
    return value


def _reject_symlink(path: Path) -> None:
    """Reject links that could redirect the shared event log outside a run."""
    if path.is_symlink():
        raise ValueError("publication_path_symlink_not_allowed")


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    """Replace an event log without exposing a truncated intermediate file."""
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = _ALLOWED_KEYS.get(event_type, set())
    result: dict[str, Any] = {}
    for key in allowed:
        value = payload.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            result[key] = None
        elif key == "retryable":
            if value is None or isinstance(value, bool):
                result[key] = value
            else:
                result[key] = None
        elif key == "attempt":
            if value is None or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            ):
                result[key] = value
            else:
                result[key] = None
        elif key in {"size_bytes", "page_count", "slide_count"}:
            if value is None or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 1
            ):
                result[key] = value
            else:
                result[key] = None
        elif key in {
            "filename",
            "error_code",
            "format",
            "status",
            "media_type",
            "download_url",
            "sha256",
        }:
            if value is None:
                result[key] = None
                continue
            if not isinstance(value, str):
                result[key] = None
                continue
            candidate = " ".join(value.split())[:500]
            if key == "filename" and (
                "/" in candidate or "\\" in candidate
            ):
                result[key] = None
                continue
            if key == "format" and candidate not in _ALLOWED_FORMATS:
                result[key] = None
                continue
            if key == "status" and candidate not in _ALLOWED_STATUSES:
                result[key] = None
                continue
            if key == "error_code" and not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,120}", candidate
            ):
                result[key] = None
                continue
            if key == "download_url" and not _DOWNLOAD_URL_RE.fullmatch(candidate):
                result[key] = None
                continue
            if key == "sha256" and not re.fullmatch(r"[0-9a-f]{64}", candidate):
                result[key] = None
                continue
            if key == "media_type" and candidate not in _MEDIA_TYPES:
                result[key] = None
                continue
            result[key] = candidate
    return result


class PublicationEventStore:
    """Append-only event stream independent from the terminal Run stream."""

    def __init__(self, run_id: str, *, runs_dir: str | Path = ".runs") -> None:
        """Initialize publication event paths for one validated run id."""
        self.run_id = _validate_component(run_id, "run_id")
        root = Path(runs_dir).resolve()
        raw_run_dir = root / self.run_id
        _reject_symlink(raw_run_dir)
        run_dir = raw_run_dir.resolve()
        if root not in run_dir.parents:
            raise ValueError("invalid_run_id")
        self.root = run_dir / "context" / "publications"
        self.path = self.root / "events.jsonl"
        self.lock_path = self.root / "events.lock"

    @property
    def exists(self) -> bool:
        """Return whether the stream has at least one event."""
        return self.path.exists()

    def _read_unlocked(self, *, repair_tail: bool = True) -> list[PublicationEvent]:
        _reject_symlink(self.path)
        if not self.path.exists():
            return []
        content = self.path.read_bytes()
        complete_tail = content.endswith(b"\n")
        lines = content.splitlines()
        events: list[PublicationEvent] = []
        expected = 1
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = PublicationEvent.model_validate_json(line)
            except Exception as exc:
                if repair_tail and index == len(lines) - 1 and not complete_tail:
                    valid = b"\n".join(lines[:index])
                    if valid:
                        valid += b"\n"
                    _atomic_replace_bytes(self.path, valid)
                    return events
                raise ValueError("publication_event_log_corrupted") from exc
            self._validate_stored_event(event, expected_sequence=expected)
            events.append(event)
            expected += 1
        if repair_tail and content and not complete_tail:
            _atomic_replace_bytes(self.path, content + b"\n")
        return events

    def _validate_stored_event(
        self,
        event: PublicationEvent,
        *,
        expected_sequence: int | None = None,
    ) -> None:
        """Validate persisted identity fields without reprojecting the payload."""
        if "schema_version" not in event.model_fields_set or (
            event.schema_version != PUBLICATION_EVENT_SCHEMA_VERSION
            or event.run_id != self.run_id
            or (
                expected_sequence is not None
                and event.sequence != expected_sequence
            )
            or event.type not in _ALLOWED_KEYS
        ):
            raise ValueError("publication_event_log_corrupted")
        try:
            _validate_component(event.publication_id, "publication_id")
        except ValueError as exc:
            raise ValueError("publication_event_log_corrupted") from exc

    def _read_tail_unlocked(self) -> PublicationEvent | None:
        """Read only the final complete event during the normal append path."""
        _reject_symlink(self.path)
        if not self.path.exists():
            return None
        size = self.path.stat().st_size
        if size == 0:
            return None
        offset = max(0, size - _TAIL_READ_BYTES)
        with self.path.open("rb") as handle:
            handle.seek(offset)
            content = handle.read()
        if not content.endswith(b"\n"):
            events = self._read_unlocked()
            return events[-1] if events else None
        trimmed = content.rstrip(b"\r\n")
        if not trimmed:
            return None
        if offset and b"\n" not in trimmed:
            events = self._read_unlocked()
            return events[-1] if events else None
        line = trimmed.rsplit(b"\n", 1)[-1].rstrip(b"\r")
        try:
            event = PublicationEvent.model_validate_json(line)
        except Exception as exc:
            raise ValueError("publication_event_log_corrupted") from exc
        self._validate_stored_event(event)
        return event

    def read(self, after: int = 0, *, read_only: bool = False) -> list[PublicationEvent]:
        """Read events with sequence greater than ``after``."""
        _reject_symlink(self.path)
        if not self.path.exists():
            return []
        if read_only:
            return [
                event for event in self._read_unlocked(repair_tail=False)
                if event.sequence > after
            ]
        self.root.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            return [event for event in self._read_unlocked() if event.sequence > after]

    def last_sequence(self) -> int:
        """Return the final sequence, or zero for an empty stream."""
        events = self.read()
        return events[-1].sequence if events else 0

    def append(
        self,
        event_type: str,
        *,
        publication_id: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> PublicationEvent:
        """Append one idempotent publication event under a file lock."""
        publication_id = _validate_component(publication_id, "publication_id")
        dedupe_key = str(dedupe_key)[:300]
        if event_type not in _ALLOWED_KEYS:
            raise ValueError("unsupported_publication_event")
        _reject_symlink(self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            tail = self._read_tail_unlocked()
            if tail is not None and tail.dedupe_key == dedupe_key:
                return tail
            sequence = tail.sequence + 1 if tail is not None else 1
            event = PublicationEvent(
                event_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{self.run_id}:publication:{dedupe_key}",
                    )
                ),
                sequence=sequence,
                run_id=self.run_id,
                publication_id=publication_id,
                type=event_type,
                payload=_safe_payload(event_type, payload),
                dedupe_key=dedupe_key,
            )
            data = (event.model_dump_json() + "\n").encode("utf-8")
            fd = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            return event


def publication_event_payload(job, *, download_url: str | None = None) -> dict[str, Any]:
    """Project one publication job into the event allowlist."""
    artifact = job.artifact
    return {
        "format": job.format,
        "status": job.status,
        "attempt": job.attempt,
        "error_code": job.error_code,
        "retryable": job.retryable,
        "filename": artifact.filename if artifact else None,
        "media_type": artifact.media_type if artifact else None,
        "size_bytes": artifact.size_bytes if artifact else None,
        "sha256": artifact.sha256 if artifact else None,
        "page_count": artifact.page_count if artifact else None,
        "slide_count": artifact.slide_count if artifact else None,
        "download_url": download_url,
    }

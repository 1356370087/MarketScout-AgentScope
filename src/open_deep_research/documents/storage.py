"""Content-addressed, owner-isolated storage for uploaded documents."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile

from .settings import DocumentSettings

_ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
_OFFICE_ROOTS = {
    ".docx": "word/",
    ".xlsx": "xl/",
    ".pptx": "ppt/",
}


class DocumentUploadError(ValueError):
    """Raised when an upload violates a deterministic safety boundary."""


@dataclass(frozen=True, slots=True)
class StagedUpload:
    """Validated temporary upload ready for a metadata transaction."""

    path: Path
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


def safe_filename(value: str) -> str:
    """Return a display-only filename without path or control characters."""
    name = Path(value or "document").name
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().strip(".")
    return name[:240] or "document"


def _sniff_media_type(path: Path, extension: str) -> str:
    with path.open("rb") as source:
        head = source.read(16)
    expected = _ALLOWED_EXTENSIONS.get(extension)
    if expected is None:
        raise DocumentUploadError("document_type_not_supported")
    if extension == ".pdf" and not head.startswith(b"%PDF-"):
        raise DocumentUploadError("document_signature_mismatch")
    if extension in _OFFICE_ROOTS and not head.startswith(b"PK"):
        raise DocumentUploadError("document_signature_mismatch")
    if extension == ".png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DocumentUploadError("document_signature_mismatch")
    if extension in {".jpg", ".jpeg"} and not head.startswith(b"\xff\xd8\xff"):
        raise DocumentUploadError("document_signature_mismatch")
    if extension in {".tif", ".tiff"} and head[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise DocumentUploadError("document_signature_mismatch")
    if extension in {".csv", ".md", ".markdown", ".txt"} and b"\x00" in head:
        raise DocumentUploadError("document_binary_text_rejected")
    return expected


def _validate_office_archive(path: Path, extension: str) -> None:
    if extension not in _OFFICE_ROOTS:
        return
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000:
                raise DocumentUploadError("document_archive_too_many_entries")
            expanded = 0
            has_root = False
            for info in infos:
                normalized = Path(info.filename.replace("\\", "/"))
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise DocumentUploadError("document_archive_path_invalid")
                expanded += max(0, info.file_size)
                if info.filename.startswith(_OFFICE_ROOTS[extension]):
                    has_root = True
                if info.compress_size and info.file_size / info.compress_size > 200:
                    raise DocumentUploadError("document_archive_ratio_exceeded")
            if expanded > 500 * 1024 * 1024:
                raise DocumentUploadError("document_archive_expanded_size_exceeded")
            if not has_root:
                raise DocumentUploadError("document_office_package_invalid")
    except zipfile.BadZipFile as exc:
        raise DocumentUploadError("document_office_package_invalid") from exc


async def stage_upload(upload: UploadFile, settings: DocumentSettings) -> StagedUpload:
    """Stream one upload to a bounded temporary file while hashing it."""
    filename = safe_filename(upload.filename or "document")
    extension = Path(filename).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise DocumentUploadError("document_type_not_supported")
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = settings.storage_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="upload-", suffix=extension, dir=staging_dir)
    path = Path(raw_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise DocumentUploadError("document_file_too_large")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size == 0:
            raise DocumentUploadError("document_file_empty")
        media_type = _sniff_media_type(path, extension)
        _validate_office_archive(path, extension)
        return StagedUpload(path, filename, media_type, size, digest.hexdigest())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def commit_upload(
    staged: StagedUpload, owner_id: str, settings: DocumentSettings
) -> tuple[str, bool]:
    """Move a staged upload and report whether this call created the blob."""
    owner_key = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
    extension = Path(staged.filename).suffix.lower()
    relative = Path(owner_key) / staged.sha256[:2] / f"{staged.sha256}{extension}"
    target = (settings.storage_dir / relative).resolve()
    root = settings.storage_dir.resolve()
    if root not in target.parents:
        raise DocumentUploadError("document_storage_path_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        staged.path.unlink(missing_ok=True)
        created = False
    else:
        os.replace(staged.path, target)
        created = True
    return relative.as_posix(), created


def resolve_storage_key(storage_key: str, settings: DocumentSettings) -> Path:
    """Resolve a database storage key without allowing directory escape."""
    relative = Path(storage_key)
    if relative.is_absolute() or ".." in relative.parts:
        raise DocumentUploadError("document_storage_path_invalid")
    root = settings.storage_dir.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise DocumentUploadError("document_storage_path_invalid")
    return target


def delete_storage_key(storage_key: str, settings: DocumentSettings) -> None:
    """Delete one original blob and empty content directories."""
    target = resolve_storage_key(storage_key, settings)
    target.unlink(missing_ok=True)
    current = target.parent
    root = settings.storage_dir.resolve()
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def copy_fileobj(source: BinaryIO, target: BinaryIO) -> None:
    """Copy a file-like object in bounded chunks."""
    shutil.copyfileobj(source, target, length=1024 * 1024)

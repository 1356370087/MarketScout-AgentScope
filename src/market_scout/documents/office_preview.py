"""Fixed Office previews via LibreOffice (plan §KB-06).

DOCX/PPTX originals are converted once into a deterministic preview PDF next
to the stored originals; parsing and review coordinates bind to that preview
version while the original stays downloadable. Conversion runs headless with
a fixed profile directory under the system temp dir.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .parsers import DocumentParseError
from .settings import DocumentSettings


def preview_path(storage_dir: Path, version_id: str) -> Path:
    """Deterministic preview location for one artifact version."""
    return (storage_dir / "previews" / f"{version_id}.pdf").resolve()


def soffice_available(settings: DocumentSettings) -> bool:
    """Return whether the configured LibreOffice binary can run."""
    binary = shutil.which(settings.soffice_path)
    if not binary:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary from settings
            [binary, "--version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def convert_to_pdf(
    source: Path, destination: Path, settings: DocumentSettings
) -> Path:
    """Convert one Office original into its fixed preview PDF."""
    binary = shutil.which(settings.soffice_path)
    if not binary:
        raise DocumentParseError("document_office_preview_unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    produced = destination.parent / f"{source.stem}.pdf"
    produced.unlink(missing_ok=True)  # never reuse a stale same-stem output
    with tempfile.TemporaryDirectory(prefix="soffice-") as profile_dir:
        command = [
            binary,
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file:///{profile_dir.replace('\\', '/')}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(destination.parent),
            str(source),
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed binary from settings
                command,
                capture_output=True,
                timeout=settings.office_preview_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentParseError("document_office_preview_timeout") from exc
        except OSError as exc:
            raise DocumentParseError("document_office_preview_unavailable") from exc
    if result.returncode != 0 or not produced.is_file():
        raise DocumentParseError("document_office_preview_failed")
    if produced != destination:
        produced.replace(destination)
    return destination

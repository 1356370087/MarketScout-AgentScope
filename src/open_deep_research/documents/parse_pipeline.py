"""Worker-side parse routing: Docling, Office previews and local readers.

PDF, images and Office previews go through the self-hosted Docling Serve when
configured (with upstream-task persistence for restart recovery); XLSX, CSV,
Markdown and TXT use local structured readers. Every fallback to the legacy
parser is recorded as a quality flag instead of failing silently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from . import docling, office_preview, structuring
from .parsers import DocumentParseError, parse_document
from .settings import DocumentSettings
from .structuring import PreparedDocument

_PDF_MEDIA = "application/pdf"
_DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PPTX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


async def _via_docling(
    path: Path,
    filename: str,
    media_type: str,
    settings: DocumentSettings,
    *,
    resume_task_id: str | None,
    on_submitted: Callable[[str], Awaitable[None]] | None,
) -> PreparedDocument:
    # convert() reports the task id right after submit (or resume start).
    task_id, result = await docling.convert(
        path,
        filename,
        media_type,
        settings,
        resume_task_id=resume_task_id,
        on_submitted=on_submitted,
    )
    prepared = structuring.from_docling(result)
    prepared.upstream_task_id = task_id
    return prepared


def _office_preview_pdf(
    original: Path,
    settings: DocumentSettings,
    *,
    version_id: str,
    filename: str,
) -> Path:
    destination = office_preview.preview_path(settings.storage_dir, version_id)
    if destination.is_file():
        return destination
    office_preview.convert_to_pdf(original, destination, settings)
    return destination


def _pptx_notes(original: Path) -> list[structuring.StructuredUnit]:
    """Keep speaker notes from the original deck beside the preview units."""
    try:
        from pptx import Presentation
    except ImportError:
        return []
    units: list[structuring.StructuredUnit] = []
    try:
        presentation = Presentation(original)
    except Exception:  # noqa: BLE001 - notes are supplementary, never fatal
        return units
    for index, slide in enumerate(presentation.slides, 1):
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        except (AttributeError, KeyError):
            continue
        if notes:
            units.append(
                structuring.StructuredUnit(
                    unit_type="notes",
                    locator={"source": f"slide:{index}:notes", "slide": index},
                    raw_text=notes,
                    index_text=f"演讲备注：{notes}",
                    attributes={"parse_method": "python-pptx"},
                )
            )
    return units


async def parse_document_structured(
    original: Path,
    filename: str,
    media_type: str,
    settings: DocumentSettings,
    *,
    version_id: str | None = None,
    resume_task_id: str | None = None,
    on_submitted: Callable[[str], Awaitable[None]] | None = None,
) -> PreparedDocument:
    """Route one artifact to the configured parser and return its structure."""
    if media_type == _XLSX_MEDIA:
        return structuring.from_xlsx(original, settings)
    if media_type == "text/csv":
        return structuring.from_csv(original)
    if media_type in {"text/plain", "text/markdown"}:
        return structuring.from_light_text(original, markdown=media_type == "text/markdown")

    docling_ready = settings.docling_configured
    if media_type == _PDF_MEDIA or media_type.startswith("image/"):
        if docling_ready:
            return await _via_docling(
                original,
                filename,
                media_type,
                settings,
                resume_task_id=resume_task_id,
                on_submitted=on_submitted,
            )
        prepared = structuring.from_parsed_document(
            parse_document(original, media_type, settings), parse_method="legacy"
        )
        prepared.quality_flags.append("docling_not_configured")
        return prepared

    if media_type in {_DOCX_MEDIA, _PPTX_MEDIA}:
        if docling_ready:
            if version_id is None:
                raise DocumentParseError("document_version_context_missing")
            try:
                preview = _office_preview_pdf(
                    original, settings, version_id=version_id, filename=filename
                )
            except DocumentParseError:
                if not office_preview.soffice_available(settings):
                    prepared = structuring.from_parsed_document(
                        parse_document(original, media_type, settings),
                        parse_method="legacy",
                    )
                    prepared.quality_flags.append("office_preview_skipped")
                    return prepared
                raise
            prepared = await _via_docling(
                preview,
                f"{filename}.preview.pdf",
                _PDF_MEDIA,
                settings,
                resume_task_id=resume_task_id,
                on_submitted=on_submitted,
            )
            prepared.quality_flags.append("office_preview_used")
            if media_type == _PPTX_MEDIA:
                prepared.units.extend(_pptx_notes(original))
            return prepared
        prepared = structuring.from_parsed_document(
            parse_document(original, media_type, settings), parse_method="legacy"
        )
        prepared.quality_flags.append("docling_not_configured")
        return prepared

    prepared = structuring.from_parsed_document(
        parse_document(original, media_type, settings), parse_method="legacy"
    )
    return prepared

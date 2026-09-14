"""FastAPI routes for the personal My Documents library."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from . import versioning
from .database import document_schema_available
from .repository import (
    DocumentConflictError,
    DocumentQuotaError,
    create_document,
    get_chunk,
    get_document,
    get_document_summary,
    list_chunks,
    list_documents,
    retry_document,
    soft_delete_document,
)
from .settings import get_document_settings
from .storage import (
    DocumentUploadError,
    commit_upload,
    delete_storage_key,
    resolve_storage_key,
    stage_upload,
)

router = APIRouter(prefix="/documents", tags=["documents"])

_UPLOAD_ERROR_STATUS = {
    "document_file_too_large": 413,
    "document_archive_too_many_entries": 413,
    "document_archive_ratio_exceeded": 413,
    "document_archive_expanded_size_exceeded": 413,
    "document_file_empty": 422,
}


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="document_research_unavailable")


async def _require_document(user: Principal, document_id: str, capability: str) -> None:
    """Gate one document route on workspace/kb capabilities (plan §2.4)."""
    from open_deep_research.knowledge.authz import document_access

    access = await document_access(user.user_id, document_id)
    if not access or capability not in access["capabilities"]:
        raise HTTPException(status_code=404, detail="document_not_found")


@router.post("", status_code=202)
async def upload_document(
    file: Annotated[UploadFile, File(description="One supported research document")],
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
    knowledge_base_id: Annotated[
        str | None, Form(description="Target knowledge base; default personal space")
    ] = None,
) -> dict[str, Any]:
    """Stream, validate and queue one document into its home knowledge base."""
    _ensure_enabled()
    from open_deep_research.knowledge.authz import (
        AuthorizationError,
        require_kb_capability,
    )

    if knowledge_base_id:
        try:
            await require_kb_capability(
                user.user_id, knowledge_base_id, "submit"
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    settings = get_document_settings()
    storage_key: str | None = None
    blob_created = False
    try:
        staged = await stage_upload(file, settings)
        storage_key, blob_created = commit_upload(staged, user.user_id, settings)
        document, deduplicated = await create_document(
            user.user_id, staged, storage_key, settings,
            knowledge_base_id=knowledge_base_id,
        )
        if deduplicated and blob_created:
            delete_storage_key(storage_key, settings)
    except DocumentUploadError as exc:
        code = str(exc).split(":", 1)[0]
        raise HTTPException(
            status_code=_UPLOAD_ERROR_STATUS.get(code, 415), detail=str(exc)
        ) from exc
    except DocumentQuotaError as exc:
        if storage_key and blob_created:
            delete_storage_key(storage_key, settings)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"document": document.model_dump(mode="json"), "deduplicated": deduplicated}


@router.get("")
async def documents(
    q: str = Query(default="", max_length=160),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List and filter the caller's active personal documents."""
    _ensure_enabled()
    if status and status not in {"queued", "processing", "ready", "failed", "deleting"}:
        raise HTTPException(status_code=422, detail="invalid_document_status")
    items, total = await list_documents(
        user.user_id, query=q, status=status, limit=limit, offset=offset
    )
    return {"items": [item.model_dump(mode="json") for item in items], "total": total}


@router.get("/{document_id}")
async def document_detail(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return metadata for one readable document."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    item = await get_document_summary(user.user_id, document_id)
    if not item:
        raise HTTPException(status_code=404, detail="document_not_found")
    return item.model_dump(mode="json")


@router.get("/{document_id}/content")
async def document_content(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> FileResponse:
    """Download an original, including a soft-deleted document kept by a Run binding."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    row = await get_document(user.user_id, document_id, include_deleted=True)
    if not row:
        raise HTTPException(status_code=404, detail="document_not_found")
    path = resolve_storage_key(row["storage_key"], get_document_settings())
    if not path.is_file():
        raise HTTPException(status_code=404, detail="document_content_not_found")
    return FileResponse(
        path, media_type=row["media_type"], filename=Path(row["filename"]).name
    )


@router.get("/{document_id}/chunks/{chunk_id}")
async def document_chunk(
    document_id: str,
    chunk_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one precise chunk for citation navigation."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    item = await get_chunk(user.user_id, document_id, chunk_id)
    if not item:
        raise HTTPException(status_code=404, detail="document_chunk_not_found")
    return item.model_dump(mode="json")


@router.get("/{document_id}/chunks")
async def document_chunks(
    document_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List bounded extracted chunks for preview and citation navigation."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    items = await list_chunks(user.user_id, document_id, limit=limit, offset=offset)
    if items is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.post("/{document_id}/retry", status_code=202)
async def retry(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Retry one failed document (contributors and up)."""
    _ensure_enabled()
    await _require_document(user, document_id, "submit")
    item = await retry_document(user.user_id, document_id)
    if not item:
        raise HTTPException(status_code=404, detail="document_not_found_or_not_failed")
    return item.model_dump(mode="json")


class GenerationDecisionRequest(BaseModel):
    """A human review decision with an optional reason."""

    reason: str = Field(default="", max_length=1000)


@router.post("/{document_id}/reindex", status_code=202)
async def reindex(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Queue a fresh draft generation for the current published version."""
    _ensure_enabled()
    await _require_document(user, document_id, "submit")
    try:
        generation_id = await versioning.queue_reindex_generation(
            user.user_id, document_id
        )
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not generation_id:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"document_id": document_id, "generation_id": generation_id, "status": "draft"}


@router.post("/{document_id}/versions", status_code=202)
async def upload_version(
    document_id: str,
    file: Annotated[UploadFile, File(description="A new artifact version")],
    note: Annotated[str, Form(description="Version note")] = "",
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Upload a new artifact version; the current published one keeps serving."""
    _ensure_enabled()
    await _require_document(user, document_id, "submit")
    settings = get_document_settings()
    storage_key: str | None = None
    blob_created = False
    try:
        staged = await stage_upload(file, settings)
        storage_key, blob_created = commit_upload(staged, user.user_id, settings)
        result = await versioning.add_document_version(
            user.user_id, document_id, staged, storage_key, note=note
        )
    except DocumentUploadError as exc:
        code = str(exc).split(":", 1)[0]
        raise HTTPException(
            status_code=_UPLOAD_ERROR_STATUS.get(code, 415), detail=str(exc)
        ) from exc
    except DocumentQuotaError as exc:
        if storage_key and blob_created:
            delete_storage_key(storage_key, settings)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        if storage_key and blob_created:
            delete_storage_key(storage_key, settings)
        raise HTTPException(status_code=404, detail="document_not_found")
    summary = await get_document_summary(user.user_id, document_id)
    return {"document": summary.model_dump(mode="json") if summary else None, **result}


@router.get("/{document_id}/versions")
async def document_versions(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List artifact versions with their newest generation status."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    items = await versioning.list_versions(user.user_id, document_id)
    if items is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"items": items}


@router.get("/{document_id}/generations")
async def document_generations(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List parse generations of one readable document, newest first."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    items = await versioning.list_generations(user.user_id, document_id)
    if items is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {"items": items}


@router.get("/{document_id}/generations/{generation_id}")
async def document_generation_review(
    document_id: str,
    generation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one generation with units, metadata candidates and quality flags."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    item = await versioning.generation_review_detail(
        user.user_id, document_id, generation_id, limit=limit, offset=offset
    )
    if not item:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return item


@router.post("/{document_id}/generations/{generation_id}/publish")
async def publish_generation(
    document_id: str,
    generation_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Publish one reviewable generation and move the current pointer."""
    _ensure_enabled()
    await _require_document(user, document_id, "review_publish")
    try:
        item = await versioning.publish_generation(
            user.user_id, document_id, generation_id
        )
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return item


@router.post("/{document_id}/generations/{generation_id}/reject")
async def reject_generation(
    document_id: str,
    generation_id: str,
    request: GenerationDecisionRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Reject a not-yet-published generation with a recorded reason."""
    _ensure_enabled()
    await _require_document(user, document_id, "review_publish")
    try:
        item = await versioning.reject_generation(
            user.user_id, document_id, generation_id, reason=request.reason
        )
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return item


class UnitCorrectionRequest(BaseModel):
    """One unit-level edit inside a correction batch."""

    unit_id: str = Field(min_length=1, max_length=64)
    revised_text: str | None = Field(default=None, max_length=200_000)
    excluded: bool | None = None
    exclusion_reason: str | None = Field(default=None, max_length=1000)
    attributes_patch: dict[str, Any] | None = None


class SplitTableRequest(BaseModel):
    """Split one table unit at a row boundary."""

    unit_id: str = Field(min_length=1, max_length=64)
    at_row: int = Field(ge=1)


class CorrectionRequest(BaseModel):
    """One optimistic-locked correction batch for a reviewable generation."""

    revision: int = Field(ge=0)
    unit_corrections: list[UnitCorrectionRequest] = Field(
        default_factory=list, max_length=200
    )
    metadata_confirmed: dict[str, Any] = Field(default_factory=dict)
    merge_tables: list[list[str]] = Field(default_factory=list, max_length=50)
    split_table: SplitTableRequest | None = None
    reason: str = Field(default="", max_length=1000)


class ReparseRequest(BaseModel):
    """Scope for a partial re-parse of a published generation."""

    pages: list[int] = Field(default_factory=list, max_length=200)
    sheets: list[str] = Field(default_factory=list, max_length=100)
    unit_ids: list[str] = Field(default_factory=list, max_length=200)
    reason: str = Field(default="", max_length=1000)


@router.post("/{document_id}/generations/{generation_id}/corrections")
async def apply_generation_corrections(
    document_id: str,
    generation_id: str,
    request: CorrectionRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Save human corrections under the generation's revision counter."""
    _ensure_enabled()
    await _require_document(user, document_id, "submit")
    from .corrections import CorrectionRevisionError, apply_corrections

    try:
        result = await apply_corrections(
            user.user_id,
            document_id,
            generation_id,
            revision=request.revision,
            unit_corrections=[
                correction.model_dump(exclude_none=True)
                for correction in request.unit_corrections
            ],
            metadata_confirmed=request.metadata_confirmed,
            merge_tables=request.merge_tables,
            split_table=request.split_table.model_dump()
            if request.split_table
            else None,
            reason=request.reason,
        )
    except CorrectionRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentQuotaError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return result


@router.post("/{document_id}/generations/{generation_id}/reparse", status_code=202)
async def queue_generation_reparse(
    document_id: str,
    generation_id: str,
    request: ReparseRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create a draft copy of a published generation with a scoped re-parse."""
    _ensure_enabled()
    await _require_document(user, document_id, "submit")
    from . import reparse

    try:
        draft_id = await reparse.queue_scoped_reparse(
            user.user_id,
            document_id,
            generation_id,
            pages=request.pages,
            sheets=request.sheets,
            unit_ids=request.unit_ids,
            reason=request.reason,
        )
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not draft_id:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return {"document_id": document_id, "generation_id": draft_id, "status": "draft"}


@router.get("/{document_id}/generations/{generation_id}/diff")
async def generation_diff(
    document_id: str,
    generation_id: str,
    against: str | None = Query(default=None, max_length=64),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return the complete pre-publish difference against the baseline."""
    _ensure_enabled()
    await _require_document(user, document_id, "view_published")
    from .versioning import GenerationNotFoundError

    try:
        result = await versioning.diff_generations(
            user.user_id, document_id, generation_id,
            against_generation_id=against,
        )
    except GenerationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="generation_not_found")
    return result


@router.post("/{document_id}/versions/{version_id}/withdraw")
async def withdraw_version(
    document_id: str,
    version_id: str,
    request: GenerationDecisionRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Withdraw a version's published generation and stop new retrieval."""
    _ensure_enabled()
    await _require_document(user, document_id, "review_publish")
    result = await versioning.withdraw_version(
        user.user_id, document_id, version_id, reason=request.reason
    )
    if not result:
        raise HTTPException(status_code=404, detail="version_not_publishable")
    return result


@router.delete("/{document_id}", status_code=202)
async def delete_document_route(
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Move one document into the recycle bin (managers, 30-day retention)."""
    _ensure_enabled()
    await _require_document(user, document_id, "manage")
    row = await soft_delete_document(user.user_id, document_id)
    if not row:
        raise HTTPException(status_code=404, detail="document_not_found")
    return {
        "id": str(row["id"]),
        "status": "trashed",
        "purge_after": row["purge_after"].isoformat() if row["purge_after"] else None,
    }

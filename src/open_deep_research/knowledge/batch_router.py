"""Batch operations and recycle-bin routes (KB-14 / KB-15)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from open_deep_research.documents.database import document_schema_available
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from . import authz

router = APIRouter(tags=["knowledge"])


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="knowledge_base_unavailable")


def _forbidden(exc: Exception) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


class BatchRequest(BaseModel):
    """One bulk operation over a fixed target set."""

    operation: str = Field(pattern="^(reparse|trash|retry)$")
    document_ids: list[str] = Field(min_length=1, max_length=100)
    knowledge_base_id: str | None = Field(default=None, max_length=64)


class BatchExecuteRequest(BaseModel):
    """Execute a pending batch now (or the worker picks it up)."""

    batch_id: str = Field(min_length=1, max_length=64)


@router.post("/knowledge/batches", status_code=202)
async def create_batch(
    body: BatchRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create one batch with per-item tracking; execution is separate."""
    _ensure_enabled()
    from .batches import BatchError, execute_batch
    from .batches import create_batch as create

    if body.knowledge_base_id:
        try:
            await authz.require_kb_capability(
                user.user_id, body.knowledge_base_id, authz.CAP_SUBMIT
            )
        except authz.AuthorizationError as exc:
            raise _forbidden(exc) from exc
    try:
        result = await create(
            user.user_id,
            body.operation,
            body.document_ids,
            knowledge_base_id=body.knowledge_base_id,
        )
        executed = await execute_batch(user.user_id, result["batch_id"])
    except BatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**result, **executed}


@router.get("/knowledge/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one batch with per-item results and failure details."""
    _ensure_enabled()
    from .batches import get_batch as get

    result = await get(user.user_id, batch_id)
    if not result:
        raise HTTPException(status_code=404, detail="batch_not_found")
    return result


@router.post("/knowledge/batches/{batch_id}/cancel")
async def cancel_batch(
    batch_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Cancel pending items; running items finish at their checkpoint."""
    _ensure_enabled()
    from .batches import cancel_batch as cancel

    return await cancel(user.user_id, batch_id)


@router.post("/knowledge/batches/{batch_id}/retry-failed", status_code=202)
async def retry_failed(
    batch_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Reset failed items to pending; successful items are never re-run."""
    _ensure_enabled()
    from .batches import execute_batch
    from .batches import retry_failed as retry

    result = await retry(user.user_id, batch_id)
    if result.get("status") == "pending":
        executed = await execute_batch(user.user_id, batch_id)
        result.update(executed)
    return result


@router.get("/knowledge-bases/{knowledge_base_id}/trash")
async def list_trash(
    knowledge_base_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List trashed documents in one knowledge base (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .trash import list_trash as list_

    return await list_(
        user.user_id, knowledge_base_id, limit=limit, offset=offset
    )


@router.post("/knowledge-bases/{knowledge_base_id}/trash/{document_id}/restore")
async def restore_from_trash(
    knowledge_base_id: str,
    document_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Restore one trashed document (managers only, guarded transition)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .trash import restore_document

    result = await restore_document(user.user_id, document_id)
    if not result:
        raise HTTPException(status_code=404, detail="document_not_in_trash")
    return result


@router.post("/knowledge/purge-due", status_code=202)
async def purge_due(
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Purge documents past their retention window (worker / admin entry)."""
    _ensure_enabled()
    from .trash import purge_due_documents

    purged = await purge_due_documents(user.user_id)
    return {"purged": purged, "count": len(purged)}

class SyncSourceRequest(BaseModel):
    """Bind one URL to a logical document for scheduled syncing."""

    document_id: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    refresh_mode: str = Field(default="daily", pattern="^(manual|daily|weekly)$")


@router.post("/knowledge-bases/{knowledge_base_id}/sync-sources", status_code=201)
async def create_sync_source(
    knowledge_base_id: str,
    body: SyncSourceRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create one web sync source (managers only, URL validated)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from open_deep_research.documents.repository import DocumentConflictError

    from .sync import SyncError
    from .sync import create_sync_source as create

    try:
        result = await create(
            user.user_id, knowledge_base_id, body.document_id, body.url,
            refresh_mode=body.refresh_mode,
        )
    except SyncError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DocumentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.post("/knowledge-bases/{knowledge_base_id}/sync-sources/{source_id}/refresh")
async def refresh_sync_source(
    knowledge_base_id: str,
    source_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Manually trigger one sync run (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .sync import run_sync

    return await run_sync(user.user_id, source_id)


@router.get("/knowledge-bases/{knowledge_base_id}/source-relations")
async def list_source_relations(
    knowledge_base_id: str,
    confirmed: bool | None = Query(default=None),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List duplicate/suspect relations for one knowledge base."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_VIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .dedup import list_relations

    items = await list_relations(
        user.user_id, knowledge_base_id, confirmed=confirmed
    )
    return {"items": items}


@router.post("/knowledge-bases/{knowledge_base_id}/source-relations/{relation_id}/confirm")
async def confirm_source_relation(
    knowledge_base_id: str,
    relation_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Confirm one suspected relation (managers only, affects corroboration)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .dedup import confirm_relation

    result = await confirm_relation(user.user_id, relation_id)
    if not result:
        raise HTTPException(status_code=404, detail="relation_not_found_or_confirmed")
    return result


@router.post("/knowledge-bases/{knowledge_base_id}/source-relations/{relation_id}/reject")
async def reject_source_relation(
    knowledge_base_id: str,
    relation_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Reject one suspected relation (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .dedup import reject_relation

    if not await reject_relation(user.user_id, relation_id):
        raise HTTPException(status_code=404, detail="relation_not_found_or_confirmed")
    return {"id": relation_id, "status": "rejected"}

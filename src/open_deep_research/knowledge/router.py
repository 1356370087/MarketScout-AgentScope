"""FastAPI routes for knowledge bases, collections and document links (KB-01)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from open_deep_research.documents.database import document_schema_available
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from .contracts import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeCollectionCreate,
    KnowledgeCollectionUpdate,
    KnowledgeDocumentLinkRequest,
)
from .repository import (
    KnowledgeConflictError,
    create_collection,
    create_knowledge_base,
    delete_collection,
    get_knowledge_base,
    link_documents,
    list_collections,
    list_knowledge_bases,
    list_knowledge_documents,
    set_knowledge_base_archived,
    unlink_document,
    update_collection,
    update_knowledge_base,
)

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="knowledge_base_unavailable")


@router.post("", status_code=201)
async def create_kb(
    request: KnowledgeBaseCreate,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create a knowledge base: default personal space, or a team workspace."""
    _ensure_enabled()
    from . import authz

    if request.workspace_id:
        try:
            role = await authz.require_workspace_member(user.user_id, request.workspace_id)
            if role not in {"owner", "admin"}:
                raise authz.AuthorizationError("workspace_admin_required")
        except authz.AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        item = await create_knowledge_base(
            user.user_id, request.name, request.description,
            workspace_id=request.workspace_id,
        )
    except KnowledgeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return item.model_dump(mode="json")


@router.get("")
async def knowledge_bases(
    archived: bool | None = Query(default=None),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List knowledge bases the caller can read, optionally by archive state."""
    _ensure_enabled()
    from . import authz

    readable = await authz.readable_kb_ids(user.user_id)
    if not readable:
        return {"items": []}
    items = await list_knowledge_bases(
        user.user_id, archived=archived, restrict_ids=readable
    )
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.get("/{kb_id}")
async def knowledge_base_detail(
    kb_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one readable knowledge base, archived or not."""
    _ensure_enabled()
    from . import authz

    if authz.CAP_VIEW not in await authz.kb_capabilities(user.user_id, kb_id):
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    item = await get_knowledge_base(user.user_id, kb_id)
    if not item:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return item.model_dump(mode="json")


@router.patch("/{kb_id}")
async def update_kb(
    kb_id: str,
    request: KnowledgeBaseUpdate,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Rename or re-describe one knowledge base (managers only)."""
    _ensure_enabled()
    from . import authz

    try:
        await authz.require_kb_capability(user.user_id, kb_id, authz.CAP_MANAGE)
    except authz.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        item = await update_knowledge_base(
            user.user_id,
            kb_id,
            name=request.name,
            description=request.description,
        )
    except KnowledgeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return item.model_dump(mode="json")


@router.post("/{kb_id}/archive")
async def archive_kb(
    kb_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Archive one knowledge base; its material stays explicit-openable."""
    _ensure_enabled()
    from . import authz

    try:
        await authz.require_kb_capability(user.user_id, kb_id, authz.CAP_MANAGE)
    except authz.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    item = await set_knowledge_base_archived(user.user_id, kb_id, archived=True)
    if not item:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return item.model_dump(mode="json")


@router.post("/{kb_id}/restore")
async def restore_kb(
    kb_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Restore one archived knowledge base."""
    _ensure_enabled()
    from . import authz

    try:
        await authz.require_kb_capability(user.user_id, kb_id, authz.CAP_MANAGE)
    except authz.AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    item = await set_knowledge_base_archived(user.user_id, kb_id, archived=False)
    if not item:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return item.model_dump(mode="json")


@router.post("/{kb_id}/collections", status_code=201)
async def create_kb_collection(
    kb_id: str,
    request: KnowledgeCollectionCreate,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create one single-level collection inside an owned knowledge base."""
    _ensure_enabled()
    try:
        item = await create_collection(
            user.user_id, kb_id, request.name, request.description
        )
    except KnowledgeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return item.model_dump(mode="json")


@router.get("/{kb_id}/collections")
async def kb_collections(
    kb_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List collections of one owned knowledge base."""
    _ensure_enabled()
    items = await list_collections(user.user_id, kb_id)
    if items is None:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.patch("/{kb_id}/collections/{collection_id}")
async def update_kb_collection(
    kb_id: str,
    collection_id: str,
    request: KnowledgeCollectionUpdate,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Rename or re-describe one collection."""
    _ensure_enabled()
    try:
        item = await update_collection(
            user.user_id,
            kb_id,
            collection_id,
            name=request.name,
            description=request.description,
        )
    except KnowledgeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="collection_not_found")
    return item.model_dump(mode="json")


@router.delete("/{kb_id}/collections/{collection_id}")
async def delete_kb_collection(
    kb_id: str,
    collection_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Remove one collection; associations go with it, documents survive."""
    _ensure_enabled()
    removed = await delete_collection(user.user_id, kb_id, collection_id)
    if not removed:
        raise HTTPException(status_code=404, detail="collection_not_found")
    return {"id": collection_id, "removed": "collection"}


@router.get("/{kb_id}/documents")
async def kb_documents(
    kb_id: str,
    collection_id: str | None = Query(default=None),
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List documents linked to one knowledge base or one of its collections."""
    _ensure_enabled()
    result = await list_knowledge_documents(
        user.user_id,
        kb_id,
        collection_id=collection_id,
        query=q,
        limit=limit,
        offset=offset,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    items, total = result
    return {"items": [item.model_dump(mode="json") for item in items], "total": total}


@router.post("/{kb_id}/documents")
async def link_kb_documents(
    kb_id: str,
    request: KnowledgeDocumentLinkRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Associate already-uploaded owned documents, optionally with a collection."""
    _ensure_enabled()
    try:
        result = await link_documents(
            user.user_id,
            kb_id,
            request.document_ids,
            collection_id=request.collection_id,
        )
    except KeyError as exc:
        # Cross-owner or unknown resources are uniformly not-found.
        missing = str(exc.args[0]) if exc.args else "link_failed"
        raise HTTPException(status_code=404, detail=missing) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return result


@router.delete("/{kb_id}/documents/{document_id}")
async def unlink_kb_document(
    kb_id: str,
    document_id: str,
    collection_id: str | None = Query(default=None),
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Remove one document's association; the document itself is never deleted."""
    _ensure_enabled()
    removed = await unlink_document(
        user.user_id, kb_id, document_id, collection_id=collection_id
    )
    if removed is None:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return {"document_id": document_id, "removed_links": removed}

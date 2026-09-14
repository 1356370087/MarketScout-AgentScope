"""Knowledge-base business health and coverage targets."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.documents.database import (
    document_schema_available,
    get_document_pool,
)
from open_deep_research.documents.identity import document_owner_id
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from . import authz, health

router = APIRouter(prefix="/knowledge-bases/{kb_id}/health", tags=["knowledge"])


async def _require(actor, kb, capability):
    if not document_schema_available():
        raise HTTPException(503, "knowledge_base_unavailable")
    try:
        await authz.require_kb_capability(actor, str(kb), capability)
    except authz.AuthorizationError as exc:
        raise HTTPException(403, str(exc)) from exc


class Target(BaseModel):
    """One expected topic for a named competitor and business period."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    period: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=200)
    min_documents: int = Field(default=1, ge=1, le=100)
    max_age_days: int = Field(default=0, ge=0, le=3650)


@router.get("")
async def get_health(
    kb_id: UUID,
    company: str = Query("", max_length=200),
    period: str = Query("", max_length=100),
    kind: str = Query(
        "", pattern="^(expired|missing_topic|pending_review|sync_failed|no_results)?$"
    ),
    days: int = Query(30, ge=1, le=365),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
):
    """Return aggregate business health and paginated actionable details."""
    await _require(user.user_id, kb_id, authz.CAP_REVIEW)
    return await health.dashboard(
        user.user_id, str(kb_id), company, period, kind, days, offset, limit
    )


@router.put("/targets")
async def set_target(
    kb_id: UUID,
    body: Target,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Create or update one coverage requirement without deleting other targets."""
    await _require(user.user_id, kb_id, authz.CAP_MANAGE)
    pool = await get_document_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """INSERT INTO knowledge_health_targets(knowledge_base_id,company,period,topic,min_documents,max_age_days,created_by)
            VALUES($1,$2,$3,$4,$5,$6,$7::uuid) ON CONFLICT(knowledge_base_id,company,period,topic)
            DO UPDATE SET min_documents=EXCLUDED.min_documents,max_age_days=EXCLUDED.max_age_days,updated_at=now() RETURNING id""",
            kb_id,
            body.company,
            body.period,
            body.topic,
            body.min_documents,
            body.max_age_days,
            document_owner_id(user.user_id),
        )
    return {"id": str(row["id"]), **body.model_dump()}


@router.delete("/targets/{target_id}")
async def delete_target(
    kb_id: UUID,
    target_id: UUID,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Delete a target only inside the authorized knowledge base."""
    await _require(user.user_id, kb_id, authz.CAP_MANAGE)
    pool = await get_document_pool()
    async with pool.acquire() as c:
        deleted = await c.fetchval(
            "DELETE FROM knowledge_health_targets WHERE id=$1 AND knowledge_base_id=$2 RETURNING id",
            target_id,
            kb_id,
        )
    if not deleted:
        raise HTTPException(404, "health_target_not_found")
    return {"deleted": True}

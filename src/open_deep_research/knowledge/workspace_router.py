"""Workspace and knowledge-base membership routes (KB-09).

Personal spaces are self-provisioned; team workspaces start with their
creator as owner. Every sensitive action lands in the audit ledger, and
ownership transfer refuses to strip the last owner.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from open_deep_research.documents.database import document_schema_available
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from . import authz

router = APIRouter(prefix="/workspaces", tags=["knowledge"])

VALID_MEMBER_ROLES = {"admin", "member"}
VALID_BASE_ROLES = {"viewer", "contributor", "manager"}


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="knowledge_base_unavailable")


class WorkspaceCreateRequest(BaseModel):
    """One team workspace; personal spaces are auto-provisioned instead."""

    name: str = Field(min_length=1, max_length=200)


class WorkspaceMemberRequest(BaseModel):
    """Add or update one workspace member (owner role only via transfer)."""

    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(admin|member)$")


class OwnershipTransferRequest(BaseModel):
    """Transfer ownership; the current owner stays as admin."""

    to_user_id: str = Field(min_length=1, max_length=64)


class BaseMemberRequest(BaseModel):
    """Grant or update one knowledge-base member role."""

    user_id: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(viewer|contributor|manager)$")


class VisibilityRequest(BaseModel):
    """Switch a knowledge base between team-visible and restricted."""

    visibility: str = Field(pattern="^(team|restricted)$")


def _forbidden(exc: authz.AuthorizationError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


async def _workspace_pool():
    from open_deep_research.documents.database import get_document_pool

    return await get_document_pool()


@router.get("")
async def list_workspaces(
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List workspaces the caller belongs to, with their knowledge bases."""
    _ensure_enabled()
    await authz.ensure_personal_workspace(user.user_id)
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT w.id, w.kind, w.name, w.status, m.role,
                      (SELECT count(*) FROM knowledge_bases kb
                        WHERE kb.workspace_id=w.id) AS knowledge_base_count
                 FROM knowledge_workspaces w
                 JOIN knowledge_workspace_members m ON m.workspace_id=w.id
                WHERE m.user_id=$1::uuid AND w.status='active'
                ORDER BY w.kind DESC, w.created_at""",
            user.user_id,
        )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "kind": row["kind"],
                "name": row["name"],
                "status": row["status"],
                "role": row["role"],
                "knowledge_base_count": int(row["knowledge_base_count"]),
            }
            for row in rows
        ]
    }


@router.post("", status_code=201)
async def create_workspace(
    body: WorkspaceCreateRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create one team workspace; the creator becomes its owner."""
    _ensure_enabled()
    pool = await _workspace_pool()
    async with pool.acquire() as connection, connection.transaction():
        workspace_id = await connection.fetchval(
            """INSERT INTO knowledge_workspaces(kind, name, created_by)
               VALUES ('team', $1, $2::uuid) RETURNING id""",
            body.name,
            user.user_id,
        )
        await connection.execute(
            """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
               VALUES ($1::uuid, $2::uuid, 'owner')""",
            workspace_id,
            user.user_id,
        )
    await authz.record_audit(
        actor_id=user.user_id,
        workspace_id=str(workspace_id),
        action="workspace.create",
        after={"name": body.name},
    )
    return {"id": str(workspace_id), "kind": "team", "name": body.name}


@router.get("/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List members of one workspace the caller belongs to."""
    _ensure_enabled()
    try:
        await authz.require_workspace_member(user.user_id, workspace_id)
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT user_id, role, created_at FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid ORDER BY created_at""",
            workspace_id,
        )
    return {
        "items": [
            {"user_id": str(row["user_id"]), "role": row["role"],
             "created_at": row["created_at"].isoformat()}
            for row in rows
        ]
    }


@router.put("/{workspace_id}/members", status_code=201)
async def upsert_workspace_member(
    workspace_id: str,
    body: WorkspaceMemberRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Add or update a member; workspace owner/admin only, audited."""
    _ensure_enabled()
    try:
        role = await authz.require_workspace_member(user.user_id, workspace_id)
        if role not in {"owner", "admin"}:
            raise authz.AuthorizationError("workspace_admin_required")
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
               VALUES ($1::uuid, $2::uuid, $3)
               ON CONFLICT (workspace_id, user_id)
               DO UPDATE SET role=excluded.role""",
            workspace_id,
            body.user_id,
            body.role,
        )
    await authz.record_audit(
        actor_id=user.user_id,
        workspace_id=workspace_id,
        action="workspace.member.upsert",
        target={"user_id": body.user_id},
        after={"role": body.role},
    )
    return {"workspace_id": workspace_id, "user_id": body.user_id, "role": body.role}


@router.delete("/{workspace_id}/members/{member_user_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_user_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Remove one member; the last owner can never be removed."""
    _ensure_enabled()
    try:
        role = await authz.require_workspace_member(user.user_id, workspace_id)
        if role not in {"owner", "admin"}:
            raise authz.AuthorizationError("workspace_admin_required")
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        owners = await connection.fetchval(
            """SELECT count(*) FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid AND role='owner'""",
            workspace_id,
        )
        target_role = await connection.fetchval(
            """SELECT role FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
            workspace_id,
            member_user_id,
        )
        if target_role == "owner" and int(owners) <= 1:
            raise HTTPException(status_code=409, detail="last_owner_not_removable")
        removed = await connection.execute(
            """DELETE FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
            workspace_id,
            member_user_id,
        )
    if removed != "DELETE 1":
        raise HTTPException(status_code=404, detail="member_not_found")
    await authz.record_audit(
        actor_id=user.user_id,
        workspace_id=workspace_id,
        action="workspace.member.remove",
        target={"user_id": member_user_id},
        before={"role": target_role},
    )
    return {"workspace_id": workspace_id, "removed": member_user_id}


@router.post("/{workspace_id}/transfer-ownership")
async def transfer_ownership(
    workspace_id: str,
    body: OwnershipTransferRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Transfer ownership; the previous owner stays as admin."""
    _ensure_enabled()
    try:
        role = await authz.require_workspace_member(user.user_id, workspace_id)
        if role != "owner":
            raise authz.AuthorizationError("workspace_owner_required")
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection, connection.transaction():
        target = await connection.fetchval(
            """SELECT 1 FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
            workspace_id,
            body.to_user_id,
        )
        if not target:
            raise HTTPException(status_code=404, detail="member_not_found")
        await connection.execute(
            """UPDATE knowledge_workspace_members
                  SET role='admin'
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid AND role='owner'""",
            workspace_id,
            user.user_id,
        )
        await connection.execute(
            """UPDATE knowledge_workspace_members
                  SET role='owner'
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
            workspace_id,
            body.to_user_id,
        )
    await authz.record_audit(
        actor_id=user.user_id,
        workspace_id=workspace_id,
        action="workspace.ownership.transfer",
        target={"to_user_id": body.to_user_id},
    )
    return {"workspace_id": workspace_id, "owner": body.to_user_id}


@router.get("/bases/{knowledge_base_id}/members")
async def list_base_members(
    knowledge_base_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List explicit knowledge-base member grants (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT user_id, role, created_at FROM knowledge_base_members
                WHERE knowledge_base_id=$1::uuid ORDER BY created_at""",
            knowledge_base_id,
        )
    return {
        "items": [
            {"user_id": str(row["user_id"]), "role": row["role"],
             "created_at": row["created_at"].isoformat()}
            for row in rows
        ]
    }


@router.put("/bases/{knowledge_base_id}/members", status_code=201)
async def upsert_base_member(
    knowledge_base_id: str,
    body: BaseMemberRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Grant or update one knowledge-base role; managers only, audited."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO knowledge_base_members(knowledge_base_id, user_id, role)
               VALUES ($1::uuid, $2::uuid, $3)
               ON CONFLICT (knowledge_base_id, user_id)
               DO UPDATE SET role=excluded.role""",
            knowledge_base_id,
            body.user_id,
            body.role,
        )
    await authz.record_audit(
        actor_id=user.user_id,
        knowledge_base_id=knowledge_base_id,
        action="kb.member.upsert",
        target={"user_id": body.user_id},
        after={"role": body.role},
    )
    return {"knowledge_base_id": knowledge_base_id,
            "user_id": body.user_id, "role": body.role}


@router.put("/bases/{knowledge_base_id}/visibility")
async def set_base_visibility(
    knowledge_base_id: str,
    body: VisibilityRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, str]:
    """Switch team-visible ↔ restricted; managers only, audited."""
    _ensure_enabled()
    try:
        context = await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    before = context["knowledge_base"]["visibility"]
    pool = await _workspace_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE knowledge_bases SET visibility=$2, updated_at=now() WHERE id=$1::uuid",
            knowledge_base_id,
            body.visibility,
        )
    await authz.record_audit(
        actor_id=user.user_id,
        knowledge_base_id=knowledge_base_id,
        action="kb.visibility.change",
        before={"visibility": before},
        after={"visibility": body.visibility},
    )
    return {"knowledge_base_id": knowledge_base_id, "visibility": body.visibility}

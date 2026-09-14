"""Unified workspace and knowledge-base authorization (plan §2.3/§2.4).

One entry point answers every knowledge-base capability question. Personal
workspaces seat exactly their owner (full capabilities); team workspaces
grant workspace owner/admin management over every base, members get viewer
on team-visible bases, and ``knowledge_base_members`` rows add or restrict
per-base roles. Revocation takes effect on the next request because every
check reads current database state.
"""

from __future__ import annotations

from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

CAP_VIEW = "view_published"
CAP_DOWNLOAD = "download"
CAP_SUBMIT = "submit"
CAP_REVIEW = "review_publish"
CAP_MANAGE = "manage"

# 权限矩阵（方案 §2.3）：贡献者含查看者能力，管理者含全部。
_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset({CAP_VIEW, CAP_DOWNLOAD}),
    "contributor": frozenset({CAP_VIEW, CAP_DOWNLOAD, CAP_SUBMIT}),
    "manager": frozenset(
        {CAP_VIEW, CAP_DOWNLOAD, CAP_SUBMIT, CAP_REVIEW, CAP_MANAGE}
    ),
}
_WORKSPACE_MANAGER_ROLES = frozenset({"owner", "admin"})


class AuthorizationError(RuntimeError):
    """Raised when the actor lacks the required capability."""


def _caps(role: str | None) -> frozenset[str]:
    return _ROLE_CAPABILITIES.get(role or "", frozenset())


async def workspace_role(user_id: str, workspace_id: str) -> str | None:
    """Return the actor's current workspace role, if any."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT role FROM knowledge_workspace_members
                WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
            workspace_id,
            document_owner_id(user_id),
        )
    return row["role"] if row else None


async def require_workspace_member(user_id: str, workspace_id: str) -> str:
    """Return the role or raise; membership is always read live."""
    role = await workspace_role(user_id, workspace_id)
    if role is None:
        raise AuthorizationError("workspace_membership_required")
    return role


async def kb_context(user_id: str, kb_id: str) -> dict[str, Any] | None:
    """Load one knowledge base with the actor's effective role resolved."""
    actor = document_owner_id(user_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        base = await connection.fetchrow(
            "SELECT * FROM knowledge_bases WHERE id=$1::uuid", kb_id
        )
        if not base:
            return None
        role: str | None = None
        source = "none"
        if base["workspace_id"]:
            ws_role = await connection.fetchval(
                """SELECT role FROM knowledge_workspace_members
                    WHERE workspace_id=$1::uuid AND user_id=$2::uuid""",
                base["workspace_id"],
                actor,
            )
            workspace = await connection.fetchrow(
                "SELECT kind FROM knowledge_workspaces WHERE id=$1::uuid",
                base["workspace_id"],
            )
            if workspace and workspace["kind"] == "personal":
                # 个人空间：唯一成员即管理者（方案 §2.3）。
                role = "manager" if ws_role == "owner" else None
                source = "personal"
            elif ws_role in _WORKSPACE_MANAGER_ROLES:
                role = "manager"
                source = "workspace"
            else:
                member_role = await connection.fetchval(
                    """SELECT role FROM knowledge_base_members
                        WHERE knowledge_base_id=$1::uuid AND user_id=$2::uuid""",
                    kb_id,
                    actor,
                )
                if member_role:
                    role = member_role
                    source = "base"
                elif ws_role == "member" and base["visibility"] == "team":
                    role = "viewer"
                    source = "workspace_default"
        legacy_owner = str(base["owner_id"]) == actor
        return {
            "knowledge_base": dict(base),
            "role": role,
            "role_source": source,
            # 个人空间兼容：老 owner_id 在无成员行时仍具管理者能力（迁移期）。
            "legacy_owner": legacy_owner and role is None,
        }


async def kb_capabilities(user_id: str, kb_id: str) -> frozenset[str]:
    """Return the actor's effective capability set on one knowledge base."""
    context = await kb_context(user_id, kb_id)
    if not context:
        return frozenset()
    if context["legacy_owner"]:
        return _ROLE_CAPABILITIES["manager"]
    return _caps(context["role"])


async def require_kb_capability(user_id: str, kb_id: str, capability: str) -> dict[str, Any]:
    """Return the kb context or raise when the capability is missing."""
    context = await kb_context(user_id, kb_id)
    if not context:
        raise AuthorizationError("knowledge_base_not_found_or_forbidden")
    if context["legacy_owner"]:
        return context
    if capability not in _caps(context["role"]):
        raise AuthorizationError(f"kb_capability_required:{capability}")
    return context


async def readable_kb_ids(user_id: str) -> list[str]:
    """Return every knowledge base the actor may read right now.

    The list feeds SQL scope filters so retrieval never sees material the
    actor cannot access (plan §2.4: filter inside the recall query).
    """
    actor = document_owner_id(user_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT kb.id
              FROM knowledge_bases kb
             WHERE kb.workspace_id IS NULL AND kb.owner_id = $1::uuid
            UNION
            SELECT kb.id
              FROM knowledge_bases kb
              JOIN knowledge_workspaces w ON w.id = kb.workspace_id
             WHERE w.kind = 'personal' AND w.created_by = $1::uuid
            UNION
            SELECT kb.id
              FROM knowledge_bases kb
              JOIN knowledge_workspace_members m ON m.workspace_id = kb.workspace_id
              JOIN knowledge_workspaces w ON w.id = kb.workspace_id
             WHERE m.user_id = $1::uuid
               AND (w.kind = 'personal' OR kb.visibility = 'team'
                    OR EXISTS (
                      SELECT 1 FROM knowledge_base_members bm
                       WHERE bm.knowledge_base_id = kb.id AND bm.user_id = $1::uuid))
            """,
            actor,
        )
    return [str(row["id"]) for row in rows]


async def resolve_readable_scope(
    user_id: str, requested_kb_ids: list[str]
) -> list[str]:
    """Intersect a requested knowledge-base scope with readable bases."""
    if not requested_kb_ids:
        return await readable_kb_ids(user_id)
    allowed = set(await readable_kb_ids(user_id))
    return [kb_id for kb_id in requested_kb_ids if kb_id in allowed]


async def document_access(user_id: str, document_id: str) -> dict[str, Any] | None:
    """Resolve the actor's capabilities on one document via its home base.

    Returns ``None`` when the document does not exist. Team documents are
    governed purely by membership; personal documents keep the legacy
    owner-id path during migration (the owner's personal space seats them
    as manager).
    """
    actor = document_owner_id(user_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT d.owner_id, d.home_knowledge_base_id
                 FROM research_documents d WHERE d.id=$1::uuid""",
            document_id,
        )
        if not row:
            return None
    if str(row["owner_id"]) == actor and not row["home_knowledge_base_id"]:
        return {"role": "manager", "capabilities": _ROLE_CAPABILITIES["manager"]}
    if not row["home_knowledge_base_id"]:
        foreign = str(row["owner_id"]) != actor
        return None if foreign else {"role": "manager", "capabilities": _ROLE_CAPABILITIES["manager"]}
    capabilities = await kb_capabilities(actor, str(row["home_knowledge_base_id"]))
    return {"role": None, "capabilities": capabilities}


async def authorize_document_version(
    user_id: str, document_id: str, generation_id: str
) -> dict[str, Any]:
    """Authorize reading one document generation (citations and previews)."""
    access = await document_access(user_id, document_id)
    if not access or CAP_VIEW not in access["capabilities"]:
        # 保存过片段 ID 或历史链接不代表撤权后仍可访问（方案 KB-09）。
        raise AuthorizationError("document_access_denied")
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT 1 FROM research_document_generations
                WHERE id=$1::uuid AND document_id=$2::uuid""",
            generation_id,
            document_id,
        )
    if not row:
        raise AuthorizationError("generation_not_found")
    return access


async def record_audit(
    *,
    actor_id: str,
    action: str,
    workspace_id: str | None = None,
    knowledge_base_id: str | None = None,
    target: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append one team audit event; failures never break the operation."""
    import json

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO knowledge_audit_events
               (actor_id, workspace_id, knowledge_base_id, action, target,
                before, after, request_id)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4,
                       CAST($5 AS jsonb), CAST($6 AS jsonb), CAST($7 AS jsonb), $8)""",
            document_owner_id(actor_id),
            workspace_id,
            knowledge_base_id,
            action,
            json.dumps(target or {}, ensure_ascii=False, default=str),
            json.dumps(before or {}, ensure_ascii=False, default=str),
            json.dumps(after or {}, ensure_ascii=False, default=str),
            request_id,
        )


async def ensure_personal_workspace(user_id: str) -> str:
    """Return the actor's personal workspace, creating it on first use."""
    actor = document_owner_id(user_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        workspace_id = await connection.fetchval(
            """INSERT INTO knowledge_workspaces(kind, name, created_by)
               VALUES ('personal', '个人空间', $1::uuid)
               ON CONFLICT DO NOTHING RETURNING id""",
            actor,
        )
        if workspace_id is None:
            workspace_id = await connection.fetchval(
                """SELECT id FROM knowledge_workspaces
                    WHERE kind='personal' AND created_by=$1::uuid""",
                actor,
            )
        await connection.execute(
            """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
               VALUES ($1::uuid, $2::uuid, 'owner') ON CONFLICT DO NOTHING""",
            workspace_id,
            actor,
        )
    return str(workspace_id)

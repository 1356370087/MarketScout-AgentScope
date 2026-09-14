"""Phase-A permission matrix against a real PostgreSQL (plan §2.3/§5.3).

Covers the multi-user gates required before team rollout: workspace roles,
knowledge-base visibility, per-base member grants, revocation immediacy,
and the personal-space compatibility path for legacy owner flows.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from open_deep_research.documents import repository
from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.storage import StagedUpload
from open_deep_research.knowledge import authz
from open_deep_research.knowledge import repository as kb_repository
from open_deep_research.knowledge.search_service import (
    SearchRequest,
    unified_search,
)

_TEST_DSN = os.environ.get("IAM_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
    pytest.mark.skipif(not _TEST_DSN, reason="IAM_TEST_DATABASE_URL not configured"),
]

OWNER = str(uuid.uuid4())
ALICE = str(uuid.uuid4())   # 团队成员
BOB = str(uuid.uuid4())     # 非成员


def _staged(name: str, content: bytes) -> StagedUpload:
    handle = NamedTemporaryFile(delete=False, suffix=Path(name).suffix)
    handle.write(content)
    handle.close()
    return StagedUpload(
        Path(handle.name), name, "text/markdown", len(content),
        hashlib.sha256(content).hexdigest(),
    )


async def _seed_team_base(pool) -> dict:
    async with pool.acquire() as connection:
        workspace = await connection.fetchval(
            """INSERT INTO knowledge_workspaces(kind, name, created_by)
               VALUES ('team', '竞品研究组', $1::uuid) RETURNING id""",
            OWNER,
        )
        await connection.executemany(
            """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
               VALUES ($1::uuid, $2::uuid, $3)""",
            [(workspace, OWNER, "owner"), (workspace, ALICE, "member")],
        )
        base = await connection.fetchval(
            """INSERT INTO knowledge_bases
                 (owner_id, name, description, workspace_id, visibility, created_by)
               VALUES ($1::uuid, '团队库', '', $2::uuid, 'team', $1::uuid) RETURNING id""",
            OWNER,
            workspace,
        )
    return {"workspace": str(workspace), "base": str(base)}


async def test_workspace_permission_matrix_and_revocation(monkeypatch):
    try:
        await _matrix_body(monkeypatch)
    finally:
        await close_document_pool()


async def _matrix_body(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    pool = await get_document_pool()
    seeded = await _seed_team_base(pool)
    base_id = seeded["base"]

    global _POOL_FOR_FINALLY  # noqa: PLW0603 - teardown on failure keeps loops sane

    async def _caps(user: str) -> frozenset[str]:
        return await authz.kb_capabilities(user, base_id)

    # 站长=管理者；普通成员在 team 可见库默认 viewer；外人无任何能力。
    assert authz.CAP_MANAGE in await _caps(OWNER)
    assert await _caps(ALICE) == authz._ROLE_CAPABILITIES["viewer"]
    assert await _caps(BOB) == frozenset()

    # 受限库：成员失去默认可见，库级授权逐级生效。
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE knowledge_bases SET visibility='restricted' WHERE id=$1::uuid",
            base_id,
        )
    assert await _caps(ALICE) == frozenset()
    await authz.require_kb_capability(OWNER, base_id, authz.CAP_MANAGE)
    from open_deep_research.knowledge.workspace_router import (
        BaseMemberRequest,
        upsert_base_member,
    )

    await upsert_base_member(
        base_id, BaseMemberRequest(user_id=ALICE, role="contributor"),
        user=_principal(OWNER),
    )
    assert authz.CAP_SUBMIT in await _caps(ALICE)
    assert authz.CAP_REVIEW not in await _caps(ALICE)

    # 撤权立即生效：删除成员行后下一次请求即无能力。
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM knowledge_base_members WHERE knowledge_base_id=$1::uuid",
            base_id,
        )
    assert await _caps(ALICE) == frozenset()

    # 可读范围：restricted 时成员默认不可见；切回 team 后可见，外人始终不可见。
    assert base_id not in await authz.readable_kb_ids(ALICE)
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE knowledge_bases SET visibility='team' WHERE id=$1::uuid", base_id
        )
    assert base_id in await authz.readable_kb_ids(ALICE)
    assert base_id not in await authz.readable_kb_ids(BOB)

    # 文档级：团队文档的读取走归属库能力，撤成员后立即拒绝。
    document, _ = await repository.create_document(
        OWNER, _staged("team-doc.md", b"team material"),
        f"perm/{uuid.uuid4().hex}.md",
        _doc_settings(),
        knowledge_base_id=base_id,
    )
    assert (await authz.document_access(ALICE, document.id))["capabilities"] >= frozenset(
        {authz.CAP_VIEW}
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM knowledge_workspace_members WHERE user_id=$1::uuid", ALICE
        )
    revoked = await authz.document_access(ALICE, document.id)
    assert revoked is None or not revoked["capabilities"]
    with pytest.raises(authz.AuthorizationError):
        await authz.authorize_document_version(
            ALICE, document.id, "00000000-0000-0000-0000-000000000000"
        )

    # 同库去重：同一文件在同一库去重，跨库可独立存在。
    duplicate, dedup = await repository.create_document(
        OWNER, _staged("team-doc.md", b"team material"),
        f"perm/{uuid.uuid4().hex}.md", _doc_settings(), knowledge_base_id=base_id,
    )
    assert dedup is True and duplicate.id == document.id
    personal_base = await kb_repository.create_knowledge_base(
        ALICE, "个人默认外库", "",
    )
    other_owner_doc, dedup2 = await repository.create_document(
        ALICE, _staged("team-doc.md", b"team material"),
        f"perm/{uuid.uuid4().hex}.md", _doc_settings(),
        knowledge_base_id=str(personal_base.id),
    )
    assert dedup2 is False and other_owner_doc.id != document.id

    # 统一检索作用域：请求受限库被静默收窄为空（不泄露存在性）。
    async def fake_embed(texts, settings=None, **_kwargs):
        return [[0.1] * 1536 for _ in texts]

    from open_deep_research.knowledge import search_service

    monkeypatch.setattr(search_service, "embed_texts", fake_embed)
    monkeypatch.setattr(search_service, "knowledge_service_key", lambda: "sk-test")
    scoped = await unified_search(
        SearchRequest(owner_id=BOB, query="team material", kb_ids=[base_id])
    )
    assert scoped["results"] == []


def _principal(user_id: str):
    from security.rbac.principal import Principal

    return Principal(
        user_id=user_id,
        email=f"{user_id}@test.invalid",
        status="active",
        session_id=None,
        roles=frozenset(),
        permissions=frozenset(),
        authz_version=0,
    )


def _doc_settings():
    from open_deep_research.documents.settings import DocumentSettings

    return DocumentSettings(max_documents_per_user=10, max_bytes_per_user=10**9)


async def test_personal_space_compatibility(monkeypatch):
    """Legacy owner flows keep full control via their personal workspace."""
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL",
        _TEST_DSN.replace("postgresql+asyncpg://", "postgresql://", 1),
    )
    pool = await get_document_pool()
    document, _ = await repository.create_document(
        BOB, _staged("own.md", b"personal material"),
        f"perm/{uuid.uuid4().hex}.md", _doc_settings(),
    )
    access = await authz.document_access(BOB, document.id)
    assert authz.CAP_MANAGE in access["capabilities"]
    foreign = await authz.document_access(ALICE, document.id)
    assert foreign is None or not foreign["capabilities"]
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM research_documents WHERE owner_id=$1::uuid", BOB
        )
    await close_document_pool()

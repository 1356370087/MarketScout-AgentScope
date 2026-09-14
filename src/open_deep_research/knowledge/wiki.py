"""Wiki pages: structured blocks, citations, versioning (KB-13).

Pages use structured Markdown blocks with stable IDs. Each block can cite
document generations or fact assertions. Page versions are immutable;
drafts use ``base_revision`` optimistic locking. Templates for company /
product / pricing provide starting structures. Stale-citation detection
marks blocks when their sources change or become unavailable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from . import authz, editorial

CITATION_STATUSES = frozenset(
    {"current", "source_changed", "source_unavailable", "needs_review"}
)

TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "company_profile": [
        {"block_type": "heading", "content": "# {entity_name} 企业档案"},
        {"block_type": "text", "content": "## 基本情况", "requires_source": False},
        {"block_type": "text", "content": "## 财务表现", "requires_source": True},
        {"block_type": "text", "content": "## 市场地位", "requires_source": True},
        {"block_type": "text", "content": "## 分析备注", "requires_source": False},
    ],
    "product_features": [
        {"block_type": "heading", "content": "# {entity_name} 产品与功能"},
        {"block_type": "text", "content": "## 核心功能", "requires_source": True},
        {"block_type": "text", "content": "## 技术规格", "requires_source": True},
        {"block_type": "text", "content": "## 竞品对比", "requires_source": True},
        {"block_type": "text", "content": "## 分析备注", "requires_source": False},
    ],
    "pricing_theme": [
        {"block_type": "heading", "content": "# {entity_name} 定价"},
        {"block_type": "text", "content": "## 定价方案", "requires_source": True},
        {"block_type": "text", "content": "## 币种与单位", "requires_source": True},
        {"block_type": "text", "content": "## 有效期", "requires_source": True},
        {"block_type": "text", "content": "## 分析备注", "requires_source": False},
    ],
}


class WikiError(RuntimeError):
    """Raised when a wiki operation is invalid."""


async def create_page(
    actor_id: str,
    knowledge_base_id: str,
    *,
    title: str,
    template: str = "company_profile",
    entity_name: str = "",
) -> dict[str, Any]:
    """Create one wiki page from a template; initial revision is a draft."""
    actor_id = document_owner_id(actor_id)
    await authz.require_kb_capability(actor_id, knowledge_base_id, authz.CAP_SUBMIT)
    if template not in TEMPLATES:
        raise WikiError(f"wiki_template_invalid:{template}")
    blocks = []
    for index, block_template in enumerate(TEMPLATES[template]):
        blocks.append(
            {
                "id": str(uuid.uuid4()),
                "ordinal": index,
                "block_type": block_template["block_type"],
                "content": block_template["content"].format(entity_name=entity_name),
                "requires_source": block_template.get("requires_source", False),
                "citations": [],
            }
        )
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        page_id = await connection.fetchval(
            """INSERT INTO knowledge_pages
                 (knowledge_base_id, title, template, entity_name, current_revision_id, created_by)
               VALUES ($1::uuid, $2, $3, $4, NULL, $5::uuid)
             RETURNING id""",
            knowledge_base_id,
            title,
            template,
            entity_name,
            actor_id,
        )
        revision_id = await connection.fetchval(
            """INSERT INTO knowledge_page_revisions
                 (page_id, revision_number, blocks, status, base_revision, created_by)
               VALUES ($1::uuid, 1, $2::jsonb, 'draft', 0, $3::uuid)
             RETURNING id""",
            page_id,
            json.dumps(blocks, ensure_ascii=False),
            actor_id,
        )
        await connection.execute(
            "UPDATE knowledge_pages SET current_revision_id=$2, updated_at=now() WHERE id=$1::uuid",
            page_id,
            revision_id,
        )
    return {
        "id": str(page_id),
        "revision_id": str(revision_id),
        "revision_number": 1,
        "status": "draft",
    }


async def save_draft(
    actor_id: str,
    page_id: str,
    *,
    base_revision: int,
    blocks: list[dict[str, Any]],
    generated: bool = False,
) -> dict[str, Any]:
    """Save one draft revision with optimistic locking on base_revision.

    Returns the new revision; a stale base_revision raises ``WikiError``
    with a 409-equivalent code (plan §KB-13: 草稿保存使用 base_revision
    乐观锁，冲突返回 409).
    """
    actor_id = document_owner_id(actor_id)
    page = await editorial.target(
        actor_id, "knowledge_pages", page_id, authz.CAP_SUBMIT
    )
    if str(
        page["created_by"]
    ) != actor_id and authz.CAP_REVIEW not in await authz.kb_capabilities(
        actor_id, str(page["knowledge_base_id"])
    ):
        raise authz.AuthorizationError("wiki_draft_not_owned")
    ids = [b.get("id") for b in blocks]
    if any(not isinstance(i, str) or not i or len(i) > 64 for i in ids) or len(
        set(ids)
    ) != len(ids):
        raise WikiError("wiki_block_ids_invalid")
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        current = await connection.fetchrow(
            """SELECT pr.revision_number, pr.status, pr.blocks
                 FROM knowledge_page_revisions pr
                 JOIN knowledge_pages p ON p.current_revision_id=pr.id
                WHERE p.id=$1::uuid FOR UPDATE OF p""",
            page_id,
        )
        if not current:
            raise WikiError("wiki_page_not_found")
        if int(current["revision_number"]) != base_revision:
            raise WikiError(f"wiki_revision_conflict:{current['revision_number']}")
        next_number = base_revision + 1
        previous = current["blocks"]
        previous = json.loads(previous) if isinstance(previous, str) else previous
        previous = {b["id"]: b for b in previous}
        for block in blocks:
            if not generated and block != previous.get(block["id"]):
                block.pop("generated_by", None)
            if block.get("block_type", "text") not in {"text", "heading", "analysis"}:
                raise WikiError("wiki_block_type_invalid")
            for citation in block.get("citations", []):
                try:
                    await editorial.source(
                        connection, str(page["knowledge_base_id"]), citation
                    )
                except ValueError as exc:
                    raise WikiError(str(exc)) from exc
        revision_id = await connection.fetchval(
            """INSERT INTO knowledge_page_revisions
                 (page_id, revision_number, blocks, status, base_revision, created_by)
               VALUES ($1::uuid, $2, $3::jsonb, 'draft', $4, $5::uuid)
             RETURNING id""",
            page_id,
            next_number,
            json.dumps(blocks, ensure_ascii=False),
            base_revision,
            actor_id,
        )
        await connection.execute(
            "UPDATE knowledge_pages SET current_revision_id=$2, updated_at=now() WHERE id=$1::uuid",
            page_id,
            revision_id,
        )
        # Persist block citations.
        for block in blocks:
            for citation in block.get("citations", []):
                await connection.execute(
                    """INSERT INTO knowledge_page_citations
                         (revision_id, block_id, citation_type, document_id,
                          generation_id, fact_assertion_id, citation_status)
                       VALUES ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6::uuid, 'current')
                       ON CONFLICT DO NOTHING""",
                    revision_id,
                    block["id"],
                    citation.get("type", "document"),
                    citation.get("document_id"),
                    citation.get("generation_id"),
                    citation.get("fact_assertion_id"),
                )
        await editorial.audit(
            connection, actor_id, page["knowledge_base_id"], "wiki_save", page_id
        )
    return {
        "revision_id": str(revision_id),
        "revision_number": next_number,
        "status": "draft",
    }


async def publish_page(actor_id: str, page_id: str) -> dict[str, Any] | None:
    """Publish the current draft; fact blocks without sources are rejected."""
    actor_id = document_owner_id(actor_id)
    page = await editorial.target(
        actor_id, "knowledge_pages", page_id, authz.CAP_REVIEW
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        revision = await connection.fetchrow(
            """SELECT pr.* FROM knowledge_page_revisions pr
                 JOIN knowledge_pages p ON p.current_revision_id=pr.id
                WHERE p.id=$1::uuid FOR UPDATE OF p""",
            page_id,
        )
        if not revision or revision["status"] != "draft":
            return None
        blocks = revision["blocks"]
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
        for block in blocks if isinstance(blocks, list) else []:
            content = str(block.get("content") or "").strip()
            heading = content.startswith("#") and "\n" not in content
            if (
                content
                and block.get("block_type") != "analysis"
                and not heading
                and not block.get("citations")
            ):
                raise WikiError(
                    f"wiki_block_missing_source:{block.get('id', 'unknown')}"
                )
            for citation in block.get("citations", []):
                try:
                    await editorial.source(
                        connection, str(page["knowledge_base_id"]), citation
                    )
                except ValueError as exc:
                    raise WikiError(str(exc)) from exc
        published = await connection.fetchrow(
            """UPDATE knowledge_page_revisions
                  SET status='published', published_at=now(), reviewed_by=$2::uuid
                WHERE id=$1::uuid AND status='draft'
             RETURNING id, revision_number""",
            revision["id"],
            actor_id,
        )
        if not published:
            return None
        await connection.execute(
            "UPDATE knowledge_pages SET published_revision_id=$2 WHERE id=$1::uuid",
            page_id,
            published["id"],
        )
        await editorial.audit(
            connection, actor_id, page["knowledge_base_id"], "wiki_publish", page_id
        )
    return {"revision_number": int(published["revision_number"]), "status": "published"}


async def get_page(
    actor_id: str, page_id: str, *, include_history: bool = False
) -> dict[str, Any] | None:
    """Return one page with its current revision and blocks."""
    actor_id = document_owner_id(actor_id)
    page_scope = await editorial.target(
        actor_id, "knowledge_pages", page_id, authz.CAP_VIEW
    )
    caps = await authz.kb_capabilities(actor_id, str(page_scope["knowledge_base_id"]))
    drafts_allowed = (
        authz.CAP_REVIEW in caps or str(page_scope["created_by"]) == actor_id
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        page = await connection.fetchrow(
            "SELECT * FROM knowledge_pages WHERE id=$1::uuid",
            page_id,
        )
        if not page:
            return None
        revision = await connection.fetchrow(
            "SELECT * FROM knowledge_page_revisions WHERE id=$1::uuid",
            page["current_revision_id"]
            if drafts_allowed
            else page["published_revision_id"],
        )
        if not revision:
            return None
        history = []
        if include_history:
            history_rows = await connection.fetch(
                """SELECT id, revision_number, status, created_at, published_at
                     FROM knowledge_page_revisions
                    WHERE page_id=$1::uuid AND ($2 OR status='published') ORDER BY revision_number DESC LIMIT 20""",
                page_id,
                drafts_allowed,
            )
            history = [
                {
                    "revision_number": int(r["revision_number"]),
                    "status": r["status"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in history_rows
            ]
        blocks = revision["blocks"]
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
        citation_statuses = await connection.fetch(
            "SELECT block_id,citation_status FROM knowledge_page_citations WHERE revision_id=$1",
            revision["id"],
        )
    return {
        "id": str(page["id"]),
        "title": page["title"],
        "template": page["template"],
        "entity_name": page["entity_name"],
        "published_revision_id": str(page["published_revision_id"])
        if page["published_revision_id"]
        else None,
        "can_edit": drafts_allowed and authz.CAP_SUBMIT in caps,
        "can_publish": authz.CAP_REVIEW in caps,
        "citation_statuses": [dict(c) for c in citation_statuses],
        "current_revision": {
            "number": int(revision["revision_number"]),
            "status": revision["status"],
            "blocks": blocks,
        },
        **({"history": history} if include_history else {}),
    }


async def check_stale_citations(page_id: str) -> list[dict[str, Any]]:
    """Mark blocks whose cited sources changed or became unavailable.

    Returns a list of stale-block reports. Auto-checks only generate
    reminders — they never rewrite human content (plan §KB-13).
    """
    pool = await get_document_pool()
    stale: list[dict[str, Any]] = []
    async with pool.acquire() as connection:
        page = await connection.fetchrow(
            "SELECT current_revision_id, published_revision_id FROM knowledge_pages WHERE id=$1::uuid",
            page_id,
        )
        if not page:
            return stale
        citations = await connection.fetch(
            """SELECT * FROM knowledge_page_citations
                WHERE revision_id=$1::uuid OR revision_id=$2::uuid""",
            page["current_revision_id"],
            page["published_revision_id"],
        )
        for citation in citations:
            status = "current"
            if citation["document_id"]:
                doc = await connection.fetchrow(
                    """SELECT deleted_at, status FROM research_documents
                        WHERE id=$1::uuid""",
                    citation["document_id"],
                )
                if (
                    not doc
                    or doc["deleted_at"]
                    or doc["status"] in ("trashed", "deleting")
                ):
                    status = "source_unavailable"
                elif citation["generation_id"]:
                    gen = await connection.fetchrow(
                        """SELECT status, supersedes_generation_id
                             FROM research_document_generations WHERE id=$1::uuid""",
                        citation["generation_id"],
                    )
                    current_gen = await connection.fetchval(
                        "SELECT current_generation_id FROM research_documents WHERE id=$1::uuid",
                        citation["document_id"],
                    )
                    if not gen or gen["status"] != "published" or not current_gen:
                        status = "source_unavailable"
                    elif str(current_gen) != str(citation["generation_id"]):
                        status = "source_changed"
            elif citation["fact_assertion_id"]:
                assertion = await connection.fetchrow(
                    "SELECT status, supersedes_id,verification FROM knowledge_fact_assertions WHERE id=$1::uuid",
                    citation["fact_assertion_id"],
                )
                if not assertion or assertion["status"] in ("withdrawn", "rejected"):
                    status = "source_unavailable"
                elif assertion["verification"] == "disputed":
                    status = "needs_review"
                elif await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM knowledge_fact_assertions WHERE supersedes_id=$1::uuid AND status='published')",
                    citation["fact_assertion_id"],
                ):
                    status = "source_changed"
                elif await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM knowledge_fact_evidence e
                    JOIN research_documents d ON d.id=e.document_id
                    JOIN research_document_generations g ON g.id=e.generation_id
                    WHERE e.assertion_id=$1 AND (d.deleted_at IS NOT NULL OR g.status<>'published'
                        OR d.current_generation_id IS DISTINCT FROM e.generation_id))""",
                    citation["fact_assertion_id"],
                ):
                    status = "needs_review"
            await connection.execute(
                "UPDATE knowledge_page_citations SET citation_status=$2 WHERE id=$1",
                citation["id"],
                status,
            )
            if status != "current":
                await connection.execute(
                    """UPDATE knowledge_page_citations
                          SET citation_status=$2
                        WHERE id=$1::uuid""",
                    citation["id"],
                    status,
                )
                stale.append(
                    {
                        "block_id": str(citation["block_id"]),
                        "citation_id": str(citation["id"]),
                        "status": status,
                    }
                )
    return stale


async def generate_page(actor, page_id, base_revision):
    """Build a source-grounded draft from published facts; preserve manual blocks."""
    page = await editorial.target(actor, "knowledge_pages", page_id, authz.CAP_SUBMIT)
    detail = await get_page(actor, page_id)
    from .facts import list_assertions

    assertions = await list_assertions(
        actor,
        str(page["knowledge_base_id"]),
        entity_name=page["entity_name"],
        status="published",
        limit=200,
    )
    blocks = [
        b
        for b in detail["current_revision"]["blocks"]
        if b.get("generated_by") != "facts-v1"
    ]
    retained_ids = {b["id"] for b in blocks}
    for fact in assertions:
        if str(uuid.uuid5(uuid.NAMESPACE_URL, page_id + fact["id"])) in retained_ids:
            continue
        blocks.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, page_id + fact["id"])),
                "block_type": "text",
                "content": f"{fact['entity_name']} · {fact['metric']}：{fact['value_text'] or fact['value_numeric']} {fact['unit']}；期间：{fact['data_period'] or '未注明'}；条件：{fact['condition_text'] or '未注明'}",
                "requires_source": True,
                "generated_by": "facts-v1",
                "citations": [{"type": "fact", "fact_assertion_id": fact["id"]}],
            }
        )
    if not assertions:
        raise WikiError("wiki_no_published_facts")
    return await save_draft(
        actor, page_id, base_revision=base_revision, blocks=blocks, generated=True
    )

"""Shared editorial authorization and source validation."""

import json

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from . import authz


async def target(actor, table, identifier, capability):
    """Authorize an editorial record using its actual knowledge base."""
    pool = await get_document_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(f"SELECT * FROM {table} WHERE id=$1::uuid", identifier)
    if not row:
        raise ValueError("editorial_target_not_found")
    await authz.require_kb_capability(actor, str(row["knowledge_base_id"]), capability)
    return row


async def source(c, kb, item):
    """Validate the complete document→generation→unit/segment chain."""
    if item.get("type") == "fact":
        valid = await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM knowledge_fact_assertions
            WHERE id=$1::uuid AND knowledge_base_id=$2::uuid AND status='published')""",
            item.get("fact_assertion_id"),
            kb,
        )
    else:
        valid = await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM research_document_generations g
            JOIN research_documents d ON d.id=g.document_id
            WHERE g.id=$1::uuid AND d.id=$2::uuid AND d.home_knowledge_base_id=$3::uuid
              AND g.status='published' AND d.deleted_at IS NULL
              AND ($4::uuid IS NULL OR EXISTS(SELECT 1 FROM research_document_units u
                  WHERE u.id=$4::uuid AND u.generation_id=g.id AND NOT u.excluded))
              AND ($5::uuid IS NULL OR EXISTS(SELECT 1 FROM research_document_segments s
                  WHERE s.id=$5::uuid AND s.generation_id=g.id)))""",
            item.get("generation_id"),
            item.get("document_id"),
            kb,
            item.get("unit_id"),
            item.get("segment_id"),
        )
    if not valid:
        raise ValueError("evidence_not_published_or_wrong_scope")


async def audit(c, actor, kb, action, identifier, detail=None):
    """Append an editorial decision to the audit log."""
    await c.execute(
        """INSERT INTO knowledge_editorial_audit
        (actor_id,knowledge_base_id,action,target_id,detail) VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::jsonb)""",
        document_owner_id(actor),
        kb,
        action,
        identifier,
        json.dumps(detail or {}, default=str),
    )

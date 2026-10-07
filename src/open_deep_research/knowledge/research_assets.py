"""Read published facts and immutable Wiki revisions inside a frozen run scope."""

import json

from pydantic import BaseModel, Field

from open_deep_research.documents.database import (
    document_schema_available,
    get_document_pool,
)
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.tools.base import ToolEffect, ToolOrigin, ToolResult, build_tool

from .authz import document_read_sql, readable_bases_sql
from .evidence_projection import document_evidence


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def freeze_assets(owner_id, selection, documents):
    """Freeze only assets whose entire citation scope belongs to selected material."""
    owner = document_owner_id(owner_id)
    generations = [item["generation_id"] for item in documents]
    cutoff = selection.retrieval.as_of_published
    valid = selection.retrieval.as_of_valid
    pool = await get_document_pool()
    async with pool.acquire() as c:
        facts = await c.fetch(
            f"""SELECT a.id,a.updated_at FROM knowledge_fact_assertions a
            WHERE a.status='published' AND a.knowledge_base_id IN ({readable_bases_sql('$1')})
              AND ($3::text IS NULL OR a.published_at<=($3::text::date::timestamp AT TIME ZONE 'UTC'))
              AND ($4::text IS NULL OR ((a.valid_from IS NULL OR a.valid_from<=$4::text::date)
                   AND (a.valid_until IS NULL OR a.valid_until>$4::text::date)))
              AND EXISTS(SELECT 1 FROM knowledge_fact_evidence e WHERE e.assertion_id=a.id)
              AND NOT EXISTS(SELECT 1 FROM knowledge_fact_evidence e WHERE e.assertion_id=a.id
                             AND NOT (e.generation_id=ANY($2::uuid[])))
            ORDER BY a.id LIMIT 200""", owner, generations, cutoff, valid)
        ids = [str(row["id"]) for row in facts]
        pages = await c.fetch(
            f"""SELECT r.id,p.id AS page_id FROM knowledge_pages p
            JOIN LATERAL (
                SELECT * FROM knowledge_page_revisions v WHERE v.page_id=p.id AND v.status='published'
                  AND (($4::text IS NULL AND v.id=p.published_revision_id)
                    OR ($4::text IS NOT NULL AND v.published_at<=($4::text::date::timestamp AT TIME ZONE 'UTC')))
                ORDER BY v.published_at DESC,v.revision_number DESC LIMIT 1
            ) r ON true
            WHERE p.knowledge_base_id IN ({readable_bases_sql('$1')})
              AND EXISTS(SELECT 1 FROM knowledge_page_citations x WHERE x.revision_id=r.id)
              AND NOT EXISTS(SELECT 1 FROM knowledge_page_citations x WHERE x.revision_id=r.id
                AND NOT ((x.citation_type='document' AND x.generation_id=ANY($2::uuid[]))
                      OR (x.citation_type='fact' AND x.fact_assertion_id=ANY($3::uuid[]))))
            ORDER BY r.id LIMIT 50""", owner, generations, ids, cutoff)
    return {"facts": [{"id": str(row["id"]), "updated_at": row["updated_at"].isoformat()} for row in facts],
            "wiki": [{"id": str(row["id"]), "page_id": str(row["page_id"])} for row in pages]}


async def read_assets(owner_id, manifest, kind, query):
    """Apply current permission and publication checks to the frozen identifiers."""
    scope = (manifest or {}).get("assets", {}).get(kind, [])
    ids = [item["id"] for item in scope]
    if not ids:
        return {"items": [], "evidence": [], "note": "本次固定资料范围没有可用的已发布知识资产。"}
    owner = document_owner_id(owner_id)
    generations = [item["generation_id"] for item in manifest["documents"]]
    pool = await get_document_pool()
    async with pool.acquire() as c:
        if kind == "facts":
            rows = await c.fetch(
                f"""SELECT a.*,k.entity_name,k.metric FROM knowledge_fact_assertions a
                JOIN knowledge_fact_keys k ON k.id=a.fact_key_id
                WHERE a.id=ANY($1::uuid[]) AND a.status='published'
                  AND a.knowledge_base_id IN ({readable_bases_sql('$2')}) ORDER BY a.id""", ids, owner)
            stamps = {item["id"]: item["updated_at"] for item in scope}
            rows = [row for row in rows if row["updated_at"].isoformat() == stamps[str(row["id"])]]
            rows = [row for row in rows if not query or query.casefold() in
                    f"{row['entity_name']} {row['metric']} {row['value_text']}".casefold()][:12]
            evidence_rows = await c.fetch(
                f"""SELECT e.assertion_id,e.excerpt AS match_excerpt,s.id AS segment_id,s.generation_id,s.unit_id,
                    s.index_text AS text,s.locator,g.document_id,d.filename
                FROM knowledge_fact_evidence e
                JOIN research_document_segments s ON s.generation_id=e.generation_id
                  AND (s.id=e.segment_id OR (e.segment_id IS NULL AND s.unit_id=e.unit_id
                       AND e.excerpt<>'' AND strpos(s.index_text,e.excerpt)>0))
                JOIN research_document_generations g ON g.id=s.generation_id
                JOIN research_documents d ON d.id=g.document_id
                WHERE e.assertion_id=ANY($1::uuid[]) AND s.generation_id=ANY($3::uuid[])
                  AND {document_read_sql('$2')} AND d.deleted_at IS NULL AND g.status='published'
                ORDER BY e.assertion_id,s.ordinal LIMIT 36""", [str(row["id"]) for row in rows], owner, generations)
            by_fact = {}
            records = []
            for row in evidence_rows:
                projected = document_evidence([dict(row)])[0]
                projected["fact_assertion_id"] = str(row["assertion_id"])
                records.append(projected)
                by_fact.setdefault(str(row["assertion_id"]), []).append(projected["evidence_id"])
            items = [{"fact_assertion_id": str(row["id"]), "entity": row["entity_name"],
                      "metric": row["metric"], "value": row["value_text"] or str(row["value_numeric"]),
                      "unit": row["unit"], "currency": row["currency"], "period": row["data_period"],
                      "condition": row["condition_text"], "verification": row["verification"],
                      "evidence_ids": by_fact.get(str(row["id"]), [])} for row in rows
                     if str(row["id"]) in by_fact]
            return {"items": items, "evidence": records}
        rows = await c.fetch(
            f"""SELECT r.*,p.title FROM knowledge_page_revisions r
            JOIN knowledge_pages p ON p.id=r.page_id
            WHERE r.id=ANY($1::uuid[]) AND r.status='published'
              AND p.knowledge_base_id IN ({readable_bases_sql('$2')}) ORDER BY r.id""", ids, owner)
        items = []
        for row in rows:
            if query and query.casefold() not in (row["title"] + str(row["blocks"])).casefold():
                continue
            citations = await c.fetch(
                "SELECT citation_type,document_id,generation_id,fact_assertion_id FROM knowledge_page_citations WHERE revision_id=$1::uuid", row["id"])
            # Revalidate all original document generations, including fact citations.
            referenced = {str(item["generation_id"]) for item in citations if item["generation_id"]}
            fact_ids = [str(item["fact_assertion_id"]) for item in citations if item["fact_assertion_id"]]
            if fact_ids:
                fact_sources = await c.fetch(
                    f"""SELECT e.generation_id,a.id,a.updated_at FROM knowledge_fact_evidence e
                    JOIN knowledge_fact_assertions a ON a.id=e.assertion_id
                    WHERE a.id=ANY($1::uuid[]) AND a.status='published'
                      AND a.knowledge_base_id IN ({readable_bases_sql('$2')})""", fact_ids, owner)
                stamps = {item["id"]: item["updated_at"] for item in manifest["assets"]["facts"]}
                if {str(item["id"]) for item in fact_sources} != set(fact_ids) or any(
                    item["updated_at"].isoformat() != stamps.get(str(item["id"])) for item in fact_sources
                ):
                    continue
                referenced.update(str(item["generation_id"]) for item in fact_sources)
            authorized = await c.fetch(
                f"""SELECT g.id FROM research_document_generations g
                JOIN research_documents d ON d.id=g.document_id
                WHERE g.id=ANY($1::uuid[]) AND g.id=ANY($3::uuid[])
                  AND {document_read_sql('$2')} AND d.deleted_at IS NULL AND g.status='published'""",
                list(referenced), owner, generations)
            if not referenced or {str(item["id"]) for item in authorized} != referenced:
                continue
            items.append({"page_id": str(row["page_id"]), "revision_id": str(row["id"]),
                "revision_number": row["revision_number"], "title": row["title"],
                "blocks": _json(row["blocks"]),
                "sources": [{key: str(value) for key, value in dict(item).items() if value is not None} for item in citations]})
            if len(items) == 5:
                break
    return {"items": items, "evidence": [],
            "note": "Wiki 是已有分析上下文。报告主张仍须检索并引用上述原始资料，不能将摘要视为独立一手证据。"}


class AssetQuery(BaseModel):
    query: str = Field(default="", max_length=200, description="A company, product or metric keyword; empty lists available items.")


def research_asset_tools():
    """Expose read-only assets through the same document permission boundary."""
    tools = []
    for kind in ("facts", "wiki"):
        async def call(input, context, progress, kind=kind):
            metadata = context.config.get("metadata") or {}
            owner = metadata.get("owner") or metadata.get("user_id")
            if not owner or not metadata.get("knowledge_manifest"):
                raise PermissionError("research_knowledge_scope_missing")
            return ToolResult(output=await read_assets(owner, metadata["knowledge_manifest"], kind, input.query))

        tools.append(build_tool(name="knowledge_" + kind, input_schema=AssetQuery,
            description=f"Read published {kind} in this run's frozen knowledge scope. Returns original document provenance; no mutation.",
            call=call, origin=ToolOrigin.LOCAL_DOCUMENT, effect=ToolEffect.SENSITIVE_READ,
            retryable=True, concurrency_safe=True, max_output_chars=50000,
            is_enabled=lambda config: document_schema_available() and bool((config.get("metadata") or {}).get("knowledge_manifest"))))
    return tools

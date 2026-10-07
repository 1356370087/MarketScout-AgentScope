"""Unified knowledge retrieval: scope, filters, fusion, rerank, context.

Implements plan §KB-05: owner and version filters stay inside the database
query, unambiguous entity aliases expand the query, three recall routes
(vector / full-text / trigram) fuse with reciprocal ranks, an optional
LiteLLM reranker keeps only directly-supporting evidence, per-document
quotas stop one file from swallowing results, and neighbours expand the
context under a character budget. Every stage reports diagnostics.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from datetime import time as datetime_time
from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.embeddings import embed_texts
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.retrieval import locator_dict
from open_deep_research.documents.settings import get_document_settings

from .accounting import knowledge_query, query_usage
from .authz import document_read_sql, readable_bases_sql
from .evidence_projection import source_excerpt
from .execution import SearchExecution
from .rerank import RerankUnavailableError, rerank_segments

DEFAULT_PARAMETERS: dict[str, Any] = {
    "candidate_limit_per_route": 40,
    "rerank_candidates": 30,
    "result_limit": 12,
    "per_document_quota": 3,
    "context_neighbors": 1,
    "context_char_budget": 2400,
    "rerank_min_score": 2,
    "total_context_char_budget": 24000,
    "parent_context": True,
    "rerank_enabled": True,
}


class SearchScopeError(ValueError):
    """Raised when a scope resolves to nothing usable."""


@dataclass(slots=True)
class SearchRequest:
    """One owner-scoped unified retrieval request."""

    owner_id: str
    query: str
    kb_ids: list[str] = field(default_factory=list)
    collection_ids: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    generation_ids: list[str] = field(default_factory=list)
    version_mode: str = "current"  # current | pinned | as_of
    as_of_published: str | None = None
    as_of_valid: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 12
    profile_version: str | None = None
    debug: bool = False
    queries: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchDiagnostics:
    """Per-stage counters persisted with the query ledger."""

    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    route_candidates: dict[str, int] = field(default_factory=dict)
    fused_candidates: int = 0
    rerank_dropped: int = 0
    quota_dropped: int = 0
    alias_expansions: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    rerank_completed: bool = True
    resolved_scope: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the diagnostics payload persisted with debug queries."""
        return {
            "stage_timings_ms": self.stage_timings_ms,
            "route_candidates": self.route_candidates,
            "fused_candidates": self.fused_candidates,
            "rerank_dropped": self.rerank_dropped,
            "quota_dropped": self.quota_dropped,
            "alias_expansions": self.alias_expansions,
            "usage": self.usage,
            "rerank_completed": self.rerank_completed,
            "resolved_scope": self.resolved_scope,
        }


async def load_profile(version: str | None) -> dict[str, Any]:
    """Load one versioned parameter set, falling back to the seeded default."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if version:
            row = await connection.fetchrow(
                """SELECT version,parameters FROM knowledge_search_profiles
                    WHERE version=$1 OR (is_default AND $1='')""",
                version,
            )
        else:
            row = await connection.fetchrow(
                """SELECT version,parameters FROM knowledge_search_profiles
                    WHERE is_default ORDER BY created_at DESC LIMIT 1"""
            )
    if version and not row:
        raise SearchScopeError("knowledge_profile_not_found")
    parameters = {**DEFAULT_PARAMETERS, "version": row["version"] if row else "builtin-v1"}
    if row and row["parameters"]:
        parsed = locator_dict(row["parameters"])
        parameters.update({key: value for key, value in parsed.items() if key in DEFAULT_PARAMETERS})
    return parameters


async def expand_entity_aliases(owner_id: str, query: str) -> tuple[str, list[str]]:
    """Append canonical names for unambiguous aliases found in the query.

    Ambiguous aliases (one alias, several entities) never auto-expand; the
    caller surfaces them for manual disambiguation instead (plan §KB-02).
    """
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT alias, e.id AS entity_id, coalesce(e.name_zh, e.name_en) AS name
                 FROM knowledge_entity_aliases a
                 JOIN knowledge_entities e ON e.id=a.entity_id
                WHERE e.owner_id=$1::uuid""",
            owner_id,
        )
    lowered = query.casefold()
    canonical: dict[str, str] = {}
    ambiguous: list[str] = []
    for row in rows:
        alias = str(row["alias"]).strip()
        if not alias or alias.casefold() not in lowered:
            continue
        name = str(row["name"] or "").strip()
        if not name:
            continue
        if alias in canonical and canonical[alias] != name:
            ambiguous.append(alias)
            canonical.pop(alias, None)
            continue
        if alias in ambiguous:
            continue
        canonical[alias] = name
    expansions = [name for name in canonical.values() if name.casefold() not in lowered]
    expanded = query
    if expansions:
        expanded = f"{query}（别名：{'、'.join(expansions)}）"
    return expanded, sorted({alias for alias in canonical})


async def resolve_scope(request: SearchRequest) -> dict[str, Any]:
    """Resolve authorized published generations, intersecting explicit scopes."""
    owner = document_owner_id(request.owner_id)
    cutoff = None
    if request.version_mode == "as_of":
        if not request.as_of_published:
            raise SearchScopeError("as_of_requires_date")
        try:
            parsed = date.fromisoformat(request.as_of_published)
            if parsed.isoformat() != request.as_of_published:
                raise ValueError("expected YYYY-MM-DD")
            cutoff = datetime.combine(parsed, datetime_time.min, timezone.utc)
        except (TypeError, ValueError) as exc:
            raise SearchScopeError("as_of_invalid_date") from exc
    if request.as_of_valid:
        try:
            if date.fromisoformat(request.as_of_valid).isoformat() != request.as_of_valid:
                raise ValueError("expected YYYY-MM-DD")
        except (TypeError, ValueError) as exc:
            raise SearchScopeError("as_of_invalid_validity_date") from exc
    if request.version_mode == "pinned" and not request.generation_ids:
        raise SearchScopeError("pinned_scope_requires_generations")
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        ids = set(request.document_ids)
        if request.kb_ids or request.collection_ids:
            links = await connection.fetch(
                f"""SELECT DISTINCT l.document_id FROM knowledge_document_links l
                JOIN knowledge_bases kb ON kb.id=l.knowledge_base_id
                WHERE kb.archived_at IS NULL AND kb.id IN ({readable_bases_sql('$1')})
                AND (kb.id=ANY($2::uuid[]) OR l.collection_id=ANY($3::uuid[]))""",
                owner, request.kb_ids, request.collection_ids,
            )
            ids.update(str(row["document_id"]) for row in links)
        scoped = bool(request.document_ids or request.kb_ids or request.collection_ids)
        if scoped and not ids:
            return {"documents": []}
        filters, filter_args = _metadata_filter_sql(request.filters, start=6)
        rows = await connection.fetch(
            f"""SELECT DISTINCT ON (d.id, CASE WHEN $6='pinned' THEN g.id ELSE NULL END)
                    d.id AS document_id,
                    g.id AS generation_id, g.metadata_snapshot, g.index_profile
                FROM research_documents d
                JOIN research_document_generations g ON g.document_id=d.id
                WHERE {document_read_sql('$1')} AND d.deleted_at IS NULL
                  AND g.status='published' AND (
                    ($6='pinned' AND g.id=ANY($3::uuid[])) OR
                    ($6='as_of' AND g.published_at<=$4::timestamptz) OR
                    ($6='current' AND g.id=d.current_generation_id))
                  AND (NOT $5::boolean OR d.id=ANY($2::uuid[])){filters}
                ORDER BY d.id, CASE WHEN $6='pinned' THEN g.id ELSE NULL END, g.published_at DESC, g.id""",
            owner, sorted(ids), request.generation_ids, cutoff, scoped, request.version_mode, *filter_args,
        )
    if request.version_mode == "pinned" and {str(row["generation_id"]) for row in rows} != set(request.generation_ids):
        raise SearchScopeError("generation_not_published_or_outside_scope")
    result = []
    for row in rows:
        if request.as_of_valid:
            validity = (locator_dict(row["metadata_snapshot"]).get("confirmed") or {}).get("validity") or {}
            if validity.get("start") and request.as_of_valid < validity["start"]:
                continue
            if validity.get("end") and request.as_of_valid >= validity["end"]:
                continue
        result.append({"document_id": str(row["document_id"]),
                       "generation_id": str(row["generation_id"]),
                       "index_profile": locator_dict(row["index_profile"])})
    return {"documents": result}


_METADATA_FIELD = {
    "doc_type": "doc_type",
    "language": "language",
}


def _metadata_filter_sql(
    filters: dict[str, Any], start: int = 0
) -> tuple[str, list[Any]]:
    """Build jsonb predicates for confirmed-or-suggested metadata filters.

    ``start`` offsets placeholder numbering so the clause can be appended to
    a query that already binds earlier parameters.
    """
    clauses: list[str] = []
    args: list[Any] = []

    def field_expr(field: str) -> str:
        return (
            f"COALESCE(NULLIF(g.metadata_snapshot->'confirmed'->>'{field}',''), "
            f"g.metadata_snapshot->'suggested'->'{field}'->>'value')"
        )

    def next_placeholder() -> int:
        return start + len(args)

    doc_types = [str(item) for item in filters.get("doc_types") or []]
    if doc_types:
        args.append(doc_types)
        clauses.append(f"{field_expr('doc_type')} = ANY(${next_placeholder()}::text[])")
    languages = [str(item) for item in filters.get("languages") or []]
    if languages:
        args.append(languages)
        clauses.append(f"{field_expr('language')} = ANY(${next_placeholder()}::text[])")
    entity_ids = [str(item) for item in filters.get("entity_ids") or []]
    if entity_ids:
        args.append(entity_ids)
        clauses.append(
            f"""COALESCE(
                  g.metadata_snapshot->'confirmed'->'company'->>'entity_id',
                  g.metadata_snapshot->'suggested'->'company'->0->'value'->>'entity_id'
                ) = ANY(${next_placeholder()}::text[])"""
        )
    start_date = filters.get("publish_date_start")
    end_date = filters.get("publish_date_end")
    if start_date or end_date:
        expression = f"NULLIF({field_expr('publish_date')}, '')"
        if start_date:
            args.append(str(start_date))
            clauses.append(f"{expression} >= ${next_placeholder()}")
        if end_date:
            args.append(str(end_date))
            clauses.append(f"{expression} <= ${next_placeholder()}")
    return (" AND " + " AND ".join(clauses)) if clauses else "", args


@dataclass(slots=True)
class SearchResult:
    """One evidence unit returned to the caller."""

    segment_id: str
    document_id: str
    generation_id: str
    filename: str
    text: str
    context_before: str
    context_after: str
    locator: dict[str, Any]
    score: float
    relevance: int | None
    unit_id: str | None = None
    parent_context: str = ""
    ordinal: int = 0
    rerank_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the finalized search payload for the response layer."""
        return {
            "segment_id": self.segment_id,
            "document_id": self.document_id,
            "generation_id": self.generation_id,
            "filename": self.filename,
            "text": self.text,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "parent_context": self.parent_context,
            "unit_id": self.unit_id,
            "locator": self.locator,
            "score": self.score,
            "relevance": self.relevance,
            "source_type": "knowledge_base",
            "source_uri": f"/documents/{self.document_id}?chunk={self.segment_id}",
        }


async def _recall(
    owner_id: str,
    expanded_query: str,
    scope: dict[str, Any],
    filters: dict[str, Any],
    parameters: dict[str, Any],
    diagnostics: SearchDiagnostics,
    execution: SearchExecution | None = None,
) -> list[dict[str, Any]]:
    """Fuse vector, full-text and trigram candidates from the database."""
    settings = get_document_settings()
    owner_id = document_owner_id(owner_id)
    generation_ids = [item["generation_id"] for item in scope["documents"]]
    if not generation_ids:
        return []
    started = time.perf_counter()
    execution = execution or SearchExecution()
    service_key = execution.embedding_key()
    vector = (await embed_texts(
        [expanded_query], settings, api_key=service_key, operation="knowledge_query" if execution.scope == "service" else "query"
    ))[0]
    diagnostics.usage["embedding_calls"] = diagnostics.usage.get("embedding_calls", 0) + 1
    diagnostics.stage_timings_ms["embed"] = (time.perf_counter() - started) * 1000

    filter_clause, filter_args = _metadata_filter_sql(filters, start=8)
    per_route = int(parameters["candidate_limit_per_route"])
    args: list[Any] = [
        owner_id,
        generation_ids,
        _vector_literal(vector),
        expanded_query.strip(),
        per_route,
        settings.trigram_min_score,
        per_route,
        per_route,
        *filter_args,
    ]
    pool = await get_document_pool()
    started = time.perf_counter()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""WITH eligible AS (
                 SELECT s.id, s.generation_id, s.unit_id, s.ordinal, s.index_text AS text,
                        s.embedding, s.search_vector, s.locator,
                        g.document_id, d.filename
                   FROM research_document_segments s
                   JOIN research_document_generations g ON g.id=s.generation_id
                   JOIN research_documents d ON d.id=g.document_id
                  WHERE {document_read_sql("$1")} AND d.deleted_at IS NULL
                    AND g.status='published'
                    AND s.generation_id=ANY($2::uuid[]){filter_clause}
               ), vector_rank AS (
                 SELECT id, row_number() OVER(ORDER BY embedding <=> $3::vector) rank
                   FROM eligible WHERE embedding IS NOT NULL
                  ORDER BY embedding <=> $3::vector LIMIT $5
               ), text_rank AS (
                 SELECT id, row_number() OVER(
                          ORDER BY ts_rank_cd(search_vector, websearch_to_tsquery('simple',$4)) DESC) rank
                   FROM eligible
                  WHERE search_vector @@ websearch_to_tsquery('simple',$4) LIMIT $7
               ), trigram_rank AS (
                 SELECT id, row_number() OVER(ORDER BY word_similarity($4,text) DESC) rank
                   FROM eligible
                  WHERE text %>> $4 AND word_similarity($4,text) >= $6
                  ORDER BY word_similarity($4,text) DESC LIMIT $8
               ), fused AS (
                 SELECT e.id,
                        coalesce(1.0/(60+v.rank),0)+coalesce(1.0/(60+t.rank),0)+coalesce(1.0/(60+g.rank),0) score
                   FROM eligible e
                   LEFT JOIN vector_rank v USING(id)
                   LEFT JOIN text_rank t USING(id)
                   LEFT JOIN trigram_rank g USING(id)
                  WHERE v.id IS NOT NULL OR t.id IS NOT NULL OR g.id IS NOT NULL
               )
               SELECT e.*, f.score,
                      (SELECT count(*) FROM vector_rank) AS vector_count,
                      (SELECT count(*) FROM text_rank) AS text_count,
                      (SELECT count(*) FROM trigram_rank) AS trigram_count
                 FROM fused f JOIN eligible e USING(id)
                ORDER BY f.score DESC, e.ordinal""",
            *args,
        )
    diagnostics.stage_timings_ms["recall"] = (time.perf_counter() - started) * 1000
    for name in ("vector", "text", "trigram"):
        diagnostics.route_candidates[name] = diagnostics.route_candidates.get(name, 0) + (int(rows[0][f"{name}_count"]) if rows else 0)
    diagnostics.fused_candidates = len(rows)
    return [dict(row) for row in rows]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(item):.6g}" for item in vector) + "]"


async def unified_search(request: SearchRequest, *, execution: SearchExecution | None = None) -> dict[str, Any]:
    """Run one scope-bound pipeline with service or research credentials."""
    execution = execution or SearchExecution()
    query_id = str(uuid.uuid4())
    if execution.scope == "service":
        async with knowledge_query(request.owner_id, query_id):
            result = await _search(request, query_id, execution)
        result["usage"] = await query_usage(query_id, request.owner_id, persist=True)
        return result
    return await _search(request, query_id, execution)


async def _search(request, query_id, execution):
    diagnostics = SearchDiagnostics()
    owner_id = document_owner_id(request.owner_id)
    parameters = dict(execution.manifest["parameters"]) if execution.manifest else await load_profile(request.profile_version)
    if execution.scope == "run":
        if not execution.manifest or not execution.manifest.get("documents"):
            raise SearchScopeError("research_knowledge_manifest_missing")
        request = replace(request, kb_ids=[], collection_ids=[],
            document_ids=[item["document_id"] for item in execution.manifest["documents"]],
            generation_ids=[item["generation_id"] for item in execution.manifest["documents"]],
            version_mode="pinned", filters=execution.manifest.get("filters", {}))
    overall = time.perf_counter()

    started = time.perf_counter()
    scope = await resolve_scope(request)
    diagnostics.stage_timings_ms["scope"] = (time.perf_counter() - started) * 1000
    diagnostics.resolved_scope = {
        "documents": len(scope["documents"]),
        "generations": scope["documents"],
        "document_ids": sorted({item['document_id'] for item in scope['documents']}),
        "version_mode": request.version_mode,
    }
    if not scope["documents"]:
        return await _finalize(
            query_id, request, [], diagnostics, parameters, overall,
        )

    expected_profile = get_document_settings().index_profile
    profiles = [item.get("index_profile") or {} for item in scope["documents"]]
    if any(profile != expected_profile for profile in profiles):
        raise SearchScopeError("knowledge_index_profile_mismatch: rebuild and publish with the configured embedding model/revision")
    if execution.manifest and any(item["index_profile"] != expected_profile for item in execution.manifest["documents"]):
        raise SearchScopeError("frozen_knowledge_index_profile_changed")
    variants = list(dict.fromkeys([request.query, *request.queries]))[:3]
    fused = {}
    for query in variants:
        expanded_query, alias_hits = await expand_entity_aliases(owner_id, query)
        diagnostics.alias_expansions = sorted(set(diagnostics.alias_expansions + alias_hits))
        recalled = await _recall(owner_id, expanded_query, scope, request.filters, parameters, diagnostics, execution)
        for row in recalled:
            key = str(row["id"])
            if key in fused:
                fused[key]["score"] += float(row["score"])
            else:
                fused[key] = dict(row)
    candidates = sorted(fused.values(), key=lambda row: (-float(row["score"]), int(row.get("ordinal") or 0), str(row["id"])))
    diagnostics.fused_candidates = len(candidates)

    rerank_pool = candidates[: int(parameters["rerank_candidates"])]
    relevance: dict[str, tuple[int, str | None]] = {}
    if not parameters.get("rerank_enabled", True):
        diagnostics.rerank_completed = False
    if rerank_pool and parameters.get("rerank_enabled", True):
        try:
            scores = await rerank_segments(request.query, rerank_pool, execution=execution)
            diagnostics.usage["rerank_calls"] = 1
            relevance = scores
        except RerankUnavailableError:
            # 检索仍返回融合结果，但明确标注未完成重排（KB-05）。
            diagnostics.rerank_completed = False
            relevance = {}

    min_score = int(parameters["rerank_min_score"])
    kept: list[dict[str, Any]] = []
    for row in rerank_pool:
        if not relevance:
            kept.append(row)  # rerank off/failed: fused order stands
            continue
        score, reason = relevance.get(str(row["id"]), (0, None))
        if score >= min_score:
            row = {**row, "relevance": score, "rerank_reason": reason}
            kept.append(row)
        else:
            diagnostics.rerank_dropped += 1
    if relevance:
        kept.sort(key=lambda row: (-row["relevance"], -float(row["score"]), int(row.get("ordinal") or 0), str(row["id"])))

    quota = int(parameters["per_document_quota"])
    single_document = len({row["document_id"] for row in scope["documents"]}) == 1
    per_doc: dict[str, int] = {}
    quota_kept: list[dict[str, Any]] = []
    for row in kept:
        document_id = str(row["document_id"])
        if not single_document and per_doc.get(document_id, 0) >= quota:
            diagnostics.quota_dropped += 1
            continue
        per_doc[document_id] = per_doc.get(document_id, 0) + 1
        quota_kept.append(row)

    selected = quota_kept[: min(request.limit, int(parameters["result_limit"]))]
    text_budget = min(1600, int(parameters.get("total_context_char_budget", 24000)) // max(1, len(selected)))
    results = [
        SearchResult(
            segment_id=str(row["id"]),
            document_id=str(row["document_id"]),
            generation_id=str(row["generation_id"]),
            filename=row["filename"],
            text=source_excerpt(row["text"], text_budget, request.query),
            context_before="",
            context_after="",
            locator=locator_dict(row["locator"]),
            score=float(row["score"]),
            relevance=row.get("relevance"),
            ordinal=int(row.get("ordinal") or 0),
            unit_id=str(row["unit_id"]) if row.get("unit_id") else None,
            rerank_reason=row.get("rerank_reason"),
        )
        for row in selected
    ]
    if results and (int(parameters["context_neighbors"]) > 0 or parameters.get("parent_context", True)):
        await _expand_context(results, parameters, diagnostics, owner_id)

    return await _finalize(
        query_id, request, [item.as_dict() for item in results],
        diagnostics, parameters, overall,
    )


async def _expand_context(results, parameters, diagnostics, owner_id):
    """Read source units and neighbours with per-result and total text budgets."""
    started = time.perf_counter()
    pool = await get_document_pool()
    total_room = max(0, int(parameters.get("total_context_char_budget", 24000)) - sum(len(result.text) for result in results))
    neighbours = max(0, int(parameters["context_neighbors"]))
    async with pool.acquire() as connection:
        for result in results:
            room = min(int(parameters["context_char_budget"]), total_room)
            if room <= 0:
                break
            if parameters.get("parent_context", True) and result.unit_id:
                parent = await connection.fetchrow(
                    f"""SELECT u.index_text,u.unit_type,u.attributes FROM research_document_units u
                    JOIN research_document_generations g ON g.id=u.generation_id
                    JOIN research_documents d ON d.id=g.document_id
                    WHERE u.id=$1::uuid AND u.generation_id=$2::uuid
                      AND {document_read_sql('$3')}""",
                    result.unit_id, result.generation_id, owner_id)
                if parent and parent["index_text"] != result.text:
                    attributes = locator_dict(parent["attributes"])
                    context = parent["index_text"]
                    if parent["unit_type"] == "table":
                        table_context = "\n".join(part for part in [" | ".join(str(item) for item in attributes.get("header", [])),
                            str(attributes.get("unit_note") or ""), str(attributes.get("footnotes") or "")] if part)
                        context = table_context or context
                    result.parent_context = context[:room]
                    room -= len(result.parent_context)
                    total_room -= len(result.parent_context)
            for direction in ("before", "after"):
                operator, order = ("<", "DESC") if direction == "before" else (">", "ASC")
                rows = await connection.fetch(
                    f"""SELECT s.index_text FROM research_document_segments s
                    JOIN research_document_generations g ON g.id=s.generation_id
                    JOIN research_documents d ON d.id=g.document_id
                    WHERE s.generation_id=$1::uuid AND s.ordinal {operator} $2::int
                      AND {document_read_sql('$4')}
                    ORDER BY s.ordinal {order} LIMIT $3""",
                    result.generation_id, result.ordinal, neighbours, owner_id)
                if direction == "before":
                    rows = list(reversed(rows))
                text = "\n".join(str(row["index_text"]) for row in rows)
                snippet = text[-room:] if direction == "before" and room else text[:room]
                setattr(result, "context_" + direction, snippet)
                room -= len(snippet)
                total_room -= len(snippet)
    diagnostics.stage_timings_ms["context"] = (time.perf_counter() - started) * 1000


async def _finalize(
    query_id: str,
    request: SearchRequest,
    results: list[dict[str, Any]],
    diagnostics: SearchDiagnostics,
    parameters: dict[str, Any],
    overall_started: float,
) -> dict[str, Any]:
    diagnostics.stage_timings_ms["total"] = (time.perf_counter() - overall_started) * 1000
    pool = await get_document_pool()
    try:
        async with pool.acquire() as connection:
            await connection.execute(
                """INSERT INTO knowledge_queries
                   (id, owner_id, query_text, scope, profile_version, result_digest, feedback_kind)
                   VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5, $6::jsonb, NULL)""",
                query_id,
                document_owner_id(request.owner_id),
                request.query,
                json.dumps(
                    {
                        "kb_ids": request.kb_ids, "collection_ids": request.collection_ids,
                        "resolved_document_ids": diagnostics.resolved_scope.get('document_ids', []),
                        "resolved_generations": diagnostics.resolved_scope.get("generations", []),
                        "effective_profile": parameters,
                        "document_ids": request.document_ids,
                        "generation_ids": request.generation_ids,
                        "version_mode": request.version_mode,
                        "as_of_published": request.as_of_published,
                        "as_of_valid": request.as_of_valid,
                        "filters": request.filters,
                        "debug": request.debug,
                    },
                    ensure_ascii=False, default=str,
                ),
                parameters.get("version", request.profile_version),
                json.dumps(
                    {
                        "hits": len(results),
                        "document_ids": sorted({item["document_id"] for item in results}),
                        "timings": diagnostics.as_dict()["stage_timings_ms"],
                        "usage": diagnostics.usage,
                        "rerank_completed": diagnostics.rerank_completed,
                        **({"diagnostics": diagnostics.as_dict()} if request.debug else {}),
                    },
                    ensure_ascii=False, default=str,
                ),
            )
    except Exception:  # noqa: BLE001 - the ledger must never fail a search
        pass
    return {
        "query_id": query_id,
        "query": request.query,
        "profile": parameters,
        "rerank_completed": diagnostics.rerank_completed,
        "results": results,
        "documents": [
            {"document_id": document_id,
             "filename": next(item["filename"] for item in results if item["document_id"] == document_id),
             "generation_id": next(
                 item["generation_id"] for item in results if item["document_id"] == document_id
             )}
            for document_id in dict.fromkeys(item["document_id"] for item in results)
        ],
        **({"diagnostics": diagnostics.as_dict()} if request.debug else {}),
    }

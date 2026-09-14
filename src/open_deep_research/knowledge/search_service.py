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
from dataclasses import dataclass, field
from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.embeddings import embed_texts
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.retrieval import locator_dict
from open_deep_research.documents.settings import get_document_settings

from .credentials import knowledge_service_key
from .rerank import RerankUnavailableError, rerank_segments

DEFAULT_PARAMETERS: dict[str, Any] = {
    "candidate_limit_per_route": 40,
    "rerank_candidates": 30,
    "result_limit": 12,
    "per_document_quota": 3,
    "context_neighbors": 1,
    "context_char_budget": 2400,
    "rerank_min_score": 2,
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
                """SELECT parameters FROM knowledge_search_profiles
                    WHERE version=$1 OR (is_default AND $1='')""",
                version,
            )
        else:
            row = await connection.fetchrow(
                """SELECT parameters FROM knowledge_search_profiles
                    WHERE is_default ORDER BY created_at DESC LIMIT 1"""
            )
    parameters = dict(DEFAULT_PARAMETERS)
    if row and row["parameters"]:
        parsed = locator_dict(row["parameters"])
        parameters.update({key: value for key, value in parsed.items() if key in parameters})
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
    """Resolve kb/collection/document selections to bound published generations.

    ``current`` binds the document's current published generation at query
    time; ``pinned`` validates explicit generation ids; ``as_of`` picks the
    newest generation published before the cut-off (plus an optional business
    validity date that a metadata validity range must cover).
    """
    owner_id = document_owner_id(request.owner_id)
    pool = await get_document_pool()
    from .authz import resolve_readable_scope

    readable_kbs = await resolve_readable_scope(
        request.owner_id,
        [str(item) for item in request.kb_ids] if request.kb_ids else [],
    )
    async with pool.acquire() as connection:
        document_ids: set[str] = {str(item) for item in request.document_ids}
        if request.kb_ids or request.collection_ids:
            # 库/集合作用域先经可读范围过滤，检索永不在召回后剔除（§2.4）。
            linked = await connection.fetch(
                """SELECT DISTINCT l.document_id
                     FROM knowledge_document_links l
                     JOIN knowledge_bases kb ON kb.id=l.knowledge_base_id
                    WHERE (l.knowledge_base_id=ANY($2::uuid[])
                           OR l.collection_id=ANY($3::uuid[]))
                      AND kb.archived_at IS NULL
                      AND (l.knowledge_base_id=ANY($4::uuid[]) OR kb.owner_id=$1::uuid)""",
                owner_id,
                [str(item) for item in request.kb_ids],
                [str(item) for item in request.collection_ids],
                readable_kbs,
            )
            document_ids.update(str(row["document_id"]) for row in linked)
        if request.version_mode == "pinned":
            if not request.generation_ids:
                raise SearchScopeError("pinned_scope_requires_generations")
            rows = await connection.fetch(
                """SELECT g.id AS generation_id, g.document_id
                     FROM research_document_generations g
                     JOIN research_documents d ON d.id=g.document_id
                    WHERE g.id=ANY($1::uuid[])
                      AND d.deleted_at IS NULL AND g.status='published'
                      AND (d.owner_id=$2::uuid
                           OR d.home_knowledge_base_id=ANY($3::uuid[]))""",
                [str(item) for item in request.generation_ids],
                owner_id,
                readable_kbs,
            )
            if len(rows) != len(set(str(item) for item in request.generation_ids)):
                raise SearchScopeError("generation_not_published_or_foreign")
            return {
                "documents": [
                    {"document_id": str(row["document_id"]),
                     "generation_id": str(row["generation_id"])}
                    for row in rows
                ]
            }
        if not (request.document_ids or request.kb_ids or request.collection_ids):
            owned = await connection.fetch(
                "SELECT id FROM research_documents WHERE owner_id=$1::uuid "
                "AND deleted_at IS NULL",
                owner_id,
            )
            document_ids.update(str(row["id"]) for row in owned)
            if not document_ids:
                return {"documents": []}
        requested_any = bool(
            request.kb_ids or request.collection_ids or request.document_ids
        )
        if not document_ids:
            if requested_any:
                # 请求的作用域整体不可读或为空：返回空结果，不泄露存在性。
                return {"documents": []}
            raise SearchScopeError("scope_resolved_to_nothing")
        if request.version_mode == "as_of":
            if not request.as_of_published:
                raise SearchScopeError("as_of_requires_date")
            rows = await connection.fetch(
                """SELECT DISTINCT ON (g.document_id)
                          g.document_id, g.id AS generation_id, g.metadata_snapshot
                     FROM research_document_generations g
                     JOIN research_documents d ON d.id=g.document_id
                    WHERE g.document_id=ANY($1::uuid[])
                      AND d.deleted_at IS NULL AND g.status='published'
                      AND (d.owner_id=$2::uuid OR d.home_knowledge_base_id=ANY($3::uuid[]))
                      AND g.published_at <= $4::timestamptz
                    ORDER BY g.document_id, g.published_at DESC""",
                sorted(document_ids),
                owner_id,
                readable_kbs,
                request.as_of_published,
            )
        else:
            rows = await connection.fetch(
                """SELECT d.id AS document_id, d.current_generation_id AS generation_id,
                          g.metadata_snapshot
                     FROM research_documents d
                     JOIN research_document_generations g ON g.id=d.current_generation_id
                    WHERE d.id=ANY($1::uuid[])
                      AND (d.owner_id=$2::uuid OR d.home_knowledge_base_id=ANY($3::uuid[]))
                      AND d.deleted_at IS NULL AND g.status='published'""",
                sorted(document_ids),
                owner_id,
                readable_kbs,
            )
        resolved = []
        validity_cut = request.as_of_valid
        for row in rows:
            if validity_cut:
                metadata = locator_dict(row["metadata_snapshot"])
                validity = (metadata.get("confirmed") or {}).get("validity") or {}
                start, end = validity.get("start"), validity.get("end")
                if start and validity_cut < start:
                    continue
                if end and validity_cut >= end:  # 左闭右开
                    continue
            resolved.append(
                {"document_id": str(row["document_id"]),
                 "generation_id": str(row["generation_id"])}
            )
        return {"documents": resolved}


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
                ) = ANY(${next_placeholder()}::uuid[])"""
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
) -> list[dict[str, Any]]:
    """Fuse vector, full-text and trigram candidates from the database."""
    settings = get_document_settings()
    owner_id = document_owner_id(owner_id)
    generation_ids = [item["generation_id"] for item in scope["documents"]]
    if not generation_ids:
        return []
    started = time.perf_counter()
    service_key = knowledge_service_key()
    vector = (await embed_texts(
        [expanded_query], settings, api_key=service_key, operation="knowledge_query"
    ))[0]
    diagnostics.usage["embedding_calls"] = 1
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
                  WHERE d.owner_id=$1::uuid AND d.deleted_at IS NULL
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
               SELECT e.*, f.score FROM fused f JOIN eligible e USING(id)
                ORDER BY f.score DESC, e.ordinal""",
            *args,
        )
    diagnostics.stage_timings_ms["recall"] = (time.perf_counter() - started) * 1000
    diagnostics.route_candidates = {"vector": per_route, "text": per_route, "trigram": per_route}
    diagnostics.fused_candidates = len(rows)
    return [dict(row) for row in rows]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(item):.6g}" for item in vector) + "]"


async def unified_search(request: SearchRequest) -> dict[str, Any]:
    """Run the full retrieval pipeline and persist the query ledger row."""
    query_id = str(uuid.uuid4())
    diagnostics = SearchDiagnostics()
    owner_id = document_owner_id(request.owner_id)
    parameters = await load_profile(request.profile_version)
    overall = time.perf_counter()

    started = time.perf_counter()
    scope = await resolve_scope(request)
    diagnostics.stage_timings_ms["scope"] = (time.perf_counter() - started) * 1000
    diagnostics.resolved_scope = {
        "documents": len(scope["documents"]),
        "document_ids": sorted({item['document_id'] for item in scope['documents']}),
        "version_mode": request.version_mode,
    }
    if not scope["documents"]:
        return await _finalize(
            query_id, request, [], diagnostics, parameters, overall,
        )

    expanded_query, alias_hits = await expand_entity_aliases(owner_id, request.query)
    diagnostics.alias_expansions = alias_hits
    candidates = await _recall(
        owner_id, expanded_query, scope, request.filters, parameters, diagnostics
    )

    rerank_pool = candidates[: int(parameters["rerank_candidates"])]
    relevance: dict[str, tuple[int, str | None]] = {}
    if rerank_pool:
        try:
            scores = await rerank_segments(request.query, rerank_pool)
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
        diagnostics.fused_candidates = len(candidates)

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

    results = [
        SearchResult(
            segment_id=str(row["id"]),
            document_id=str(row["document_id"]),
            generation_id=str(row["generation_id"]),
            filename=row["filename"],
            text=row["text"],
            context_before="",
            context_after="",
            locator=locator_dict(row["locator"]),
            score=float(row["score"]),
            relevance=row.get("relevance"),
            ordinal=int(row.get("ordinal") or 0),
            rerank_reason=row.get("rerank_reason"),
        )
        for row in quota_kept[: int(parameters["result_limit"])]
    ]
    if int(parameters["context_neighbors"]) > 0 and results:
        await _expand_context(results, parameters, diagnostics)

    return await _finalize(
        query_id, request, [item.as_dict() for item in results],
        diagnostics, parameters, overall,
    )


async def _expand_context(
    results: list[SearchResult], parameters: dict[str, Any], diagnostics: SearchDiagnostics
) -> None:
    """Attach bounded neighbouring segments as before/after context."""
    started = time.perf_counter()
    pool = await get_document_pool()
    budget = int(parameters["context_char_budget"])
    async with pool.acquire() as connection:
        for result in results:
            used = 0
            for direction in ("before", "after"):
                operator = "<" if direction == "before" else ">"
                order = "DESC" if direction == "before" else "ASC"
                row = await connection.fetchrow(
                    f"""SELECT index_text FROM research_document_segments
                        WHERE generation_id=$1::uuid AND ordinal {operator} $2::int
                        ORDER BY ordinal {order} LIMIT 1""",
                    result.generation_id,
                    result.ordinal,
                )
                if not row:
                    continue
                room = max(0, budget - used)
                snippet = str(row["index_text"])[:room]
                used += len(snippet)
                if direction == "before":
                    result.context_before = snippet
                else:
                    result.context_after = snippet
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
                request.profile_version,
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

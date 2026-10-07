"""Owner-filtered hybrid retrieval over frozen Run document selections.

Searches the caller's published generations: a segment participates only via
the document's ``current_generation_id`` pointer, or — when a Run already
bound its sources — via the generation frozen in ``research_run_sources`` so
mid-run publishes cannot drift the Run to new material.
"""

from __future__ import annotations

import json
from typing import Any

from .database import get_document_pool
from .identity import document_owner_id


def locator_dict(raw: Any) -> dict[str, Any]:
    """Decode a jsonb locator that asyncpg hands back as a JSON string."""
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return dict(raw or {})


async def search_document_chunks(*, owner_id: str, document_ids: list[str], query: str,
                                 api_key: str | None = None, limit: int = 12,
                                 run_id: str | None = None) -> list[dict[str, Any]]:
    """Compatibility entry point; credentials come from the bound execution scope."""
    from open_deep_research.knowledge.execution import SearchExecution
    from open_deep_research.knowledge.search_service import (
        SearchRequest,
        unified_search,
    )

    if api_key is not None:
        raise ValueError("use_bound_run_key_or_knowledge_service_key")
    if not document_ids or not query.strip():
        return []
    execution = None
    if run_id:
        from open_deep_research.knowledge.search_service import DEFAULT_PARAMETERS
        pool = await get_document_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT r.document_id,r.generation_id,g.index_profile FROM research_run_sources r
                   JOIN research_document_generations g ON g.id=r.generation_id
                   WHERE r.run_id=$1 AND r.owner_id=$2::uuid AND r.document_id=ANY($3::uuid[])""",
                run_id, document_owner_id(owner_id), document_ids)
        if not rows:
            raise ValueError("research_knowledge_manifest_missing")
        execution = SearchExecution(scope="run", run_id=run_id, manifest={
            "parameters": {**DEFAULT_PARAMETERS, "version": "legacy-rrf-v1", "rerank_enabled": False, "parent_context": False, "context_neighbors": 0, "per_document_quota": 40},
            "documents": [{"document_id":str(row["document_id"]),"generation_id":str(row["generation_id"]),
                           "index_profile":locator_dict(row["index_profile"])} for row in rows]})
    result = await unified_search(SearchRequest(owner_id=owner_id, document_ids=document_ids,
                                                query=query, limit=limit), execution=execution)
    return [{**item, "chunk_id": item["segment_id"], "source_type": "local_document"} for item in result["results"]]

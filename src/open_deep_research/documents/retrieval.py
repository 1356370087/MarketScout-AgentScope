"""Owner-filtered hybrid retrieval over frozen Run document selections.

Searches the caller's published generations: a segment participates only via
the document's ``current_generation_id`` pointer, or — when a Run already
bound its sources — via the generation frozen in ``research_run_sources`` so
mid-run publishes cannot drift the Run to new material.
"""

from __future__ import annotations

import json
import time
from typing import Any

from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import get_prometheus_metrics

from .database import get_document_pool
from .embeddings import embed_texts, vector_literal
from .identity import document_owner_id
from .settings import get_document_settings


def locator_dict(raw: Any) -> dict[str, Any]:
    """Decode a jsonb locator that asyncpg hands back as a JSON string."""
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return dict(raw or {})


async def search_document_chunks(
    *,
    owner_id: str,
    document_ids: list[str],
    query: str,
    api_key: str | None = None,
    limit: int = 12,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fuse vector, full-text and trigram ranks after mandatory tenant filters."""
    if not document_ids or not query.strip():
        return []
    started = time.perf_counter()
    metrics = get_prometheus_metrics(Configuration.from_runnable_config(None))
    settings = get_document_settings()
    try:
        vector = (
            await embed_texts(
                [query], settings, api_key=api_key, operation="query"
            )
        )[0]
        pool = await get_document_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """WITH eligible AS (
                     SELECT s.*, g.document_id AS document_id, s.index_text AS text,
                            d.filename,
                            coalesce(rss.generation_id, d.current_generation_id) AS bound_generation
                       FROM research_document_segments s
                       JOIN research_document_generations g ON g.id=s.generation_id
                       JOIN research_documents d ON d.id=g.document_id
                       LEFT JOIN research_run_sources rss
                              ON rss.run_id=$7 AND rss.document_id=d.id
                             AND rss.generation_id IS NOT NULL
                      WHERE d.owner_id=$1::uuid AND d.deleted_at IS NULL
                        AND g.document_id=ANY($2::uuid[])
                        AND s.generation_id=coalesce(rss.generation_id, d.current_generation_id)
                   ), vector_rank AS (
                     SELECT id,row_number() OVER(ORDER BY embedding <=> $3::vector) rank FROM eligible
                     WHERE embedding IS NOT NULL ORDER BY embedding <=> $3::vector LIMIT 40
                   ), text_rank AS (
                     SELECT id,row_number() OVER(ORDER BY ts_rank_cd(search_vector,websearch_to_tsquery('simple',$4)) DESC) rank
                     FROM eligible WHERE search_vector @@ websearch_to_tsquery('simple',$4) LIMIT 40
                    ), trigram_rank AS (
                      SELECT id,row_number() OVER(ORDER BY word_similarity($4,text) DESC) rank
                      FROM eligible
                      -- Keep the indexed column on the left: pg_trgm's
                      -- commutator form is the indexable `%>>` predicate.
                      WHERE text %>> $4 AND word_similarity($4,text) >= $6
                      ORDER BY word_similarity($4,text) DESC
                      LIMIT 40
                   ), fused AS (
                     SELECT e.id,
                       coalesce(1.0/(60+v.rank),0)+coalesce(1.0/(60+t.rank),0)+coalesce(1.0/(60+g.rank),0) score
                     FROM eligible e LEFT JOIN vector_rank v USING(id) LEFT JOIN text_rank t USING(id)
                     LEFT JOIN trigram_rank g USING(id)
                     WHERE v.id IS NOT NULL OR t.id IS NOT NULL OR g.id IS NOT NULL
                   ) SELECT e.*,f.score FROM fused f JOIN eligible e USING(id)
                     ORDER BY f.score DESC,e.ordinal LIMIT $5""",
                document_owner_id(owner_id),
                document_ids,
                vector_literal(vector),
                query.strip(),
                limit,
                settings.trigram_min_score,
                run_id,
            )
    except Exception:
        if metrics is not None:
            metrics.observe_document_retrieval(
                time.perf_counter() - started, 0, outcome="error"
            )
        raise
    results = []
    for row in rows:
        locator = locator_dict(row["locator"])
        results.append(
            {
                "document_id": str(row["document_id"]),
                "chunk_id": str(row["id"]),
                "generation_id": str(row["bound_generation"]),
                "filename": row["filename"],
                "locator": locator,
                "heading": locator.get("heading"),
                "text": row["text"],
                "score": float(row["score"]),
                "source_type": "local_document",
                "source_uri": f"/documents/{row['document_id']}?chunk={row['id']}",
            }
        )
    if metrics is not None:
        metrics.observe_document_retrieval(
            time.perf_counter() - started, len(results), outcome="success"
        )
    return results

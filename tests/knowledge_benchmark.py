"""Knowledge retrieval performance benchmark (plan §KB-08 / §5.4).

Seeds synthetic published generations with fake vectors directly in SQL
(no model spend), then measures recall-SQL latency under concurrency.
Scaled runs are honest: pass --docs/--segments to size the corpus and report
the actual numbers; do not extrapolate them to the 500k target.

Usage:
    python tests/knowledge_benchmark.py --docs 200 --segments-per-doc 250 \
        --concurrency 5 --queries 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid

from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.settings import get_document_settings

OWNER_SENTINEL = "benchmark-owner"


async def seed(pool, owner: str, docs: int, segments_per_doc: int) -> int:
    vector = "[" + ",".join(["0.001"] * 1536) + "]"
    async with pool.acquire() as connection:
        existing = await connection.fetchval(
            "SELECT count(*) FROM research_documents WHERE owner_id=$1::uuid", owner
        )
        if int(existing) >= docs:
            return int(existing)
        for doc_index in range(docs):
            sha = f"{doc_index:064x}"[:64].ljust(64, "0")
            document_id = await connection.fetchval(
                """INSERT INTO research_documents
                   (owner_id, filename, media_type, size_bytes, sha256, storage_key, status,
                    chunk_count, current_generation_id)
                   VALUES ($1::uuid, $2, 'text/markdown', 10, $3, 'bench', 'ready', 0, NULL)
                   RETURNING id""",
                owner,
                f"bench-{doc_index}.md",
                sha,
            )
            version_id = await connection.fetchval(
                """INSERT INTO research_document_versions
                   (document_id, version_no, filename, media_type, size_bytes, sha256, storage_key)
                   VALUES ($1::uuid, 1, $2, 'text/markdown', 10, $3, 'bench') RETURNING id""",
                document_id, f"bench-{doc_index}.md", sha,
            )
            generation_id = await connection.fetchval(
                """INSERT INTO research_document_generations
                   (document_id, version_id, status, published_at, metadata_snapshot)
                   VALUES ($1::uuid, $2::uuid, 'published', now(),
                           '{"confirmed": {"doc_type": "行业报告"}}'::jsonb)
                   RETURNING id""",
                document_id, version_id,
            )
            unit_id = await connection.fetchval(
                """INSERT INTO research_document_units
                   (generation_id, ordinal, unit_type, locator, raw_text, index_text)
                   VALUES ($1::uuid, 0, 'paragraph', '{"source": "bench"}'::jsonb,
                           '基准语料', '基准语料') RETURNING id""",
                generation_id,
            )
            rows = []
            for segment_index in range(segments_per_doc):
                rows.append(
                    (
                        str(uuid.uuid4()),
                        generation_id,
                        unit_id,
                        segment_index,
                        f"基准语料 {doc_index} 段 {segment_index}：营收、毛利率与市场份额说明文本。",
                        json.dumps({"source": f"section:{segment_index}"}),
                        vector,
                        "bench-embedding",
                        f"{doc_index:064d}{segment_index:064d}"[:64].ljust(64, "0"),
                    )
                )
                if len(rows) >= 2000:
                    await connection.executemany(
                        """INSERT INTO research_document_segments
                           (id, generation_id, unit_id, ordinal, index_text, locator,
                            embedding, embedding_model, content_hash)
                           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                                   $7::vector, $8, $9)""",
                        rows,
                    )
                    rows = []
            if rows:
                await connection.executemany(
                    """INSERT INTO research_document_segments
                       (id, generation_id, unit_id, ordinal, index_text, locator,
                        embedding, embedding_model, content_hash)
                       VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                               $7::vector, $8, $9)""",
                    rows,
                )
            await connection.execute(
                "UPDATE research_documents SET current_generation_id=$2 WHERE id=$1",
                document_id, generation_id,
            )
    return docs


async def cleanup(pool, owner: str) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM research_documents WHERE owner_id=$1::uuid", owner
        )


RECALL_SQL = """
WITH eligible AS (
  SELECT s.id, s.index_text AS text, s.embedding, s.search_vector
    FROM research_document_segments s
    JOIN research_document_generations g ON g.id=s.generation_id
    JOIN research_documents d ON d.id=g.document_id
   WHERE d.owner_id=$1::uuid AND d.deleted_at IS NULL
     AND s.generation_id=ANY($2::uuid[])
), vector_rank AS (
  SELECT id, row_number() OVER(ORDER BY embedding <=> $3::vector) rank
    FROM eligible WHERE embedding IS NOT NULL
   ORDER BY embedding <=> $3::vector LIMIT 40
), text_rank AS (
  SELECT id, row_number() OVER(
    ORDER BY ts_rank_cd(search_vector, websearch_to_tsquery('simple',$4)) DESC) rank
    FROM eligible WHERE search_vector @@ websearch_to_tsquery('simple',$4) LIMIT 40
), fused AS (
  SELECT e.id, coalesce(1.0/(60+v.rank),0)+coalesce(1.0/(60+t.rank),0) score
    FROM eligible e
    LEFT JOIN vector_rank v USING(id) LEFT JOIN text_rank t USING(id)
   WHERE v.id IS NOT NULL OR t.id IS NOT NULL
)
SELECT count(*) FROM (SELECT id FROM fused ORDER BY score DESC LIMIT 12) top
"""


async def one_query(pool, owner: str, generation_ids: list[str], vector: str) -> float:
    started = time.perf_counter()
    async with pool.acquire() as connection:
        await connection.fetchval(
            RECALL_SQL, owner, generation_ids, vector, "营收 毛利率"
        )
    return (time.perf_counter() - started) * 1000


async def measure(pool, owner: str, concurrency: int, queries: int) -> dict:
    async with pool.acquire() as connection:
        generation_ids = [
            str(row["id"])
            for row in await connection.fetch(
                """SELECT g.id FROM research_document_generations g
                    JOIN research_documents d ON d.current_generation_id=g.id
                   WHERE d.owner_id=$1::uuid LIMIT 400""",
                owner,
            )
        ]
    vector = "[" + ",".join(["0.001"] * 1536) + "]"
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def worker() -> None:
        async with semaphore:
            latencies.append(await one_query(pool, owner, generation_ids, vector))

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(queries)))
    latencies.sort()

    def percentile(fraction: float) -> float:
        index = min(len(latencies) - 1, round(fraction * (len(latencies) - 1)))
        return latencies[index]

    return {
        "queries": len(latencies),
        "concurrency": concurrency,
        "scope_generations": len(generation_ids),
        "p50_ms": round(percentile(0.50), 1),
        "p95_ms": round(percentile(0.95), 1),
        "wall_seconds": round(time.perf_counter() - started, 2),
        "median_ms": round(statistics.median(latencies), 1),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=200)
    parser.add_argument("--segments-per-doc", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--keep", action="store_true", help="skip cleanup afterwards")
    args = parser.parse_args()

    settings = get_document_settings()
    if not settings.configured:
        raise SystemExit(1)
    owner = str(uuid.uuid5(uuid.NAMESPACE_URL, f"insightforge:bench:{OWNER_SENTINEL}"))
    owner = document_owner_id(owner)
    pool = await get_document_pool()
    await seed(pool, owner, args.docs, args.segments_per_doc)
    async with pool.acquire() as connection:
        await connection.fetchval(
            """SELECT count(*) FROM research_document_segments s
                 JOIN research_documents d ON d.current_generation_id=s.generation_id
                WHERE d.owner_id=$1::uuid""",
            owner,
        )
    await measure(pool, owner, args.concurrency, args.queries)
    if not args.keep:
        await cleanup(pool, owner)
    await close_document_pool()


if __name__ == "__main__":
    asyncio.run(main())

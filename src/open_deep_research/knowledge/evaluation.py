"""Retrieval evaluation: Recall@12, nDCG@12, no-answer errors, citations.

Eval items bind expected evidence to text snippets (substring match over the
top results) so sets stay portable across corpus ids. The harness runs the
real unified search per item and persists metrics + per-item details to
``knowledge_eval_runs``; semantic support of answers stays human-reviewed
(plan §5.4), only structural citation validity is checked automatically.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any
from uuid import uuid4

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from .search_service import SearchRequest, unified_search


def _snippet_hit(snippet: str, results: list[dict[str, Any]]) -> bool:
    needle = snippet.strip()
    return any(needle in (item.get("text") or "") or needle in (item.get("context_before") or "")
               or needle in (item.get("context_after") or "") for item in results)


def recall_at_k(expected: list[str], results: list[dict[str, Any]], k: int) -> float:
    """Fraction of expected snippets present in the top-k evidence."""
    if not expected:
        return 0.0
    hits = sum(_snippet_hit(snippet, results[:k]) for snippet in expected)
    return hits / len(expected)


def ndcg_at_k(expected: list[str], results: list[dict[str, Any]], k: int) -> float:
    """Binary-relevance nDCG: an expected snippet in rank i contributes 1/ln(i+2)."""
    if not expected:
        return 0.0
    gains = 0.0
    for snippet in expected:
        for index, item in enumerate(results[:k]):
            if _snippet_hit(snippet, [item]):
                gains += 1.0 / math.log2(index + 2)
                break
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(expected), k)))
    return gains / ideal if ideal else 0.0


def evaluate_answer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Structural citation validity for one answer run."""
    if payload.get("status") == "no_evidence":
        return {"status": "no_evidence", "citation_valid": True}
    if payload.get("status") != "answered":
        return {"status": payload.get("status"), "citation_valid": True}
    evidence_ids = {item["segment_id"] for item in payload.get("evidence") or []}
    cited = {
        segment_id
        for citation in payload.get("citations") or []
        for segment_id in citation.get("segment_ids") or []
    }
    return {
        "status": "answered",
        # Auto-check: every cited id belongs to this evidence set (100% gate).
        "citation_valid": bool(cited) and cited.issubset(evidence_ids),
        "support": payload.get("support"),
    }


async def run_evaluation(
    owner_id: str,
    set_row: dict[str, Any],
    *,
    profile_version: str | None = None,
    with_answers: bool = False,
) -> dict[str, Any]:
    """Execute one eval set through the real pipeline and store the run."""
    from .answer import answer_question

    owner_id = document_owner_id(owner_id)
    items = set_row.get("items") or []
    run_id = str(uuid4())
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO knowledge_eval_runs(id, set_id, owner_id, profile_version)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4)""",
            run_id, str(set_row["id"]), owner_id, profile_version,
        )
    started = time.perf_counter()
    details: list[dict[str, Any]] = []
    recall_sum = ndcg_sum = 0.0
    no_answer_errors = 0
    citation_total = citation_valid = 0
    answerable = unanswerable = 0
    for item in items:
        request = SearchRequest(
            owner_id=owner_id,
            query=str(item.get("question") or ""),
            document_ids=[str(doc) for doc in (item.get("document_ids") or [])],
            filters=item.get("filters") or {},
            profile_version=profile_version,
        )
        expected = [
            str(snippet) for snippet in (item.get("expected") or {}).get("snippets") or []
        ]
        entry: dict[str, Any] = {"id": item.get("id"), "category": item.get("category")}
        if with_answers and item.get("expected", {}).get("answerable") is False:
            answer = await answer_question(request)
            check = evaluate_answer_payload(answer)
            entry["answer_check"] = check
            citation_total += 1
            citation_valid += 1 if check["citation_valid"] else 0
            if answer.get("status") == "answered" and check["citation_valid"]:
                no_answer_errors += 1  # fabricated answer on a no-answer item
            unanswerable += 1
            details.append(entry)
            continue
        search = await unified_search(request)
        results = search.get("results") or []
        item_recall = recall_at_k(expected, results, 12)
        item_ndcg = ndcg_at_k(expected, results, 12)
        recall_sum += item_recall
        ndcg_sum += item_ndcg
        answerable += 1
        entry.update(
            {
                "recall_at_12": round(item_recall, 4),
                "ndcg_at_12": round(item_ndcg, 4),
                "hits": len(results),
            }
        )
        details.append(entry)
    total_items = answerable + unanswerable
    metrics = {
        "items": total_items,
        "recall_at_12": round(recall_sum / answerable, 4) if answerable else None,
        "ndcg_at_12": round(ndcg_sum / answerable, 4) if answerable else None,
        "no_answer_error_rate": round(no_answer_errors / unanswerable, 4) if unanswerable else None,
        "citation_validity": round(citation_valid / citation_total, 4) if citation_total else None,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "answer_latency_p95_ms": _p95(details),
        "cost": "unknown",  # 拿不到费用时标记未知，不能记为零（plan §KB-08）
    }
    async with pool.acquire() as connection:
        await connection.execute(
            """UPDATE knowledge_eval_runs
               SET metrics=$2::jsonb, details=$3::jsonb, finished_at=now()
                WHERE id=$1::uuid""",
            run_id,
            json.dumps(metrics, ensure_ascii=False),
            json.dumps(details, ensure_ascii=False, default=str),
        )
    return {"run_id": run_id, "metrics": metrics, "details": details}


def _p95(details: list[dict[str, Any]]) -> float | None:
    latencies = sorted(
        float(item.get("latency_ms") or 0) for item in details if item.get("latency_ms")
    )
    if not latencies:
        return None
    index = min(len(latencies) - 1, max(0, round(0.95 * (len(latencies) - 1))))
    return latencies[index]


async def list_eval_sets() -> list[dict[str, Any]]:
    """List evaluation sets with item counts."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT id, name, jsonb_array_length(items) AS item_count, created_at
                 FROM knowledge_eval_sets ORDER BY created_at DESC"""
        )
    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "item_count": int(row["item_count"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


async def create_eval_set(name: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Create one evaluation set."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """INSERT INTO knowledge_eval_sets(name, items)
               VALUES ($1, $2::jsonb)
               ON CONFLICT (name) DO UPDATE SET items=excluded.items
               RETURNING id, name""",
            name,
            json.dumps(items, ensure_ascii=False, default=str),
        )
    return {"id": str(row["id"]), "name": row["name"], "item_count": len(items)}


def _decode_jsonb(raw: Any, default: Any) -> Any:
    """Decode a jsonb column that asyncpg hands back as a JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return default if raw is None else raw


async def get_eval_set(set_id: str) -> dict[str, Any] | None:
    """Load one evaluation set with its items."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM knowledge_eval_sets WHERE id=$1::uuid", set_id
        )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "items": _decode_jsonb(row["items"], []),
    }

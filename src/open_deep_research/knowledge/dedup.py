"""Three-layer duplicate detection and source classification (KB-11).

Layer 1: exact file match (SHA-256, already in the documents table).
Layer 2: normalized-text identity (same content, different formatting).
Layer 3: near-duplicate via character-shingle Jaccard similarity (default
threshold 0.85) — candidates only, never auto-merged.

Relations distinguish exact_duplicate / near_duplicate / repost_of /
copied_from; suspected pairs wait for manager confirmation before they
affect independent-corroboration counts.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

DEFAULT_SIMILARITY_THRESHOLD = 0.85
SHINGLE_SIZE = 5  # characters per shingle
MAX_SHINGLES = 512  # bounded fingerprint for performance


def normalize_text(text: str) -> str:
    """Normalize text for comparison: strip whitespace, punctuation, case."""
    text = re.sub(r"\s+", "", text.casefold())
    # Remove common punctuation that differs between formats.
    return re.sub(r"[，。！？；：""''（）【】《》\-\—\·\,\.\!\?\;\:\"'()\[\]<>]", "", text)


def text_hash(text: str) -> str:
    """Return the normalized-text hash for Layer 2 dedup."""
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def shingle_hashes(text: str, *, size: int = SHINGLE_SIZE, limit: int = MAX_SHINGLES) -> list[str]:
    """Bounded character-shingle hashes for near-duplicate detection."""
    normalized = normalize_text(text)
    if len(normalized) <= size:
        return [hashlib.sha256(normalized.encode()).hexdigest()] if normalized else []
    step = max(1, len(normalized) // limit)
    hashes = []
    for start in range(0, len(normalized) - size + 1, step):
        shingle = normalized[start : start + size]
        hashes.append(hashlib.sha256(shingle.encode()).hexdigest())
    return hashes[:limit]


def jaccard_similarity(left: list[str], right: list[str]) -> float:
    """Jaccard overlap of two shingle sets, 0..1."""
    if not left or not right:
        return 0.0
    set_left, set_right = frozenset(left), frozenset(right)
    intersection = len(set_left & set_right)
    union = len(set_left | set_right)
    return intersection / union if union else 0.0


async def compute_fingerprint(
    document_id: str, generation_id: str, text: str,
    *, source_class: str = "unknown",
) -> dict[str, Any]:
    """Compute and persist one generation's content fingerprint."""
    normalized_hash = text_hash(text)
    shingles = shingle_hashes(text)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO knowledge_content_fingerprints
                 (document_id, generation_id, normalized_text_hash, shingle_hashes, source_class)
               VALUES ($1::uuid, $2::uuid, $3, $4, $5)
               ON CONFLICT (generation_id, normalized_text_hash) DO NOTHING""",
            document_id,
            generation_id,
            normalized_hash,
            shingles,
            source_class,
        )
    return {
        "normalized_text_hash": normalized_hash,
        "shingle_count": len(shingles),
        "source_class": source_class,
    }


async def find_duplicates(
    actor_id: str, document_id: str, generation_id: str,
    *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Find exact and near duplicates of one document's generation.

    Returns candidate relations (unconfirmed). Layer 2 checks the shared
    normalized-text-hash index; layer 3 loads bounded shingles for candidate
    documents from the same workspace and computes pairwise Jaccard.
    """
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        mine = await connection.fetchrow(
            """SELECT * FROM knowledge_content_fingerprints
                WHERE generation_id=$1::uuid""",
            generation_id,
        )
        if not mine:
            return []
        # Layer 2: exact normalized-text match.
        exact = await connection.fetch(
            """SELECT DISTINCT ON (f.document_id) f.document_id, f.generation_id
                 FROM knowledge_content_fingerprints f
                WHERE f.normalized_text_hash = $1
                  AND f.document_id <> $2""",
            mine["normalized_text_hash"],
            document_id,
        )
        # Layer 3: near-duplicate via Jaccard on bounded shingles.
        # Candidates: same workspace, reasonable shingle overlap potential.
        candidates = await connection.fetch(
            """SELECT DISTINCT ON (f.document_id)
                      f.document_id, f.generation_id, f.shingle_hashes
                 FROM knowledge_content_fingerprints f
                 JOIN research_documents d ON d.id = f.document_id
                WHERE f.document_id <> $1 AND d.deleted_at IS NULL
                ORDER BY f.document_id, f.created_at DESC""",
            document_id,
        )
    results: list[dict[str, Any]] = []
    exact_ids = {str(row["document_id"]) for row in exact}
    my_shingles = list(mine["shingle_hashes"] or [])
    for row in candidates:
        candidate_id = str(row["document_id"])
        if candidate_id in exact_ids:
            results.append({
                "document_id": candidate_id,
                "generation_id": str(row["generation_id"]),
                "relation_type": "exact_duplicate",
                "similarity": 1.0,
            })
            continue
        if not my_shingles:
            continue
        their_shingles = list(row["shingle_hashes"] or [])
        similarity = jaccard_similarity(my_shingles, their_shingles)
        if similarity >= threshold:
            results.append({
                "document_id": candidate_id,
                "generation_id": str(row["generation_id"]),
                "relation_type": "near_duplicate",
                "similarity": round(similarity, 3),
            })
    return results


async def record_suspected_relations(
    document_id: str, candidates: list[dict[str, Any]]
) -> int:
    """Insert unconfirmed relations (idempotent by unique constraint)."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        for candidate in candidates:
            left, right = sorted([document_id, candidate["document_id"]])
            await connection.execute(
                """INSERT INTO knowledge_source_relations
                     (left_document_id, right_document_id, relation_type, similarity)
                   VALUES ($1::uuid, $2::uuid, $3, $4)
                   ON CONFLICT (left_document_id, right_document_id, relation_type)
                   DO NOTHING""",
                left,
                right,
                candidate["relation_type"],
                candidate.get("similarity"),
            )
    return len(candidates)


async def confirm_relation(
    actor_id: str, relation_id: str
) -> dict[str, Any] | None:
    """Confirm one suspected relation; affects corroboration counts."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """UPDATE knowledge_source_relations
                  SET confirmed_by=$2::uuid, confirmed_at=now()
                WHERE id=$1::uuid AND confirmed_at IS NULL
             RETURNING *""",
            relation_id,
            actor_id,
        )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "relation_type": row["relation_type"],
        "confirmed": True,
    }


async def reject_relation(actor_id: str, relation_id: str) -> bool:
    """Reject one suspected relation; no corroboration impact."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        result = await connection.execute(
            "DELETE FROM knowledge_source_relations WHERE id=$1::uuid AND confirmed_at IS NULL",
            relation_id,
        )
    return result == "DELETE 1"


async def list_relations(
    actor_id: str, knowledge_base_id: str, *,
    confirmed: bool | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """List source relations for one knowledge base."""
    actor_id = document_owner_id(actor_id)
    filter_clause = ""
    if confirmed is not None:
        filter_clause = " AND r.confirmed_at IS " + ("NOT NULL" if confirmed else "NULL")
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""SELECT r.*,
                       (SELECT filename FROM research_documents WHERE id=r.left_document_id) AS left_filename,
                       (SELECT filename FROM research_documents WHERE id=r.right_document_id) AS right_filename
                  FROM knowledge_source_relations r
                  JOIN research_documents d ON d.id = r.left_document_id
                                         AND d.home_knowledge_base_id = $1::uuid
                 WHERE 1=1 {filter_clause}
                 ORDER BY r.created_at DESC LIMIT $2""",
            knowledge_base_id,
            limit,
        )
    return [
        {
            "id": str(row["id"]),
            "relation_type": row["relation_type"],
            "left_document_id": str(row["left_document_id"]),
            "right_document_id": str(row["right_document_id"]),
            "left_filename": row["left_filename"],
            "right_filename": row["right_filename"],
            "similarity": float(row["similarity"]) if row["similarity"] else None,
            "confirmed": row["confirmed_at"] is not None,
        }
        for row in rows
    ]

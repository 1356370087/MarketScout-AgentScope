"""Project retrieved excerpts into the existing research evidence contract."""

import hashlib
import re


def source_excerpt(text: str, maximum: int, query: str = "") -> str:
    """Return a contiguous source window, preserving verifiable quotation text."""
    if len(text) <= maximum:
        return text
    lowered = text.casefold()
    terms = sorted(set(re.findall(r"[\w-]{3,}", query.casefold())), key=len, reverse=True)
    hit = next((lowered.index(term) for term in terms if term in lowered), 0)
    start = max(0, min(hit - maximum // 4, len(text) - maximum))
    return text[start:start + maximum]


def document_evidence(results):
    """Keep retrieval rank separate from unassessed factual confidence."""
    records = []
    for item in results:
        segment = str(item.get("segment_id") or item.get("chunk_id"))
        excerpt = source_excerpt(str(item["text"]).strip(), 1600, str(item.get("match_excerpt") or ""))
        uri = f"/documents/{item['document_id']}?chunk={segment}"
        records.append({
            "evidence_id": "ev-local-" + hashlib.sha256(f"{segment}:{excerpt}".encode()).hexdigest()[:20],
            "claim": str(item.get("heading") or excerpt)[:600], "supporting_excerpt": excerpt,
            "document_id": str(item["document_id"]), "chunk_id": segment,
            "generation_id": str(item["generation_id"]), "unit_id": item.get("unit_id"),
            "locator": item.get("locator") or {}, "source_url": uri, "source_uri": uri,
            "source_title": item["filename"], "source_type": "local_document",
            "source_authority": 0.0, "confidence": None, "security_status": "accepted",
            "retrieval_score": item.get("score"), "relevance": item.get("relevance"),
        })
    return records

"""Stable URL identities and provider-neutral candidate merging."""

import hashlib
from urllib.parse import urlsplit

from open_deep_research.documents.contracts import normalize_source_url
from open_deep_research.web.models import CandidateSource

PROVIDER_URLS = {
    "tavily": "https://api.tavily.com/search",
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "bing": "https://www.bing.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
}


def canonicalize_url(url: str) -> str:
    """Normalize a public HTTP URL without changing content-bearing parameters."""
    try:
        return normalize_source_url(
            str(url),
            reject_private=False,
            strip_trailing_slash=False,
        )
    except ValueError as exc:
        raise ValueError("Candidate URL must be absolute HTTP(S)") from exc


def stable_id(prefix: str, value: str) -> str:
    """Return a compact stable identifier for a normalized value."""
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def normalize_candidates(
    candidates: list[CandidateSource], limit: int
) -> list[CandidateSource]:
    """Canonicalize and merge candidates discovered by multiple queries."""
    merged: dict[str, CandidateSource] = {}
    for candidate in candidates:
        try:
            canonical = canonicalize_url(
                candidate.original_url or candidate.canonical_url
            )
        except TypeError, ValueError:
            continue
        domain = urlsplit(canonical).hostname or ""
        existing = merged.get(canonical)
        if existing is not None:
            existing.query_ids = list(
                dict.fromkeys(existing.query_ids + candidate.query_ids)
            )
            known = {(d.provider, d.query, d.rank) for d in existing.discoveries}
            existing.discoveries.extend(
                d
                for d in candidate.discoveries
                if (d.provider, d.query, d.rank) not in known
            )
            if not existing.snippet and candidate.snippet:
                existing.snippet = candidate.snippet
            existing.provider_rank = min(
                existing.provider_rank, candidate.provider_rank
            )
            continue
        data = candidate.model_copy(
            update={
                "candidate_id": stable_id("src", canonical),
                "canonical_url": canonical,
                "domain": domain,
            }
        )
        merged[canonical] = data
    return list(merged.values())[:limit]

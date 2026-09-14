"""Knowledge-side service credentials and usage metering (plan §KB-04).

Standalone knowledge retrieval and Q&A must not depend on a Research Run
Key. Calls ride the deployment's LiteLLM service key with explicit, typed
error codes when the credential is missing or a per-user daily budget is
exhausted; research runs keep their Run Key and never fall back to this
credential.
"""

from __future__ import annotations

import os

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.settings import get_document_settings


class KnowledgeCredentialError(RuntimeError):
    """Raised when the knowledge service credential is unavailable."""


class KnowledgeBudgetExceeded(RuntimeError):
    """Raised when a per-user model-call budget for the day is exhausted."""


def knowledge_service_key() -> str:
    """Return the service key reserved for knowledge-side model calls."""
    key = os.getenv("LITELLM_SERVICE_KEY", "").strip()
    if not key:
        raise KnowledgeCredentialError("knowledge_service_key_unavailable")
    return key


def daily_model_call_budget() -> int:
    """Per-user knowledge model-call cap per day; 0 disables the cap."""
    try:
        return max(0, int(os.getenv("KNOWLEDGE_MODEL_CALLS_PER_USER_DAY", "0")))
    except ValueError:
        return 0


async def check_and_count_usage(
    owner_id: str, operation: str, calls: int = 1
) -> None:
    """Enforce the optional daily cap and record metered usage.

    Usage is counted per user, request and operation category; research runs
    never reach this path, so no research records are fabricated.
    """
    budget = daily_model_call_budget()
    if budget <= 0:
        return
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        used = await connection.fetchval(
            """SELECT coalesce(sum((result_digest->'usage'->>$2)::int), 0)
                 FROM knowledge_queries
                WHERE owner_id=$1::uuid
                  AND created_at >= date_trunc('day', now())""",
            owner_id,
            operation,
        )
    if int(used or 0) + calls > budget:
        raise KnowledgeBudgetExceeded(
            f"knowledge_budget_exceeded:{operation}:{int(used or 0)}/{budget}"
        )


def answer_model() -> str:
    """Model route used for single-turn answers (empty = unconfigured)."""
    return os.getenv("KNOWLEDGE_ANSWER_MODEL", "").strip() or (
        get_document_settings().metadata_suggestion_model
    )


def rerank_model() -> str:
    """Model route used for semantic reranking (empty = unconfigured)."""
    return os.getenv("KNOWLEDGE_RERANK_MODEL", "").strip()

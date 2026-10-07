"""Knowledge-side service credentials and usage metering (plan §KB-04).

Standalone knowledge retrieval and Q&A must not depend on a Research Run
Key. Calls ride the deployment's LiteLLM service key with explicit, typed
error codes when the credential is missing or a per-user daily budget is
exhausted; research runs keep their Run Key and never fall back to this
credential.
"""

from __future__ import annotations

import os

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




def answer_model() -> str:
    """Model route used for single-turn answers (empty = unconfigured)."""
    return os.getenv("KNOWLEDGE_ANSWER_MODEL", "").strip() or (
        get_document_settings().metadata_suggestion_model
    )


def rerank_model() -> str:
    """Model route used for semantic reranking (empty = unconfigured)."""
    return os.getenv("KNOWLEDGE_RERANK_MODEL", "").strip()

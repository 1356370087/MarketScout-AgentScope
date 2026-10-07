"""Knowledge retrieval, Q&A, saved-search, feedback and evaluation routes."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from open_deep_research.documents.database import document_schema_available
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from .answer import AnswerUnavailableError, answer_question
from .credentials import KnowledgeBudgetExceeded, KnowledgeCredentialError
from .evaluation import (
    create_eval_set,
    get_eval_set,
    list_eval_sets,
    run_evaluation,
)
from .rerank import RerankUnavailableError
from .search_service import (
    SearchRequest,
    SearchScopeError,
    load_profile,
    unified_search,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="knowledge_base_unavailable")


def _error_status(exc: Exception) -> int:
    if isinstance(exc, KnowledgeCredentialError | AnswerUnavailableError):
        return 503
    if isinstance(exc, KnowledgeBudgetExceeded | SearchScopeError):
        return 409
    if isinstance(exc, RerankUnavailableError):
        return 503
    return 400


class SearchFilters(BaseModel):
    """Metadata filters; same field unions, different fields intersect."""

    doc_types: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=10)
    entity_ids: list[str] = Field(default_factory=list, max_length=50)
    publish_date_start: str | None = Field(default=None, max_length=10)
    publish_date_end: str | None = Field(default=None, max_length=10)


class KnowledgeSearchRequest(BaseModel):
    """Unified retrieval request; the owner always comes from auth context."""

    query: str = Field(min_length=1, max_length=2000)
    queries: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=2)
    kb_ids: list[str] = Field(default_factory=list, max_length=20)
    collection_ids: list[str] = Field(default_factory=list, max_length=20)
    document_ids: list[str] = Field(default_factory=list, max_length=200)
    generation_ids: list[str] = Field(default_factory=list, max_length=500)
    version_mode: str = Field(default="current", pattern="^(current|pinned|as_of)$")
    as_of_published: str | None = Field(default=None, max_length=10)
    as_of_valid: str | None = Field(default=None, max_length=10)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = Field(default=12, ge=1, le=50)
    profile_version: str | None = Field(default=None, max_length=64)
    debug: bool = False


def _to_search_request(body: KnowledgeSearchRequest, owner_id: str) -> SearchRequest:
    return SearchRequest(
        owner_id=owner_id,
        query=body.query,
        queries=body.queries,
        kb_ids=body.kb_ids,
        collection_ids=body.collection_ids,
        document_ids=body.document_ids,
        generation_ids=body.generation_ids,
        version_mode=body.version_mode,
        as_of_published=body.as_of_published,
        as_of_valid=body.as_of_valid,
        filters=body.filters.model_dump(exclude_none=True),
        limit=body.limit,
        profile_version=body.profile_version,
        debug=body.debug,
    )


@router.post("/search")
async def knowledge_search(
    body: KnowledgeSearchRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Run the unified retrieval pipeline over published knowledge material."""
    _ensure_enabled()
    try:
        return await unified_search(_to_search_request(body, user.user_id))
    except (SearchScopeError, KnowledgeCredentialError, KnowledgeBudgetExceeded) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc


@router.post("/answer")
async def knowledge_answer(
    body: KnowledgeSearchRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Answer one question strictly from the retrieved evidence, with citations."""
    _ensure_enabled()
    try:
        return await answer_question(_to_search_request(body, user.user_id))
    except AnswerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (SearchScopeError, KnowledgeCredentialError, KnowledgeBudgetExceeded) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc


@router.get("/search-profiles")
async def search_profiles(
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List versioned retrieval parameter sets for comparison."""
    _ensure_enabled()
    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT version, parameters, is_default, note, created_at
                 FROM knowledge_search_profiles ORDER BY created_at DESC"""
        )
    from open_deep_research.documents.retrieval import locator_dict
    from open_deep_research.documents.settings import get_document_settings

    return {
        "index_profile": get_document_settings().index_profile,
        "items": [
            {
                "version": row["version"],
                "parameters": locator_dict(row["parameters"]),
                "is_default": row["is_default"],
                "note": row["note"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]
    }


class SavedSearchRequest(BaseModel):
    """One reusable search condition, not a material snapshot."""

    name: str = Field(min_length=1, max_length=200)
    query: str = Field(default="", max_length=2000)
    scope: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    version_mode: str = Field(default="current", max_length=24)


@router.get("/saved-searches")
async def list_saved_searches(
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List the caller's saved search conditions."""
    _ensure_enabled()
    from open_deep_research.documents.database import get_document_pool
    from open_deep_research.documents.identity import document_owner_id

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT * FROM knowledge_saved_searches
                WHERE owner_id=$1::uuid ORDER BY updated_at DESC""",
            document_owner_id(user.user_id),
        )
    return {"items": [dict(row) | {"id": str(row["id"])} for row in rows]}


@router.post("/saved-searches", status_code=201)
async def save_search(
    body: SavedSearchRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create or update one saved search by name."""
    _ensure_enabled()
    from open_deep_research.documents.database import get_document_pool
    from open_deep_research.documents.identity import document_owner_id

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """INSERT INTO knowledge_saved_searches
                 (owner_id, name, query_text, scope, filters, version_mode)
               VALUES ($1::uuid, $2, $3, $4::jsonb, $5::jsonb, $6)
               ON CONFLICT (owner_id, name) DO UPDATE SET
                 query_text=excluded.query_text, scope=excluded.scope,
                 filters=excluded.filters, version_mode=excluded.version_mode,
                 updated_at=now()
               RETURNING id""",
            document_owner_id(user.user_id),
            body.name,
            body.query,
            json.dumps(body.scope, ensure_ascii=False),
            json.dumps(body.filters, ensure_ascii=False),
            body.version_mode,
        )
    return {"id": str(row["id"]), "name": body.name}


class FeedbackRequest(BaseModel):
    """Query feedback routed into the pending list, never auto-index changes."""

    query_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(
        pattern="^(not_found|wrong_hit|outdated|parse_error|citation_error)$"
    )
    note: str = Field(default="", max_length=2000)


@router.post("/feedback", status_code=202)
async def submit_feedback(
    body: FeedbackRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, str]:
    """Attach review feedback to one recorded query."""
    _ensure_enabled()
    from open_deep_research.documents.database import get_document_pool
    from open_deep_research.documents.identity import document_owner_id

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        result = await connection.execute(
            """UPDATE knowledge_queries
                  SET feedback_kind=$3, feedback_note=$4
                WHERE id=$1::uuid AND owner_id=$2::uuid""",
            body.query_id,
            document_owner_id(user.user_id),
            body.kind,
            body.note,
        )
    if result != "UPDATE 1":
        raise HTTPException(status_code=404, detail="query_not_found")
    return {"query_id": body.query_id, "status": "recorded"}


class EvalSetRequest(BaseModel):
    """One evaluation set: items carry expected evidence snippets."""

    name: str = Field(min_length=1, max_length=200)
    items: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class EvalRunRequest(BaseModel):
    """Run one set through the live pipeline."""

    set_id: str = Field(min_length=1, max_length=64)
    profile_version: str | None = Field(default=None, max_length=64)
    with_answers: bool = False


@router.get("/evaluations")
async def evaluations(
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List evaluation sets and their recent runs."""
    _ensure_enabled()
    from open_deep_research.documents.database import get_document_pool

    sets = await list_eval_sets()
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        runs = await connection.fetch(
            """SELECT r.id, r.set_id, r.profile_version, r.metrics, r.started_at, r.finished_at
                 FROM knowledge_eval_runs r ORDER BY r.started_at DESC LIMIT 20"""
        )
    from open_deep_research.documents.retrieval import locator_dict

    return {
        "sets": sets,
        "runs": [
            {
                "id": str(row["id"]),
                "set_id": str(row["set_id"]),
                "profile_version": row["profile_version"],
                "metrics": locator_dict(row["metrics"]),
                "started_at": row["started_at"].isoformat(),
                "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            }
            for row in runs
        ],
    }


@router.post("/evaluations/sets", status_code=201)
async def upsert_eval_set(
    body: EvalSetRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create or replace one evaluation set."""
    _ensure_enabled()
    return await create_eval_set(body.name, body.items)


@router.post("/evaluations/runs", status_code=202)
async def start_eval_run(
    body: EvalRunRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Execute one evaluation set now and return its metrics."""
    _ensure_enabled()
    set_row = await get_eval_set(body.set_id)
    if not set_row:
        raise HTTPException(status_code=404, detail="eval_set_not_found")
    try:
        return await run_evaluation(
            user.user_id,
            set_row,
            profile_version=body.profile_version,
            with_answers=body.with_answers,
        )
    except (KnowledgeCredentialError, KnowledgeBudgetExceeded, RerankUnavailableError) as exc:
        raise HTTPException(status_code=_error_status(exc), detail=str(exc)) from exc


async def default_parameters() -> dict[str, Any]:
    """Expose the effective default profile for diagnostics pages."""
    return await load_profile(None)

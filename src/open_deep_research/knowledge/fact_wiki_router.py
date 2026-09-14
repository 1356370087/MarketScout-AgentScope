"""Fact ledger and wiki routes (KB-12 / KB-13 / KB-15 export)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from open_deep_research.documents.database import document_schema_available
from open_deep_research.documents.identity import document_owner_id
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import DOCUMENT_READ_OWN, DOCUMENT_WRITE_OWN
from security.rbac.principal import Principal

from . import authz


class EditorialRoute(APIRoute):
    """Enforce URL scope and translate editorial errors for all endpoints."""

    def get_route_handler(self):
        """Wrap the request handler with identifier and record-scope validation."""
        handler = super().get_route_handler()

        async def guarded(request: Request):
            from open_deep_research.documents.database import get_document_pool

            from .facts import FactError
            from .wiki import WikiError

            for key, value in request.path_params.items():
                if key.endswith("_id"):
                    try:
                        UUID(value)
                    except ValueError as exc:
                        raise HTTPException(422, "invalid_identifier") from exc
            for param, table in [
                ("page_id", "knowledge_pages"),
                ("assertion_id", "knowledge_fact_assertions"),
            ]:
                identifier = request.path_params.get(param)
                if identifier:
                    pool = await get_document_pool()
                    async with pool.acquire() as c:
                        exists = await c.fetchval(
                            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id=$1::uuid AND knowledge_base_id=$2::uuid)",
                            identifier,
                            request.path_params["knowledge_base_id"],
                        )
                    if not exists:
                        raise HTTPException(404, "editorial_target_not_found")
            try:
                return await handler(request)
            except authz.AuthorizationError as exc:
                raise HTTPException(403, str(exc)) from exc
            except (FactError, WikiError, ValueError) as exc:
                raise HTTPException(
                    409 if "conflict" in str(exc) else 422, str(exc)
                ) from exc

        return guarded


router = APIRouter(tags=["knowledge"], route_class=EditorialRoute)


def _ensure_enabled() -> None:
    if not document_schema_available():
        raise HTTPException(status_code=503, detail="knowledge_base_unavailable")


def _forbidden(exc: Exception) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


# ---------------------------------------------------------------------------
# KB-12: Facts
# ---------------------------------------------------------------------------


class FactEvidenceItem(BaseModel):
    """One document generation and optional unit or segment supporting an assertion."""

    document_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=64)
    unit_id: str | None = Field(default=None, max_length=64)
    segment_id: str | None = Field(default=None, max_length=64)
    excerpt: str = Field(default="", max_length=1000)


class FactSubmitRequest(BaseModel):
    """A candidate value with comparison context and supporting evidence."""

    entity_name: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=200)
    value_text: str = Field(default="", max_length=2000)
    value_numeric: str | None = Field(default=None, max_length=50)
    unit: str = Field(default="", max_length=64)
    currency: str = Field(default="", max_length=8)
    scale: str = Field(default="", max_length=32)
    data_period: str = Field(default="", max_length=100)
    valid_from: str | None = Field(default=None, max_length=10)
    valid_until: str | None = Field(default=None, max_length=10)
    condition_text: str = Field(default="", max_length=500)
    raw_statement: str = Field(default="", max_length=5000)
    value_origin: str = Field(
        default="source", pattern="^(source|formula|model_inferred)$"
    )
    region: str = Field(default="", max_length=100)
    period_label: str = Field(default="", max_length=100)
    workspace_id: str | None = Field(default=None, max_length=64)
    evidence: list[FactEvidenceItem] = Field(default_factory=list, max_length=20)
    extraction_key: str = Field(default="", max_length=200)


@router.post("/knowledge-bases/{knowledge_base_id}/facts", status_code=201)
async def submit_fact(
    knowledge_base_id: str,
    body: FactSubmitRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Submit one fact assertion as a draft (contributors and up)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_SUBMIT
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .facts import FactError, submit_assertion

    try:
        return await submit_assertion(
            user.user_id,
            knowledge_base_id,
            entity_name=body.entity_name,
            metric=body.metric,
            value_text=body.value_text,
            value_numeric=body.value_numeric,
            unit=body.unit,
            currency=body.currency,
            scale=body.scale,
            data_period=body.data_period,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
            condition_text=body.condition_text,
            raw_statement=body.raw_statement,
            value_origin=body.value_origin,
            region=body.region,
            period_label=body.period_label,
            workspace_id=body.workspace_id,
            evidence=[item.model_dump() for item in body.evidence],
            extraction_key=body.extraction_key,
        )
    except FactError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/knowledge-bases/{knowledge_base_id}/facts")
async def list_facts(
    knowledge_base_id: str,
    entity: str = Query(default="", max_length=200),
    metric: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=24),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List fact assertions with filters."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_VIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .facts import list_assertions

    items = await list_assertions(
        user.user_id,
        knowledge_base_id,
        entity_name=entity,
        metric=metric,
        status=status,
    )
    return {"items": items}


class FactDecisionRequest(BaseModel):
    """A publication or rejection decision with optional supersession."""

    reason: str = Field(default="", max_length=1000)
    supersedes_id: str | None = Field(default=None, max_length=64)


@router.post("/knowledge-bases/{knowledge_base_id}/facts/{assertion_id}/publish")
async def publish_fact(
    knowledge_base_id: str,
    assertion_id: str,
    body: FactDecisionRequest | None = None,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Publish one fact assertion (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_REVIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .facts import publish_assertion

    result = await publish_assertion(
        user.user_id,
        assertion_id,
        supersedes_id=body.supersedes_id if body else None,
    )
    if not result:
        raise HTTPException(status_code=404, detail="fact_not_publishable")
    return result


@router.post("/knowledge-bases/{knowledge_base_id}/facts/{assertion_id}/reject")
async def reject_fact(
    knowledge_base_id: str,
    assertion_id: str,
    body: FactDecisionRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Reject one fact assertion with a reason."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_REVIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .facts import reject_assertion

    result = await reject_assertion(user.user_id, assertion_id, reason=body.reason)
    if not result:
        raise HTTPException(status_code=404, detail="fact_not_rejectable")
    return result


@router.post(
    "/knowledge-bases/{knowledge_base_id}/facts/extract/{document_id}/{generation_id}",
    status_code=202,
)
async def extract_facts(
    knowledge_base_id: str,
    document_id: str,
    generation_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Extract candidate facts from one published generation (idempotent)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_SUBMIT
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .facts import extract_candidates_for_generation

    submitted = await extract_candidates_for_generation(
        user.user_id,
        knowledge_base_id,
        document_id,
        generation_id,
    )
    return {"candidates": len(submitted), "items": submitted}


# ---------------------------------------------------------------------------
# KB-13: Wiki
# ---------------------------------------------------------------------------


class WikiCreateRequest(BaseModel):
    """Page title, entity scope and template selection."""

    title: str = Field(min_length=1, max_length=500)
    template: str = Field(default="company_profile", max_length=64)
    entity_name: str = Field(default="", max_length=200)


class WikiDraftRequest(BaseModel):
    """A complete draft block set guarded by its base revision."""

    base_revision: int = Field(ge=0)
    blocks: list[dict[str, Any]] = Field(min_length=1, max_length=200)


@router.post("/knowledge-bases/{knowledge_base_id}/pages", status_code=201)
async def create_wiki_page(
    knowledge_base_id: str,
    body: WikiCreateRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Create one wiki page from a template."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_SUBMIT
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .wiki import WikiError, create_page

    try:
        return await create_page(
            user.user_id,
            knowledge_base_id,
            title=body.title,
            template=body.template,
            entity_name=body.entity_name,
        )
    except WikiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/knowledge-bases/{knowledge_base_id}/pages")
async def list_wiki_pages(
    knowledge_base_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """List wiki pages in one knowledge base."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_VIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT p.id, p.title, p.template, p.entity_name,
                      (SELECT pr.status FROM knowledge_page_revisions pr
                        WHERE pr.id=p.current_revision_id) AS status,
                      p.updated_at
                FROM knowledge_pages p
                WHERE p.knowledge_base_id=$1::uuid
                  AND (p.published_revision_id IS NOT NULL OR p.created_by=$2::uuid OR $3)
                ORDER BY p.updated_at DESC""",
            knowledge_base_id,
            document_owner_id(user.user_id),
            authz.CAP_REVIEW
            in await authz.kb_capabilities(user.user_id, knowledge_base_id),
        )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "title": row["title"],
                "template": row["template"],
                "entity_name": row["entity_name"],
                "status": row["status"],
                "updated_at": row["updated_at"].isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/knowledge-bases/{knowledge_base_id}/pages/{page_id}")
async def get_wiki_page(
    knowledge_base_id: str,
    page_id: str,
    include_history: bool = Query(default=False),
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Return one wiki page with its current revision blocks."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_VIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .wiki import get_page

    result = await get_page(user.user_id, page_id, include_history=include_history)
    if not result:
        raise HTTPException(status_code=404, detail="page_not_found")
    return result


@router.put("/knowledge-bases/{knowledge_base_id}/pages/{page_id}/draft")
async def save_wiki_draft(
    knowledge_base_id: str,
    page_id: str,
    body: WikiDraftRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Save one draft revision (optimistic lock on base_revision)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_SUBMIT
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .wiki import WikiError, save_draft

    try:
        return await save_draft(
            user.user_id,
            page_id,
            base_revision=body.base_revision,
            blocks=body.blocks,
        )
    except WikiError as exc:
        if "revision_conflict" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/knowledge-bases/{knowledge_base_id}/pages/{page_id}/publish")
async def publish_wiki_page(
    knowledge_base_id: str,
    page_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> dict[str, Any]:
    """Publish the current draft; unsourced fact blocks are rejected."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_REVIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .wiki import WikiError, publish_page

    try:
        result = await publish_page(user.user_id, page_id)
    except WikiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="page_not_publishable")
    return result


@router.post("/knowledge-bases/{knowledge_base_id}/pages/{page_id}/check-stale")
async def check_wiki_stale(
    knowledge_base_id: str,
    page_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
) -> dict[str, Any]:
    """Check and mark stale citations; returns reminders, never rewrites."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_VIEW
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .wiki import check_stale_citations

    stale = await check_stale_citations(page_id)
    return {"stale_blocks": stale, "count": len(stale)}


# ---------------------------------------------------------------------------
# KB-15: Export / Import
# ---------------------------------------------------------------------------


@router.get("/knowledge-bases/{knowledge_base_id}/export")
async def export_kb(
    knowledge_base_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
) -> Response:
    """Export one knowledge base as a ZIP archive (managers only)."""
    _ensure_enabled()
    try:
        await authz.require_kb_capability(
            user.user_id, knowledge_base_id, authz.CAP_MANAGE
        )
    except authz.AuthorizationError as exc:
        raise _forbidden(exc) from exc
    from .exporter import export_knowledge_base

    try:
        zip_bytes, filename = await export_knowledge_base(
            user.user_id, knowledge_base_id
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class FactReviewRequest(BaseModel):
    """A human verification and adoption decision."""

    verification: str = Field(
        default="verified", pattern="^(unverified|verified|disputed)$"
    )
    adopted: bool = False
    reason: str = Field(default="", max_length=1000)


@router.post("/knowledge-bases/{knowledge_base_id}/facts/{assertion_id}/review")
async def review_fact(
    knowledge_base_id: str,
    assertion_id: str,
    body: FactReviewRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Record the manager verification decision for an assertion."""
    from .facts import review_value

    return await review_value(user.user_id, assertion_id, **body.model_dump())


@router.post("/knowledge-bases/{knowledge_base_id}/facts/{assertion_id}/withdraw")
async def withdraw_fact(
    knowledge_base_id: str,
    assertion_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Withdraw a published assertion while retaining its evidence."""
    from .facts import withdraw_assertion

    return await withdraw_assertion(user.user_id, assertion_id)


@router.get("/knowledge-bases/{knowledge_base_id}/facts/{assertion_id}")
async def fact_detail(
    knowledge_base_id: str,
    assertion_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
):
    """Return authorized evidence and published assertions sharing the comparison key."""
    from open_deep_research.documents.database import get_document_pool
    from open_deep_research.documents.identity import document_owner_id

    from . import editorial

    row = await editorial.target(
        user.user_id, "knowledge_fact_assertions", assertion_id, authz.CAP_VIEW
    )
    if (
        row["status"] != "published"
        and str(row["created_by"]) != document_owner_id(user.user_id)
        and authz.CAP_REVIEW
        not in await authz.kb_capabilities(user.user_id, knowledge_base_id)
    ):
        raise HTTPException(403, "fact_draft_forbidden")
    pool = await get_document_pool()
    async with pool.acquire() as c:
        evidence = await c.fetch(
            "SELECT * FROM knowledge_fact_evidence WHERE assertion_id=$1::uuid",
            assertion_id,
        )
        conflicts = await c.fetch(
            "SELECT id,value_text,value_numeric,currency,unit,scale,verification,adopted FROM knowledge_fact_assertions WHERE fact_key_id=$1 AND knowledge_base_id=$2 AND id<>$3 AND status='published' AND currency=$4 AND unit=$5 AND data_period=$6",
            row["fact_key_id"],
            row["knowledge_base_id"],
            row["id"],
            row["currency"],
            row["unit"],
            row["data_period"],
        )
    return {
        "assertion": dict(row),
        "evidence": [dict(e) for e in evidence],
        "alternatives": [dict(e) for e in conflicts],
    }


class WikiGenerateRequest(BaseModel):
    """The page revision against which generation should run."""

    base_revision: int = Field(ge=1)


@router.post("/knowledge-bases/{knowledge_base_id}/pages/{page_id}/generate")
async def generate_wiki(
    knowledge_base_id: str,
    page_id: str,
    body: WikiGenerateRequest,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Create a source-linked draft from published facts."""
    from .wiki import generate_page

    return await generate_page(user.user_id, page_id, body.base_revision)


@router.get("/knowledge-bases/{knowledge_base_id}/pages/{page_id}/revisions/{number}")
async def wiki_revision(
    knowledge_base_id: str,
    page_id: str,
    number: int,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
):
    """Read a previously published page revision."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_VIEW)
    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM knowledge_page_revisions WHERE page_id=$1::uuid AND revision_number=$2 AND status='published'",
            page_id,
            number,
        )
    if not row:
        raise HTTPException(404, "revision_not_found")
    return dict(row)


@router.post("/knowledge-bases/{knowledge_base_id}/exports", status_code=202)
async def queue_export(
    knowledge_base_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Queue an archive for a knowledge-base manager."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_MANAGE)
    from open_deep_research.documents.database import get_document_pool

    from .maintenance import enqueue

    pool = await get_document_pool()
    async with pool.acquire() as c:
        identifier = await enqueue(c, knowledge_base_id, user.user_id, "export", {})
    return {"id": identifier, "status": "queued"}


@router.post("/knowledge-bases/{knowledge_base_id}/imports/precheck")
async def stage_import(
    knowledge_base_id: str,
    file: UploadFile = File(...),
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Persist and validate an uploaded archive before explicit import confirmation."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_MANAGE)
    from open_deep_research.documents.database import get_document_pool

    from .exporter import precheck_import
    from .maintenance import artifact_dir

    identifier = str(uuid4())
    path = artifact_dir() / f"{identifier}.zip"
    try:
        size = 0
        with path.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > int(
                    os.getenv("KNOWLEDGE_IMPORT_MAX_BYTES", str(10 * 1024**3))
                ):
                    raise HTTPException(413, "archive_too_large")
                dest.write(chunk)
        result = await asyncio.to_thread(precheck_import, path)
        if not result["ok"]:
            path.unlink(missing_ok=True)
            return result
        pool = await get_document_pool()
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO knowledge_jobs(id,knowledge_base_id,actor_id,kind,business_key,status,payload,result)
                VALUES($1::uuid,$2::uuid,$3::uuid,'import_precheck',$1,'completed',$4::jsonb,$5::jsonb)""",
                identifier,
                knowledge_base_id,
                document_owner_id(user.user_id),
                json.dumps({"filename": path.name}),
                json.dumps(result),
            )
        return {**result, "id": identifier}
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.post(
    "/knowledge-bases/{knowledge_base_id}/imports/{job_id}/confirm", status_code=202
)
async def confirm_import(
    knowledge_base_id: str,
    job_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_WRITE_OWN.code)),
):
    """Schedule a previously validated archive once for its uploading manager."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_MANAGE)
    from open_deep_research.documents.database import get_document_pool

    from .maintenance import artifact_dir, enqueue

    pool = await get_document_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT payload FROM knowledge_jobs WHERE id=$1::uuid AND knowledge_base_id=$2::uuid AND actor_id=$3::uuid AND kind='import_precheck' AND status='completed'",
            job_id,
            knowledge_base_id,
            document_owner_id(user.user_id),
        )
        if not row:
            raise HTTPException(404, "import_precheck_not_found")
        payload = json.loads(row["payload"])
        path = artifact_dir() / payload["filename"]
        if (
            not path.exists()
            or time.time() - path.stat().st_mtime
            > int(os.getenv("KNOWLEDGE_EXPORT_TTL_HOURS", "24")) * 3600
        ):
            raise HTTPException(410, "import_expired")
        identifier = await enqueue(
            c, knowledge_base_id, user.user_id, "import", payload, "import:" + job_id
        )
    return {"id": identifier, "status": "queued"}


@router.get("/knowledge-bases/{knowledge_base_id}/jobs")
async def transfer_jobs(
    knowledge_base_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
):
    """List the caller-owned jobs in the authorized knowledge base."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_VIEW)
    from open_deep_research.documents.database import get_document_pool

    pool = await get_document_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,kind,status,attempts,error,result,created_at FROM knowledge_jobs WHERE knowledge_base_id=$1::uuid AND actor_id=$2::uuid ORDER BY created_at DESC LIMIT 100",
            knowledge_base_id,
            document_owner_id(user.user_id),
        )
    return {"items": [{**dict(r), "result": json.loads(r["result"])} for r in rows]}


@router.get("/knowledge-bases/{knowledge_base_id}/exports/{job_id}/download")
async def download_export(
    knowledge_base_id: str,
    job_id: str,
    user: Principal = Depends(require_permissions(DOCUMENT_READ_OWN.code)),
):
    """Recheck manager access and expiry before serving an archive."""
    await authz.require_kb_capability(user.user_id, knowledge_base_id, authz.CAP_MANAGE)
    from open_deep_research.documents.database import get_document_pool

    from .maintenance import artifact_dir

    pool = await get_document_pool()
    async with pool.acquire() as c:
        ready = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM knowledge_jobs WHERE id=$1::uuid AND knowledge_base_id=$2::uuid AND kind='export' AND status='completed')",
            job_id,
            knowledge_base_id,
        )
    if not ready:
        raise HTTPException(404, "export_not_ready")
    path = artifact_dir() / f"{job_id}.zip"
    if (
        not path.exists()
        or time.time() - path.stat().st_mtime
        > int(os.getenv("KNOWLEDGE_EXPORT_TTL_HOURS", "24")) * 3600
    ):
        raise HTTPException(410, "export_expired")
    return FileResponse(
        path, media_type="application/zip", filename=f"knowledge-{job_id}.zip"
    )

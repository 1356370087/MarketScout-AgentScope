"""Native run publication commands using the existing durable publisher worker."""

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.report import enqueue_report_publication
from open_deep_research.api.contracts import PublicationRequest
from open_deep_research.events.publications import (
    PublicationEventStore,
    publication_event_payload,
)
from open_deep_research.report.models import PublisherTheme
from open_deep_research.report.publication_store import (
    PublicationJobStore,
    get_publisher_settings,
    worker_available,
)
from open_deep_research.report.publishers import resolve_publication_format
from security.rbac.dependencies import require_permissions
from security.rbac.permissions import RESEARCH_RUN_CONTROL_OWN, RESEARCH_RUN_READ_OWN


def publication_response(job):
    row = job.public_dict() if hasattr(job, "public_dict") else dict(job)
    root = f"/runs/{row['run_id']}/publications"
    row.update(
        status_url=f"{root}/{row['publication_id']}",
        events_url=f"{root}/events",
        download_url=f"{root}/{row['publication_id']}/download",
    )
    if row.get("artifact"):
        row["artifact"]["download_url"] = row["download_url"]
    return row


def build_publication_router(service):
    from open_deep_research.api.research_router import http_errors

    router = APIRouter()
    read = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code))
    control = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code))

    async def store_for(run_id, owner):
        await service.snapshot(run_id, owner)
        if service.runs_dir is None:
            raise HTTPException(503, "publication_storage_unavailable")
        return PublicationJobStore(run_id, runs_dir=str(service.runs_dir))

    def job_for(store, publication_id):
        job = store.get(publication_id)
        if job is None:
            raise HTTPException(404, "publication_not_found")
        return job

    @router.post("/runs/{run_id}/publications")
    async def create(run_id: str, request: PublicationRequest, principal=read):
        with http_errors():
            settings = get_publisher_settings()
            if not settings.enabled:
                raise HTTPException(503, "publisher_disabled")
            store = await store_for(run_id, principal.user_id)
            state, _ = await service.store.load(run_id, principal.user_id)
            if state.status != "completed":
                raise HTTPException(409, "run_not_completed")
            publication_format = resolve_publication_format(request.format)
            if len(state.final_report) > settings.max_input_chars:
                raise HTTPException(413, "publication_input_too_large")
            existing_ids = {
                job.publication_id for job in await asyncio.to_thread(store.list)
            }
            recovery = await RecoverySession.open(
                service.store, run_id, principal.user_id
            )
            try:
                job = await enqueue_report_publication(
                    recovery,
                    recovery.snapshot,
                    publication_format=publication_format,
                    theme=request.theme
                    or PublisherTheme.model_validate(
                        state.application.get("publication_theme") or {}
                    ),
                    runs_dir=str(service.runs_dir),
                    max_attempts=settings.max_attempts,
                )
                response = publication_response(job)
                response["reused"] = job["publication_id"] in existing_ids
                return JSONResponse(
                    response,
                    status_code=202 if job["status"] in {"queued", "running"} else 200,
                )
            finally:
                await recovery.close()

    @router.get("/runs/{run_id}/publications")
    async def listing(run_id: str, principal=read):
        with http_errors():
            store = await store_for(run_id, principal.user_id)
            snapshot = await service.snapshot(run_id, principal.user_id)
            return {
                "run_id": run_id,
                "items": [
                    publication_response(job)
                    for job in (await asyncio.to_thread(store.list))[:100]
                ],
                "events_url": f"/runs/{run_id}/publications/events",
                "default_theme": snapshot["output"].get("publication_theme")
                or PublisherTheme().model_dump(mode="json"),
                "worker": "ready"
                if worker_available(get_publisher_settings())
                else "degraded",
            }

    @router.get("/runs/{run_id}/publications/events")
    async def events(
        run_id: str,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        principal=read,
    ):
        from open_deep_research.api.streams import (
            StreamOptions,
            _publication_event_iterator,
            _sse_headers,
        )
        from open_deep_research.configuration import Configuration
        from security.rbac.database import session_scope
        from security.rbac.dependencies import reauthorize_session
        from security.rbac.settings import get_settings

        with http_errors():
            await store_for(run_id, principal.user_id)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except ValueError:
            raise HTTPException(400, "invalid_publication_event_cursor") from None
        if cursor < 0:
            raise HTTPException(400, "invalid_publication_event_cursor")
        store = PublicationEventStore(run_id, runs_dir=str(service.runs_dir))
        if cursor > await asyncio.to_thread(store.last_sequence):
            raise HTTPException(409, "publication_event_cursor_ahead")

        async def authorize(actor):
            async with session_scope() as db:
                return await reauthorize_session(db, actor) is not None

        class Shutdown:
            def is_set(self):
                return service.closed

        options = StreamOptions(
            configuration=Configuration.from_runnable_config(None),
            shutdown=Shutdown(),
            reauth_interval=get_settings().sse_reauth_interval,
            authorize=authorize,
            publisher_settings=get_publisher_settings(),
        )
        return StreamingResponse(
            _publication_event_iterator(
                store, after=cursor, principal=principal, options=options
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @router.get("/runs/{run_id}/publications/{publication_id}")
    async def status(run_id: str, publication_id: str, principal=read):
        with http_errors():
            store = await store_for(run_id, principal.user_id)
            return publication_response(job_for(store, publication_id))

    @router.post("/runs/{run_id}/publications/{publication_id}/retry")
    async def retry(run_id: str, publication_id: str, principal=control):
        with http_errors():
            store = await store_for(run_id, principal.user_id)
            state, _ = await service.store.load(run_id, principal.user_id)
            if state.status != "completed":
                raise HTTPException(409, "run_not_completed")
            job_for(store, publication_id)

            def requeued(job):
                PublicationEventStore(run_id, runs_dir=str(service.runs_dir)).append(
                    "publication.queued",
                    publication_id=job.publication_id,
                    payload=publication_event_payload(job),
                    dedupe_key=f"{job.publication_id}:retry:{job.attempt}:{job.max_attempts}",
                )

            try:
                return publication_response(
                    await asyncio.to_thread(
                        store.retry, publication_id, on_requeued=requeued
                    )
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @router.get("/runs/{run_id}/publications/{publication_id}/download")
    async def download(run_id: str, publication_id: str, principal=read):
        with http_errors():
            store = await store_for(run_id, principal.user_id)
            job = job_for(store, publication_id)
            if job.status != "completed" or job.artifact is None:
                raise HTTPException(409, "publication_not_ready")
            try:
                path = await asyncio.to_thread(store.artifact_path, job, verify=True)
            except FileNotFoundError:
                raise HTTPException(404, "publication_artifact_missing") from None
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return FileResponse(
                path,
                media_type=job.artifact.media_type,
                filename=job.artifact.filename,
                headers={
                    "ETag": f'"{job.artifact.sha256}"',
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    return router

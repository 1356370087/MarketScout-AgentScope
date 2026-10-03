"""Browser-compatible commands for a host-bound native research application."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Header, HTTPException
from open_deep_research.api.admission import LimitedStreamingResponse

from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
)
from open_deep_research.api.contracts import (
    HumanActionRequest,
    ResumeRunRequest,
    RunRequest,
)
from open_deep_research.api.streams import _sse, _sse_headers
from open_deep_research.configuration import Configuration
from open_deep_research.security.inputs import (
    validate_http_configurable,
    validate_http_metadata,
)
from security.rbac.database import session_scope
from security.rbac.dependencies import reauthorize_session, require_permissions
from security.rbac.permissions import (
    RESEARCH_RUN_CONTROL_OWN,
    RESEARCH_RUN_CREATE,
    RESEARCH_RUN_INTERACT_OWN,
    RESEARCH_RUN_READ_OWN,
)
from security.rbac.settings import get_settings


@contextmanager
def http_errors():
    """Keep ownership misses indistinguishable from absent run identifiers."""
    try:
        yield
    except KeyError:
        raise HTTPException(404, "Run not found") from None
    except (FenceLost, RecoveryConflict) as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def build_research_router(service):
    """Install after the host binds configuration authorization and pipeline ports."""
    router = APIRouter()
    from open_deep_research.api.team_routes import build_team_router

    router.include_router(build_team_router(service))
    read = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code))
    create = Depends(require_permissions(RESEARCH_RUN_CREATE.code))
    control = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code))
    interact = Depends(require_permissions(RESEARCH_RUN_INTERACT_OWN.code))
    idempotency = Header(default=None, alias="Idempotency-Key", max_length=256)
    event_cursor = Header(default=None, alias="Last-Event-ID")

    @router.post("/runs")
    async def create_run(
        request: RunRequest, principal=create, idempotency_key: str | None = idempotency
    ):
        with http_errors():
            run_id = await service.create(
                request, principal, idempotency_key=idempotency_key
            )
            return {"run_id": run_id, "events_url": f"/runs/{run_id}/events"}

    @router.get("/runs")
    async def list_runs(
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
        principal=read,
    ):
        with http_errors():
            if not 1 <= limit <= 100:
                raise ValueError("invalid_run_limit")
            try:
                return await service.list_runs(
                    principal.user_id, limit=limit, cursor=cursor, status=status
                )
            except ValueError as exc:
                if str(exc) == "invalid_cursor":
                    raise HTTPException(400, "invalid_cursor") from None
                raise

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str, principal=read):
        with http_errors():
            return await service.snapshot(run_id, principal.user_id)

    @router.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, request: ResumeRunRequest, principal=control):
        with http_errors():
            validate_http_configurable(request.configurable)
            validate_http_metadata(request.metadata)
            await service.resume(run_id, principal.user_id, request.configurable)
            return {"run_id": run_id, "status": "running"}

    @router.post("/runs/{run_id}/human-actions/{action_id}")
    async def human_action(
        run_id: str, action_id: str, request: HumanActionRequest, principal=interact
    ):
        with http_errors():
            return await service.decide(
                run_id,
                principal.user_id,
                action_id,
                request.action,
                request.message or "",
            )

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, principal=control):
        with http_errors():
            return await service.cancel(run_id, principal.user_id)

    @router.delete("/runs/{run_id}")
    async def delete_run(run_id: str, force: bool = False, dry_run: bool = False, principal=control):
        with http_errors():
            return await service.retention.delete(run_id, principal, force=force, dry_run=dry_run)

    async def stream(run_id, principal, cursor):
        config = Configuration.from_runnable_config(None)
        loop = asyncio.get_running_loop()
        last_output = last_auth = loop.time()
        while not service.closed:
            now = loop.time()
            if (
                principal.session_id
                and now - last_auth >= get_settings().sse_reauth_interval
            ):
                async with session_scope() as db:
                    if await reauthorize_session(db, principal) is None:
                        return
                last_auth = now
            # Read current state before events so a newly committed terminal
            # event is replayed before the stream closes.
            snapshot = await service.snapshot(run_id, principal.user_id)
            records = await service.events(run_id, principal.user_id)
            for event in records:
                if event.sequence <= cursor:
                    continue
                yield _sse(event)
                cursor, last_output = event.sequence, loop.time()
            if snapshot["status"] in {"completed", "failed", "cancelled"}:
                return
            if loop.time() - last_output >= config.sse_heartbeat_seconds:
                yield ": keep-alive\n\n"
                last_output = loop.time()
            await asyncio.sleep(config.sse_poll_interval_ms / 1000)

    @router.get("/runs/{run_id}/events")
    async def events(
        run_id: str,
        after: int = 0,
        last_event_id: str | None = event_cursor,
        principal=read,
    ):
        with http_errors():
            records = await service.events(run_id, principal.user_id)
            snapshot = await service.snapshot(run_id, principal.user_id)
        try:
            cursor = int(last_event_id) if last_event_id is not None else after
        except ValueError:
            raise HTTPException(400, "invalid_event_cursor") from None
        if cursor < 0:
            raise HTTPException(400, "invalid_event_cursor")
        if cursor > (records[-1].sequence if records else 0):
            raise HTTPException(409, "event_cursor_ahead")
        token = await service.admission._reserve_sse_connection(
            principal, Configuration.from_runnable_config(None)
        )
        return LimitedStreamingResponse(
            (_sse(event) for event in records if event.sequence > cursor)
            if snapshot.get("read_only")
            else stream(run_id, principal, cursor),
            admission=service.admission,
            release_token=token,
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @router.post("/runs/stream")
    async def create_stream(request: RunRequest, principal=create):
        token = await service.admission._reserve_sse_connection(
            principal, Configuration.from_runnable_config(None)
        )
        try:
            with http_errors():
                run_id = await service.create(request, principal)
        except BaseException:
            await service.admission.connection_limiter.release(token)
            raise
        return LimitedStreamingResponse(
            stream(run_id, principal, 0),
            admission=service.admission,
            release_token=token,
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    from open_deep_research.api.publications import build_publication_router

    router.include_router(build_publication_router(service))

    return router

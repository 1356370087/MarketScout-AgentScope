"""Create and resume research runs through explicit host scheduling ports."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from open_deep_research.api.contracts import RunRequest, ResumeRunRequest
from open_deep_research.api.streams import _sse_headers
from open_deep_research.configuration import Configuration
from open_deep_research.documents.repository import bind_run_sources, release_run_sources
from open_deep_research.events.public import RunEventStore, event_publisher_from_config
from open_deep_research.logging_config import current_request_id
from open_deep_research.report.models import PublisherTheme
from open_deep_research.run_context import JournalCorruptedError, RunContextError
from open_deep_research.security.inputs import validate_http_configurable, validate_http_metadata
from security.rbac import Principal, apply_principal_to_config, require_permissions
from security.rbac.permissions import RESEARCH_RUN_CREATE, RESEARCH_RUN_CONTROL_OWN

logger = logging.getLogger(__name__)


def _request_query_preview(request: RunRequest) -> str:
    for message in request.messages:
        if str(message.get("role", "")).lower() in {"user", "human"}:
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            return " ".join(str(content).split())[:280]
    return ""


def _run_title(request: RunRequest, run_id: str) -> str:
    explicit = " ".join((request.title or "").split())
    preview = _request_query_preview(request)
    return (explicit or preview[:80] or run_id)[:160]


class RunStartRoutes:
    """Keep request handling separate from runtime state and background workers."""

    def __init__(self, *,
                 engine_factory,
                 load_engine,
                 lookup_record,
                 release_sse,
                 validate_sources,
                 config_from_request,
                 enforce_limits,
                 reserve_sse,
                 new_record,
                 run_background,
                 run_resumed,
                 remember_run,
                 limited_sse,
                 public_events,
                 find_idempotent,
                 require_owner,
                 ):
        self.engine_factory = engine_factory
        self.load_engine = load_engine
        self.lookup_record = lookup_record
        self.release_sse = release_sse
        self.validate_sources = validate_sources
        self.config_from_request = config_from_request
        self.enforce_limits = enforce_limits
        self.reserve_sse = reserve_sse
        self.new_record = new_record
        self.run_background = run_background
        self.run_resumed = run_resumed
        self.remember_run = remember_run
        self.limited_sse = limited_sse
        self.public_events = public_events
        self.find_idempotent = find_idempotent
        self.require_owner = require_owner
        self.router = APIRouter()
        self.router.add_api_route("/runs/stream", self.stream_run, methods=["POST"])
        self.router.add_api_route("/runs", self.create_run, methods=["POST"])
        self.router.add_api_route("/runs/{run_id}/resume", self.resume_run, methods=["POST"], status_code=202)

    async def stream_run(
        self,
        request: RunRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_CREATE.code)),
    ) -> StreamingResponse:
        """Run a research request and stream events with SSE."""
        selected_documents = await self.validate_sources(request, user)
        config = self.config_from_request(request, user)
        configurable = Configuration.from_runnable_config(config)
        self.enforce_limits(user, configurable)
        release_token = await self.reserve_sse(user, configurable)
        bound_run_id: str | None = None
        try:
            engine = self.engine_factory(config)
            record = self.new_record(
                run_id=engine.run_id,
                engine=engine,
                status="running",
                config=config,
            )
            await bind_run_sources(record.run_id, user.user_id, selected_documents)
            bound_run_id = record.run_id
            if engine.context_store is not None:
                engine.context_store.initialize(user.user_id, config)
                engine.context_store._update_manifest(  # noqa: SLF001
                    title=_run_title(request, engine.run_id),
                    query_preview=_request_query_preview(request),
                    publication_theme=(
                        request.publication_theme or PublisherTheme()
                    ).model_dump(mode="json"),
                )
            await event_publisher_from_config(config).publish(
                "run.created",
                payload={"status": "pending"},
                dedupe_key="run:created",
            )
            record.task = asyncio.create_task(self.run_background(record, request, config))
            self.remember_run(record, config)
            logger.info(
                "run created",
                extra={
                    "actor": user.user_id,
                    "action": "run.created",
                    "run_id": record.run_id,
                },
            )
            store = RunEventStore(record.run_id, runs_dir=configurable.runs_dir)
        except Exception:
            if bound_run_id is not None:
                with contextlib.suppress(Exception):
                    await release_run_sources(bound_run_id)
            await self.release_sse(release_token)
            raise
        return StreamingResponse(
            self.limited_sse(
                self.public_events(store, principal=user),
                release_token,
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )


    async def create_run(
        self,
        request: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        user: Principal = Depends(require_permissions(RESEARCH_RUN_CREATE.code)),
    ) -> dict[str, Any]:
        """Create a background research run."""
        selected_documents = await self.validate_sources(request, user)
        config = self.config_from_request(request, user)
        if idempotency_key:
            existing = self.find_idempotent(config, user.user_id, idempotency_key)
            if existing is not None:
                return {
                    "run_id": existing.run_id,
                    "status": existing.status,
                    "events_url": f"/runs/{existing.run_id}/events",
                    "last_event_id": existing.last_public_event_seq,
                    "idempotent_replay": True,
                }
        self.enforce_limits(user, Configuration.from_runnable_config(config))
        engine = self.engine_factory(config)
        record = self.new_record(
            run_id=engine.run_id,
            engine=engine,
            status="running",
            config=config,
        )
        bound = False
        try:
            await bind_run_sources(record.run_id, user.user_id, selected_documents)
            bound = True
            if engine.context_store is not None:
                engine.context_store.initialize(user.user_id, config)
                engine.context_store._update_manifest(  # noqa: SLF001
                    title=_run_title(request, engine.run_id),
                    query_preview=_request_query_preview(request),
                    idempotency_key=idempotency_key,
                    publication_theme=(
                        request.publication_theme or PublisherTheme()
                    ).model_dump(mode="json"),
                )
            created = await event_publisher_from_config(config).publish(
                "run.created",
                payload={"status": "pending"},
                dedupe_key="run:created",
            )
            record.task = asyncio.create_task(self.run_background(record, request, config))
            self.remember_run(record, config)
        except Exception:
            if bound:
                with contextlib.suppress(Exception):
                    await release_run_sources(record.run_id)
            raise
        logger.info(
            "run created",
            extra={"actor": user.user_id, "action": "run.created", "run_id": record.run_id},
        )
        return {
            "run_id": record.run_id,
            "status": record.status,
            "events_url": f"/runs/{record.run_id}/events",
            "last_event_id": created.sequence,
            "idempotent_replay": False,
        }


    async def resume_run(
        self,
        run_id: str,
        request: ResumeRunRequest,
        user: Principal = Depends(require_permissions(RESEARCH_RUN_CONTROL_OWN.code)),
    ) -> dict[str, str]:
        """Explicitly resume an interrupted file-backed Query run."""
        active = self.lookup_record(run_id)
        if active is not None:
            self.require_owner(active, user)
            if active.status == "completed":
                raise HTTPException(status_code=409, detail="run_already_completed")
            if active.status == "cancelled":
                raise HTTPException(status_code=409, detail="run_not_recoverable")
            if active.status not in {"failed", "cancelled"}:
                raise HTTPException(status_code=409, detail="run_already_active")

        try:
            validate_http_configurable(request.configurable)
            validate_http_metadata(request.metadata)
        except ValueError as exc:
            logger.warning("security.unsafe_config_rejected: %s", exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config = apply_principal_to_config(
            {
                "configurable": dict(request.configurable),
                "metadata": {
                    **request.metadata,
                    "run_id": run_id,
                    "deployment_surface": "http",
                    "request_id": current_request_id(),
                },
            },
            user,
        )
        runs_dir = str(request.configurable.get("runs_dir") or Configuration.from_runnable_config(None).runs_dir)
        try:
            engine = self.load_engine(run_id, runs_dir=runs_dir, config=config)
            if engine.context_store is None:
                raise JournalCorruptedError("run_not_recoverable")
            replay = engine.context_store.replay()
        except RunContextError as exc:
            if str(exc).startswith("run_schema_not_resumable:"):
                raise HTTPException(
                    status_code=409,
                    detail=str(exc),
                ) from None
            raise HTTPException(status_code=409, detail="run_not_recoverable") from None
        except (ValueError, OSError):
            raise HTTPException(status_code=409, detail="run_not_recoverable") from None
        if not replay.manifest.owner_id or replay.manifest.owner_id != user.user_id:
            raise HTTPException(status_code=404, detail="Run not found")
        if replay.manifest.status == "completed":
            raise HTTPException(status_code=409, detail="run_already_completed")
        if replay.manifest.status == "cancelled" or replay.manifest.next_stage == "cancelled":
            raise HTTPException(status_code=409, detail="run_not_recoverable")
        try:
            await engine.acquire_run_lease()
        except Exception as exc:
            from open_deep_research.tasks.lease import LeaseConflictError

            if isinstance(exc, LeaseConflictError):
                raise HTTPException(status_code=409, detail="run_already_active") from None
            raise

        effective_config = getattr(engine, "config", config)
        record = self.new_record(
            run_id=run_id,
            engine=engine,
            status="running",
            config=effective_config,
        )
        record.task = asyncio.create_task(self.run_resumed(record))
        self.remember_run(record, effective_config)
        logger.info(
            "run resumed",
            extra={"actor": user.user_id, "action": "run.resumed", "run_id": run_id},
        )
        return {"run_id": run_id, "status": "running"}



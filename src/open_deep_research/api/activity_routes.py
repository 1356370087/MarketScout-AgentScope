"""Public run events and task activity projections over their separate cursors."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from open_deep_research.api.observability import _observability_store
from open_deep_research.api.streams import _sse_headers
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import RunEventStore
from open_deep_research.events.task_activity import (
    TaskActivityStore,
    derive_trace_activity,
)
from open_deep_research.run_context import RunContextStore, JournalCorruptedError
from security.rbac import Principal, require_permissions
from security.rbac.settings import local_dev_bypass_enabled
from security.rbac.permissions import (
    RESEARCH_TASK_ACTIVITY_READ_OWN,
    RESEARCH_RUN_READ_OWN,
    RESEARCH_DIAGNOSTICS_PREVIEW,
)


def _task_activity_preview_allowed(user: Principal) -> bool:
    """Authorize bounded diagnostic previews without trusting the browser."""
    if local_dev_bypass_enabled():
        return True
    enabled = os.environ.get("TASK_ACTIVITY_PREVIEW_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return enabled and user.has(RESEARCH_DIAGNOSTICS_PREVIEW.code)


def _require_task_in_run(
    run_id: str,
    task_id: str,
    configurable: Configuration,
) -> Any:
    """Return the public task projection or hide unknown/cross-run task IDs."""
    projection = RunEventStore(run_id, runs_dir=configurable.runs_dir).project()
    task = projection.task_items.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


class ActivityRoutes:
    """Bind streaming to the application connection and authorization lifecycle."""

    def __init__(
        self,
        *,
        lookup_record,
        require_run_owner,
        require_record_owner,
        reserve_sse_connection,
        limited_sse,
        task_activity_iterator,
        public_event_iterator,
        native_service=lambda: None,
    ):
        self.lookup_record = lookup_record
        self.native_service = native_service
        self._require_run_owner = require_run_owner
        self._require_record_owner = require_record_owner
        self._reserve_sse_connection = reserve_sse_connection
        self._limited_sse = limited_sse
        self._task_activity_iterator = task_activity_iterator
        self._public_event_iterator = public_event_iterator
        self.router = APIRouter(tags=["activity"])
        self.router.add_api_route(
            "/runs/{run_id}/tasks/{task_id}/activity",
            self.get_task_activity,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/tasks/{task_id}/activity/stream",
            self.stream_task_activity,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/runs/{run_id}/events", self.stream_run_events, methods=["GET"]
        )

    async def _task_context(self, run_id, task_id, user):
        """Resolve native ownership and task membership from SQL, then legacy history."""
        service = self.native_service()
        if service is not None:
            try:
                state, _ = await service.store.load(run_id, user.user_id)
            except KeyError:
                pass
            else:
                from open_deep_research.events.public import project_public_events
                from open_deep_research.agentscope_runtime.run_config import RunConfig

                projection = project_public_events(await service.events(run_id, user.user_id))
                if task_id not in projection.task_items:
                    raise HTTPException(404, "Task not found")
                config = RunConfig.restore(state.application["configuration"]).compatibility_projection()
                config["configurable"]["runs_dir"] = str(service.runs_dir)
                return SimpleNamespace(status=state.status), Configuration.from_runnable_config(config)
        record, configurable = self._require_run_owner(run_id, user)
        _require_task_in_run(run_id, task_id, configurable)
        return record, configurable

    async def get_task_activity(
        self,
        run_id: str,
        task_id: str,
        before: int | None = None,
        limit: int = 100,
        kind: str | None = None,
        user: Principal = Depends(
            require_permissions(RESEARCH_TASK_ACTIVITY_READ_OWN.code)
        ),
    ) -> dict[str, Any]:
        """Return one reverse-page of safe task activity in chronological order."""
        _record, configurable = await self._task_context(run_id, task_id, user)
        if before is not None and before < 1:
            raise HTTPException(status_code=400, detail="invalid_activity_cursor")
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="activity_limit_out_of_range")
        valid_kinds = {
            "lifecycle",
            "model",
            "tool",
            "source",
            "quality",
            "checkpoint",
            "control",
            "security",
            "error",
        }
        if kind is not None and kind not in valid_kinds:
            raise HTTPException(status_code=400, detail="invalid_activity_kind")

        store = TaskActivityStore(run_id, task_id, runs_dir=configurable.runs_dir)
        source = "native"
        if store.exists:
            items, has_more, last_event_id = await asyncio.to_thread(
                store.page,
                before=before,
                limit=limit,
                kind=kind,
            )
        else:
            observed = _observability_store()
            run = observed.get_run(run_id, user_id=user.user_id)
            all_derived = derive_trace_activity(
                run_id,
                task_id,
                observed.list_spans(run_id) if run is not None else [],
            )
            last_event_id = all_derived[-1].sequence if all_derived else 0
            source = "derived_trace" if all_derived else "summary_only"
            derived = all_derived
            if kind is not None:
                derived = [event for event in derived if event.kind == kind]
            if before is not None:
                derived = [event for event in derived if event.sequence < before]
            has_more = len(derived) > limit
            items = derived[-limit:]
        return {
            "run_id": run_id,
            "task_id": task_id,
            "items": [event.public_dict() for event in items],
            "oldest_sequence": items[0].sequence if items else 0,
            "last_event_id": last_event_id,
            "has_more": has_more,
            "detail_level": "preview"
            if _task_activity_preview_allowed(user)
            else "summary",
            "source": source,
            "stream_url": f"/runs/{run_id}/tasks/{task_id}/activity/stream",
        }

    async def stream_task_activity(
        self,
        run_id: str,
        task_id: str,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        user: Principal = Depends(
            require_permissions(RESEARCH_TASK_ACTIVITY_READ_OWN.code)
        ),
    ) -> StreamingResponse:
        """Replay and tail a task-local activity stream while the drawer is open."""
        record, configurable = await self._task_context(run_id, task_id, user)
        store = TaskActivityStore(run_id, task_id, runs_dir=configurable.runs_dir)
        cursor = after
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid_activity_cursor"
                ) from None
        if cursor < 0:
            raise HTTPException(status_code=400, detail="invalid_activity_cursor")
        current = await asyncio.to_thread(store.last_sequence)
        if cursor > current:
            raise HTTPException(status_code=409, detail="activity_cursor_ahead")
        if not store.exists and (
            record is None or record.status in {"completed", "failed", "cancelled"}
        ):
            raise HTTPException(
                status_code=409, detail="activity_stream_unavailable_legacy_run"
            )
        release_token = await self._reserve_sse_connection(user, configurable)
        return StreamingResponse(
            self._limited_sse(
                self._task_activity_iterator(store, after=cursor, principal=user),
                release_token,
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    async def stream_run_events(
        self,
        run_id: str,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        user: Principal = Depends(require_permissions(RESEARCH_RUN_READ_OWN.code)),
    ) -> StreamingResponse:
        """Replay and tail the durable public event stream for a run."""
        record = self.lookup_record(run_id)
        if record is not None:
            self._require_record_owner(record, user)
            configurable = Configuration.from_runnable_config(record.engine.config)
        else:
            configurable = Configuration.from_runnable_config(None)
            try:
                manifest = RunContextStore(
                    run_id, runs_dir=configurable.runs_dir
                ).load_manifest()
            except ValueError, JournalCorruptedError, OSError:
                raise HTTPException(status_code=404, detail="Run not found") from None
            if not manifest.owner_id or manifest.owner_id != user.user_id:
                raise HTTPException(status_code=404, detail="Run not found")

        store = RunEventStore(run_id, runs_dir=configurable.runs_dir)
        if not store.exists and (
            record is None or (record.task is not None and record.task.done())
        ):
            raise HTTPException(
                status_code=409, detail="event_stream_unavailable_legacy_run"
            )
        cursor = after
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid_event_cursor"
                ) from None
        if cursor < 0:
            raise HTTPException(status_code=400, detail="invalid_event_cursor")
        current = await asyncio.to_thread(store.last_sequence)
        if cursor > current:
            raise HTTPException(status_code=409, detail="event_cursor_ahead")
        release_token = await self._reserve_sse_connection(user, configurable)
        return StreamingResponse(
            self._limited_sse(
                self._public_event_iterator(store, after=cursor, principal=user),
                release_token,
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

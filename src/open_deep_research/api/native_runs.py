"""Durable application boundary for AgentScope research HTTP commands.

The host supplies authorized configuration and a resource-owning pipeline factory.
No legacy engine or in-memory run record is used as checkpoint authority.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import traceback
from contextlib import suppress
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentscope.message import AssistantMsg, UserMsg
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_events import public_events
from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
    digest,
)
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.admission import ApiAdmission
from open_deep_research.api.history import HistoricalRunReader
from open_deep_research.api.projections import _stable_output
from open_deep_research.events.public import PublicEvent, project_public_events
from open_deep_research.security.inputs import (
    validate_client_messages,
    validate_http_configurable,
    validate_http_metadata,
)


def browser_status(state):
    """Preserve the existing browser status vocabulary at the HTTP boundary."""
    if state.status == "ready":
        return "pending"
    if state.status == "waiting":
        if state.pending:
            return {
                "clarify_with_user": "awaiting_clarification",
                "plan_approval": "awaiting_plan_approval",
                "outline_approval": "awaiting_outline_approval",
            }[state.pending.stage]
        return "awaiting_fetch_budget_approval"
    return state.status


class NativeRuns:
    """Coordinate durable commands and supervised local executors.

    ``pipeline_factory(record, run_config, recovery)`` is an async context
    manager that yields a ResearchPipeline and closes all models/tools on exit.
    ``prepare_config(request, principal)`` performs source and live IAM checks.
    """

    def __init__(self, store, pipeline_factory, prepare_config, *, runs_dir=None, admission=None):
        self.store = store
        self.pipeline_factory = pipeline_factory
        self.prepare_config = prepare_config
        self.tasks = {}
        self.closed = False
        self.runs_dir = Path(runs_dir) if runs_dir is not None else None
        self.admission = admission or ApiAdmission()
        # The supported single-worker deployment serializes admission through
        # the durable insert; simultaneous requests cannot overbook one slot.
        self.creation_lock = asyncio.Lock()
        from open_deep_research.api.retention import NativeRunRetention
        self.retention = NativeRunRetention(self)

    def history(self, run_id, owner):
        if self.runs_dir is None:
            raise KeyError("run not found")
        try:
            return HistoricalRunReader(self.runs_dir, run_id, owner)
        except OSError:
            raise KeyError("run not found") from None
        except ValueError:
            raise RecoveryConflict("historical_artifact_corrupted") from None

    def history_read(self, run_id, owner, *, events=False):
        archive = self.history(run_id, owner)
        if events and not archive._path("public_events.jsonl").exists():
            raise RecoveryConflict("event_stream_unavailable_legacy_run")
        try:
            return archive.events() if events else archive.snapshot()
        except OSError, ValueError:
            raise RecoveryConflict("historical_artifact_corrupted") from None

    async def resume(self, run_id, owner, overrides=None):
        try:
            state, _ = await self.store.load(run_id, owner)
        except KeyError:
            self.history(run_id, owner)
            raise RecoveryConflict("legacy_checkpoint_read_only") from None
        RunConfig.restore(state.application["configuration"], overrides=overrides)
        await self.start(run_id, owner, resume_failed=True)

    async def create(self, request, principal, *, idempotency_key=None):
        async with self.creation_lock:
            return await self._create(request, principal, idempotency_key=idempotency_key)

    async def _create(self, request, principal, *, idempotency_key=None):
        if self.closed:
            raise RecoveryConflict("runtime_shutting_down")
        validate_http_configurable(request.configurable)
        validate_http_metadata(request.metadata)
        validate_client_messages(request.messages)
        if not request.messages:
            raise ValueError("messages must not be empty")
        if any(
            not isinstance(message.get("content"), str) for message in request.messages
        ):
            raise ValueError("research message content must be text")
        owner = principal.user_id
        run_id = (
            uuid5(NAMESPACE_URL, digest([owner, idempotency_key])).hex
            if idempotency_key
            else uuid4().hex
        )
        request_digest = digest(request.model_dump(mode="json"))
        try:
            previous, _ = await self.store.load(run_id, owner)
        except KeyError:
            previous = None
        if previous is not None:
            if previous.application.get("request_digest") != request_digest:
                raise RecoveryConflict("idempotency_key_payload_mismatch")
            if previous.status in {"ready", "running"}:
                with suppress(FenceLost):
                    await self.start(run_id, owner)
            return run_id
        from open_deep_research.configuration import Configuration

        # Admission is deployment policy, not a client-overridable run option.
        cfg = Configuration.from_runnable_config(None)
        async with self.store.engine.connect() as connection:
            active = await connection.scalar(select(func.count()).select_from(self.store.runs).where(
                self.store.runs.c.user_id == owner,
                self.store.runs.c.snapshot["status"].as_string().not_in({"completed", "failed", "cancelled"}),
            ))
        self.admission.enforce_creation(principal, cfg, active)
        prepared = await self.prepare_config(request, principal)
        from open_deep_research.logging_config import current_request_id
        prepared["metadata"] = {
            **request.metadata, **prepared.get("metadata", {}),
            "request_id": current_request_id(),
        }
        config = RunConfig.compile(prepared)
        messages = [
            (
                UserMsg
                if message.get("role", message.get("type")) in {"user", "human"}
                else AssistantMsg
            )(
                "user"
                if message.get("role", message.get("type")) in {"user", "human"}
                else "assistant",
                message["content"],
            )
            for message in request.messages
        ]
        try:
            await self.store.create_from_config(
                owner,
                run_id,
                config,
                messages=messages,
                application={
                    "evaluation_capture": prepared.get("evaluation_capture") is True,
                    "knowledge_manifest": prepared.get("metadata", {}).get("knowledge_manifest"),
                    "selected_source_snapshots": prepared.get("metadata", {}).get("selected_source_snapshots", []),
                    "request_digest": request_digest,
                    "request_metadata": {**request.metadata, "request_id": current_request_id()},
                    "identity": {
                        "session_id": principal.session_id,
                        "authz_version": principal.authz_version,
                    },
                    "configuration": config.snapshot(),
                    # 交互开关（如 enable_human_in_loop）不在冻结契约内；
                    # 持久化已校验的请求配置，供管线工厂合并到运行配置。
                    "request_configurable": dict(request.configurable),
                    "title": request.title or request.messages[-1]["content"][:160],
                    "created_at": time.time(),
                    "source_selection": request.source_selection.model_dump(
                        mode="json"
                    ),
                    "publication_theme": request.publication_theme.model_dump(
                        mode="json"
                    )
                    if request.publication_theme
                    else {},
                },
            )
        except IntegrityError:
            previous, _ = await self.store.load(run_id, owner)
            if previous.application.get("request_digest") != request_digest:
                raise RecoveryConflict("idempotency_key_payload_mismatch") from None
        with suppress(FenceLost):
            await self.start(run_id, owner)
        return run_id

    async def start(self, run_id, owner, *, automatic=False, resume_failed=False):
        if self.closed:
            raise RecoveryConflict("runtime_shutting_down")
        state, _ = await self.store.load(run_id, owner)
        if state.status in {"completed", "cancelled"}:
            raise RecoveryConflict("run_not_recoverable")
        previous = self.tasks.get(run_id)
        if previous is not None and not previous.done():
            return False
        recovery = await RecoverySession.open(self.store, run_id, owner)
        try:
            if resume_failed and state.status == "failed":
                async with self.store.transaction(recovery.lease) as (connection, row):
                    persisted = row["snapshot"]
                    has_operations = await connection.scalar(select(exists().where(
                        self.store.ops.c.run_id == run_id
                    )))
                    budget_exhausted = any(
                        row["used"].get(dimension, 0)
                        + row["reserved"].get(dimension, 0) >= maximum
                        for dimension, maximum in row["limits"].items()
                    )
                    deadline_exceeded = (
                        row["deadline"] is not None
                        and await self.store._now(connection) >= row["deadline"]
                    )
                    # 显式重试仅重开尚未进入研究阶段或外部操作的初始化失败。
                    if (
                        persisted["status"] != "failed"
                        or persisted.get("completed")
                        or persisted.get("inflight")
                        or persisted.get("completion_outcome", {}).get("action") == "terminate"
                        or persisted.get("error") in {
                            "UnknownOperation", "BudgetExhausted", "DeadlineExceeded", "ResearchTerminated"
                        }
                        or has_operations
                        or budget_exhausted
                        or deadline_exceeded
                    ):
                        raise RecoveryConflict("run_not_recoverable")
                ready = recovery.snapshot.model_copy(deep=True)
                ready.status, ready.error = "ready", None
                await recovery.save(ready)
            if automatic and recovery.snapshot.status not in {"ready", "running", "waiting"}:
                await recovery.close()
                return False
        except BaseException:
            await recovery.close()
            raise
        task = asyncio.create_task(self._execute(recovery))
        self.tasks[run_id] = task

        def completed(finished):
            if self.tasks.get(run_id) is finished:
                self.tasks.pop(run_id, None)
            if not finished.cancelled() and finished.exception() is not None:
                logging.getLogger(__name__).error(
                    "Native executor cleanup failed: %s",
                    type(finished.exception()).__name__,
                )

        task.add_done_callback(completed)
        return True

    async def recover_interrupted(self):
        """Restart unowned native work and acknowledged decisions after shutdown.

        Waiting without an answer and terminal failures require explicit user
        action. RecoverySession keeps unknown model/tool outcomes quarantined;
        discovery never overrides those operation receipts or another lease.
        """
        if self.closed:
            return 0
        runs, decisions = self.store.runs, self.store.decisions
        async with self.store.engine.connect() as connection:
            rows = (await connection.execute(select(runs.c.run_id, runs.c.user_id).where(
                runs.c.expires <= self.store._clock(connection),
                or_(
                    runs.c.snapshot["status"].as_string().in_({"ready", "running"}),
                    (runs.c.snapshot["status"].as_string() == "waiting") & exists(
                        select(1).where(decisions.c.run_id == runs.c.run_id, decisions.c.state == "pending")
                    ),
                ),
            ))).all()
        started = 0
        for run_id, owner in rows:
            try:
                admitted = await self.start(run_id, owner, automatic=True)
            except (FenceLost, KeyError):
                continue
            except RecoveryConflict as error:
                if str(error) == "run_not_recoverable":
                    continue
                raise
            started += int(admitted)
        return started

    async def _execute(self, recovery):
        try:
            config = RunConfig.restore(recovery.snapshot.application["configuration"])
            async with self.pipeline_factory(
                recovery.snapshot, config, recovery
            ) as pipeline:
                await recovery.consume_decisions(pipeline)
                async for _ in pipeline.reply_stream():
                    pass
        except asyncio.CancelledError:
            # Shutdown retains the durable inflight operation; explicit cancel
            # has already fenced the executor in RecoveryStore.request_cancel.
            raise
        except Exception as exc:  # noqa: BLE001 - persist a sanitized execution failure
            # 只记录调用帧和异常类型，避免上游异常文本中的凭据进入日志。
            logging.getLogger(__name__).error(
                "Native execution failed (%s):\n%s",
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            state = recovery.snapshot.model_copy(deep=True)
            state.status, state.error = "failed", type(exc).__name__
            with suppress(FenceLost):
                await recovery.save(state)
        finally:
            await recovery.close()

    async def decide(
        self, run_id, owner, action_id, action, message="", command_id=None
    ):
        command_id = command_id or digest([action_id, action, message])
        result = await self.store.submit_decision(
            run_id,
            owner,
            command_id,
            action_id,
            {"action": action, "feedback": message},
        )
        # The database commit is the acknowledgement; resume always consumes
        # queued commands again after a crash between commit and scheduling.
        if run_id in self.tasks:
            await asyncio.shield(self.tasks[run_id])
        state, _ = await self.store.load(run_id, owner)
        if state.status not in {"completed", "cancelled"}:
            await self.start(run_id, owner)
        return {"status": result, "action_id": action_id}

    async def cancel(self, run_id, owner, command_id=None):
        await self.store.request_cancel(
            run_id, owner, command_id or "http-cancel:" + run_id
        )
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"run_id": run_id, "status": "cancelled"}

    async def events(self, run_id, owner):
        try:
            state, _ = await self.store.load(run_id, owner)
        except KeyError:
            return self.history_read(run_id, owner, events=True)
        created = state.application.get("created_at", 0)
        from datetime import UTC, datetime

        output = []
        for event in await self.store.events(run_id, owner):
            for index, (kind, stage, payload) in enumerate(public_events(event)):
                output.append(
                    PublicEvent(
                        run_id=run_id,
                        event_id=f"{event['event_id']}:{index}",
                        sequence=len(output) + 1,
                        timestamp=datetime.fromtimestamp(
                            event["payload"].get("timestamp", created), UTC
                        ).isoformat(),
                        type=kind,
                        stage=stage,
                        payload=payload,
                        dedupe_key=f"native:{event['event_id']}:{index}",
                    )
                )
        return output

    async def snapshot(self, run_id, owner):
        try:
            state, revision = await self.store.load(run_id, owner)
        except KeyError:
            return self.history_read(run_id, owner)
        projection = project_public_events(await self.events(run_id, owner))
        status = browser_status(state)
        from open_deep_research.api.publications import run_publications
        publications = await run_publications(run_id, self.runs_dir) if self.runs_dir is not None else []
        research = await self.store.research_progress_view(run_id, owner)
        from open_deep_research.agentscope_runtime.efficiency import progress_summary

        efficiency = progress_summary(research, state.coverage_contract)
        return {
            "run_id": run_id,
            "engine": "agentscope",
            "status": status,
            "title": state.application.get("title", run_id),
            "created_at": state.application.get("created_at"),
            "revision": revision,
            "pending_human_action": projection.pending_human_action,
            "pending_security_approvals": projection.pending_security_approvals,
            "progress": {**projection.model_dump(mode="json"), "status": status, **({"efficiency": efficiency} if research else {})},
            "output": {
                **_stable_output(
                    state.report_product,
                    publications=publications,
                    publication_theme=state.application.get("publication_theme"),
                    preferred_output_format=state.application.get("request_configurable", {}).get("output_format")
                    or state.application.get("configuration", {})
                    .get("contract", {})
                    .get("configurable", {})
                    .get("output_format"),
                ),
                "markdown": state.final_report if state.status == "completed" else "",
                **({"status": status} if state.status in {"failed", "cancelled"} else {}),
            },
            "last_event_id": projection.last_event_id,
            "events_url": f"/runs/{run_id}/events",
        }

    async def list_runs(self, owner, *, limit=50, cursor=None, status=None):
        async with self.store.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(self.store.runs.c.run_id).where(
                            self.store.runs.c.user_id == owner,
                        )
                    )
                )
                .scalars()
                .all()
            )
        items = [await self.snapshot(run_id, owner) for run_id in rows]
        if self.runs_dir is not None and self.runs_dir.exists():
            for path in self.runs_dir.glob("*/context/manifest.json"):
                run_id = path.parent.parent.name
                if run_id in rows:
                    continue
                with suppress(KeyError, RecoveryConflict):
                    items.append(self.history_read(run_id, owner))
        if status is not None:
            items = [item for item in items if item["status"] == status]
        items.sort(
            key=lambda item: (item["created_at"] or 0, item["run_id"]), reverse=True
        )
        if cursor is not None:
            try:
                position = json.loads(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                )
                if not isinstance(position, list) or len(position) != 2:
                    raise ValueError("invalid_cursor")
                boundary = (float(position[0]), str(position[1]))
            except ValueError, TypeError, IndexError:
                raise ValueError("invalid_cursor") from None
            items = [
                item
                for item in items
                if (item["created_at"] or 0, item["run_id"]) < boundary
            ]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        [page[-1]["created_at"], page[-1]["run_id"]],
                        separators=(",", ":"),
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
        return {"items": page, "next_cursor": next_cursor}

    async def aclose(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

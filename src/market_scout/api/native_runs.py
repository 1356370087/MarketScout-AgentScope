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

from market_scout.agentscope_runtime.recovery import RecoverySession
from market_scout.agentscope_runtime.recovery_events import public_events
from market_scout.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
    digest,
)
from market_scout.agentscope_runtime.run_config import RunConfig
from market_scout.api.admission import ApiAdmission
from market_scout.api.history import HistoricalRunReader
from market_scout.api.projections import _stable_output
from market_scout.events.public import PublicEvent, project_public_events
from market_scout.security.inputs import (
    validate_client_messages,
    validate_http_configurable,
    validate_http_metadata,
)


def idempotent_run_id(owner, key):
    """Stable execution identity shared with durable dispatch intents."""
    return uuid5(NAMESPACE_URL, digest([owner, key])).hex


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
                "report_time_extension_approval": "awaiting_report_time_extension_approval",
            }[state.pending.stage]
        return "awaiting_budget_approval"
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
        self.automation_start_lock = asyncio.Lock()
        from market_scout.api.retention import NativeRunRetention
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

    async def resume(self, run_id, owner, overrides=None, *, automation=False):
        try:
            state, _ = await self.store.load(run_id, owner)
        except KeyError:
            self.history(run_id, owner)
            raise RecoveryConflict("legacy_checkpoint_read_only") from None
        RunConfig.restore(state.application["configuration"], overrides=overrides)
        if state.application.get("identity", {}).get("kind") == "intelligence" and not automation:
            raise RecoveryConflict("automation_resume_via_work_item")
        if run_id in self.tasks and not self.tasks[run_id].done():
            return True if automation else None
        started = await self.start(run_id, owner, resume_failed=True)
        return started if automation else None

    async def create(self, request, principal, *, idempotency_key=None):
        async with self.creation_lock:
            return await self._create(request, principal, idempotency_key=idempotency_key)

    async def create_automated(self, request, *, item_id, authorization_id, manifest, execution_number=1):
        """Trusted coordinator entry; HTTP metadata cannot select this identity."""
        from market_scout.intelligence.authorization import principal_for_grant, require_managed_accounting
        require_managed_accounting()
        from market_scout.intelligence.contracts import STRICT_QUALITY
        principal, grant, plan = await principal_for_grant(authorization_id)
        for key, value in STRICT_QUALITY.items():
            if request.configurable.get(key) is not value:
                raise ValueError("continuous_research_requires_strict_quality")
        identity = {"kind": "intelligence", "authorization_id": grant["id"], "plan_id": plan["id"],
                    "item_id": item_id, "execution_number": execution_number, "authz_version": principal.authz_version}
        async with self.creation_lock:
            return await self._create(request, principal, idempotency_key=f"intelligence:{item_id}:{execution_number}",
                automation={"identity": identity, "observation_manifest": manifest,
                    "budget_micro_usd": grant["scope"]["budget"]["run_micro_usd"]})

    async def create_review(self, parent_id, inputs, principal, key, *, preview=False):
        from market_scout.agentscope_runtime.derived_review import review_inputs
        parent, _ = await self.store.load(parent_id, principal.user_id)
        request, version, estimate = review_inputs(parent, inputs)
        if preview:
            from market_scout.platform import runtime, quota
            month = await quota.runway(principal.user_id) if runtime.enabled() else None
            return {"estimated_review_micro_usd": estimate, "monthly_available": month and month["available"], **version}
        if not key:
            raise ValueError("draft_review_requires_idempotency_key")
        async with self.creation_lock:
            return await self._create(request, principal, idempotency_key=key, derivation=version)

    async def _create(self, request, principal, *, idempotency_key=None, automation=None, derivation=None):
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
            idempotent_run_id(owner, idempotency_key)
            if idempotency_key
            else uuid4().hex
        )
        request_digest = digest({**request.model_dump(mode="json"), **({"automation": automation} if automation else {}),
                                 **({"review_derivation": derivation} if derivation else {})})
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
        from market_scout.configuration import Configuration

        # Admission is deployment policy, not a client-overridable run option.
        from market_scout.platform import runtime as platform_runtime
        if platform_runtime.enabled():
            from market_scout.platform.integration import prepare_run
            cfg, prepared = await prepare_run(self.prepare_config, request, principal)
        else:
            cfg = Configuration.from_runnable_config(None)
            prepared = None
        async with self.store.engine.connect() as connection:
            active = await connection.scalar(select(func.count()).select_from(self.store.runs).where(
                self.store.runs.c.user_id == owner,
                self.store.runs.c.snapshot["status"].as_string().not_in({"completed", "failed", "cancelled"}),
            ))
        self.admission.enforce_creation(principal, cfg, active)
        if prepared is None:
            prepared = await self.prepare_config(request, principal)
        from market_scout.logging_config import current_request_id
        prepared["metadata"] = {
            **request.metadata, **prepared.get("metadata", {}),
            "request_id": current_request_id(),
        }
        if automation:
            prepared["metadata"]["observation_manifest"] = automation["observation_manifest"]
        if derivation:
            if platform_runtime.enabled():
                from market_scout.platform.quota import runway
                if (await runway(owner))["available"] == 0:
                    raise RecoveryConflict("monthly_quota_exhausted")
        config = RunConfig.compile(prepared, cost_ceiling_micro_usd=derivation["max_cost_micro_usd"] if derivation else None)
        if derivation:
            cap = config.get("max_run_cost_micro_usd")
            if (not cap or cap > derivation["max_cost_micro_usd"] or not config.get("report_review_enabled")
                    or config.get("report_review_fail_open") or not config.get("quality_evaluation_enabled")
                    or config.get("quality_evaluation_fail_open")):
                raise ValueError("review_requires_strict_gates_and_authorized_cost_limit")
        if automation:
            from market_scout.intelligence.authorization import validate_frozen_automation
            validate_frozen_automation(config, automation["budget_micro_usd"])
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
                    "evaluation_capture": prepared.get("evaluation_capture") is True or config.compatibility_projection()["metadata"]["run_config_schema_version"] >= 18,
                    "knowledge_manifest": prepared.get("metadata", {}).get("knowledge_manifest"),
                    "selected_source_snapshots": prepared.get("metadata", {}).get("selected_source_snapshots", []),
                    "request_digest": request_digest,
                    **({"review_derivation": derivation} if derivation else {}),
                    "request_metadata": {**request.metadata, "request_id": current_request_id()},
                    "identity": automation["identity"] if automation else {
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
        state, _ = await self.store.load(run_id, owner)
        if state.application.get("identity", {}).get("kind") == "intelligence":
            async with self.automation_start_lock:
                from market_scout.intelligence.settings import settings
                identity = state.application["identity"]
                async with self.store.engine.connect() as connection:
                    runs = self.store.runs
                    active = (await connection.execute(select(runs.c.snapshot["application"]["identity"]["plan_id"].as_string()).where(
                        runs.c.run_id != run_id, runs.c.owner.is_not(None), runs.c.expires > self.store._clock(connection),
                        runs.c.snapshot["application"]["identity"]["kind"].as_string() == "intelligence"))).scalars().all()
                if len(active) >= settings().max_runs or identity["plan_id"] in active:
                    return False
                return await self._start(run_id, owner, automatic=automatic, resume_failed=resume_failed)
        return await self._start(run_id, owner, automatic=automatic, resume_failed=resume_failed)

    async def _can_resume_catalogue(self, conn, row):
        """Resume a paid writer's committed result after a provable local assembly failure."""
        import hashlib
        from market_scout.report.references import generated_catalogue_conflict
        state = row["snapshot"]
        if (state.get("error") != "ValueError" or state.get("inflight") != "final_report_generation"
                or state.get("completed", [])[-1:] != ["outline_approval"] or any(row["reserved"].values())):
            return False
        product = state.get("report_product") or {}
        draft, sha = product.get("final_report_draft"), product.get("report_draft_sha256")
        artifact = state.get("application", {}).get("report_review_artifacts", {}).get(sha)
        if not draft or not artifact or artifact.get("review") or hashlib.sha256(draft.encode()).hexdigest() != sha:
            return False
        ops = self.store.ops.c
        unsafe = await conn.scalar(select(exists().where(ops.run_id == row["run_id"], or_(
            ops.state != "committed", ops.kind.in_(["model:report_review", "model:report_revisor"]),
            ops.result["status"].as_string().in_(["uncertain", "unknown"]),
            ops.result["outcome"]["status"].as_string().in_(["uncertain", "unknown"]),
            ops.result["cost_status"].as_string() == "unknown", ops.result["usage_status"].as_string() == "unknown",
            ops.result["uncertain"].as_boolean().is_(True)))))
        if unsafe:
            return False
        results = await conn.scalars(select(ops.result).where(ops.run_id == row["run_id"],
            ops.kind == "model:final_report", ops.key.like("final_report_generation:%:report:lead.final_report:%")))
        for result in results:
            if (result or {}).get("codec") != "agentscope-response-v1":
                continue
            blocks = (result.get("response") or {}).get("content") or []
            writer = blocks if isinstance(blocks, str) else "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            if generated_catalogue_conflict(draft, writer):
                return True
        return False

    async def _start(self, run_id, owner, *, automatic=False, resume_failed=False):
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
                    local_catalogue = await self._can_resume_catalogue(connection, row)
                    budget_exhausted = any(
                        row["used"].get(dimension, 0)
                        + row["reserved"].get(dimension, 0) >= maximum
                        for dimension, maximum in row["limits"].items()
                        if not (local_catalogue and dimension == "fetch_calls")
                    )
                    deadline_exceeded = (
                        row["deadline"] is not None
                        and await self.store._now(connection) >= row["deadline"]
                    )
                    if (
                        persisted["status"] != "failed"
                        or (not local_catalogue and (persisted.get("completed") or persisted.get("inflight") or has_operations))
                        or persisted.get("completion_outcome", {}).get("action") == "terminate"
                        or persisted.get("error") in {
                            "UnknownOperation", "BudgetExhausted", "DeadlineExceeded", "ResearchTerminated"
                        }
                        or budget_exhausted
                        or deadline_exceeded
                    ):
                        raise RecoveryConflict("run_not_recoverable")
                ready = recovery.snapshot.model_copy(deep=True)
                ready.status, ready.error = "ready", None
                if local_catalogue:
                    ready.application["report_preflight_recovery"] = {"reason": "duplicate_generated_bibliography",
                        "initial_draft_sha256": ready.report_product["report_draft_sha256"]}
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

    async def recover_interrupted(self, *, kind=None):
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
                *([runs.c.snapshot["application"]["identity"]["kind"].as_string() == kind] if kind else []),
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
            from market_scout.platform import runtime as platform_runtime
            snapshot = config.compatibility_projection().get("metadata", {}).get("platform_snapshot")
            owner = recovery.lease.user_id
            secrets = await platform_runtime.credential_values(snapshot, owner) if snapshot else None
            with platform_runtime.scope(snapshot, owner, secrets):
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
        self, run_id, owner, action_id, action, message="", command_id=None, *, source_selection=None, expected_version=None, extension_seconds=None, limits=None
    ):
        if source_selection is not None:
            snapshot, _ = await self.store.load(run_id, owner)
            if snapshot.application.get("identity", {}).get("kind") == "intelligence":
                raise RecoveryConflict("automation_source_changes_require_plan_authorization")
        payload = {"action": action, "feedback": message}
        if source_selection is not None:
            payload["source_selection"] = source_selection
        if expected_version is not None:
            payload["expected_version"] = expected_version
        if extension_seconds is not None:
            payload["extension_seconds"] = extension_seconds
        if limits is not None:
            payload["limits"] = limits
        command_id = command_id or digest([action_id, payload])
        result = await self.store.submit_decision(
            run_id,
            owner,
            command_id,
            action_id,
            payload,
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
        from market_scout.api.publications import run_publications
        publications = await run_publications(run_id, self.runs_dir) if self.runs_dir is not None else []
        research = await self.store.research_progress_view(run_id, owner)
        from market_scout.agentscope_runtime.efficiency import progress_summary

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
            "progress": {**projection.model_dump(mode="json"), "status": status,
                "source_plan": state.application.get("source_plan"),
                **({"efficiency": efficiency} if research else {})},
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

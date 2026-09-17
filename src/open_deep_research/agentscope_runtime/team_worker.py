"""Durable native Researcher workers, team controls and immutable result artifacts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from agentscope.middleware import MiddlewareBase
from agentscope.state import AgentState
from pydantic import BaseModel
from sqlalchemy import text

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.agentscope_runtime.research_agents import (
    ResearchAssignment,
    ResearchHandoff,
)
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.agentscope_runtime.team import native_task
from open_deep_research.configuration import Configuration
from open_deep_research.tasks.state import TaskSnapshot
from open_deep_research.tasks.team_protocol import MemberIdentity
from open_deep_research.tools.base import (
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)


class WorkerBusy(RuntimeError):
    """Another live executor owns this task."""


class TaskStopped(RuntimeError):
    """A persisted control command stopped the task."""


class _Say(BaseModel):
    to: str = "lead"
    content: str


class _Inputs(MiddlewareBase):
    def __init__(self, worker):
        self.worker = worker

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        await self.worker.consume_inputs()
        async for event in next_handler(**input_kwargs):
            yield event


class LeaderInbox(MiddlewareBase):
    """Persist consumed TeamSay input before exposing it to the next model call."""

    def __init__(self, workers):
        self.workers = workers

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        await self.workers.consume_leader_inputs()
        async for event in next_handler(**input_kwargs):
            yield event


class _HandoffGate:
    def __init__(self, quality, config_provider):
        self.quality, self.config_provider = quality, config_provider

    async def handoff(self, outcome, contract):
        cfg = Configuration.from_runnable_config(self.config_provider())
        if cfg.quality_evaluation_enabled:
            return await self.quality.handoff(outcome, contract)
        from open_deep_research.quality.gate import (
            HandoffAssessment,
            deterministic_handoff_checks,
        )

        checks = deterministic_handoff_checks(
            outcome.model_dump(mode="json"),
            min_sources=cfg.quality_evaluation_min_sources,
            coverage_contract=contract,
        )
        return HandoffAssessment(
            accepted=checks["passed"],
            relevance=3,
            source_quality=3,
            evidence_coverage=3,
            groundedness=3,
            deterministic_checks=checks,
            reason="Deterministic admission; semantic quality evaluation disabled",
            caveats=["semantic_quality_disabled"],
        )


class TeamWorkers:
    """Supervisor dispatch port; the same execute method runs in separate processes.

    Subprocess hosts rebuild authorized runtime resources and call ``execute``
    with an already-created task id. Credentials never travel in a task message.
    """

    def __init__(
        self,
        team,
        recovery,
        researcher,
        quality,
        artifact_dir,
        *,
        ttl=30,
        failpoint=None,
        external=False,
        launcher=None,
    ):
        self.team, self.recovery = team, recovery
        self.researcher, self.quality = researcher, quality
        self.artifact_dir = Path(artifact_dir)
        self.ttl, self.failpoint = ttl, failpoint
        self._assign_lock = asyncio.Lock()
        self._active = set()
        self._service = None
        self.closed = False
        self.external = external
        self.launcher = launcher

    async def hit(self, point):
        if self.failpoint:
            await self.failpoint(point)

    async def stop(self, task_id):
        await self.team.command("stop:" + task_id, "task_stop", {"task_id": task_id})

    async def consume_leader_inputs(self):
        for event in await self.team.pending("lead"):

            async def apply(db, item):
                if item.type == "message":
                    await db.execute(
                        """UPDATE research_team_members SET session=jsonb_set(session,'{_native_inbox}',
                           coalesce(session->'_native_inbox','[]'::jsonb) || $2::jsonb)
                           WHERE run_id=$1 AND member_id='lead'""",
                        self.team.lease.run_id,
                        json.dumps(
                            [
                                f"TeamSay {item.sender}: {item.payload.get('content', '')}"
                            ]
                        ),
                    )

            await self.team.apply_input("lead", event.event_id, apply)
        async with self.team.transport.store.pool.acquire() as db:
            inbox = await db.fetchval(
                "SELECT session->'_native_inbox' FROM research_team_members WHERE run_id=$1 AND member_id='lead'",
                self.team.lease.run_id,
            )
        self.recovery.snapshot.feedback_by_task["supervisor"] = (
            json.loads(inbox) if inbox else []
        )

    async def prepare(self, assignment, contract, feedback=()):
        """Idempotently persist the assignment before scheduling its executor."""
        async with self._assign_lock:
            rows = await self.team.service.tasks(self.team.lease.run_id)
            existing = next(
                (row for row in rows if row["task_id"] == assignment.task_id), None
            )
            if existing:
                if (
                    existing["research_topic"] != assignment.research_topic
                    or existing["requirement_ids"] != assignment.requirement_ids
                    or existing["coverage_contract"] != contract
                ):
                    raise ValueError("task id reused with another assignment")
            else:
                cfg = Configuration.from_runnable_config(
                    self.researcher.config_provider()
                )
                snapshot = TaskSnapshot(
                    task_id=assignment.task_id,
                    run_id=self.team.lease.run_id,
                    user_id=self.team.lease.user_id,
                    research_topic=assignment.research_topic,
                    requirement_ids=assignment.requirement_ids,
                    coverage_contract=contract,
                    pending_update_instructions=list(feedback),
                )
                await self.team.command(
                    "task:" + assignment.task_id,
                    "task_create",
                    {
                        "snapshot": snapshot.model_dump(mode="json"),
                        "max_tasks": max(
                            1,
                            cfg.max_researcher_iterations
                            * cfg.max_concurrent_research_units,
                        ),
                        "single_task": contract.get("single_research_task", False),
                    },
                )
                existing = next(
                    row
                    for row in await self.team.service.tasks(self.team.lease.run_id)
                    if row["task_id"] == assignment.task_id
                )
            if not existing["owner"]:
                async with self.team.transport.store.pool.acquire() as db:
                    member = await db.fetchval(
                        """SELECT m.member_id FROM research_team_members m WHERE run_id=$1
                           AND member_id<>'lead' AND status<>'closed' AND NOT EXISTS (
                               SELECT 1 FROM research_team_tasks t WHERE t.run_id=m.run_id
                               AND t.owner=m.member_id AND t.status IN ('pending','running','waiting_for_confirmation'))
                           ORDER BY member_id LIMIT 1""",
                        self.team.lease.run_id,
                    )
                if member is None:
                    cfg = Configuration.from_runnable_config(
                        self.researcher.config_provider()
                    )
                    member = await self.team.add_member(
                        "member:" + assignment.task_id,
                        "worker-" + assignment.task_id,
                        "research",
                        max_members=cfg.max_concurrent_research_units,
                    )
                await self.team.command(
                    "claim:" + assignment.task_id,
                    "task_claim",
                    {"task_id": assignment.task_id, "owner": member},
                )
            await self.recovery.store.register_task(self.team.lease, assignment.task_id)

    async def dispatch(self, assignment, contract, feedback=()):
        if self.closed:
            raise RuntimeError("team workers are closed")
        await self.prepare(assignment, contract, feedback)
        if self.external:
            while not self.closed:
                async with (
                    self.team.transport.store.pool.acquire() as db,
                    db.transaction(),
                ):
                    await self.team.transport.guard(db)
                rows = await self.team.service.tasks(self.team.lease.run_id)
                row = next(row for row in rows if row["task_id"] == assignment.task_id)
                if row["status"] == "completed":
                    return await self.artifact(assignment.task_id)
                if row["status"] in {"cancelled", "failed", "timed_out"}:
                    raise TaskStopped(row["status"])
                if row.get("_worker_error"):
                    raise RuntimeError(row["_worker_error"])
                if self.launcher is not None:
                    await self.launcher.ensure_started(
                        self.team.lease, assignment.task_id,
                        not_before=(row.get("_native_worker") or {}).get("expires", 0),
                    )
                await asyncio.sleep(0.1)
            raise RuntimeError("team workers are closed")
        return await self.execute(assignment.task_id)

    async def serve(self):
        """Run an independently hosted consumer; SQL receipts survive lost wakeups."""

        async def attempt(task_id):
            try:
                await self.execute(task_id)
            except WorkerBusy, TaskStopped:
                return

        cfg = Configuration.from_runnable_config(self.researcher.config_provider())
        self._service = asyncio.current_task()
        try:
            while not self.closed:
                rows = await self.team.service.tasks(self.team.lease.run_id)
                ready = [
                    row
                    for row in rows
                    if row["owner"]
                    and row["status"] in {"pending", "running"}
                    and not row.get("_worker_error")
                ]
                if ready:
                    await asyncio.gather(
                        *(
                            attempt(row["task_id"])
                            for row in ready[: cfg.max_concurrent_research_units]
                        )
                    )
                await asyncio.sleep(0.1)
        finally:
            self._service = None

    async def execute(self, task_id):
        if self.closed:
            raise RuntimeError("team workers are closed")
        current = asyncio.current_task()
        self._active.add(current)
        try:
            return await _Worker(self, task_id).run()
        finally:
            self._active.discard(current)

    async def aclose(self):
        self.closed = True
        pending = list(self._active)
        if self._service is not None and self._service is not asyncio.current_task():
            pending.append(self._service)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.launcher is not None:
            await self.launcher.aclose()

    async def artifact(self, task_id):
        rows = await self.team.service.tasks(self.team.lease.run_id)
        row = next(row for row in rows if row["task_id"] == task_id)
        if row["status"] != "completed" or row["result"] is None:
            raise ValueError("task result is not committed")
        result = ResearchHandoff.model_validate(row["result"])
        result.assessment["handoff"] = row["handoff_assessment"]
        async with self.team.transport.store.pool.acquire() as db, db.transaction():
            await self.team.transport.guard(db)
        session_id = self.team.identity("task", task_id)
        native = await self.team.storage.get_session(
            self.team.lease.user_id, "", session_id
        )
        if native is not None:
            native.state = AgentState.model_validate(result.agent_state)
            native.state.tasks_context.tasks = [native_task(row)]
            await self.team.storage.update_session_state(
                self.team.lease.user_id, native.agent_id, session_id, native.state
            )
        data = result.model_dump_json().encode()
        checksum = hashlib.sha256(data).hexdigest()
        folder = (
            self.artifact_dir
            / self.team.identity("artifacts")
            / self.team.identity("task", task_id)
        )
        path = folder / (checksum + ".json")
        if row.get("result_artifact_sha256"):
            if (
                checksum != row["result_artifact_sha256"]
                or not path.exists()
                or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
            ):
                raise ValueError("research artifact integrity failure")
            return result
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (uuid4().hex + ".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        await self.hit("artifact_written")
        async with self.team.transport.store.pool.acquire() as db, db.transaction():
            await self.team.transport.guard(db)
            await db.execute(
                """UPDATE research_team_tasks SET snapshot=snapshot || $3::jsonb
                   WHERE run_id=$1 AND task_id=$2 AND status='completed'""",
                self.team.lease.run_id,
                task_id,
                json.dumps(
                    {
                        "result_artifact_path": str(path),
                        "result_artifact_sha256": checksum,
                    }
                ),
            )
        return result


class _Worker:
    def __init__(self, host, task_id):
        self.host, self.task_id = host, task_id
        self.team = copy.copy(host.team)
        self.team.transport = copy.copy(host.team.transport)
        self.token = uuid4().hex
        self.parent_guard = host.team.transport.guard
        self.team.transport.guard = self.guard
        self.team.service = type(host.team.service)(self.team.transport)
        self.member_id = None
        self.session = None
        self.current = None
        self.failure = None

    async def claim(self):
        async with self.team.transport.store.pool.acquire() as db, db.transaction():
            await self.parent_guard(db)
            schema = await db.fetchval("SELECT current_schema()")
            self.task_table = '"' + schema.replace('"', '""') + '".research_team_tasks'
            row = await db.fetchrow(
                """UPDATE research_team_tasks SET status='running', version=version+1,
                   snapshot=(snapshot - '_worker_error') || jsonb_build_object('_native_worker',
                     jsonb_build_object('token',$3::text,'epoch',$4::bigint,
                       'expires',extract(epoch from clock_timestamp())+$5::double precision))
                   WHERE run_id=$1 AND task_id=$2 AND owner IS NOT NULL
                     AND status IN ('pending','running') AND (
                       snapshot->'_native_worker' IS NULL
                       OR (snapshot->'_native_worker'->>'epoch')::bigint<>$4
                       OR (snapshot->'_native_worker'->>'expires')::double precision<=extract(epoch from clock_timestamp()))
                   RETURNING *""",
                self.team.lease.run_id,
                self.task_id,
                self.token,
                self.team.lease.fence,
                self.host.ttl,
            )
            if row is None:
                state = await db.fetchval(
                    "SELECT status FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
                    self.team.lease.run_id,
                    self.task_id,
                )
                if state == "completed":
                    return None
                if state in {"cancelled", "failed", "timed_out"}:
                    raise TaskStopped(state)
                raise WorkerBusy(self.task_id)
            self.member_id = row["owner"]
            return json.loads(row["snapshot"])

    async def guard(self, db):
        await self.parent_guard(db)
        current = await db.fetchval(
            """SELECT task_id FROM research_team_tasks WHERE run_id=$1 AND task_id=$2
               AND owner=$3 AND status IN ('running','completed')
               AND snapshot->'_native_worker'->>'token'=$4
               AND (snapshot->'_native_worker'->>'expires')::double precision>extract(epoch from clock_timestamp())""",
            self.team.lease.run_id,
            self.task_id,
            self.member_id,
            self.token,
        )
        if current is None:
            raise FenceLost("worker execution token expired, cancelled or superseded")

    async def journal_guard(self, conn):
        current = await conn.scalar(
            text(
                f"""SELECT task_id FROM {self.task_table} WHERE run_id=:run AND task_id=:task
                AND owner=:member AND status='running'
                AND snapshot->'_native_worker'->>'token'=:token
                AND (snapshot->'_native_worker'->>'expires')::double precision>extract(epoch from clock_timestamp())"""
            ),
            {
                "run": self.team.lease.run_id,
                "task": self.task_id,
                "member": self.member_id,
                "token": self.token,
            },
        )
        if current is None:
            raise FenceLost("worker cannot commit a model or tool receipt")

    async def consume_inputs(self):
        async with self.team.transport.store.pool.acquire() as db, db.transaction():
            await self.guard(db)
        for event in await self.team.pending(self.member_id):

            async def apply(db, item):
                if item.is_control:
                    await db.execute(
                        "UPDATE research_team_tasks SET status='cancelled',version=version+1 WHERE run_id=$1 AND task_id=$2 AND status='running'",
                        self.team.lease.run_id,
                        self.task_id,
                    )
                elif item.type == "message":
                    await db.execute(
                        """UPDATE research_team_tasks SET snapshot=jsonb_set(snapshot,'{_team_feedback}',
                           coalesce(snapshot->'_team_feedback','[]'::jsonb) || $3::jsonb)
                           WHERE run_id=$1 AND task_id=$2""",
                        self.team.lease.run_id,
                        self.task_id,
                        json.dumps([str(item.payload.get("content", ""))]),
                    )

            await self.team.apply_input(self.member_id, event.event_id, apply)
            if event.is_control:
                raise TaskStopped(event.type)
        async with self.team.transport.store.pool.acquire() as db:
            feedback = await db.fetchval(
                "SELECT snapshot->'_team_feedback' FROM research_team_tasks WHERE run_id=$1 AND task_id=$2",
                self.team.lease.run_id,
                self.task_id,
            )
        self.session.snapshot.feedback_by_task[self.task_id] = (
            json.loads(feedback) if feedback else []
        )

    def say_tool(self):
        identity = MemberIdentity(
            run_id=self.team.lease.run_id, member_id=self.member_id, name=self.member_id
        )

        async def say(input, context, progress):
            receipt = await self.team.say(
                identity,
                "say:" + self.task_id + ":" + context.tool_call_id,
                input.to,
                input.content,
            )
            return ToolResult(output=receipt)

        return build_tool(
            name="TeamSay",
            input_schema=_Say,
            description="Send durable research findings or questions to lead or a teammate.",
            call=say,
            origin=ToolOrigin.SYSTEM,
            effect=ToolEffect.COORDINATION_WRITE,
            execution_zone=ToolExecutionZone.HOST_CONTROL,
            concurrency_safe=True,
        )

    async def watch(self):
        try:
            while True:
                await asyncio.sleep(min(self.host.ttl / 3, 0.2))
                row = next(
                    row
                    for row in await self.team.service.tasks(self.team.lease.run_id)
                    if row["task_id"] == self.task_id
                )
                if row["status"] == "completed":
                    return
                if row["status"] == "cancelled":
                    raise TaskStopped("cancelled")
                await self.consume_inputs()
                async with (
                    self.team.transport.store.pool.acquire() as db,
                    db.transaction(),
                ):
                    await self.guard(db)
                    await db.execute(
                        """UPDATE research_team_tasks SET snapshot=jsonb_set(snapshot,'{_native_worker,expires}',
                           to_jsonb(extract(epoch from clock_timestamp())+$3::double precision))
                           WHERE run_id=$1 AND task_id=$2""",
                        self.team.lease.run_id,
                        self.task_id,
                        self.host.ttl,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - stop the executor on lost authority
            self.failure = exc
            self.current.cancel()

    async def run(self):
        snapshot = await self.claim()
        if snapshot is None:
            return await self.host.artifact(self.task_id)
        self.current = asyncio.current_task()
        store = copy.copy(self.host.recovery.store)
        store.commit_guard = self.journal_guard
        self.session = RecoverySession(
            store,
            self.team.lease,
            self.host.recovery.snapshot.model_copy(deep=True),
            failpoint=self.host.failpoint,
            model_accounting=self.host.recovery.model_accounting,
        )
        researcher = copy.copy(self.host.researcher)
        original = researcher.models
        researcher.models = ResearchModels(
            original.factory,
            model_for=original.model_for,
            context_chars=original.context_chars,
            recovery=self.session,
        )
        quality = NativeResearchQuality(researcher.models, researcher.config_provider)
        researcher.quality = quality
        await self.team.task_session(self.task_id, self.member_id)
        watcher = asyncio.create_task(self.watch())
        try:
            with self.session.scope("team-worker", 0), self.session.task(self.task_id):
                await self.consume_inputs()
                assignment = ResearchAssignment(
                    task_id=self.task_id,
                    research_topic=snapshot["research_topic"],
                    requirement_ids=snapshot["requirement_ids"],
                )
                if snapshot.get("_native_handoff"):
                    outcome = ResearchHandoff.model_validate(
                        snapshot["_native_handoff"]
                    )
                else:
                    outcome = await researcher.run(
                        assignment,
                        snapshot["coverage_contract"],
                        snapshot.get("pending_update_instructions", []),
                        coordination_tools=[self.say_tool()],
                        worker_middlewares=[_Inputs(self)],
                    )
                    async with (
                        self.team.transport.store.pool.acquire() as db,
                        db.transaction(),
                    ):
                        await self.guard(db)
                        await db.execute(
                            "UPDATE research_team_tasks SET snapshot=snapshot || $3::jsonb WHERE run_id=$1 AND task_id=$2",
                            self.team.lease.run_id,
                            self.task_id,
                            json.dumps(
                                {"_native_handoff": outcome.model_dump(mode="json")}
                            ),
                        )
                    await self.host.hit("handoff_prepared")
                await self.team.admit_handoff(
                    "handoff:" + self.task_id,
                    self.member_id,
                    outcome,
                    snapshot["coverage_contract"],
                    _HandoffGate(quality, researcher.config_provider),
                )
                await self.host.hit("handoff_committed")
                # Stop control polling after terminal commit, then project the session.
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                return await self.host.artifact(self.task_id)
        except asyncio.CancelledError:
            if self.failure:
                raise self.failure
            raise
        except Exception as exc:
            async with self.team.transport.store.pool.acquire() as db, db.transaction():
                await self.parent_guard(db)
                await db.execute(
                    """UPDATE research_team_tasks SET snapshot=snapshot || $3::jsonb
                       WHERE run_id=$1 AND task_id=$2 AND status='running'
                       AND snapshot->'_native_worker'->>'token'=$4""",
                    self.team.lease.run_id,
                    self.task_id,
                    json.dumps({"_worker_error": type(exc).__name__}),
                    self.token,
                )
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            # Do not release the leader's M6 run lease; expire only this attempt.
            async with self.team.transport.store.pool.acquire() as db, db.transaction():
                await self.parent_guard(db)
                await db.execute(
                    """UPDATE research_team_tasks SET snapshot=jsonb_set(snapshot,'{_native_worker,expires}','0')
                       WHERE run_id=$1 AND task_id=$2 AND snapshot->'_native_worker'->>'token'=$3""",
                    self.team.lease.run_id,
                    self.task_id,
                    self.token,
                )

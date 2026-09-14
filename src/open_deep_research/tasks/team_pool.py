"""Run-owned event-driven persistent research members."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import fields

from open_deep_research.configuration import Configuration
from open_deep_research.tasks.executor import run_task_with_control
from open_deep_research.tasks.lease import LeaderLeaseManager
from open_deep_research.tasks.recovery import CheckpointManager
from open_deep_research.tasks.registry import TaskRecord, TaskStatus
from open_deep_research.tasks.team_members import MemberSessions
from open_deep_research.tasks.team_protocol import MemberIdentity, member_identity
from open_deep_research.tasks.team_runtime import team_runtime


class ResearchTeamPool:
    """Keep member identity and sessions while isolating every research task."""

    def __init__(self, *, config, registry, execute_research):
        """Bind a pool to the current run's existing lease epoch."""
        self.config = config
        self.configurable = Configuration.from_runnable_config(config)
        self.registry = registry
        self.execute_research = execute_research
        metadata = config.get("metadata", {})
        self.run_id = str(metadata.get("run_id", "default"))
        self.fence_token = metadata.get("run_fence_token")
        self.lease = LeaderLeaseManager(
            runs_dir=self.configurable.runs_dir, run_id=self.run_id,
            owner_id=metadata.get("run_lease_owner_id"),
            lease_seconds=self.configurable.leader_lease_seconds,
        )
        self.lease.fence_token = self.fence_token
        self.members: dict[str, asyncio.Task] = {}
        self.active: dict[str, asyncio.Task] = {}
        self._stopping = False
        self._started = False
        self.capacity = asyncio.Semaphore(self.configurable.max_concurrent_research_units)

    def check_health(self):
        """Surface failed member loops to the owning Supervisor."""
        for task in self.members.values():
            if task.done() and not task.cancelled() and task.exception():
                error = task.exception()
                logging.getLogger(__name__).error("Research member loop failed", exc_info=(type(error), error, error.__traceback__))
                self.service.transport.publishers.pop(self.run_id, None)
                raise RuntimeError("research_member_runtime_failed") from error

    async def start(self):
        """Require the QueryEngine lease and restore members from PostgreSQL."""
        if self._started:
            return
        if self.fence_token is None or not await self.lease.is_owner():
            raise RuntimeError("team_requires_live_query_engine_lease")
        self.service = await team_runtime.start()
        self.service.transport.leases[self.run_id] = self.lease
        from open_deep_research.events.public import event_publisher_from_config
        self.service.transport.publishers[self.run_id] = event_publisher_from_config(self.config)
        self.sessions = MemberSessions(self.service.store)
        self._started = True
        async def advance_epoch():
            async with self.service.store.pool.acquire() as db:
                await db.execute(
                    "UPDATE research_teams SET fence_token=$2 WHERE run_id=$1 AND fence_token<$2",
                    self.run_id, self.fence_token,
                )
        loop = asyncio.get_running_loop()
        await self.lease.run_fenced(self.fence_token, lambda: asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(advance_epoch(), timeout=8), loop,
        ).result())
        await self.refresh_members()

    async def refresh_members(self):
        """Start a single asyncio loop for each persisted non-lead member."""
        async with self.service.store.pool.acquire() as db:
            rows = await db.fetch(
                "SELECT member_id,name FROM research_team_members WHERE run_id=$1 AND member_id<>'lead' AND status<>'closed'",
                self.run_id,
            )
        for row in rows:
            if row["member_id"] not in self.members:
                identity = MemberIdentity(run_id=self.run_id, member_id=row["member_id"], name=row["name"])
                self.members[identity.member_id] = asyncio.create_task(self._member_loop(identity))
                self.service.transport.signal(self.run_id, identity.member_id).set()

    async def _member_loop(self, identity):
        token = member_identity.set(identity)
        signal = self.service.transport.signal(self.run_id, identity.member_id)
        try:
            while not self._stopping:
                signal.clear()
                events = await self.service.store.pending(self.run_id, identity.member_id)
                for event in events:
                    if event.type in {"cancel_request", "task_stop"}:
                        record = self.registry.get(event.payload.get("task_id", ""))
                        if record and record.run_id == self.run_id:
                            record.cancelled.set()
                    elif event.type == "shutdown_request":
                        return
                    await self.sessions.accept(self.run_id, identity.member_id, event)
                running = self.active.get(identity.member_id)
                if running is not None and running.done():
                    await running
                    self.active.pop(identity.member_id)
                    running = None
                if running is None:
                    tasks = await self.service.tasks(self.run_id)
                    owned = next((task for task in tasks if task["owner"] == identity.member_id
                                  and task["status"] in {"pending", "running", "waiting_for_confirmation"}), None)
                    if owned is None:
                        for task in tasks:
                            if task["owner"] is not None or task["status"] != "pending":
                                continue
                            admitted = {item["task_id"] for item in tasks if item["status"] == "completed"
                                        and item["admission_status"] in {"accepted", "accepted_with_caveats"}}
                            if any(blocker not in admitted for blocker in task["blocked_by"]):
                                continue
                            try:
                                await self.service.command(
                                    identity, f"auto-claim:{task['task_id']}:{identity.member_id}:{task['version']}",
                                    "task_claim", {"task_id": task["task_id"]}, fence_token=self.fence_token,
                                )
                            except ValueError:
                                continue
                            signal.set()
                            break
                    else:
                        running = asyncio.create_task(self._research_slot(identity, owned))
                        running.add_done_callback(lambda _: signal.set())
                        self.active[identity.member_id] = running
                await signal.wait()
        finally:
            member_identity.reset(token)

    async def _research_slot(self, identity, snapshot):
        async with self.capacity:
            current = next((task for task in await self.service.tasks(self.run_id)
                            if task["task_id"] == snapshot["task_id"]), None)
            if current is None or current["status"] not in {"pending", "running", "waiting_for_confirmation"}:
                return
            await self._research(identity, current)

    async def _research(self, identity, snapshot):
        data = {key: value for key, value in snapshot.items()
                if key in {field.name for field in fields(TaskRecord)}
                and key not in {"status", "phase", "background_task", "control_queue", "cancelled"}}
        record = TaskRecord(**data)
        record.assigned_teammate_id = identity.member_id
        record.status = TaskStatus.PENDING
        session = await self.sessions.load(self.run_id, identity.member_id)
        record.memory_context = json.dumps({
            "notice": "Team context is guidance, not admitted evidence. Verify sources for this task.",
            "results": session.get("results", {}),
            "messages": session.get("messages", [])[-20:],
        }, ensure_ascii=False)
        self.registry.restore(record)
        record.background_task = asyncio.current_task()
        try:
            await run_task_with_control(
                record, self.config, self.registry, self.execute_research,
                checkpoint_manager=CheckpointManager(runs_dir=self.configurable.runs_dir, run_id=self.run_id),
                runs_dir=self.configurable.runs_dir, run_id=self.run_id,
                event_log_enabled=self.configurable.event_log_enabled, fence_token=self.fence_token,
            )
        finally:
            from open_deep_research.tasks.team_bridge import checkpoint_callbacks
            checkpoint_callbacks.pop((self.run_id, record.task_id), None)
        if record.result:
            await self.sessions.remember_result(
                self.run_id, identity.member_id, record.task_id,
                str(record.result.get("compressed_research", "")), record.result_artifact_path or "",
            )

    async def shutdown(self, timeout_seconds=10):
        """Stop this run's tasks before QueryEngine releases its lease."""
        self._stopping = True
        if self._started:
            self.service.transport.publishers.pop(self.run_id, None)
        tasks = [*self.members.values(), *self.active.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.members.clear()
        self.active.clear()

    async def send_control(self, *, task_id, message_type, payload, priority=0):
        """Route a control request to the task's persisted owner."""
        del priority
        from open_deep_research.tasks.team_inbox import TeamInbox
        tasks = await self.service.tasks(self.run_id)
        target = next((task for task in tasks if task["task_id"] == task_id), None)
        if target is None or not target["owner"]:
            raise ValueError("task_has_no_owner")
        await TeamInbox(self.run_id).send(
            recipient=target["owner"], sender="lead", message_type=message_type,
            payload={"task_id": task_id, **payload},
        )

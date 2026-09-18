"""Persistent Docker member loop over the shared SQL task board."""

import asyncio
import copy
import json
import logging
import random
import time
from contextvars import ContextVar
from uuid import uuid4

import asyncpg

from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.agentscope_runtime.team_worker import (
    TeamWorkers,
    TaskStopped,
    WorkerBusy,
)
from open_deep_research.tasks.team_protocol import MemberIdentity

member_task = ContextVar("team_member_task", default="supervisor")


class TeamsWorkers(TeamWorkers):
    """Lead schedules members; task dispatch only waits for durable outcomes."""

    async def prepare(
        self,
        assignment,
        contract,
        feedback=(),
        *,
        blocked_by=(),
        owner=None,
        proposal_id=None,
        subject=None,
        active_form=None,
        metadata=None,
    ):
        from open_deep_research.configuration import Configuration
        from open_deep_research.tasks.state import TaskSnapshot

        cfg = Configuration.from_runnable_config(self.researcher.config_provider())
        await self.team.command(
            "task:" + assignment.task_id,
            "task_create",
            {
                "snapshot": TaskSnapshot(
                    task_id=assignment.task_id,
                    run_id=self.team.lease.run_id,
                    user_id=self.team.lease.user_id,
                    research_topic=assignment.research_topic,
                    display_title=subject or assignment.research_topic,
                    requirement_ids=assignment.requirement_ids,
                    coverage_contract=contract,
                    pending_update_instructions=list(feedback),
                ).model_dump(mode="json"),
                "max_tasks": cfg.max_researcher_iterations
                * cfg.max_concurrent_research_units,
                "single_task": contract.get("single_research_task", False),
                "blocked_by": list(blocked_by),
                "proposal_id": proposal_id,
                "owner": owner,
                "active_form": active_form,
                "metadata": metadata or {},
            },
        )
        await self.recovery.store.register_task(self.team.lease, assignment.task_id)

    async def dispatch(self, assignment, contract, feedback=()):
        while not self.closed:
            row = next(
                r
                for r in await self.team.service.tasks(self.team.lease.run_id)
                if r["task_id"] == assignment.task_id
            )
            if row["status"] == "completed":
                return await self.artifact(assignment.task_id)
            if row["status"] in {"failed", "cancelled", "timed_out"}:
                raise TaskStopped(row["status"])
            if row.get("_worker_error"):
                raise RuntimeError(row["_worker_error"])
            await self.ensure_members()
            await asyncio.sleep(0.5)
        raise TaskStopped("team_closed")

    async def ensure_members(self):
        if self.launcher is None:
            return
        async with self.team.transport.store.pool.acquire() as db:
            rows = await db.fetch(
                """SELECT member_id,extract(epoch from lease_expires) AS expires FROM research_team_members
                   WHERE run_id=$1 AND member_id<>'lead' AND status NOT IN ('closed','failed')""",
                self.team.lease.run_id,
            )
        for row in rows:
            await self.launcher.ensure_started(
                self.team.lease,
                "member:" + row["member_id"],
                not_before=float(row["expires"] or 0),
            )

    async def run_member(self, member_id):
        return await MemberLoop(self, member_id).run()

    async def wait_for_updates(self):
        """Wait for actionable state, not a timer that spends another model turn."""
        from open_deep_research.budgets import DeadlineExceeded

        await self.ensure_members()
        budget = await self.recovery.store.budget(self.team.lease.run_id, self.team.lease.user_id)
        deadline = budget["deadline"]
        rows = await self.team.service.tasks(self.team.lease.run_id)
        initial = [(row["task_id"], row["version"]) for row in rows]
        while not self.closed:
            if deadline is not None and time.time() >= deadline:
                raise DeadlineExceeded("run deadline exceeded while waiting for team updates")
            if await self.team.pending("lead"):
                return
            if all(row["status"] in {"completed", "failed", "cancelled", "timed_out"} for row in rows):
                return
            failed_blockers = {row["task_id"] for row in rows
                if row["status"] in {"failed", "cancelled", "timed_out"}
                or (row["status"] == "completed" and row.get("admission_status") == "rejected")}
            active = any(row["status"] in {"running", "waiting_for_confirmation"} for row in rows)
            if not active and any(failed_blockers.intersection(row.get("unresolvedBlockedBy", []))
                                  for row in rows if row["status"] == "pending"):
                return
            if any(row.get("phase") == "awaiting_plan_review" for row in rows):
                return
            if [(row["task_id"], row["version"]) for row in rows] != initial:
                return
            await asyncio.sleep(0.5)
            rows = await self.team.service.tasks(self.team.lease.run_id)
        raise TaskStopped("team_closed")

    async def aclose(self):
        await super().aclose()
        if self.launcher is not None:
            # Controller confirmed cleanup; cancellation already revoked business authority.
            async with self.team.transport.store.pool.acquire() as db:
                await db.execute(
                    """UPDATE research_team_members m SET status='closed',
                    session=session || '{"exit_reason":"user_cancelled_cleanup"}'::jsonb
                    WHERE m.run_id=$1 AND m.status='stopping' AND EXISTS (
                        SELECT 1 FROM research_teams t WHERE t.run_id=m.run_id AND t.status='cancelled')""",
                    self.team.lease.run_id,
                )

    async def finish_team(self):
        """Called only after Lead explicitly requests completion."""
        await self.consume_leader_inputs()
        async with self.team.transport.store.pool.acquire() as db:
            if await db.fetchval(
                "SELECT 1 FROM research_team_proposals WHERE run_id=$1 AND status='pending' LIMIT 1",
                self.team.lease.run_id,
            ):
                raise ValueError("review_pending_task_proposals_before_completion")
            if await db.fetchval(
                """SELECT 1 FROM research_coordination_events e
                WHERE run_id=$1 AND event->>'type' IN ('message','send_message')
                  AND coalesce(event->'payload'->'message'->>'type','text') NOT IN ('shutdown_request','shutdown_response')
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(e.event->'recipients') dest
                      WHERE NOT EXISTS (SELECT 1 FROM research_coordination_receipts r
                          WHERE r.event_id=e.event_id AND r.recipient=dest.value AND r.applied)) LIMIT 1""",
                self.team.lease.run_id,
            ):
                raise ValueError(
                    "wait_for_team_messages_to_be_applied_before_completion"
                )
        for member in await self.team.members():
            if member["member_id"] != "lead" and member["status"] not in {
                "closed",
                "failed",
            }:
                await self.team.send_message(
                    self.team.leader,
                    "finish:" + member["member_id"],
                    member["member_id"],
                    {"type": "shutdown_request", "reason": "Lead 已完成任务汇总"},
                )
        for _ in range(60):
            await self.consume_leader_inputs()
            members = await self.team.members()
            if all(
                m["member_id"] == "lead" or m["status"] in {"closed", "failed"}
                for m in members
            ):
                break
            await asyncio.sleep(0.5)
        else:
            if self.launcher:
                await self.launcher.aclose()
            async with self.team.transport.store.pool.acquire() as db, db.transaction():
                await self.team.transport.guard(db)
                await db.execute(
                    "UPDATE research_team_members SET status='closed',execution_token=NULL,lease_expires=NULL,session=session || '{\"exit_reason\":\"shutdown_timeout_forced_cleanup\"}'::jsonb WHERE run_id=$1 AND member_id<>'lead'",
                    self.team.lease.run_id,
                )
        await self.consume_leader_inputs()
        await self.team.command("team-finished", "team_close", {})


class MemberLoop:
    def __init__(self, host, member_id):
        self.host = copy.copy(host)
        self.host.team = copy.copy(host.team)
        self.team = self.host.team
        self.team.transport = copy.copy(host.team.transport)
        self.parent_guard = host.team.transport.guard
        self.team.transport.guard = self.guard
        self.team.service = type(host.team.service)(self.team.transport)
        self.member_id, self.token = member_id, uuid4().hex
        self.host.member_token = self.token
        self.identity = MemberIdentity(
            run_id=self.team.lease.run_id, member_id=member_id, name=member_id
        )
        self.active = None

    async def guard(self, db):
        await self.parent_guard(db)
        if not await db.fetchval(
            """SELECT 1 FROM research_team_members WHERE run_id=$1 AND member_id=$2
               AND execution_token=$3 AND lease_expires>clock_timestamp() AND status NOT IN ('closed','failed') FOR SHARE""",
            self.team.lease.run_id,
            self.member_id,
            self.token,
        ):
            raise FenceLost("member executor lease lost")

    async def acquire(self):
        async with self.team.transport.store.pool.acquire() as db, db.transaction():
            await self.parent_guard(db)
            row = await db.fetchrow(
                """UPDATE research_team_members SET execution_token=$3,execution_epoch=execution_epoch+1,
                   lease_expires=clock_timestamp()+interval '30 seconds'
                   WHERE run_id=$1 AND member_id=$2 AND status NOT IN ('closed','failed')
                     AND (lease_expires IS NULL OR lease_expires<=clock_timestamp()) RETURNING execution_epoch""",
                self.team.lease.run_id,
                self.member_id,
                self.token,
            )
            if not row:
                raise FenceLost("member already has a live executor")
            await db.execute(
                """UPDATE research_team_plans SET status='superseded' WHERE run_id=$1 AND owner=$2 AND status='pending'""",
                self.team.lease.run_id,
                self.member_id,
            )
            await db.execute(
                """UPDATE research_team_tasks SET phase='planning',version=version+1
                   WHERE run_id=$1 AND owner=$2 AND status='running' AND phase='awaiting_plan_review'""",
                self.team.lease.run_id,
                self.member_id,
            )

    async def heartbeat(self):
        while True:
            await asyncio.sleep(10)
            async with self.team.transport.store.pool.acquire() as db, db.transaction():
                await self.parent_guard(db)
                updated = await db.fetchval(
                    """UPDATE research_team_members SET lease_expires=clock_timestamp()+interval '30 seconds'
                       WHERE run_id=$1 AND member_id=$2 AND execution_token=$3
                       AND lease_expires>clock_timestamp() RETURNING member_id""",
                    self.team.lease.run_id,
                    self.member_id,
                    self.token,
                )
                if not updated:
                    raise FenceLost("member heartbeat rejected")

    async def next_task(self):
        for attempt in range(5):
            tasks = await self.team.service.tasks(self.team.lease.run_id)
            active = next(
                (
                    t
                    for t in tasks
                    if t["owner"] == self.member_id and t["status"] == "running"
                ),
                None,
            )
            if active:
                return active
            ready = [
                t
                for t in tasks
                if t["status"] == "pending"
                and t["owner"] in {None, self.member_id}
                and not t["unresolvedBlockedBy"]
            ]
            ready.sort(key=lambda t: t["owner"] != self.member_id)
            if not ready:
                return None
            row = ready[0]
            try:
                result = await self.team.command(
                    "claim:" + uuid4().hex,
                    "task_claim",
                    {
                        "task_id": row["task_id"],
                        "version": row["version"],
                        "execution_token": self.token,
                    },
                    member=self.identity,
                )
                if result["claimed"]:
                    return row
                if result["reason"] != "version_conflict":
                    return None
            except (
                asyncpg.UniqueViolationError,
                asyncpg.SerializationError,
                asyncpg.DeadlockDetectedError,
            ):
                pass
            await asyncio.sleep(random.uniform(0, min(1, 0.05 * 2**attempt)))
        return None

    async def run(self):
        await self.acquire()
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            while not self.host.closed:
                if heartbeat.done():
                    await heartbeat
                member = next(
                    m
                    for m in await self.team.members()
                    if m["member_id"] == self.member_id
                )
                if member["status"] in {"stopping", "closed"}:
                    if member["status"] == "stopping":
                        async with self.team.transport.store.pool.acquire() as db:
                            request_id = await db.fetchval(
                                "SELECT session->>'shutdown_request' FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                                self.team.lease.run_id,
                                self.member_id,
                            )
                        if request_id:
                            pending = await self.team.pending(self.member_id)
                            if not any(
                                event.event_id == request_id for event in pending
                            ):
                                await asyncio.sleep(0.5)
                                continue
                            for event in pending:

                                async def checkpoint(db, item):
                                    await db.execute(
                                        "UPDATE research_team_members SET session=session || $3::jsonb WHERE run_id=$1 AND member_id=$2",
                                        self.team.lease.run_id,
                                        self.member_id,
                                        json.dumps(
                                            {
                                                "exit_reason": "graceful_shutdown",
                                                "shutdown_applied": request_id,
                                            }
                                        ),
                                    )

                                await self.team.apply_input(
                                    self.member_id, event.event_id, checkpoint
                                )
                            await self.team.send_message(
                                self.identity,
                                "shutdown-ack:" + request_id,
                                "lead",
                                {
                                    "type": "shutdown_response",
                                    "request_id": request_id,
                                    "approve": True,
                                    "reason": "任务已保存，成员退出",
                                },
                            )
                    break
                row = await self.next_task()
                if row is None:
                    # Persist idle discussion without a busy model polling loop.
                    for event in await self.team.pending(self.member_id):
                        if (
                            event.type in {"message", "send_message"}
                            and not event.is_control
                        ):
                            from open_deep_research.agentscope_runtime.teams_discussion import (
                                discuss,
                            )

                            await discuss(self, event)
                            continue
                        from open_deep_research.tasks.team_messages import message_text

                        async def apply(db, item):
                            if item.type in {"message", "send_message"}:
                                await db.execute(
                                    """UPDATE research_team_members SET session=jsonb_set(session,'{discussion}',
                                       coalesce(session->'discussion','[]'::jsonb) || $3::jsonb)
                                       WHERE run_id=$1 AND member_id=$2""",
                                    self.team.lease.run_id,
                                    self.member_id,
                                    json.dumps(
                                        [f"{item.sender}: {message_text(item)}"]
                                    ),
                                )

                        await self.team.apply_input(
                            self.member_id, event.event_id, apply
                        )
                    await asyncio.sleep(0.5)
                    continue
                task_id = row["task_id"]
                token = member_task.set(task_id)
                try:
                    self.active = asyncio.create_task(self.host.execute(task_id))
                    done, _ = await asyncio.wait(
                        {heartbeat, self.active}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if heartbeat in done:
                        await heartbeat
                    await self.active
                except TaskStopped:
                    pass
                except WorkerBusy:
                    # The member lease may expire just before the old task lease.
                    await asyncio.sleep(0.5)
                    continue
                except Exception as exc:
                    if isinstance(exc, FenceLost):
                        raise
                    logging.getLogger(__name__).exception(
                        "Team task %s failed", task_id
                    )
                    async with (
                        self.team.transport.store.pool.acquire() as db,
                        db.transaction(),
                    ):
                        await self.guard(db)
                        await db.execute(
                            """UPDATE research_team_tasks SET status='failed',phase=NULL,version=version+1,
                               snapshot=snapshot || $3::jsonb WHERE run_id=$1 AND task_id=$2 AND status='running'""",
                            self.team.lease.run_id,
                            task_id,
                            json.dumps({"error_message": type(exc).__name__}),
                        )
                finally:
                    if self.active and not self.active.done():
                        self.active.cancel()
                        await asyncio.gather(self.active, return_exceptions=True)
                    member_task.reset(token)
                async with (
                    self.team.transport.store.pool.acquire() as db,
                    db.transaction(),
                ):
                    await self.guard(db)
                    await db.execute(
                        """UPDATE research_team_members SET current_task_id=NULL,
                           status=CASE WHEN status='stopping' THEN status ELSE 'idle' END
                           WHERE run_id=$1 AND member_id=$2""",
                        self.team.lease.run_id,
                        self.member_id,
                    )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            async with self.team.transport.store.pool.acquire() as db:
                await db.execute(
                    """UPDATE research_team_members SET lease_expires=NULL,execution_token=NULL,
                       status=CASE WHEN status='stopping' THEN 'closed' ELSE status END
                       WHERE run_id=$1 AND member_id=$2 AND execution_token=$3""",
                    self.team.lease.run_id,
                    self.member_id,
                    self.token,
                )

"""Native team/session projection over the existing domain coordination ledger.

The application supplies an authenticated M6 lease and an asyncpg pool pointing
at the existing coordination tables. No legacy agent loop is imported.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import NAMESPACE_URL, uuid5

from agentscope.app import SubAgentTemplate
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    SessionConfig,
    TeamData,
    TeamMember,
    TeamOrigin,
    TeamRecord,
)
from agentscope.state import AgentState, Task, TaskContext

from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.tasks.team_protocol import MemberIdentity, TeamEvent
from open_deep_research.tasks.team_service import TeamService
from open_deep_research.tasks.team_store import TeamStore

logger = logging.getLogger(__name__)


def native_task(snapshot: dict) -> Task:
    """Project domain status without marking rejected/cancelled work complete."""
    status = snapshot["status"]
    accepted = snapshot.get("admission_status") in {"accepted", "accepted_with_caveats"}
    state = (
        "completed"
        if status == "completed" and accepted
        else "in_progress"
        if status in {"running", "waiting_for_confirmation"}
        else "pending"
    )
    return Task(
        id=snapshot["task_id"],
        subject=snapshot.get("display_title") or snapshot["research_topic"],
        description=snapshot["research_topic"],
        state=state,
        owner=snapshot.get("owner"),
        blocked_by=snapshot.get("blocked_by", []),
        metadata={
            "run_id": snapshot["run_id"],
            "domain_status": status,
            "admission_status": snapshot.get("admission_status", "pending"),
            "requirement_ids": snapshot.get("requirement_ids", []),
        },
    )


class FencedTeamTransport:
    """Use the existing SQL transaction ledger; MessageBus only wakes readers.

    Unlike broker half messages, business mutation, event and recipient receipts
    commit together. Lost wakeups are recovered by reading pending SQL receipts.
    The runtime's MessageBus owns the configured remote RocketMQ connection.
    """

    def __init__(
        self, pool, lease, *, recovery_schema="agentscope_runtime", message_bus=None
    ):
        self.store = TeamStore(pool)
        self.lease = lease
        self.message_bus = message_bus
        schema = '"' + recovery_schema.replace('"', '""') + '"'
        self.run_table = schema + '."as_recovery_runs"'

    async def guard(self, db):
        lease = self.lease
        current = await db.fetchval(
            f"""UPDATE {self.run_table} SET revision=revision+1
                WHERE run_id=$1 AND user_id=$2 AND owner=$3 AND fence=$4
                  AND engine='agentscope' AND version=1
                  AND expires>extract(epoch from clock_timestamp())
                RETURNING fence""",
            lease.run_id,
            lease.user_id,
            lease.owner,
            lease.fence,
        )
        if current is None:
            raise FenceLost("expired or superseded team executor")
        # The existing team fence follows the run epoch; no new lease is added.
        await db.execute(
            "UPDATE research_teams SET fence_token=$2 WHERE run_id=$1",
            lease.run_id,
            lease.fence,
        )

    async def transact(self, event, mutate):
        if event.run_id != self.lease.run_id or event.fence_token != self.lease.fence:
            raise FenceLost("team event does not belong to this run lease")
        async with self.store.pool.acquire() as db, db.transaction():
            await self.guard(db)
        state, result = await self.store.prepare(event)
        if state == "ABORTED":
            raise ValueError("coordination operation aborted")
        if state != "COMMITTED":

            async def guarded(db):
                await self.guard(db)
                return await mutate(db)

            result = await self.store.commit(event, guarded, durable_recipients=True)
        if self.message_bus is not None:
            try:
                await self.message_bus.publish(
                    "team:" + event.run_id, {"event_id": event.event_id}
                )
            except Exception:  # noqa: BLE001 - SQL receipts remain recoverable
                logger.warning("Team event committed; wakeup delivery failed")
        return result


class NativeResearchTeam:
    """Project authorized domain members into public AgentScope storage APIs.

    Member AgentRecord definitions are reused within one run. Every task gets
    its own session, so prompts, evidence and AgentState never leak to the next
    task. Stable IDs make partial native provisioning repairable after restart.
    """

    def __init__(
        self,
        storage,
        transport: FencedTeamTransport,
        *,
        leader_agent_id,
        leader_session_id,
        template: SubAgentTemplate,
    ):
        self.storage, self.transport = storage, transport
        self.lease = transport.lease
        self.service = TeamService(transport)
        self.leader_agent_id, self.leader_session_id = (
            leader_agent_id,
            leader_session_id,
        )
        self.template = template.model_copy(deep=True)
        if (
            not template.override_leader_mode
            or template.extend_leader_permission_rules
            or template.extend_leader_working_directories
        ):
            raise ValueError(
                "research team templates require explicit isolated permissions"
            )
        self.lock = asyncio.Lock()
        self.team_id = self.identity("team")
        self.leader = MemberIdentity(
            run_id=self.lease.run_id, member_id="lead", name="lead", role="lead"
        )

    def identity(self, *parts):
        return uuid5(
            NAMESPACE_URL, json.dumps([self.lease.user_id, self.lease.run_id, *parts])
        ).hex

    async def command(self, operation_id, kind, payload, *, member=None):
        """Host-bound identity only; tool callers must not supply arbitrary roles."""
        return await self.service.command(
            member or self.leader,
            operation_id,
            kind,
            payload,
            fence_token=self.lease.fence,
        )

    async def _leader_session(self):
        session = await self.storage.get_session(
            self.lease.user_id, self.leader_agent_id, self.leader_session_id
        )
        if session is None or session.agent_id != self.leader_agent_id:
            raise PermissionError("leader session is not owned by this user and agent")
        if session.team_id not in {None, self.team_id}:
            raise ValueError("leader session belongs to another run team")
        return session

    async def create(self, name, description=""):
        await self._leader_session()
        await self.command("native-team:create", "team_create", {"name": name})
        # Bind leader/template intent in the existing member record before native I/O.
        binding = {
            "team_id": self.team_id,
            "agent_id": self.leader_agent_id,
            "session_id": self.leader_session_id,
            "description": description,
            "template": self.template.model_dump(mode="json"),
        }
        async with self.transport.store.pool.acquire() as db, db.transaction():
            await self.transport.guard(db)
            current = json.loads(
                await db.fetchval(
                    "SELECT session FROM research_team_members WHERE run_id=$1 AND member_id='lead'",
                    self.lease.run_id,
                )
            )
            if current and any(
                current.get(key) != value for key, value in binding.items()
            ):
                raise ValueError("team leader or frozen template changed")
            await db.execute(
                "UPDATE research_team_members SET session=$2::jsonb WHERE run_id=$1 AND member_id='lead'",
                self.lease.run_id,
                json.dumps(binding),
            )
        await self.reconcile()
        return self.team_id

    async def add_member(self, operation_id, name, purpose, *, max_members=5):
        result = await self.command(
            operation_id,
            "member_spawn",
            {"name": name, "purpose": purpose, "max_members": max_members},
        )
        await self.reconcile()
        return result["member_id"]

    async def reconcile(self):
        """Repair deterministic native projections without resetting session state."""
        async with self.lock:
            leader_session = await self._leader_session()
            async with self.transport.store.pool.acquire() as db, db.transaction():
                await self.transport.guard(db)
                team = await db.fetchrow(
                    "SELECT * FROM research_teams WHERE run_id=$1", self.lease.run_id
                )
                rows = await db.fetch(
                    "SELECT * FROM research_team_members WHERE run_id=$1 ORDER BY member_id",
                    self.lease.run_id,
                )
            if team is None or team["status"] != "active":
                raise ValueError("team is not active")
            binding = next(
                json.loads(row["session"]) for row in rows if row["member_id"] == "lead"
            )
            if binding.get("session_id") != self.leader_session_id or binding.get(
                "template"
            ) != self.template.model_dump(mode="json"):
                raise ValueError("team binding or template mismatch")
            members = []
            for row in rows:
                if row["member_id"] == "lead" or row["status"] == "closed":
                    continue
                agent_id = self.identity("member", row["member_id"])
                record = await self.storage.get_agent(self.lease.user_id, agent_id)
                if record is None:
                    await self.storage.upsert_agent(
                        self.lease.user_id,
                        AgentRecord(
                            id=agent_id,
                            user_id=self.lease.user_id,
                            source="team",
                            data=AgentData(
                                id=agent_id,
                                name=row["name"],
                                system_prompt=self.template.system_prompt_template.format(
                                    team_name=team["name"],
                                    team_description=binding["description"],
                                    member_name=row["name"],
                                    member_description=row["purpose"],
                                    leader_name="lead",
                                ),
                                context_config=self.template.context_config.model_copy(
                                    deep=True
                                ),
                                react_config=self.template.react_config.model_copy(
                                    deep=True
                                ),
                            ),
                        ),
                    )
                session_id = json.loads(row["session"]).get(
                    "session_id", self.identity("idle", row["member_id"])
                )
                await self._ensure_session(
                    agent_id, session_id, leader_session.config, None
                )
                members.append(
                    TeamMember(
                        owner_id=self.lease.user_id,
                        agent_id=agent_id,
                        session_id=session_id,
                        role="created",
                    )
                )
            await self.storage.upsert_team(
                self.lease.user_id,
                TeamRecord(
                    id=self.team_id,
                    user_id=self.lease.user_id,
                    session_id=self.leader_session_id,
                    leader_agent_id=self.leader_agent_id,
                    data=TeamData(
                        name=team["name"],
                        description=binding["description"],
                        members=members,
                    ),
                ),
            )
            await self.storage.set_session_team_id(
                self.lease.user_id, self.leader_session_id, self.team_id
            )
            async with self.transport.store.pool.acquire() as db, db.transaction():
                await self.transport.guard(db)
            return members

    async def _ensure_session(self, agent_id, session_id, config, task):
        session = await self.storage.get_session(
            self.lease.user_id, agent_id, session_id
        )
        if session is None:
            state = AgentState(
                permission_context=self.template.permission_context.model_copy(
                    deep=True
                ),
                tasks_context=TaskContext(tasks=[task] if task else []),
            )
            session = await self.storage.upsert_session(
                user_id=self.lease.user_id,
                agent_id=agent_id,
                session_id=session_id,
                config=SessionConfig(**config.model_dump()),
                state=state,
                origin=TeamOrigin(),
            )
        await self.storage.set_session_team_id(
            self.lease.user_id, session_id, self.team_id
        )
        return session

    async def task_session(self, task_id, member_id):
        """Open a task-isolated native session only after domain claim succeeds."""
        leader = await self._leader_session()
        tasks = await self.service.tasks(self.lease.run_id)
        snapshot = next((task for task in tasks if task["task_id"] == task_id), None)
        if snapshot is None or snapshot["owner"] != member_id:
            raise PermissionError("task has not been claimed by this member")
        if snapshot["status"] not in {"pending", "running", "waiting_for_confirmation"}:
            raise ValueError("task is terminal")
        session_id, agent_id = (
            self.identity("task", task_id),
            self.identity("member", member_id),
        )
        async with self.transport.store.pool.acquire() as db, db.transaction():
            await self.transport.guard(db)
            await db.execute(
                "UPDATE research_team_members SET session=$3::jsonb WHERE run_id=$1 AND member_id=$2",
                self.lease.run_id,
                member_id,
                json.dumps({"session_id": session_id, "task_id": task_id}),
            )
        await self.reconcile()
        session = await self._ensure_session(
            agent_id, session_id, leader.config, native_task(snapshot)
        )
        # Reconcile may have created the task session before the task projection.
        if not session.state.tasks_context.tasks:
            session.state.tasks_context.tasks = [native_task(snapshot)]
            await self.storage.update_session_state(
                self.lease.user_id, agent_id, session_id, session.state
            )
        return session

    async def pending(self, member_id):
        """Read control-first durable input; no ack-on-read or legacy pool lookup."""
        async with self.transport.store.pool.acquire() as db, db.transaction():
            await self.transport.guard(db)
            member = await db.fetchval(
                "SELECT member_id FROM research_team_members WHERE run_id=$1 AND member_id=$2 AND status<>'closed'",
                self.lease.run_id,
                member_id,
            )
            if not member:
                raise PermissionError("unknown team member")
        return await self.transport.store.pending(self.lease.run_id, member_id)

    async def apply_input(self, member_id, event_id, mutate):
        """Apply a domain SQL update and its receipt in one fenced transaction.

        ``mutate`` must contain only SQL work on the supplied connection. Native
        session effects instead belong in the M6 operation journal before ACK.
        """
        async with self.transport.store.pool.acquire() as db, db.transaction():
            await self.transport.guard(db)
            event = await db.fetchval(
                """UPDATE research_coordination_receipts r SET applied=true
                   FROM research_coordination_events e WHERE r.event_id=e.event_id
                     AND e.run_id=$1 AND r.recipient=$2 AND r.event_id=$3 AND NOT r.applied
                   RETURNING e.event""",
                self.lease.run_id,
                member_id,
                event_id,
            )
            if event is not None:
                await mutate(db, TeamEvent.model_validate_json(event))
                return True
            return False

    async def say(self, member: MemberIdentity, command_id, to, content):
        return await self.command(
            command_id, "message", {"to": to, "content": content}, member=member
        )

    async def admit_handoff(self, command_id, member_id, outcome, contract, quality):
        """Commit only assignment-matching, quality-assessed research results."""
        from open_deep_research.agentscope_runtime.recovery_store import digest

        event = TeamEvent(
            operation_id=command_id,
            run_id=self.lease.run_id,
            sender=member_id,
            recipients=["lead"],
            type="task_handoff",
            payload={
                "task_id": outcome.task_id,
                "digest": digest(outcome.model_dump(mode="json")),
            },
            fence_token=self.lease.fence,
        )
        previous = await self.transport.store.prepared_event(
            self.lease.run_id, command_id
        )
        if previous is not None:
            state, result = await self.transport.store.prepare(event)
            if state == "COMMITTED":
                # Even replayed results must cross the current ownership fence.
                async def unused(db):
                    raise AssertionError("committed handoff must not execute")

                return await self.transport.transact(event, unused)
        tasks = await self.service.tasks(self.lease.run_id)
        snapshot = next(
            (row for row in tasks if row["task_id"] == outcome.task_id), None
        )
        if snapshot is None or snapshot["owner"] != member_id:
            raise PermissionError("handoff owner mismatch")
        if (
            set(outcome.requirement_ids) != set(snapshot["requirement_ids"])
            or outcome.research_topic != snapshot["research_topic"]
        ):
            raise ValueError("handoff differs from delegated requirements or topic")
        if snapshot["status"] not in {"pending", "running"}:
            raise ValueError("task cannot accept a handoff")
        assessment = await quality.handoff(outcome, contract)
        admitted = assessment.accepted and outcome.termination in {
            "completed",
            "research_complete",
        }
        result = outcome.model_dump(mode="json")
        if not admitted:
            result["evidence_registry"] = []
            result["compressed_research"] = ""
        receipt = {
            "task_id": outcome.task_id,
            "accepted": admitted,
            "assessment": assessment.model_dump(mode="json"),
        }

        async def commit(db):
            domain = dict(snapshot)
            domain.pop("_native_worker", None)
            domain.pop("_team_feedback", None)
            domain.update(
                status="completed",
                admission_status="accepted" if admitted else "rejected",
                result=result,
                handoff_assessment=receipt["assessment"],
            )
            updated = await db.fetchval(
                """UPDATE research_team_tasks SET status='completed',admission_status=$4,
                   snapshot=snapshot || $5::jsonb,version=version+1,fence_token=$6
                   WHERE run_id=$1 AND task_id=$2 AND owner=$3
                     AND status IN ('pending','running') AND version=$7 RETURNING task_id""",
                self.lease.run_id,
                outcome.task_id,
                member_id,
                domain["admission_status"],
                json.dumps(domain),
                self.lease.fence,
                snapshot["version"],
            )
            if updated is None:
                raise ValueError("task changed during handoff assessment")
            return receipt

        return await self.transport.transact(event, commit)


def research_member_template(*, max_iters=10):
    """Public, serializable template; tool permissions remain host-governed."""
    from agentscope.agent import ReActConfig

    return SubAgentTemplate(
        type="researcher",
        description="证据驱动的研究成员",
        system_prompt_template="你是 {team_name} 的研究成员 {member_name}。职责：{member_description}。团队目标：{team_description}。向 {leader_name} 汇报可核验的证据与缺口。",
        react_config=ReActConfig(max_iters=max_iters),
        override_leader_mode=True,
        extend_leader_permission_rules=False,
        extend_leader_working_directories=False,
    )

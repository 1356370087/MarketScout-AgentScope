"""Teams behavior against PostgreSQL, including conflicting concurrent claims."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_team import env, task
from open_deep_research.tasks.team_protocol import MemberIdentity
from open_deep_research.tasks.team_delivery import TeamDelivery

pytestmark = pytest.mark.asyncio


async def test_task_list_keeps_later_tasks_visible_with_large_upstream_artifacts():
    from unittest.mock import AsyncMock
    from open_deep_research.agentscope_runtime.teams_tools import member_tools
    from open_deep_research.configuration import Configuration
    from open_deep_research.tools.governance import _serialize_governed_output

    artifact = {"compressed_research": "evidence " * 10000, "evidence_registry": {"source": "official"}}
    rows = [
        {"task_id": "postgres", "display_title": "PostgreSQL", "status": "completed",
         "admission_status": "accepted", "result": artifact},
        {"task_id": "rocketmq", "display_title": "RocketMQ", "status": "completed",
         "admission_status": "accepted", "result": artifact},
        {"task_id": "aggregate", "status": "running", "blockedBy": ["postgres", "rocketmq"]},
    ]
    team = SimpleNamespace(lease=SimpleNamespace(run_id="run"),
                           service=SimpleNamespace(tasks=AsyncMock(return_value=rows)))
    tools = {tool.name: tool for tool in member_tools(SimpleNamespace(team=team, member_id="member", task_id="aggregate"))}
    result = await tools["TaskList"].call(SimpleNamespace(), None, None)
    visible = _serialize_governed_output(tools["TaskList"], result.output, Configuration(max_mcp_output_chars=30000))
    assert all(task_id in visible for task_id in ("postgres", "rocketmq", "aggregate"))
    assert "truncated" not in visible and "compressed_research" not in visible
    detail = await tools["TaskGet"].call(SimpleNamespace(task_id="rocketmq"), None, None)
    assert detail.output["result"]["compressed_research"] == artifact["compressed_research"]
    assert detail.output["result"]["evidence_registry"] == artifact["evidence_registry"]
    from open_deep_research.tasks.team_service import task_view
    rejected = {**rows[0], "admission_status": "rejected", "result": {"termination": "exceed_max_iters"}}
    for summary in (True, False):
        view = task_view(rejected, summary=summary)
        assert view["admission_reason"] == "research_execution_not_completed: exceed_max_iters"


async def test_team_task_creation_requires_explicit_fact_scope():
    from pydantic import ValidationError
    from open_deep_research.agentscope_runtime.teams_tools import TaskCreateInput

    for value in ({"subject": "PostgreSQL"}, {"subject": "PostgreSQL", "requirement_ids": []}):
        with pytest.raises(ValidationError):
            TaskCreateInput.model_validate(value)
    with pytest.raises(ValidationError):
        TaskCreateInput(subject="PostgreSQL", requirement_ids=["COV-21-0e6e43c7cde5"])
    task = TaskCreateInput(subject="PostgreSQL", requirement_ids=["COV-21-0e6e43c7cde5"],
                           owner="db", blockedBy=["upstream"])
    assert task.requirement_ids == ["COV-21-0e6e43c7cde5"]
    assert task.owner == "db" and task.blockedBy == ["upstream"]
    schema = TaskCreateInput.model_json_schema()
    assert schema["properties"]["owner"]["type"] == "string"
    payload = {"subject": "Aggregate", "requirement_ids": ["COV-21-0e6e43c7cde5"], "owner": "", "blockedBy": ["upstream"]}
    assert TaskCreateInput.model_validate(payload).owner is None
    with pytest.raises(ValidationError, match="空字符串"):
        TaskCreateInput.model_validate({**payload, "owner": "null"})


async def test_lead_wait_requires_real_update_not_sixty_empty_polls(monkeypatch):
    from unittest.mock import AsyncMock
    from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers

    row = {"task_id": "one", "version": 1, "status": "running"}
    polls = []
    real_sleep = asyncio.sleep

    async def poll_delay(_):
        polls.append(1)
        if len(polls) == 75:
            row["version"] = 2
        await real_sleep(0)

    blocked = [
        {"task_id": "old", "version": 2, "status": "completed", "admission_status": "rejected"},
        {"task_id": "down", "version": 1, "status": "pending", "unresolvedBlockedBy": ["old"]},
    ]
    team = SimpleNamespace(lease=SimpleNamespace(run_id="run", user_id="owner"),
        pending=AsyncMock(return_value=[]), service=SimpleNamespace(tasks=AsyncMock(side_effect=lambda _: [dict(row), *blocked])))
    host = SimpleNamespace(team=team, closed=False, ensure_members=AsyncMock(),
        recovery=SimpleNamespace(store=SimpleNamespace(budget=AsyncMock(return_value={"deadline": None}))))
    monkeypatch.setattr(asyncio, "sleep", poll_delay)
    await asyncio.wait_for(TeamsWorkers.wait_for_updates(host), 2)
    assert len(polls) == 75
    # An unresolved failed predecessor must return to Lead for remediation.
    team.service.tasks = AsyncMock(return_value=[
        {"task_id": "up", "version": 2, "status": "failed"},
        {"task_id": "down", "version": 1, "status": "pending", "unresolvedBlockedBy": ["up"]},
    ])
    await TeamsWorkers.wait_for_updates(host)
    assert len(polls) == 75


async def test_lead_wait_remains_cancellable_and_deadline_bound():
    from unittest.mock import AsyncMock
    from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers
    from open_deep_research.budgets import DeadlineExceeded

    team = SimpleNamespace(lease=SimpleNamespace(run_id="run", user_id="owner"), pending=AsyncMock(return_value=[]),
        service=SimpleNamespace(tasks=AsyncMock(return_value=[{"task_id": "one", "version": 1, "status": "running"}])))
    host = SimpleNamespace(team=team, closed=False, ensure_members=AsyncMock(),
        recovery=SimpleNamespace(store=SimpleNamespace(budget=AsyncMock(return_value={"deadline": 1}))))
    with pytest.raises(DeadlineExceeded):
        await TeamsWorkers.wait_for_updates(host)
    host.recovery.store.budget.return_value = {"deadline": None}
    waiting = asyncio.create_task(TeamsWorkers.wait_for_updates(host))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


async def setup(env, mode="direct"):
    build, _, _, pool = env
    team = await build()
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_teams SET mode='teams',execution_mode=$2 WHERE run_id=$1",
            team.lease.run_id,
            mode,
        )
    return team, pool


async def member(team, pool, name, mode=None):
    member_id = await team.add_member(
        "add:" + name, name, "research", max_members=25, execution_mode=mode
    )
    token = uuid4().hex
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_members SET execution_token=$3,execution_epoch=1,lease_expires=clock_timestamp()+interval '60 seconds' WHERE run_id=$1 AND member_id=$2",
            team.lease.run_id,
            member_id,
            token,
        )
    return MemberIdentity(
        run_id=team.lease.run_id, member_id=member_id, name=name
    ), token


async def claim(team, identity, token, task_id):
    row = next(
        t
        for t in await team.service.tasks(team.lease.run_id)
        if t["task_id"] == task_id
    )
    return await team.command(
        "claim:" + uuid4().hex,
        "task_claim",
        {
            "task_id": task_id,
            "version": row["version"],
            "execution_token": token,
        },
        member=identity,
    )


async def test_twenty_competing_members_one_claim(env):
    team, pool = await setup(env)
    members = [await member(team, pool, f"worker-{i}") for i in range(20)]
    await task(team, "one")
    results = await asyncio.gather(*(claim(team, m, t, "one") for m, t in members))
    assert sum(row["claimed"] for row in results) == 1
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["phase"] == "executing" and row["execution_mode"] == "direct"


async def test_assignment_dependency_and_quality_unlock(env):
    team, pool = await setup(env)
    who, token = await member(team, pool, "worker")
    await task(team, "upstream")
    await task(team, "downstream", ["upstream"])
    rows = await team.service.tasks(team.lease.run_id)
    a, b = rows
    assert a["blocks"] == ["downstream"] and b["blockedBy"] == ["upstream"]
    await team.command(
        "assign-b",
        "task_assign",
        {"task_id": "downstream", "owner": who.member_id, "version": b["version"]},
    )
    assert not (await claim(team, who, token, "downstream"))["claimed"]
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET status='completed',admission_status='rejected' WHERE run_id=$1 AND task_id='upstream'",
            team.lease.run_id,
        )
    assert not (await claim(team, who, token, "downstream"))["claimed"]
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET admission_status='accepted' WHERE run_id=$1 AND task_id='upstream'",
            team.lease.run_id,
        )
    assert (await claim(team, who, token, "downstream"))["claimed"]


async def test_cycle_and_stale_dependency_version(env):
    team, pool = await setup(env)
    await task(team, "a")
    await task(team, "b", ["a"])
    a = next(
        t for t in await team.service.tasks(team.lease.run_id) if t["task_id"] == "a"
    )
    with pytest.raises(ValueError, match="cycle"):
        await team.command(
            "cycle",
            "task_update",
            {"task_id": "a", "version": a["version"], "addBlockedBy": ["b"]},
        )
    with pytest.raises(ValueError, match="not_found"):
        await team.command(
            "missing",
            "task_update",
            {"task_id": "a", "version": a["version"], "addBlockedBy": ["absent"]},
        )


async def test_plan_inheritance_rejection_and_explicit_approval(env):
    team, pool = await setup(env, "plan_approval")
    who, token = await member(team, pool, "planner")
    other, _ = await member(team, pool, "direct", "direct")
    rows = await team.members()
    assert next(m for m in rows if m["member_id"] == other.member_id)["mode_override"]
    await task(team, "one")
    assert (await claim(team, who, token, "one"))["phase"] == "planning"
    body = {
        "type": "plan_approval_request",
        "task_id": "one",
        "plan": {
            "objective": "Research",
            "requirement_ids": ["COV-1"],
            "steps": ["Read sources"],
            "budget": "2 searches",
            "acceptance_criteria": ["2 independent sources"],
        },
    }
    result = await team.send_message(who, "plan1", "lead", body)
    plan = (await team.service.tasks(team.lease.run_id))[0]
    assert plan["phase"] == "awaiting_plan_review"
    response = {
        "type": "plan_approval_response",
        "task_id": "one",
        "version": 1,
        "request_id": result["event_id"],
        "approve": False,
        "feedback": "Specify official sources",
    }
    with pytest.raises(PermissionError):
        await team.send_message(other, "spoof", who.member_id, response)
    await team.send_message(team.leader, "reject", who.member_id, response)
    assert (await team.service.tasks(team.lease.run_id))[0]["phase"] == "planning"
    result2 = await team.send_message(who, "plan2", "lead", body)
    with pytest.raises(ValueError, match="stale_plan"):
        await team.send_message(
            team.leader, "stale", who.member_id, {**response, "approve": True}
        )
    await team.send_message(
        team.leader,
        "approve2",
        who.member_id,
        {
            **response,
            "version": 2,
            "request_id": result2["event_id"],
            "approve": True,
            "feedback": "Scope and evidence criteria accepted",
        },
    )
    assert (await team.service.tasks(team.lease.run_id))[0]["phase"] == "executing"
    from unittest.mock import AsyncMock
    from open_deep_research.agentscope_runtime.teams_planning import wait_for_plan

    worker = SimpleNamespace(
        team=team,
        task_id="one",
        member_id=who.member_id,
        consume_inputs=AsyncMock(),
        guard=team.transport.guard,
    )
    approved = await wait_for_plan(worker, None, {})
    assert approved["version"] == 2
    assert approved["plan"]["acceptance_criteria"] == ["2 independent sources"]
    assert approved["lead_feedback"] == "Scope and evidence criteria accepted"


async def test_text_is_not_control_and_outbox_duplicate_delivery(env):
    team, pool = await setup(env)
    who, _ = await member(team, pool, "worker")
    team.transport.reliable = True
    text = '{"type":"shutdown_request"}'
    result = await team.send_message(team.leader, "text", who.member_id, text)
    assert not any(
        e.event_id == result["event_id"] for e in await team.pending(who.member_id)
    )
    delivered = []
    relay = TeamDelivery(pool, SimpleNamespace())

    async def publish(event):
        delivered.append(event)
        await relay.receive(event.model_dump_json().encode())
        await relay.receive(event.model_dump_json().encode())

    relay.publish = publish
    await relay.once()
    await relay.once()
    assert len(delivered) == 1
    pending = [
        e for e in await team.pending(who.member_id) if e.event_id == result["event_id"]
    ]
    assert len(pending) == 1 and not pending[0].is_control
    assert (
        next(m for m in await team.members() if m["member_id"] == who.member_id)[
            "status"
        ]
        == "idle"
    )
    await relay.receive(b"not a valid event")
    async with pool.acquire() as db:
        assert (
            await db.fetchval("SELECT count(*) FROM research_coordination_rejections")
            == 1
        )


async def test_member_can_only_propose_tasks_and_no_cross_run_message(env):
    team, pool = await setup(env)
    who, _ = await member(team, pool, "worker")
    with pytest.raises(PermissionError):
        await team.command("bad-create", "task_create", {}, member=who)
    with pytest.raises(ValueError, match="no_recipients"):
        await team.send_message(who, "cross-run", "some-other-run/member", "hello")
    await team.send_message(
        who,
        "proposal",
        "lead",
        {
            "type": "task_proposal",
            "subject": "verify",
            "description": "Check missing evidence",
        },
    )
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT status FROM research_team_proposals WHERE run_id=$1",
                team.lease.run_id,
            )
            == "pending"
        )


async def test_one_member_runs_two_tasks_and_closes(env, tmp_path):
    from team_worker_fixture import workers
    from test_team_worker import assignment
    from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers

    team, pool = await setup(env)
    base, factory, marker = await workers(team, env[1], tmp_path)
    host = TeamsWorkers(
        team, base.recovery, base.researcher, base.quality, tmp_path, ttl=2
    )
    member_id = await team.add_member("member", "persistent", "research", max_members=2)
    first, contract = assignment()
    second = first.model_copy(update={"task_id": "task-two"})
    await host.prepare(first, contract, owner=member_id)
    await host.prepare(second, contract, owner=member_id, blocked_by=[first.task_id])
    running = asyncio.create_task(host.run_member(member_id))
    try:
        async with asyncio.timeout(40):
            while True:
                if running.done():
                    await running
                rows = await team.service.tasks(team.lease.run_id)
                if all(row["status"] == "completed" for row in rows):
                    break
                await asyncio.sleep(0.05)
        assert len(marker.read_text().splitlines()) == 2
        async with pool.acquire() as db:
            assert (
                await db.fetchval(
                    "SELECT execution_epoch FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                    team.lease.run_id,
                    member_id,
                )
                == 1
            )
        await host.finish_team()
        await asyncio.wait_for(running, 5)
        assert (
            next(m for m in await team.members() if m["member_id"] == member_id)[
                "status"
            ]
            == "closed"
        )
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await host.aclose()


async def test_member_cannot_claim_second_task_or_write_after_lease_loss(env):
    team, pool = await setup(env)
    who, token = await member(team, pool, "worker")
    await task(team, "one")
    await task(team, "two")
    assert (await claim(team, who, token, "one"))["claimed"]
    assert not (await claim(team, who, token, "two"))["claimed"]
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET status='completed' WHERE run_id=$1 AND task_id='one'",
            team.lease.run_id,
        )
        await db.execute(
            "UPDATE research_team_members SET lease_expires=clock_timestamp()-interval '1 second' WHERE run_id=$1 AND member_id=$2",
            team.lease.run_id,
            who.member_id,
        )
    assert not (await claim(team, who, token, "two"))["claimed"]


async def test_outbox_retries_same_event_after_publish_crash(env):
    team, pool = await setup(env)
    who, _ = await member(team, pool, "worker")
    team.transport.reliable = True
    sent = await team.send_message(team.leader, "retry-message", who.member_id, "hello")
    relay = TeamDelivery(pool, SimpleNamespace())

    async def crash(event):
        await relay.receive(event.model_dump_json().encode())
        raise ConnectionError("ack lost")

    relay.publish = crash
    await relay.once()
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_coordination_outbox SET next_attempt=clock_timestamp() WHERE event_id=$1",
            sent["event_id"],
        )

    async def success(event):
        await relay.receive(event.model_dump_json().encode())

    relay.publish = success
    await relay.once()
    assert (
        len(
            [
                e
                for e in await team.pending(who.member_id)
                if e.event_id == sent["event_id"]
            ]
        )
        == 1
    )
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT attempts FROM research_coordination_outbox WHERE event_id=$1",
                sent["event_id"],
            )
            == 2
        )


async def test_member_restart_supersedes_pending_plan_and_fences_old_executor(
    env, tmp_path
):
    from team_worker_fixture import workers
    from open_deep_research.agentscope_runtime.teams_worker import MemberLoop
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost

    team, pool = await setup(env, "plan_approval")
    host, _, _ = await workers(team, env[1], tmp_path)
    member_id = await team.add_member("planner", "planner", "research", max_members=2)
    old = MemberLoop(host, member_id)
    await old.acquire()
    await task(team, "one")
    await claim(old.team, old.identity, old.token, "one")
    body = {
        "type": "plan_approval_request",
        "task_id": "one",
        "plan": {
            "objective": "Research",
            "requirement_ids": ["COV-1"],
            "steps": ["Read official sources"],
            "budget": "2 searches",
            "acceptance_criteria": ["Traceable evidence"],
        },
    }
    first = await old.team.send_message(old.identity, "old-plan", "lead", body)
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_members SET lease_expires=clock_timestamp()-interval '1 second' WHERE run_id=$1 AND member_id=$2",
            team.lease.run_id,
            member_id,
        )
    restored = MemberLoop(host, member_id)
    await restored.acquire()
    async with pool.acquire() as db:
        with pytest.raises(FenceLost):
            await old.guard(db)
    response = {
        "type": "plan_approval_response",
        "task_id": "one",
        "version": 1,
        "request_id": first["event_id"],
        "approve": True,
        "feedback": "Reviewed",
    }
    with pytest.raises(ValueError, match="stale_plan"):
        await team.send_message(team.leader, "old-approval", member_id, response)
    assert (await team.service.tasks(team.lease.run_id))[0]["phase"] == "planning"
    current = await restored.team.send_message(
        restored.identity, "new-plan", "lead", body
    )
    await team.send_message(
        team.leader,
        "new-approval",
        member_id,
        {**response, "version": 2, "request_id": current["event_id"]},
    )
    assert (await team.service.tasks(team.lease.run_id))[0]["phase"] == "executing"


async def test_idle_message_replay_freezes_board_input(env, tmp_path, monkeypatch):
    from team_worker_fixture import workers
    from open_deep_research.agentscope_runtime.teams_worker import MemberLoop
    from open_deep_research.agentscope_runtime.teams_discussion import (
        discuss,
        DiscussionReply,
        ResearchModels,
    )

    team, pool = await setup(env)
    host, _, _ = await workers(team, env[1], tmp_path)
    member_id = await team.add_member("reader", "reader", "research", max_members=2)
    loop = MemberLoop(host, member_id)
    await loop.acquire()
    sent = await team.send_message(
        team.leader, "question", member_id, "Summarize existing evidence"
    )
    event = next(
        e for e in await team.pending(member_id) if e.event_id == sent["event_id"]
    )
    prompts = []

    async def model(self, role, prompt, schema, state):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ConnectionError("interrupted model attempt")
        return DiscussionReply(summary="Existing evidence summarized")

    monkeypatch.setattr(ResearchModels, "structured", model)
    with pytest.raises(ConnectionError):
        await discuss(loop, event)
    await task(team, "board-changed-after-crash")
    await discuss(loop, event)
    assert prompts[0] == prompts[1]
    assert not any(e.event_id == event.event_id for e in await team.pending(member_id))
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT session ? 'pending_discussion' FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                team.lease.run_id,
                member_id,
            )
            is False
        )


async def test_explicit_team_creation_allows_lead_to_start_without_team(env):
    from open_deep_research.agentscope_runtime.team_host import NativeTeamHost
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot

    build, recovery, storage, pool = env
    original = await build()
    run_id = uuid4().hex
    state = ResearchSnapshot(run_id=run_id, config_fingerprint="frozen")
    state.application["configuration"] = RunConfig.compile(
        {
            "configurable": {
                "enable_async_research": True,
                "async_research_mode": "teams",
            }
        }
    ).snapshot()
    await recovery.create_run("owner", state)
    lease = await recovery.acquire(run_id, "owner", ttl=120)
    host = NativeTeamHost(
        pool, storage, None, recovery_schema=original.transport.run_table.split('"')[1]
    )
    team = await host.bind(SimpleNamespace(lease=lease, snapshot=state))
    assert await team.pending("lead") == []
    assert await team.members() == []
    with pytest.raises(ValueError, match="team_not_active"):
        await team.add_member("early", "early", "research")
    await team.create("explicit", mode="teams", execution_mode="plan_approval")
    await team.create("explicit", mode="teams", execution_mode="plan_approval")
    restored = await host.bind(SimpleNamespace(lease=lease, snapshot=state))
    assert len(await restored.members()) == 1
    assert restored.leader_session_id == team.leader_session_id


async def test_four_rejected_plans_require_human_revision_not_metadata(env):
    team, pool = await setup(env, "plan_approval")
    who, token = await member(team, pool, "planner")
    await task(team, "one")
    row = (await team.service.tasks(team.lease.run_id))[0]
    await team.command(
        "metadata",
        "task_update",
        {
            "task_id": "one",
            "version": row["version"],
            "metadata": {"plan_revision_limit": 99},
        },
    )
    await claim(team, who, token, "one")
    for version in range(1, 5):
        plan = await team.send_message(
            who,
            "plan" + str(version),
            "lead",
            {
                "type": "plan_approval_request",
                "task_id": "one",
                "plan": {
                    "objective": "Research",
                    "requirement_ids": ["COV-1"],
                    "steps": ["Read sources"],
                    "budget": "2 searches",
                    "acceptance_criteria": ["2 official sources"],
                },
            },
        )
        await team.send_message(
            team.leader,
            "reject" + str(version),
            who.member_id,
            {
                "type": "plan_approval_response",
                "task_id": "one",
                "version": version,
                "request_id": plan["event_id"],
                "approve": False,
                "feedback": "Revise validation",
            },
        )
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["phase"] == "awaiting_human"
    await team.command(
        "human-revise",
        "task_plan_human",
        {
            "task_id": "one",
            "version": row["version"],
            "feedback": "Narrow the sources",
            "user_id": "owner",
        },
    )
    assert (await team.service.tasks(team.lease.run_id))[0]["phase"] == "planning"


async def test_gateway_blocks_tools_before_plan_approval(env):
    from open_deep_research.agentscope_runtime.gateway_ledger import SQLGatewayLedger

    team, pool = await setup(env, "plan_approval")
    who, token = await member(team, pool, "planner")
    await task(team, "one")
    await claim(team, who, token, "one")
    ledger = SQLGatewayLedger(SimpleNamespace(lease=team.lease), {})
    ledger.team = team
    with pytest.raises(PermissionError, match="team_plan_approval_required"):
        await ledger.reserve_tool(SimpleNamespace(task_id="one"))
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET status='cancelled',phase=NULL WHERE run_id=$1 AND task_id='one'",
            team.lease.run_id,
        )
    with pytest.raises(PermissionError, match="team_task_not_executing"):
        await ledger.reserve_tool(SimpleNamespace(task_id="one"))


async def test_failed_run_terminates_team_board(env):
    from open_deep_research.agentscope_runtime.run_config import RunConfig

    team, pool = await setup(env)
    await member(team, pool, "worker")
    await task(team, "unfinished")
    recovery = env[1]
    snapshot, _ = await recovery.load(team.lease.run_id, team.lease.user_id)
    snapshot.application["configuration"] = RunConfig.compile(
        {
            "configurable": {
                "enable_async_research": True,
                "async_research_mode": "teams",
            }
        }
    ).snapshot()
    snapshot.status = "failed"
    await recovery.save(team.lease, snapshot)
    assert (await team.service.tasks(team.lease.run_id))[0]["status"] == "failed"
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM research_team_members WHERE run_id=$1 AND execution_token IS NOT NULL",
                team.lease.run_id,
            )
            == 0
        )


async def test_governance_accepts_structured_messages_without_stringifying(env):
    from open_deep_research.agentscope_runtime.teams_tools import communication_tools
    from open_deep_research.tools.governance import validate_tool_args

    team, pool = await setup(env)
    tool = communication_tools(team, team.leader, "test:")[0]
    body = {
        "type": "plan_approval_response",
        "task_id": "t",
        "version": 1,
        "request_id": "r",
        "approve": False,
        "feedback": "Revise steps",
    }
    arguments = {"to": "worker", "message": body}
    assert validate_tool_args(tool, arguments) is None
    typed = tool.input_schema.model_validate(arguments)
    assert typed.message.type == "plan_approval_response"
    literal = tool.input_schema.model_validate(
        {"to": "worker", "message": json.dumps(body)}
    )
    assert isinstance(literal.message, str)
    import jsonschema
    from pydantic import ValidationError

    projected = tool.input_schema.model_json_schema()
    assert projected["properties"]["message"]["type"] == ["object", "string"]
    jsonschema.validate(arguments, projected)
    jsonschema.validate({"to": "worker", "message": "ordinary text"}, projected)
    with pytest.raises(ValidationError):
        tool.input_schema.model_validate(
            {"to": "worker", "message": {"type": "plan_approval_response"}}
        )

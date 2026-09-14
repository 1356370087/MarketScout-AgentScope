"""PostgreSQL failure windows for RocketMQ business transactions."""

import asyncio
import importlib
import os
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_store import TeamStore


@pytest_asyncio.fixture
async def store():
    dsn = os.getenv("TEAM_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEAM_TEST_DATABASE_URL required (isolated test database)")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    schema = "team_test_" + os.urandom(8).hex()
    async with pool.acquire() as db:
        await db.execute(f'CREATE SCHEMA "{schema}"')
    await pool.close()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4,
                                   server_settings={"search_path": schema})
    migration = importlib.import_module("security.rbac.migrations.versions.0016_research_teams")
    statements = []
    with patch.object(migration.op, "execute", statements.append):
        migration.upgrade()
    async with pool.acquire() as db:
        for sql in statements:
            await db.execute(sql)
    try:
        yield TeamStore(pool)
    finally:
        async with pool.acquire() as db:
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await pool.close()


def event(operation="op-1"):
    return TeamEvent(operation_id=operation, run_id="run-1", sender="lead",
                     recipients=["alice", "bob"], type="message", payload={"text": "hello"})


@pytest.mark.asyncio
async def test_rollback_leaves_no_business_event(store):
    message = event()
    await store.prepare(message)

    async def fail(db):
        raise ValueError("business failure")

    with pytest.raises(ValueError):
        await store.commit(message, fail)
    assert await store.outcome(message.event_id) == "PREPARED"
    await store.abort(message)
    assert await store.outcome(message.event_id) == "ABORTED"


@pytest.mark.asyncio
async def test_replayed_operation_mutates_once(store):
    message = event()
    await store.prepare(message)
    calls = []

    async def mutation(db):
        calls.append(1)
        return {"task_id": "one"}

    results = await asyncio.gather(store.commit(message, mutation), store.commit(message, mutation))
    assert results == [{"task_id": "one"}, {"task_id": "one"}]
    assert len(calls) == 1
    await store.abort(message)
    assert await store.outcome(message.event_id) == "COMMITTED"
    replay = event()
    assert await store.prepare(replay) == ("COMMITTED", {"task_id": "one"})
    assert replay.event_id == message.event_id


@pytest.mark.asyncio
async def test_expired_prepare_cannot_commit_late(store):
    message = event()
    await store.prepare(message)
    async with store.pool.acquire() as db:
        await db.execute("UPDATE research_coordination_transactions SET deadline=now()-interval '1 second'")
    assert await store.outcome(message.event_id) == "ABORTED"

    async def mutation(db):
        pytest.fail("expired mutation must not execute")

    with pytest.raises(RuntimeError, match="expired"):
        await store.commit(message, mutation)


@pytest.mark.asyncio
async def test_receipt_is_durable_deduplicated_and_run_scoped(store):
    message = event()
    await store.receive(message)
    await store.receive(message)
    assert len(await store.pending("run-1", "alice")) == 1
    assert not await store.pending("run-2", "alice")
    await store.applied("run-2", "alice", [message.event_id])
    assert len(await store.pending("run-1", "alice")) == 1
    await store.applied("run-1", "alice", [message.event_id])
    assert not await store.pending("run-1", "alice")
    assert len(await store.pending("run-1", "bob")) == 1


@pytest.mark.asyncio
async def test_operation_id_cannot_change_payload(store):
    await store.prepare(event())
    changed = event()
    changed.payload = {"text": "different"}
    with pytest.raises(ValueError, match="input_mismatch"):
        await store.prepare(changed)


@pytest.mark.asyncio
async def test_real_rocketmq_transaction_delivery(store):
    endpoint = os.getenv("TEAM_TEST_ROCKETMQ_ENDPOINT")
    if not endpoint:
        pytest.skip("TEAM_TEST_ROCKETMQ_ENDPOINT required")
    from open_deep_research.tasks.rocketmq_transport import RocketMQTransport

    transport = RocketMQTransport(
        store, endpoint=endpoint,
        prefix=os.environ["TEAM_TEST_ROCKETMQ_PREFIX"],
        consumer_group_prefix=os.environ["TEAM_TEST_ROCKETMQ_PREFIX"] + "_" + os.urandom(4).hex(),
    )
    try:
        await transport.start()
        message = event("real-" + os.urandom(8).hex())
        message.run_id = "delivery-" + os.urandom(8).hex()

        async def mutation(db):
            return {"delivered": True}

        assert await transport.transact(message, mutation) == {"delivered": True}
        await asyncio.wait_for(transport.signal(message.run_id, "alice").wait(), timeout=45)
        assert any(item.event_id == message.event_id for item in await store.pending(message.run_id, "alice"))
        assert await store.outcome(message.event_id) == "COMMITTED"
    finally:
        await transport.close()


class LocalTransactions:
    def __init__(self, store):
        self.store = store

    async def transact(self, message, mutation):
        state, result = await self.store.prepare(message)
        if state == "COMMITTED":
            return result
        return await self.store.commit(message, mutation)


@pytest.mark.asyncio
async def test_real_team_member_reuse_dependency_and_tools(store, tmp_path, monkeypatch):
    """Exercise the actual pool/executor/tool boundary against PostgreSQL and MQ."""
    endpoint = os.getenv("TEAM_TEST_ROCKETMQ_ENDPOINT")
    if not endpoint:
        pytest.skip("TEAM_TEST_ROCKETMQ_ENDPOINT required")
    from open_deep_research.tasks.lease import LeaderLeaseManager
    from open_deep_research.tasks.registry import get_task_registry
    from open_deep_research.tasks.rocketmq_transport import RocketMQTransport
    from open_deep_research.tasks.team_protocol import member_identity
    from open_deep_research.tasks.team_runtime import team_runtime
    from open_deep_research.tasks.team_service import TeamService
    from open_deep_research.tasks.team_state import PostgresTaskStateStore
    from open_deep_research.tasks.teammate_pool import (
        get_teammate_pool,
        shutdown_teammate_pool,
    )
    from open_deep_research.tools.base import ToolContext
    from open_deep_research.tools.governance import (
        AgentRole,
        execute_governed_tool_call,
    )
    from open_deep_research.tools.registry import assemble_toolset
    from open_deep_research.tools.supervisor.deps import SupervisorToolDeps
    from open_deep_research.tools.team import build_team_tools

    run_id = "pool-" + os.urandom(8).hex()
    lease = LeaderLeaseManager(runs_dir=str(tmp_path), run_id=run_id, lease_seconds=120)
    epoch = await lease.acquire()
    config = {"configurable": {"enable_async_research": True, "sandbox_enabled": False,
        "runs_dir": str(tmp_path), "event_log_enabled": False, "observability_enabled": False,
        "quality_evaluation_enabled": False, "search_api": "none", "enable_memory": False},
        "metadata": {"run_id": run_id, "run_fence_token": epoch.fence_token,
                     "run_lease_owner_id": lease.owner_id}}
    transport = RocketMQTransport(store, endpoint=endpoint, prefix=os.environ["TEAM_TEST_ROCKETMQ_PREFIX"],
        consumer_group_prefix=os.environ["TEAM_TEST_ROCKETMQ_PREFIX"] + "_" + os.urandom(4).hex())
    await transport.start()
    service = TeamService(transport)
    state = PostgresTaskStateStore(service)
    async def start():
        return service
    monkeypatch.setattr(team_runtime, "start", start)
    monkeypatch.setattr(team_runtime, "state", state)
    executions = []
    stop_started = asyncio.Event()
    resume_started = asyncio.Event()
    resumed = []
    async def researcher(task_state, task_config):
        identity = member_identity.get()
        assert identity is not None
        executions.append(identity.member_id)
        if task_state["research_topic"] == "Stop this task":
            stop_started.set()
            await asyncio.Event().wait()
        if task_state["research_topic"] == "Resume this task":
            if not resumed:
                from langchain_core.messages import HumanMessage

                from open_deep_research.agents.query_state import QueryLoopState
                resumed.append(identity.member_id)
                await task_state["_query_checkpoint_callback"](QueryLoopState(
                    state_key="researcher:" + task_config["metadata"]["task_id"],
                    role=AgentRole.RESEARCHER, messages=(HumanMessage(content="saved input"),),
                    consumed_input_ids=("already-delivered",),
                ))
                resume_started.set()
                await asyncio.Event().wait()
            assert task_state["query_state_snapshot"]["consumed_input_ids"] == ["already-delivered"]
            assert identity.member_id == resumed[0]
        tools = await assemble_toolset(AgentRole.RESEARCHER, task_config)
        assert "SendMessage" in {tool.name for tool in tools}
        outcome = await execute_governed_tool_call({"name": "SendMessage", "id": "notify-" + task_config["metadata"]["task_id"],
            "args": {"to": "lead", "message": "working"}}, {tool.name: tool for tool in tools},
            AgentRole.RESEARCHER, task_config)
        assert outcome.error is None, outcome.error
        return {"compressed_research": "verified summary", "raw_notes": [], "metrics": {}}
    deps = SupervisorToolDeps(enable_async_research=True, researcher_ainvoke=researcher)
    tools = {tool.name: tool for tool in build_team_tools(deps)}
    async def call(name, op, args):
        tool = tools[name]
        return (await tool.call(tool.input_schema.model_validate(args), ToolContext(config, "supervisor", op, op))).output
    try:
        await call("TeamCreate", "team", {"name": "Test team"})
        await call("SpawnTeammate", "member", {"name": "alice", "purpose": "Research"})
        first = await call("TaskCreate", "first", {"subject": "First", "description": "First topic"})
        second = await call("TaskCreate", "second", {"subject": "Second", "description": "Second topic", "blocked_by": [first["task_id"]]})
        async def wait_complete(task_id):
            # Real Broker group reassignment can overlap the preceding consumer shutdown.
            async with asyncio.timeout(60):
                while True:
                    get_teammate_pool(config, get_task_registry(), researcher).check_health()
                    snapshot = await state.get(task_id, run_id=run_id)
                    if snapshot.status.value == "completed":
                        return snapshot
                    await asyncio.sleep(.1)
        completed = await wait_complete(first["task_id"])
        assert (await state.get(second["task_id"], run_id=run_id)).status.value == "pending"
        completed.admission_status = "accepted"
        completed.version += 1
        await state.upsert(completed)
        await wait_complete(second["task_id"])
        assert len(executions) == 2 and executions[0] == executions[1]
        stop_task = await call("TaskCreate", "stop-me", {"subject": "Stop", "description": "Stop this task"})
        await asyncio.wait_for(stop_started.wait(), 15)
        await call("TaskStop", "stop-command", {"task_id": stop_task["task_id"]})
        followup = await call("TaskCreate", "followup", {"subject": "After stop", "description": "Continue after cancellation"})
        try:
            await wait_complete(followup["task_id"])
        except TimeoutError:
            pool = get_teammate_pool(config, get_task_registry(), researcher)
            pytest.fail(str({
                "tasks": [(t["task_id"], t["status"], t["owner"]) for t in await service.tasks(run_id)],
                "members": [(key, t.done(), t.cancelled(), [(f.f_code.co_name, f.f_lineno) for f in t.get_stack()]) for key,t in pool.members.items()],
                "cancelled": get_task_registry().get(stop_task["task_id"]).cancelled.is_set(),
                "control_receipts": [dict(r) for r in await store.pool.fetch("SELECT r.recipient,r.applied FROM research_coordination_receipts r JOIN research_coordination_events e USING(event_id) WHERE e.run_id=$1 AND e.event->>'type'='task_stop'",run_id)],
                "active": [(key, t.done(), t.cancelled(), [(f.f_code.co_name, f.f_lineno) for f in t.get_stack()]) for key,t in pool.active.items()],
            }))
        assert (await state.get(stop_task["task_id"], run_id=run_id)).status.value == "cancelled"
        recovering = await call("TaskCreate", "recover", {"subject": "Resume", "description": "Resume this task"})
        await asyncio.wait_for(resume_started.wait(), 15)
        await shutdown_teammate_pool(config)
        await lease.release()
        lease = LeaderLeaseManager(runs_dir=str(tmp_path), run_id=run_id, lease_seconds=120)
        next_epoch = await lease.acquire()
        assert next_epoch.fence_token > epoch.fence_token
        config["metadata"].update(run_fence_token=next_epoch.fence_token, run_lease_owner_id=lease.owner_id)
        await get_teammate_pool(config, get_task_registry(), researcher).start()
        recovered = await wait_complete(recovering["task_id"])
        assert recovered.fence_token == next_epoch.fence_token
        assert not list(tmp_path.rglob("mailbox/*.json"))
    finally:
        await shutdown_teammate_pool(config)
        await transport.close()
        await lease.release()


@pytest.mark.asyncio
async def test_task_dependency_and_claim_rules(store):
    from open_deep_research.tasks.state import TaskSnapshot
    from open_deep_research.tasks.team_protocol import MemberIdentity
    from open_deep_research.tasks.team_service import TeamService

    service = TeamService(LocalTransactions(store))
    lead = MemberIdentity(run_id="r", member_id="lead", name="lead", role="lead")
    async def command(op, kind, payload, identity=lead):
        return await service.command(identity, op, kind, payload, fence_token=1)
    await command("team", "team_create", {"name": "Research"})
    alice = await command("alice", "member_spawn", {"name": "alice", "purpose": "Research", "max_members": 2})
    bob = await command("bob", "member_spawn", {"name": "bob", "purpose": "Review", "max_members": 2})
    for task_id, blockers in [("a", []), ("b", ["a"])]:
        await command(task_id, "task_create", {
            "snapshot": TaskSnapshot(task_id=task_id, run_id="r").model_dump(mode="json"),
            "max_tasks": 10, "blocked_by": blockers,
        })
    with pytest.raises(ValueError, match="cycle"):
        await command("cycle", "task_dependencies", {"task_id": "a", "blocked_by": ["b"]})
    with pytest.raises(ValueError, match="unavailable"):
        await command("blocked", "task_claim", {"task_id": "b", "owner": "alice"})
    result = await command("claim", "task_claim", {"task_id": "a", "owner": "alice"})
    assert result["owner"] == alice["member_id"]
    with pytest.raises(ValueError, match="unavailable"):
        await command("duplicate", "task_claim", {"task_id": "a", "owner": "bob"})
    identity = MemberIdentity(run_id="r", member_id=bob["member_id"], name="bob")
    with pytest.raises(PermissionError):
        await command("impersonate", "task_claim", {"task_id": "b", "owner": "alice"}, identity)
    async with store.pool.acquire() as db:
        await db.execute("UPDATE research_team_tasks SET status='completed',admission_status='rejected' WHERE task_id='a'")
    with pytest.raises(ValueError, match="unavailable"):
        await command("reject", "task_claim", {"task_id": "b", "owner": "bob"})
    async with store.pool.acquire() as db:
        await db.execute("UPDATE research_team_tasks SET admission_status='accepted' WHERE task_id='a'")
    await command("claim-b", "task_claim", {"task_id": "b", "owner": "bob"})
    assert len(await service.tasks("r")) == 2


@pytest.mark.asyncio
async def test_broker_commit_failure_preserves_local_outcome(store):
    from types import SimpleNamespace

    from open_deep_research.tasks.rocketmq_transport import RocketMQTransport
    transport = RocketMQTransport(store, endpoint="unused")
    def lost_reply():
        raise OSError("broker commit reply lost")
    transport.producer = SimpleNamespace(
        begin_transaction=lambda: SimpleNamespace(commit=lost_reply),
        send=lambda *_: None,
    )
    message = event("commit-reply-lost")
    calls = []
    async def mutate(db):
        calls.append(1)
        return {"committed": True}
    assert await transport.transact(message, mutate) == {"committed": True}
    assert await store.outcome(message.event_id) == "COMMITTED"
    assert await transport.transact(event("commit-reply-lost"), mutate) == {"committed": True}
    assert calls == [1]


@pytest.mark.asyncio
async def test_control_input_precedes_regular_backlog(store):
    for index in range(101):
        await store.receive(event(f"regular-{index}"))
    control = event("stop")
    control.type = "task_stop"
    await store.receive(control)
    pending = await store.pending("run-1", "alice")
    assert len(pending) == 100
    assert pending[0].event_id == control.event_id

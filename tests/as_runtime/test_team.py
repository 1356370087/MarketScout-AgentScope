"""M7 native team projections with real PostgreSQL domain coordination."""

import ast
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import AgentData, AgentRecord, SessionConfig
from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
from agentscope.message import UserMsg

from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryStore,
)
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.agentscope_runtime.team import (
    FencedTeamTransport,
    NativeResearchTeam,
    native_task,
    research_member_template,
)
from open_deep_research.tasks.state import TaskSnapshot
from open_deep_research.tasks.team_protocol import MemberIdentity

pytestmark = pytest.mark.asyncio


async def test_native_team_http_uses_sql_assignment_and_durable_messages(env):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.api.native_runs import NativeRuns
    from open_deep_research.api.research_router import build_research_router
    from security.rbac.dependencies import get_current_principal
    from tests.auth_helpers import research_principal

    build, store, _, pool = env
    team = await build()
    await task(team, "http-task")
    member = await team.add_member("member-http", "worker-http", "research")
    await team.command("claim-http", "task_claim", {"task_id": "http-task", "owner": member})
    state, _ = await store.load(team.lease.run_id, "owner")
    state.application["configuration"] = RunConfig.compile({"configurable": {"enable_async_research": True}}).snapshot()
    await store.save(team.lease, state)
    factory = SimpleNamespace(
        runtime=SimpleNamespace(_team_host=SimpleNamespace(pool=pool)),
        active={team.lease.run_id: {"team": team}},
    )
    service = NativeRuns(store, factory, None)
    app = FastAPI()
    app.include_router(build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal("owner")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://native") as client:
        base = "/runs/" + team.lease.run_id
        response = await client.get(base + "/team")
        assert response.status_code == 200, response.text
        assert response.json()["tasks"][0]["owner"] == member
        body = {"to": member, "message": "Check sources", "command_id": "one"}
        first = await client.post(base + "/team/messages", json=body)
        second = await client.post(base + "/team/messages", json=body)
        assert first.status_code == 200 and second.json() == first.json()
        feedback = {"type": "direction", "message": "Check year", "task_id": "http-task", "command_id": "two"}
        assert (await client.post(base + "/feedback", json=feedback)).status_code == 200
        messages = [event for event in await team.pending(member) if event.type == "message"]
        assert len(messages) == 2
        app.dependency_overrides[get_current_principal] = lambda: research_principal("foreign")
        assert (await client.post(base + "/team/messages", json=body)).status_code == 404


@pytest_asyncio.fixture
async def env(pg_url):
    dsn = pg_url.replace("postgresql+asyncpg", "postgresql")
    schema = "m7_" + uuid4().hex
    connection = await asyncpg.connect(dsn)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
    # Execute the published migration unchanged, without importing the old engine.
    tree = ast.parse(
        Path("src/security/rbac/migrations/versions/0016_research_teams.py").read_text(
            encoding="utf-8"
        )
    )
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    ddl = next(
        node.value.value for node in upgrade.body if isinstance(node, ast.Assign)
    )
    async with pool.acquire() as db:
        await db.execute(ddl)
    kwargs = {"connect_args": {"server_settings": {"search_path": schema}}}
    recovery = RecoveryStore(pg_url, engine_kwargs=kwargs)
    await recovery.create_tables()
    storage = AsyncSQLAlchemyStorage(
        pg_url, create_tables=True, auto_migrate=False, engine_kwargs=kwargs
    )
    async with storage:

        async def build(run_id=None, user="owner", bus=None, run_config=None):
            run_id = run_id or uuid4().hex
            if run_config is None:
                await recovery.create_run(
                    user, ResearchSnapshot(run_id=run_id, config_fingerprint="frozen")
                )
            else:
                await recovery.create_from_config(user, run_id, run_config,
                    application={"configuration": run_config.snapshot()})
            lease = await recovery.acquire(run_id, user, ttl=120)
            agent_id = await storage.upsert_agent(
                user,
                AgentRecord(
                    user_id=user,
                    data=AgentData(
                        name="lead",
                        context_config=ContextConfig(),
                        react_config=ReActConfig(),
                    ),
                ),
            )
            session = await storage.upsert_session(
                user, agent_id, SessionConfig(name="leader", workspace_id=run_id)
            )
            transport = FencedTeamTransport(
                pool, lease, recovery_schema=schema, message_bus=bus
            )
            team = NativeResearchTeam(
                storage,
                transport,
                leader_agent_id=agent_id,
                leader_session_id=session.id,
                template=research_member_template(),
            )
            await team.create("research", "frozen goal")
            return team

        yield build, recovery, storage, pool
    await recovery.aclose()
    await pool.close()
    await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await connection.close()


async def task(team, task_id, blockers=()):
    snapshot = TaskSnapshot(
        task_id=task_id,
        run_id=team.lease.run_id,
        user_id=team.lease.user_id,
        research_topic="question " + task_id,
        requirement_ids=["COV-1"],
    )
    await team.command(
        "create:" + task_id,
        "task_create",
        {
            "snapshot": snapshot.model_dump(mode="json"),
            "max_tasks": 10,
            "blocked_by": list(blockers),
        },
    )


async def claim(team, task_id, member):
    return await team.command(
        "claim:" + task_id + member, "task_claim", {"task_id": task_id, "owner": member}
    )


async def test_native_team_reuses_member_but_isolates_tasks_and_runs(env):
    build, _, storage, pool = env
    team = await build()
    member = await team.add_member("add", "alice", "sources")
    assert await team.add_member("add", "alice", "sources") == member
    await task(team, "one")
    await claim(team, "one", member)
    first = await team.task_session("one", member)
    first.state.context.append(UserMsg("user", "private evidence"))
    await storage.update_session_state(
        team.lease.user_id, first.agent_id, first.id, first.state
    )
    restored = await team.task_session("one", member)
    assert restored.state.context[0].get_text_content() == "private evidence"
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET status='completed',admission_status='accepted' WHERE run_id=$1 AND task_id='one'",
            team.lease.run_id,
        )
    await task(team, "two")
    await claim(team, "two", member)
    second = await team.task_session("two", member)
    assert second.agent_id == first.agent_id and second.id != first.id
    assert second.state.context == []
    assert [t.id for t in second.state.tasks_context.tasks] == ["two"]
    other = await build()
    other_member = await other.add_member("add", "alice", "sources")
    assert other.identity("member", other_member) != first.agent_id
    await other.reconcile()
    with pytest.raises(PermissionError):
        await other.task_session("one", member)


async def test_partial_native_provisioning_repaired_without_duplicate_members(
    env, monkeypatch
):
    build, _, storage, pool = env
    team = await build()
    original = storage.upsert_agent

    async def fail(*args, **kwargs):
        raise RuntimeError("native store unavailable")

    monkeypatch.setattr(storage, "upsert_agent", fail)
    with pytest.raises(RuntimeError):
        await team.add_member("add", "alice", "sources")
    monkeypatch.setattr(storage, "upsert_agent", original)
    member = await team.add_member("add", "alice", "sources")
    members = await team.reconcile()
    assert len(members) == 1 and members[0].agent_id == team.identity("member", member)
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM research_team_members WHERE run_id=$1",
                team.lease.run_id,
            )
            == 2
        )


async def test_dependencies_single_claim_cycle_cancel_and_native_status(env):
    build, _, _, pool = env
    team = await build()
    a = await team.add_member("a", "alice", "sources")
    b = await team.add_member("b", "bob", "analysis")
    await task(team, "one")
    await task(team, "two", ["one"])
    with pytest.raises(ValueError, match="unavailable"):
        await claim(team, "two", a)
    with pytest.raises(ValueError, match="cycle"):
        await team.command(
            "cycle", "task_dependencies", {"task_id": "one", "blocked_by": ["two"]}
        )
    outcomes = await asyncio.gather(
        claim(team, "one", a), claim(team, "one", b), return_exceptions=True
    )
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET status='completed',admission_status='rejected' WHERE run_id=$1 AND task_id='one'",
            team.lease.run_id,
        )
    with pytest.raises(ValueError, match="unavailable"):
        await claim(team, "two", a)
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET admission_status='accepted' WHERE run_id=$1 AND task_id='one'",
            team.lease.run_id,
        )
    await claim(team, "two", a)
    member = MemberIdentity(run_id=team.lease.run_id, member_id=b, name="bob")
    with pytest.raises(PermissionError):
        await team.command(
            "stop-worker", "task_stop", {"task_id": "two"}, member=member
        )
    await team.command("stop", "task_stop", {"task_id": "two"})
    rows = await team.service.tasks(team.lease.run_id)
    assert (
        native_task(next(row for row in rows if row["task_id"] == "two")).state
        == "pending"
    )
    assert (
        native_task(next(row for row in rows if row["task_id"] == "one")).state
        == "completed"
    )


async def test_control_priority_atomic_receipt_duplicate_and_lost_wakeup(env):
    class Offline:
        async def publish(self, *args):
            raise ConnectionError("broker unavailable")

    build, _, _, pool = env
    team = await build(bus=Offline())
    member = await team.add_member("a", "alice", "sources")
    await team.say(team.leader, "normal", "alice", "context")
    await team.command("control", "cancel_request", {"to": "alice"})
    pending = await team.pending(member)
    assert [item.type for item in pending] == ["cancel_request", "message"]
    await team.say(team.leader, "normal", "alice", "context")
    assert len(await team.pending(member)) == 2
    applied = []

    async def apply(db, event):
        await db.execute(
            "UPDATE research_team_members SET purpose='applied' WHERE run_id=$1 AND member_id=$2",
            team.lease.run_id,
            member,
        )
        applied.append(event.event_id)

    async def fail(db, event):
        await apply(db, event)
        raise RuntimeError("before commit")

    with pytest.raises(RuntimeError):
        await team.apply_input(member, pending[0].event_id, fail)
    async with pool.acquire() as db:
        assert (
            await db.fetchval(
                "SELECT purpose FROM research_team_members WHERE run_id=$1 AND member_id=$2",
                team.lease.run_id,
                member,
            )
            == "sources"
        )
    assert await team.apply_input(member, pending[0].event_id, apply)
    assert not await team.apply_input(member, pending[0].event_id, apply)
    assert [item.type for item in await team.pending(member)] == ["message"]


async def test_new_leader_epoch_recovers_receipts_and_rejects_old_writer(env):
    build, recovery, _, _ = env
    team = await build()
    member = await team.add_member("a", "alice", "sources")
    await team.say(team.leader, "message", "alice", "persisted")
    await recovery.release(team.lease)
    lease = await recovery.acquire(team.lease.run_id, team.lease.user_id, ttl=120)
    newer = NativeResearchTeam(
        team.storage,
        FencedTeamTransport(
            team.transport.store.pool,
            lease,
            recovery_schema=team.transport.run_table.split('"')[1],
        ),
        leader_agent_id=team.leader_agent_id,
        leader_session_id=team.leader_session_id,
        template=team.template,
    )
    await newer.reconcile()
    assert len(await newer.pending(member)) == 1
    with pytest.raises(FenceLost):
        await team.say(team.leader, "late", "alice", "stale")
    with pytest.raises(FenceLost):
        await team.pending(member)


async def test_foreign_leader_and_changed_template_rejected(env):
    build, _, _, _ = env
    team = await build()
    another = await build(user="other")
    wrong = NativeResearchTeam(
        team.storage,
        team.transport,
        leader_agent_id=another.leader_agent_id,
        leader_session_id=another.leader_session_id,
        template=team.template,
    )
    with pytest.raises(PermissionError):
        await wrong.create("research")
    team.template.react_config.max_iters += 1
    with pytest.raises(ValueError, match="template"):
        await team.reconcile()


@pytest.mark.parametrize("accepted", [True, False])
async def test_handoff_requires_owned_requirements_and_admission(env, accepted):
    from pydantic import BaseModel

    from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff

    class Assessment(BaseModel):
        accepted: bool
        reason: str = "quality result"

    class Quality:
        calls = 0

        async def handoff(self, outcome, contract):
            self.calls += 1
            return Assessment(accepted=accepted)

    build, _, _, _ = env
    team = await build()
    member = await team.add_member("add", "alice", "sources")
    await task(team, "one")
    await task(team, "two", ["one"])
    await claim(team, "one", member)
    handoff = ResearchHandoff(
        task_id="one",
        research_topic="question one",
        requirement_ids=["foreign"],
        compressed_research="finding",
        evidence_registry=[{"id": "evidence"}],
    )
    quality = Quality()
    with pytest.raises(ValueError, match="delegated"):
        await team.admit_handoff("handoff", member, handoff, None, quality)
    handoff.requirement_ids = ["COV-1"]
    result = await team.admit_handoff("handoff", member, handoff, None, quality)
    assert result["accepted"] is accepted
    assert await team.admit_handoff("handoff", member, handoff, None, quality) == result
    assert quality.calls == 1
    rows = await team.service.tasks(team.lease.run_id)
    outcome = next(row for row in rows if row["task_id"] == "one")
    if accepted:
        assert outcome["result"]["compressed_research"] == "finding"
        await claim(team, "two", member)
    else:
        assert outcome["result"]["evidence_registry"] == []
        with pytest.raises(ValueError, match="unavailable"):
            await claim(team, "two", member)


async def test_cancel_during_assessment_rejects_late_handoff(env):
    from types import SimpleNamespace

    from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff

    build, _, _, _ = env
    team = await build()
    member = await team.add_member("add", "alice", "sources")
    await task(team, "one")
    await claim(team, "one", member)

    class Quality:
        async def handoff(self, outcome, contract):
            await team.command("stop", "task_stop", {"task_id": "one"})
            return SimpleNamespace(
                accepted=True, model_dump=lambda **kwargs: {"accepted": True}
            )

    handoff = ResearchHandoff(
        task_id="one",
        research_topic="question one",
        requirement_ids=["COV-1"],
        compressed_research="finding",
    )
    with pytest.raises(ValueError, match="changed"):
        await team.admit_handoff("handoff", member, handoff, None, Quality())
    assert (await team.service.tasks(team.lease.run_id))[0]["status"] == "cancelled"


@pytest.mark.skipif(
    os.getenv("AS_TEST_ROCKETMQ") != "1",
    reason="requires explicit remote RocketMQ acceptance endpoint",
)
async def test_remote_wakeup_observes_already_committed_team_receipt(env):
    from open_deep_research.agentscope_runtime.broadcast import RocketMQBroadcast
    from open_deep_research.agentscope_runtime.pgbus import PostgreSQLMessageBus

    endpoint = os.environ["AS_TEST_ROCKETMQ_ENDPOINT"]
    prefix = os.environ["AS_TEST_ROCKETMQ_PREFIX"]
    writer = RocketMQBroadcast(endpoint, topic_prefix=prefix)
    reader = RocketMQBroadcast(endpoint, topic_prefix=prefix)
    writer_bus = PostgreSQLMessageBus("unused", broadcast=writer)
    reader_bus = PostgreSQLMessageBus("unused", broadcast=reader)
    stream = None
    pending = None
    try:
        await writer.start()
        await reader.start_consumer(
            os.environ["AS_TEST_ROCKETMQ_GROUP"], channels=["wake"]
        )
        build, _, _, _ = env
        team = await build()
        member = await team.add_member("add", "alice", "sources")
        ready = asyncio.Event()
        stream = reader_bus.subscribe("team:" + team.lease.run_id, on_ready=ready.set)
        pending = asyncio.create_task(anext(stream))
        await ready.wait()
        team.transport.message_bus = writer_bus
        await team.say(team.leader, "remote-message", "alice", "durable-before-wakeup")
        notice = await asyncio.wait_for(pending, 30)
        receipts = await team.pending(member)
        assert [event.event_id for event in receipts] == [notice["event_id"]]
        assert receipts[0].payload["content"] == "durable-before-wakeup"
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
        await reader.aclose()
        await writer.aclose()


async def test_deployment_team_binding_reuses_leader_after_restart(env):
    from types import SimpleNamespace

    from open_deep_research.agentscope_runtime.team_host import NativeTeamHost

    build, recovery, storage, pool = env
    original = await build()
    # Use a separate run; an existing team's leader binding is immutable.
    run_id = uuid4().hex
    await recovery.create_run(
        "owner", ResearchSnapshot(run_id=run_id, config_fingerprint="frozen")
    )
    lease = await recovery.acquire(run_id, "owner", ttl=120)
    schema = original.transport.run_table.split('"')[1]
    host = NativeTeamHost(pool, storage, None, recovery_schema=schema)
    team = await host.bind(SimpleNamespace(lease=lease))
    member = await team.add_member("member-1", "researcher", "Evidence")
    restored = await host.bind(SimpleNamespace(lease=lease))
    assert restored.leader_session_id == team.leader_session_id
    assert restored.leader_agent_id == team.leader_agent_id
    assert any(
        item.agent_id == team.identity("member", member)
        for item in await restored.reconcile()
    )
    await host.aclose()
    assert not pool.is_closing()

"""Real native worker dispatch, control handling, artifacts and process death."""

import asyncio
import json
import os
import sys
from dataclasses import asdict

import pytest
from team_worker_fixture import workers
from test_team import env as team_env

from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment
from open_deep_research.agentscope_runtime.team_worker import TaskStopped, WorkerBusy
from open_deep_research.quality.contract import build_research_coverage_contract

pytestmark = pytest.mark.asyncio
env = team_env


def assignment():
    contract = build_research_coverage_contract(
        [{"role": "user", "content": "研究市场增长"}]
    )
    return ResearchAssignment(
        task_id="task-one",
        research_topic="研究市场增长",
        requirement_ids=list(contract.delegable_requirement_ids()),
    ), contract.model_dump(mode="json")


async def test_member_retry_reopens_only_its_safe_started_receipts(env, tmp_path):
    import copy
    from open_deep_research.agentscope_runtime.team_worker import _Worker
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost, RecoveryConflict, UnknownOperation

    build, store, _, pool = env
    team = await build()
    host, _, _ = await workers(team, store, tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    old = _Worker(host, item.task_id)
    await old.claim()
    old_store = copy.copy(store)
    old_store.commit_guard = old.journal_guard
    prefix = f"team-worker:0:{item.task_id}:"
    safe = prefix + "model:researcher:0"
    unsafe = prefix + "tool:write"
    foreign = "team-worker:0:another-task:model:researcher:0"
    await old_store.begin_operation(team.lease, safe, "model", {}, replay_safe=True)
    await old_store.begin_operation(team.lease, unsafe, "tool", {}, replay_safe=False)
    await store.begin_operation(team.lease, foreign, "model", {}, replay_safe=True)
    with pytest.raises(WorkerBusy):
        await _Worker(host, item.task_id).claim()
    with pytest.raises(RecoveryConflict, match="already executing"):
        await old_store.begin_operation(team.lease, safe, "model", {}, replay_safe=True)
    async with pool.acquire() as db:
        await db.execute("UPDATE research_team_tasks SET snapshot=jsonb_set(snapshot,'{_native_worker,expires}','0') WHERE run_id=$1 AND task_id=$2", team.lease.run_id, item.task_id)
    new = _Worker(host, item.task_id)
    await new.claim()
    restored = copy.copy(store)
    restored.commit_guard = new.journal_guard
    assert not (await restored.begin_operation(team.lease, safe, "model", {}, replay_safe=True))["replayed"]
    with pytest.raises(FenceLost):
        await old_store.commit_operation(team.lease, safe, {})
    await restored.commit_operation(team.lease, safe, {"done": True})
    with pytest.raises(UnknownOperation):
        await restored.begin_operation(team.lease, unsafe, "tool", {}, replay_safe=True)
    with pytest.raises(RecoveryConflict, match="already executing"):
        await store.begin_operation(team.lease, foreign, "model", {}, replay_safe=True)
    await host.aclose()


async def test_native_worker_teamsay_handoff_artifact_and_replay(env, tmp_path):
    build, store, storage, _ = env
    team = await build()
    host, factory, marker = await workers(team, store, tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    owner = (await team.service.tasks(team.lease.run_id))[0]["owner"]
    await team.say(team.leader, "feedback-one", owner, "核对增长数据的年份")
    await team.say(team.leader, "feedback-one", owner, "核对增长数据的年份")
    outcome = await host.dispatch(item, contract)
    assert outcome.compressed_research, json.dumps(
        outcome.assessment, ensure_ascii=True
    )
    assert outcome.assessment["handoff"]["accepted"]
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["_team_feedback"] == ["核对增长数据的年份"]
    assert any(
        "核对增长数据的年份" in (message.get_text_content() or "")
        for model in factory.instances
        for call in model.calls
        for message in call["messages"]
    )
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    messages = await team.pending("lead")
    assert (
        sum(
            event.type == "send_message" and event.payload["message"] == "Evidence ready"
            for event in messages
        )
        == 1
    )
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["result_artifact_sha256"]
    native = await storage.get_session(
        team.lease.user_id, "", team.identity("task", item.task_id)
    )
    assert (
        native.state.context
        and native.state.tasks_context.tasks[0].state == "completed"
    )
    assert await host.execute(item.task_id) == outcome
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    from pathlib import Path

    Path(row["result_artifact_path"]).write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        await host.artifact(item.task_id)


async def test_active_worker_single_claim_control_cancel_and_cleanup(env, tmp_path):
    build, store, _, _ = env
    team = await build()
    host, factory, _ = await workers(team, store, tmp_path, slow=True)
    item, contract = assignment()
    active = asyncio.create_task(host.dispatch(item, contract))
    await asyncio.wait_for(factory.entered.wait(), 10)
    with pytest.raises(WorkerBusy):
        await host.execute(item.task_id)
    row = (await team.service.tasks(team.lease.run_id))[0]
    await team.command("stop-now", "cancel_request", {"to": row["owner"]})
    with pytest.raises(TaskStopped):
        await asyncio.wait_for(active, 5)
    assert (await team.service.tasks(team.lease.run_id))[0]["status"] == "cancelled"
    await host.aclose()
    assert not host._active


@pytest.mark.parametrize(
    "window",
    [
        "tool_planned",
        "tool_committed",
        "completion_committed",
        "handoff_prepared",
        "handoff_committed",
        "artifact_written",
    ],
)
async def test_killed_worker_recovers_without_duplicate_tool(
    env, tmp_path, pg_url, window
):
    build, store, _, pool = env
    team = await build()
    host, _, marker = await workers(team, store, tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    async with pool.acquire() as db:
        schema = await db.fetchval("SELECT current_schema()")
    ready = tmp_path / "ready.txt"
    request = tmp_path / "worker.json"
    request.write_text(
        json.dumps(
            {
                "schema": schema,
                "url": pg_url,
                "lease": asdict(team.lease),
                "leader_agent_id": team.leader_agent_id,
                "leader_session_id": team.leader_session_id,
                "root": str(tmp_path),
                "task_id": item.task_id,
                "window": window,
                "ready": str(ready),
            }
        ),
        encoding="utf-8",
    )
    from pathlib import Path

    child_env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [str(Path("src").resolve()), str(Path("tests/as_runtime").resolve())]
        ),
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/as_runtime/team_worker_fixture.py",
        str(request),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if ready.exists() or child.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if not ready.exists():
            child.kill()
            output = await child.communicate()
            pytest.fail(repr(output))
        child.kill()
        await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.communicate()
    await asyncio.sleep(1.7)
    outcome = await host.execute(item.task_id)
    assert outcome.assessment["handoff"]["accepted"]
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    assert (await team.service.tasks(team.lease.run_id))[0]["result_artifact_sha256"]


async def test_external_consumer_drains_on_close_and_task_result_survives(
    env, tmp_path
):
    build, store, _, _ = env
    team = await build()
    sender, _, _ = await workers(team, store, tmp_path)
    sender.external = True
    consumer, _, _ = await workers(team, store, tmp_path)
    service = asyncio.create_task(consumer.serve())
    try:
        item, contract = assignment()
        result = await asyncio.wait_for(sender.dispatch(item, contract), 15)
        assert result.compressed_research
    finally:
        await consumer.aclose()
        await sender.aclose()
    assert service.done() and not consumer._active


async def test_superseded_worker_cannot_commit_model_receipt(env, tmp_path, record_property):
    from sqlalchemy import select

    from open_deep_research.agentscope_runtime.recovery_store import FenceLost

    build, store, _, pool = env
    team = await build()
    host, factory, _ = await workers(team, store, tmp_path, slow=True)
    item, contract = assignment()
    running = asyncio.create_task(host.dispatch(item, contract))
    await asyncio.wait_for(factory.entered.wait(), 10)
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE research_team_tasks SET snapshot=jsonb_set(snapshot,'{_native_worker,token}','\"new-executor\"') WHERE run_id=$1",
            team.lease.run_id,
        )
    with pytest.raises(FenceLost):
        await asyncio.wait_for(running, 5)
    async with store.engine.connect() as conn:
        records = (
            (
                await conn.execute(
                    select(store.ops.c.kind, store.ops.c.state).where(
                        store.ops.c.run_id == team.lease.run_id
                    )
                )
            )
            .all()
        )
    record_property("operation_states", json.dumps([list(row) for row in records]))
    # Coordination/context receipts committed before takeover remain valid;
    # only the in-flight model result is fenced by this scenario.
    model_states = [state for kind, state in records if kind.startswith("model:")]
    assert model_states
    assert "committed" not in model_states
    assert not (await team.service.tasks(team.lease.run_id))[0].get("_worker_error")


async def test_supervisor_dispatches_native_worker_and_merges_admitted_coverage(
    env, tmp_path
):
    from test_research_migration import ScriptedModel, tool_call

    from open_deep_research.agentscope_runtime.research_agents import Supervisor

    build, store, _, _ = env
    team = await build()
    host, factory, _ = await workers(team, store, tmp_path, quality_enabled=True)
    item, contract = assignment()
    original_build = factory.build

    def build_model(role):
        if role == "supervisor":
            return ScriptedModel(
                [
                    [
                        tool_call(
                            "TaskCreate",
                            "delegate",
                            research_topic=item.research_topic,
                            requirement_ids=item.requirement_ids,
                        )
                    ],
                    [tool_call("WaitForTeamEvents", "wait")],
                    [tool_call("ResearchComplete", "finish")],
                ]
            )
        return original_build(role)

    factory.build = build_model
    config = host.researcher.config_provider()
    config["configurable"]["enable_async_research"] = True
    config["configurable"]["max_researcher_iterations"] = 8
    lead = Supervisor(
        host.researcher.models,
        lambda: config,
        host.researcher,
        run_id=team.lease.run_id,
        quality=host.quality,
        team_workers=host,
    )
    with host.recovery.scope("research_supervisor", 0):
        outcomes, state = await lead.run("研究市场增长", contract)
    assert len(outcomes) == 1 and outcomes[0]["evidence_registry"]
    assert state["middle_context"]["coverage_ledger"]
    assert all(
        row["status"] == "supported"
        for row in state["middle_context"]["coverage_ledger"].values()
    )
    assert any(
        "Evidence ready" in text
        for text in host.recovery.snapshot.feedback_by_task["supervisor"]
    )
    assert not await team.pending("lead")


async def test_runtime_shutdown_cancels_active_consumer_before_borrowed_storage(
    env, tmp_path
):
    from open_deep_research.agentscope_runtime.app import ASRuntime
    from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

    build, store, storage, pool = env
    team = await build()
    host, factory, _ = await workers(team, store, tmp_path, slow=True)
    item, contract = assignment()
    await host.prepare(item, contract)
    runtime = ASRuntime(ASRuntimeSettings(None, "unused", True, "test_"), storage, None)
    service = runtime.start_team_workers(host)
    await asyncio.wait_for(factory.entered.wait(), 10)
    await asyncio.wait_for(runtime.aclose(), 5)
    assert service.done() and host.closed and not host._active
    async with pool.acquire() as db, db.transaction():
        await team.transport.guard(db)
    with pytest.raises(RuntimeError, match="shutting_down"):
        runtime.start_team_workers(host)


async def test_durable_entry_rebinds_team_to_new_leader_lease(env, tmp_path):
    from types import SimpleNamespace

    from open_deep_research.agentscope_runtime.research import (
        open_durable_research_pipeline,
    )
    from open_deep_research.agentscope_runtime.team import (
        FencedTeamTransport,
        NativeResearchTeam,
        research_member_template,
    )
    from open_deep_research.tools.base import ToolExecutionZone

    build, store, storage, pool = env
    old = await build()
    host, factory, _ = await workers(old, store, tmp_path)
    item, contract = assignment()
    result = await host.dispatch(item, contract)
    await store.release(old.lease)
    rebound = []

    async def bind(recovery):
        async with pool.acquire() as db:
            schema = await db.fetchval("SELECT current_schema()")
        team = NativeResearchTeam(
            storage,
            FencedTeamTransport(pool, recovery.lease, recovery_schema=schema),
            leader_agent_id=old.leader_agent_id,
            leader_session_id=old.leader_session_id,
            template=research_member_template(),
        )
        await team.create("research", "frozen goal")
        rebound.append(team)
        return team

    flow = await open_durable_research_pipeline(
        store=store,
        user_id=old.lease.user_id,
        run_id=old.lease.run_id,
        team_factory=bind,
        team_artifact_dir=tmp_path,
        run_config=SimpleNamespace(
            compatibility_projection=lambda: {
                "metadata": {"run_config_fingerprint": "frozen"}
            }
        ),
        model_factory=factory,
        config_provider=host.researcher.config_provider,
        tools_for=host.researcher.tools_for,
        checkpoint_path=tmp_path / "unused.json",
        local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
    )
    try:
        assert rebound[0].lease.fence > old.lease.fence
        restored, _, marker = await workers(rebound[0], store, tmp_path)
        assert await restored.execute(item.task_id) == result
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        await flow.recovery.close()

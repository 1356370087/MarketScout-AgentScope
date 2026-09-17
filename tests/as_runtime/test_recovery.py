"""M6 durable state, operation windows, fencing, decisions and accounting."""

import asyncio
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from agentscope.message import UserMsg
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select, update
from test_research_migration import Stages, consume

from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryConflict,
    RecoveryStore,
    UnknownOperation,
)
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
    ResearchSnapshot,
)
from open_deep_research.budgets import BudgetExhausted, DeadlineExceeded
from open_deep_research.tools.base import ToolEffect, ToolResult
from open_deep_research.tools.governance import (
    GovernedToolCallResult,
    ToolOutcomeMessage,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path):
    value = RecoveryStore(
        "sqlite+aiosqlite:///" + (tmp_path / "recovery.db").as_posix()
    )
    await value.create_tables()
    yield value
    await value.aclose()


async def create(store, **kwargs):
    state = ResearchSnapshot(
        run_id=uuid4().hex, config_fingerprint="frozen", messages=[UserMsg("user", "q")]
    )
    await store.create_run("owner", state, **kwargs)
    lease = await store.acquire(state.run_id, "owner")
    return state, lease


@pytest.mark.parametrize(
    "stage", ["memory_extract_and_write", "final_report_generation"]
)
@pytest.mark.parametrize("window", ["effect_returned", "operation_committed"])
async def test_external_stage_port_never_repeats_unknown_or_committed_effect(
    store, stage, window
):
    from open_deep_research.agentscope_runtime.recovery import RecoveryStages

    state, lease = await create(store)
    calls = []

    class Ports:
        write_memory = True
        report_writer = True

        async def execute(self, stage, snapshot):
            calls.append(stage)
            snapshot.final_report = "persisted report"

    async def crash(point):
        if point == window:
            raise RuntimeError("crash")

    first = RecoverySession(store, lease, state, failpoint=crash)
    with pytest.raises(RuntimeError, match="crash"):
        await RecoveryStages(Ports(), first).execute(stage, state.model_copy(deep=True))
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    try:
        replayed = second.snapshot.model_copy(deep=True)
        if window == "effect_returned":
            with pytest.raises(UnknownOperation):
                await RecoveryStages(Ports(), second).execute(stage, replayed)
        else:
            await RecoveryStages(Ports(), second).execute(stage, replayed)
            assert replayed.final_report == "persisted report"
        assert calls == [stage]
    finally:
        await second.close()


async def test_checkpoint_version_identity_and_no_credentials(store):
    state, lease = await create(store)
    state.agent_states["worker"] = {
        "version": 1,
        "context": [],
        "credential_reference": "run-key",
    }
    await store.save(lease, state)
    loaded, revision = await store.load(state.run_id, "owner")
    assert loaded == state and revision == 1
    with pytest.raises(KeyError):
        await store.load(state.run_id, "other")
    state.agent_states["api_key"] = "forbidden"
    with pytest.raises(ValueError, match="credential"):
        await store.save(lease, state)
    async with store.engine.begin() as conn:
        await conn.execute(update(store.runs).values(version=2))
    with pytest.raises(ValueError, match="version"):
        await store.load(state.run_id, "owner")


async def test_fencing_expiry_takeover_and_late_release(store):
    state, old = await create(store)
    with pytest.raises(FenceLost):
        await store.acquire(state.run_id, "owner")
    async with store.engine.begin() as conn:
        await conn.execute(update(store.runs).values(expires=0))
    new = await store.acquire(state.run_id, "owner")
    assert new.fence > old.fence
    await store.release(old)
    with pytest.raises(FenceLost):
        await store.save(old, state)
    await store.save(new, state)


async def test_committed_tool_is_replayed_without_calling_effect(store):
    state, lease = await create(store)
    count = 0

    async def effect():
        nonlocal count
        count += 1
        return {"result": count}

    first = RecoverySession(store, lease, state)
    with first.scope("research", 0):
        assert await first.operation("tool", {"args": 1}, effect) == {"result": 1}
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    with second.scope("research", 0):
        assert await second.operation("tool", {"args": 1}, effect) == {"result": 1}
    assert count == 1


@pytest.mark.parametrize("safe", [False, True])
async def test_unknown_effect_is_quarantined_unless_explicitly_replay_safe(store, safe):
    state, lease = await create(store)
    await store.begin_operation(lease, "op", "tool", {"a": 1}, replay_safe=safe)
    await store.release(lease)
    current = await store.acquire(state.run_id, "owner")
    if safe:
        result = await store.begin_operation(
            current, "op", "tool", {"a": 1}, replay_safe=True
        )
        assert not result["replayed"]
    else:
        with pytest.raises(UnknownOperation):
            await store.begin_operation(current, "op", "tool", {"a": 1})
        await store.resolve_operation(current, "op", result={"verified": True})
        result = await store.begin_operation(current, "op", "tool", {"a": 1})
        assert result["replayed"]
    with pytest.raises(RecoveryConflict):
        await store.begin_operation(current, "op", "tool", {"a": 2})


async def test_old_fence_cannot_commit_tool_receipt(store):
    state, old = await create(store)
    await store.begin_operation(old, "op", "tool", {}, replay_safe=True)
    await store.release(old)
    new = await store.acquire(state.run_id, "owner")
    await store.begin_operation(new, "op", "tool", {}, replay_safe=True)
    with pytest.raises(FenceLost):
        await store.commit_operation(old, "op", {"stale": True})
    await store.commit_operation(new, "op", {"ok": True})


async def test_six_dimension_atomic_reservations_and_idempotent_settlement(store):
    dimensions = [
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cost_micro_usd",
        "fetch_calls",
    ]
    state, lease = await create(store, limits={key: 10 for key in dimensions})
    results = await asyncio.gather(
        *[
            store.begin_operation(
                lease, str(i), "model", {}, reserve={key: 6 for key in dimensions}
            )
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(value, BudgetExhausted) for value in results) == 1
    success = next(
        str(i) for i, result in enumerate(results) if isinstance(result, dict)
    )
    await store.commit_operation(
        lease, success, {"ok": True}, actual={key: 3 for key in dimensions}
    )
    await store.commit_operation(
        lease, success, {"ok": True}, actual={key: 3 for key in dimensions}
    )
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"] == {key: 3 for key in dimensions}
    assert all(value == 0 for value in budget["reserved"].values())


async def test_known_no_effect_releases_reservation_and_deadline_blocks_new_call(store):
    _state, lease = await create(store, limits={"tool_calls": 1})
    await store.begin_operation(lease, "one", "tool", {}, reserve={"tool_calls": 1})
    await store.resolve_operation(lease, "one", not_executed=True)
    await store.begin_operation(lease, "two", "tool", {}, reserve={"tool_calls": 1})
    await store.commit_operation(lease, "two", {})
    async with store.engine.begin() as conn:
        await conn.execute(update(store.runs).values(deadline=time.time() - 10))
    assert (await store.begin_operation(lease, "two", "tool", {}))["replayed"]
    with pytest.raises(DeadlineExceeded):
        await store.begin_operation(lease, "three", "model", {})


async def test_outbox_projection_and_cursor_rollback_together(store):
    state, lease = await create(store)
    await store.begin_operation(lease, "one", "tool", {})
    await store.commit_operation(lease, "one", {"done": True}, actual={"tool_calls": 1})
    projection = Table(
        "test_projection",
        MetaData(),
        Column("event_id", String, primary_key=True),
        Column("count", Integer),
    )
    async with store.engine.begin() as conn:
        await conn.run_sync(projection.metadata.create_all)

    async def fail(conn, event):
        await conn.execute(
            insert(projection).values(event_id=event["event_id"], count=1)
        )
        raise RuntimeError("crash before cursor")

    with pytest.raises(RuntimeError):
        await store.project(lease, "usage", fail)

    async def apply(conn, event):
        await conn.execute(
            insert(projection).values(event_id=event["event_id"], count=1)
        )

    assert await store.project(lease, "usage", apply) == 1
    assert await store.project(lease, "usage", apply) == 0
    events = await store.events(state.run_id, "owner")
    assert len(events) == 1


async def test_decision_is_atomic_with_checkpoint_and_replay_is_stale_safe(store):
    state, lease = await create(store)
    state.completed = [
        "summarize_messages",
        "memory_recall",
        "clarify_with_user",
        "write_research_brief",
    ]
    state.pending = PendingDecision(stage="plan_approval", question="q")
    state.status = "waiting"
    await store.save(lease, state)
    action_id = state.pending.id
    await store.submit_decision(
        state.run_id, "owner", "cmd", action_id, {"action": "approve"}
    )
    await store.release(lease)
    session = await RecoverySession.open(store, state.run_id, "owner")
    flow = ResearchPipeline(
        session.snapshot,
        Stages(),
        session.save,
        config_fingerprint="frozen",
        recovery=session,
    )
    await session.consume_decisions(flow)
    assert flow.state.pending is None
    assert not await store.pending_decisions(session.lease)
    assert (
        await store.submit_decision(
            state.run_id, "owner", "cmd", action_id, {"action": "approve"}
        )
        == "applied"
    )
    with pytest.raises(RecoveryConflict):
        await store.submit_decision(
            state.run_id, "owner", "cmd", action_id, {"action": "cancel"}
        )


async def test_multiple_operation_approvals_preserve_remaining_items(store):
    state, lease = await create(store)
    state.approvals = {
        "one": {"kind": "tool", "payload": {"call_id": "a"}},
        "two": {"kind": "egress", "payload": {"domain": "example.test"}},
    }
    state.status = "waiting"
    session = RecoverySession(store, lease, state)
    flow = ResearchPipeline(
        state, Stages(), session.save, config_fingerprint="frozen", recovery=session
    )
    await flow.decide("one", "approve")
    assert set(flow.state.approvals) == {"two"}
    assert flow.state.status == "waiting"
    await flow.decide("two", "cancel")
    assert flow.state.status == "cancelled"


async def test_process_cancel_is_recoverable_and_user_cancel_is_terminal(store):
    from agentscope.event import UserInterruptEvent

    started = asyncio.Event()

    class Waiting:
        async def execute(self, stage, state):
            started.set()
            await asyncio.Event().wait()

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    flow = ResearchPipeline(
        state, Waiting(), session.save, config_fingerprint="frozen", recovery=session
    )
    task = asyncio.create_task(consume(flow))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    loaded, _ = await store.load(state.run_id, "owner")
    assert loaded.status == "ready" and loaded.error == "process_interrupted"
    await consume(flow, UserInterruptEvent(reply_id=flow.state.reply_id))
    loaded, _ = await store.load(state.run_id, "owner")
    assert loaded.status == "cancelled"


async def test_pg_cross_instance_fence_and_committed_receipt(pg_url):
    first, second = RecoveryStore(pg_url), RecoveryStore(pg_url)
    try:
        await first.create_tables()
        state, old = await create(first)
        await first.begin_operation(old, "op", "tool", {})
        await first.commit_operation(old, "op", {"value": 1})
        await first.release(old)
        new = await second.acquire(state.run_id, "owner")
        assert (await second.begin_operation(new, "op", "tool", {}))["result"] == {
            "value": 1
        }
        with pytest.raises(FenceLost):
            await first.save(old, state)
    finally:
        await first.aclose()
        await second.aclose()


async def test_model_result_commits_before_replay_and_cost_is_settled_once(store):
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse, ChatUsage

    state, lease = await create(store, limits={"cost_micro_usd": 10000})
    count = 0

    async def call():
        nonlocal count
        count += 1
        return ChatResponse(
            content=[TextBlock(text="answer")],
            is_last=True,
            usage=ChatUsage(input_tokens=2, output_tokens=3, time=0.01),
        )

    first = RecoverySession(store, lease, state)
    with first.scope("brief", 0):
        await first.model(
            "supervisor", [UserMsg("user", "q")], call, max_tokens=10, pricing=(1, 2)
        )
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    with second.scope("brief", 0):
        response = await second.model(
            "supervisor", [UserMsg("user", "q")], call, max_tokens=10, pricing=(1, 2)
        )
    assert response.content[0].text == "answer" and count == 1
    assert (await store.budget(state.run_id, "owner"))["used"]["cost_micro_usd"] == 8


async def test_tool_receipt_precedes_observation_and_unknown_write_is_not_repeated(
    store,
):
    from test_research_migration import Empty

    from open_deep_research.tools.base import (
        ToolEffect,
        ToolOrigin,
        ToolResult,
        build_tool,
    )
    from open_deep_research.tools.governance import (
        GovernedToolCallResult,
        ToolOutcomeMessage,
    )

    state, lease = await create(store)
    count = 0

    async def call(*args):
        nonlocal count
        count += 1
        return ToolResult(output={"written": True})

    tool = build_tool(
        name="write",
        input_schema=Empty,
        description="write",
        call=call,
        origin=ToolOrigin.SYSTEM,
        effect=ToolEffect.EXTERNAL_WRITE,
    )

    async def handler():
        result = await call()
        return GovernedToolCallResult(ToolOutcomeMessage("ok", "write", "one"), result)

    async def crash(point):
        if point == "operation_committed":
            raise RuntimeError("process crash after receipt")

    first = RecoverySession(store, lease, state, failpoint=crash)
    with first.scope("research", 0), pytest.raises(RuntimeError):
        await first.tool(tool, "one", {}, handler)
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    with second.scope("research", 0):
        result = await second.tool(tool, "one", {}, handler)
    assert result.result.output == {"written": True} and count == 1


async def test_budget_approval_and_feedback_are_consumed_once(store):
    state, lease = await create(store, limits={"tool_calls": 1})
    state.status = "waiting"
    state.approvals = {
        "budget": {"kind": "budget", "payload": {"dimension": "tool_calls"}}
    }
    await store.save(lease, state)
    await store.register_task(lease, "task-1")
    await store.submit_decision(
        state.run_id,
        "owner",
        "raise",
        "budget",
        {"action": "approve", "limits": {"tool_calls": 5}},
    )
    await store.submit_decision(
        state.run_id,
        "owner",
        "feedback",
        "feedback:task-1",
        {"action": "feedback", "task_id": "task-1", "feedback": "只研究国内"},
    )
    with pytest.raises(RecoveryConflict):
        await store.submit_decision(
            state.run_id,
            "owner",
            "bad",
            "feedback:other",
            {"action": "feedback", "task_id": "other", "feedback": "wrong"},
        )
    session = RecoverySession(store, lease, state)
    flow = ResearchPipeline(
        state, Stages(), session.save, config_fingerprint="frozen", recovery=session
    )
    await session.consume_decisions(flow)
    await session.consume_decisions(flow)
    assert flow.state.feedback_by_task == {"task-1": ["只研究国内"]}
    assert (await store.budget(state.run_id, "owner"))["limits"]["tool_calls"] == 5


async def test_exact_tool_approval_cannot_authorize_changed_arguments(store):
    from open_deep_research.agentscope_runtime.recovery_store import digest

    state, lease = await create(store)
    state.approval_grants = {
        "action": {
            "kind": "tool",
            "payload": {
                "operation_key": "outside:pipeline:tool:c",
                "tool_name": "write",
                "call_id": "c",
                "arguments_digest": digest({"path": "one"}),
            },
        }
    }
    session = RecoverySession(store, lease, state)
    assert session.tool_config({}, "write", "c", {"path": "one"})["metadata"][
        "approved_sensitive_tool_call_ids"
    ] == ["c"]
    assert (
        session.tool_config({}, "write", "c", {"path": "two"})["metadata"][
            "approved_sensitive_tool_call_ids"
        ]
        == []
    )


async def test_failed_decision_never_publishes_wakeup(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from open_deep_research.agentscope_runtime.app import ASRuntime

    state, _ = await create(store)
    runtime = SimpleNamespace(message_bus=SimpleNamespace(queue_push=AsyncMock()))
    with pytest.raises(RecoveryConflict):
        await ASRuntime.submit_research_decision(
            runtime,
            store,
            run_id=state.run_id,
            user_id="owner",
            command_id="c",
            action_id="stale",
            payload={"action": "approve"},
        )
    runtime.message_bus.queue_push.assert_not_called()


@pytest.mark.parametrize("crash_at", ["effect_returned", "operation_committed"])
async def test_hard_process_exit_requires_no_finally_to_preserve_receipt(
    tmp_path, crash_at
):
    import sys
    from pathlib import Path

    database = "sqlite+aiosqlite:///" + (tmp_path / "crash.db").as_posix()
    value = RecoveryStore(database)
    await value.create_tables()
    state, lease = await create(value)
    await value.release(lease)
    marker = tmp_path / "external-effect.txt"
    worker = Path(__file__).with_name("recovery_crash_fixture.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(worker),
        database,
        state.run_id,
        str(marker),
        crash_at,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 73, errors.decode(errors="replace")
    await asyncio.sleep(0.25)
    session = await RecoverySession.open(value, state.run_id, "owner")

    async def must_not_repeat():
        pytest.fail("external write must not repeat")

    with session.scope("research", 0):
        if crash_at == "effect_returned":
            with pytest.raises(UnknownOperation):
                await session.operation("write", {}, must_not_repeat)
        else:
            assert await session.operation("write", {}, must_not_repeat) == {
                "written": True
            }
    assert marker.read_text(encoding="utf-8") == "effect\n"
    await session.close()
    await value.aclose()


async def test_native_agent_restart_replays_model_and_tool_without_duplicate_effect(
    store,
):
    from agentscope.agent import Agent, ReActConfig
    from agentscope.message import TextBlock
    from test_research_migration import Empty, ScriptedModel, cfg, tool_call

    from open_deep_research.agentscope_runtime.recovery import JournalMiddleware
    from open_deep_research.agentscope_runtime.research_agents import _reply
    from open_deep_research.agentscope_runtime.tools import (
        ToolGovernanceMiddleware,
        prepare_toolkit,
    )
    from open_deep_research.tools.base import (
        ToolEffect,
        ToolExecutionZone,
        ToolOrigin,
        ToolResult,
        build_tool,
    )
    from open_deep_research.tools.governance import AgentRole

    state, lease = await create(store)
    count = 0

    async def write(*args):
        nonlocal count
        count += 1
        return ToolResult(output="written")

    tool = build_tool(
        name="write_fixture",
        input_schema=Empty,
        description="write",
        call=write,
        origin=ToolOrigin.SYSTEM,
        effect=ToolEffect.EXTERNAL_WRITE,
        execution_zone=ToolExecutionZone.HOST_CONTROL,
    )
    first_model = ScriptedModel(
        [[tool_call("write_fixture", "write-id")], [TextBlock(text="done")]]
    )

    async def run(session, model):
        toolkit = await prepare_toolkit(
            [tool],
            role=AgentRole.RESEARCHER,
            config_provider=lambda: cfg(require_sensitive_tool_approval=False),
            run_id=state.run_id,
            task_id="worker",
            local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
        )
        toolkit.journal = session
        agent = Agent(
            name="worker",
            model=model,
            toolkit=toolkit,
            system_prompt="write once",
            react_config=ReActConfig(max_iters=2),
            middlewares=[
                JournalMiddleware(session, "researcher", 100),
                ToolGovernanceMiddleware(),
            ],
        )
        with session.scope("research", 0):
            return await _reply(agent, [UserMsg("user", "write once")])

    first = RecoverySession(store, lease, state)
    await run(first, first_model)
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    no_calls = ScriptedModel([[TextBlock(text="must not call")]])
    result, _ = await run(second, no_calls)
    assert result.get_text_content() == "done"
    assert count == 1 and not no_calls.calls


async def test_user_cancel_revokes_active_executor_and_cannot_be_overwritten(store):
    state, lease = await create(store)
    await store.request_cancel(state.run_id, "owner", "cancel-once")
    await store.request_cancel(state.run_id, "owner", "cancel-once")
    with pytest.raises(FenceLost):
        await store.save(lease, state)
    snapshot, _ = await store.load(state.run_id, "owner")
    assert snapshot.status == "cancelled" and snapshot.error == "user_cancelled"
    assert (
        sum(
            e["payload"]["type"] == "research.cancelled"
            for e in await store.events(state.run_id, "owner")
        )
        == 1
    )


async def test_multiple_native_confirmations_are_consumed_durably(store):
    from agentscope.event import ConfirmResult, UserConfirmResultEvent
    from agentscope.message import ToolCallBlock

    state, lease = await create(store)
    state.approvals = {
        key: {"kind": "tool", "payload": {"call_id": key}}
        for key in ("one", "two", "three")
    }
    state.status = "waiting"
    await store.save(lease, state)
    session = RecoverySession(store, lease, state)
    flow = ResearchPipeline(
        state, Stages(), session.save, config_fingerprint="frozen", recovery=session
    )
    await consume(
        flow,
        UserConfirmResultEvent(
            reply_id=state.reply_id,
            confirm_results=[
                ConfirmResult(
                    confirmed=True,
                    tool_call=ToolCallBlock(id=key, name="tool", input="{}"),
                )
                for key in ("one", "two")
            ],
        ),
    )
    stored, _ = await store.load(state.run_id, "owner")
    assert set(stored.approvals) == {"three"}
    assert len(stored.approval_grants) == 2
    assert not await store.pending_decisions(lease)


async def test_complete_pipeline_recovers_after_tool_commit_without_repeating_tool(
    store, tmp_path
):
    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse, StructuredResponse
    from test_research_migration import Empty, ScriptedModel, cfg, evidence, tool_call

    from open_deep_research.agentscope_runtime.model_policy import (
        ModelCallPolicy,
        ModelPolicyMiddleware,
    )
    from open_deep_research.agentscope_runtime.research import build_research_pipeline
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.tools.base import (
        ToolExecutionZone,
        ToolOrigin,
        ToolResult,
        build_tool,
    )

    class ProcessCrash(BaseException):
        pass

    config = cfg(max_researcher_iterations=4, quality_evaluation_min_sources=1)
    run_config = RunConfig.compile(config)
    run_id = uuid4().hex
    await store.create_from_config(
        "owner", run_id, run_config, messages=[UserMsg("user", "市场规模")]
    )
    calls = 0

    class Model(ScriptedModel):
        async def generate_structured_output(self, messages, schema):
            return StructuredResponse(content={"research_brief": "市场规模"})

    class Factory:
        run = SimpleNamespace(get=lambda name: {})

        def __init__(self):
            self.models = {
                "supervisor": Model(
                    [
                        [
                            tool_call(
                                "ConductResearch", "delegate", research_topic="市场规模"
                            )
                        ],
                        [tool_call("ResearchComplete", "lead-done")],
                    ]
                ),
                "researcher": Model(
                    [
                        [tool_call("web_research", "search")],
                        [tool_call("ResearchComplete", "worker-done")],
                    ]
                ),
            }

        def build(self, role):
            return self.models[role]

        def descriptor(self, role):
            return {"model": "fixture", "max_output_tokens": 1000}

        def policy_middleware(self, role, candidates=None):
            return ModelPolicyMiddleware(
                ModelCallPolicy(candidates or [self.build(role)], circuit_enabled=False)
            )

        async def complete_with_recovery(self, *args, **kwargs):
            return ChatResponse(
                content=[TextBlock(text="报告明确基于[测试证据](https://example.test/source)。")], is_last=True
            )

    async def call(*args):
        nonlocal calls
        calls += 1
        return ToolResult(output={"evidence": [evidence()]})

    async def tools_for(assignment):
        return [
            build_tool(
                name="web_research",
                input_schema=Empty,
                description="research",
                call=call,
                origin=ToolOrigin.SYSTEM,
                execution_zone=ToolExecutionZone.HOST_CONTROL,
            )
        ]

    async def crash(point):
        if point == "operation_committed":
            async with store.engine.connect() as conn:
                committed = await conn.scalar(
                    select(store.ops.c.key).where(
                        store.ops.c.run_id == run_id,
                        store.ops.c.kind == "tool",
                        store.ops.c.state == "committed",
                    )
                )
            if committed:
                raise ProcessCrash()

    session = await RecoverySession.open(store, run_id, "owner", failpoint=crash)
    kwargs = {
        "run_id": run_id,
        "run_config": run_config,
        "config_provider": lambda: config,
        "tools_for": tools_for,
        "checkpoint_path": tmp_path / "unused.json",
        "local_zones": frozenset({ToolExecutionZone.HOST_CONTROL}),
    }
    flow = build_research_pipeline(**kwargs, model_factory=Factory(), recovery=session)
    with pytest.raises(ProcessCrash):
        await consume(flow)
    assert calls == 1
    await session.close()
    second = await RecoverySession.open(store, run_id, "owner")
    resumed = build_research_pipeline(
        **kwargs, model_factory=Factory(), recovery=second
    )
    await consume(resumed)
    assert resumed.state.status == "completed" and calls == 1
    assert not (tmp_path / "unused.json").exists()


async def test_sensitive_tool_pause_restart_approval_executes_once(store):
    from agentscope.agent import Agent
    from agentscope.message import TextBlock
    from test_research_migration import Empty, ScriptedModel, cfg, tool_call

    from open_deep_research.agentscope_runtime.recovery import (
        JournalMiddleware,
        RecoveryStages,
    )
    from open_deep_research.agentscope_runtime.research_agents import _reply
    from open_deep_research.agentscope_runtime.tools import (
        ToolGovernanceMiddleware,
        prepare_toolkit,
    )
    from open_deep_research.tools.base import (
        ToolEffect,
        ToolExecutionZone,
        ToolOrigin,
        ToolResult,
        build_tool,
    )
    from open_deep_research.tools.governance import AgentRole

    state, lease = await create(store)
    count = 0

    async def write(*args):
        nonlocal count
        count += 1
        return ToolResult(output="written")

    tool = build_tool(
        name="sensitive_write",
        input_schema=Empty,
        description="write",
        call=write,
        origin=ToolOrigin.SYSTEM,
        effect=ToolEffect.EXTERNAL_WRITE,
        execution_zone=ToolExecutionZone.HOST_CONTROL,
    )

    class Steps(Stages):
        def __init__(self, session, model):
            super().__init__()
            self.session, self.model = session, model

        async def execute(self, stage, state):
            if stage == "summarize_messages":
                toolkit = await prepare_toolkit(
                    [tool],
                    role=AgentRole.RESEARCHER,
                    config_provider=lambda: cfg(),
                    run_id=state.run_id,
                    task_id="worker",
                    local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
                )
                toolkit.journal = self.session
                agent = Agent(
                    name="worker",
                    model=self.model,
                    toolkit=toolkit,
                    system_prompt="write",
                    middlewares=[
                        JournalMiddleware(self.session, "researcher", 100),
                        ToolGovernanceMiddleware(),
                    ],
                )
                await _reply(agent, [UserMsg("user", "write")])
            await super().execute(stage, state)

    first = RecoverySession(store, lease, state)
    inner = Steps(first, ScriptedModel([[tool_call("sensitive_write", "sensitive")]]))
    flow = ResearchPipeline(
        state,
        RecoveryStages(inner, first),
        first.save,
        config_fingerprint="frozen",
        recovery=first,
    )
    await consume(flow)
    assert flow.state.status == "waiting" and count == 0
    action_id = next(iter(flow.state.approvals))
    await store.submit_decision(
        state.run_id, "owner", "approve", action_id, {"action": "approve"}
    )
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    flow = ResearchPipeline(
        second.snapshot,
        RecoveryStages(
            Steps(second, ScriptedModel([[TextBlock(text="done")]])), second
        ),
        second.save,
        config_fingerprint="frozen",
        recovery=second,
    )
    await second.consume_decisions(flow)
    await consume(flow)
    assert count == 1 and flow.state.status == "completed"


async def test_egress_authority_failure_keeps_decision_pending(store):
    state, lease = await create(store)
    state.status = "waiting"
    state.approvals = {
        "egress": {"kind": "egress", "payload": {"detail": {"domain": "example.test"}}}
    }
    await store.save(lease, state)
    await store.submit_decision(
        state.run_id, "owner", "cmd", "egress", {"action": "approve"}
    )

    async def authority(action_id, payload):
        raise OSError("authority unavailable")

    session = RecoverySession(store, lease, state, approval_applier=authority)
    flow = ResearchPipeline(
        state, Stages(), session.save, config_fingerprint="frozen", recovery=session
    )
    with pytest.raises(OSError):
        await session.consume_decisions(flow)
    assert flow.state.status == "waiting"
    assert len(await store.pending_decisions(lease)) == 1

    async def recover(action_id, payload):
        assert action_id == "egress"

    session.approval_applier = recover
    await session.consume_decisions(flow)
    assert flow.state.status == "ready"


async def test_task_feedback_enters_next_new_model_call_and_replays_consistently(store):
    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse
    from agentscope.state import AgentState

    state, lease = await create(store)
    calls = 0

    async def model():
        nonlocal calls
        calls += 1
        return ChatResponse(content=[TextBlock(text="answer")], is_last=True)

    first = RecoverySession(store, lease, state)
    agent = SimpleNamespace(state=AgentState(context=[UserMsg("user", "q")]))
    with first.scope("research", 0), first.task("target"):
        await first.model(
            "researcher", list(agent.state.context), model, agent=agent, max_tokens=10
        )
    state.feedback_by_task = {"target": ["只研究国内"], "other": ["不应泄漏"]}
    await first.save(state)
    await first.close()
    second = await RecoverySession.open(store, state.run_id, "owner")
    agent = SimpleNamespace(state=AgentState(context=[UserMsg("user", "q")]))
    with second.scope("research", 0), second.task("target"):
        await second.model(
            "researcher", list(agent.state.context), model, agent=agent, max_tokens=10
        )
        assert calls == 1
        messages = list(agent.state.context)
        await second.model("researcher", messages, model, agent=agent, max_tokens=10)
        assert "只研究国内" in messages[-1].get_text_content()
        assert "不应泄漏" not in str(messages)
    await second.close()
    third = await RecoverySession.open(store, state.run_id, "owner")
    agent = SimpleNamespace(state=AgentState(context=[UserMsg("user", "q")]))
    with third.scope("research", 0), third.task("target"):
        await third.model(
            "researcher", list(agent.state.context), model, agent=agent, max_tokens=10
        )
        await third.model(
            "researcher", list(agent.state.context), model, agent=agent, max_tokens=10
        )
    assert calls == 2


async def test_public_log_delivery_deduplicates_after_publish_before_cursor_crash(
    store, tmp_path
):
    from open_deep_research.events.public import RunEventPublisher, RunEventStore

    state, lease = await create(store)
    await store.begin_operation(lease, "tool", "tool", {}, reserve={"tool_calls": 1})
    await store.commit_operation(lease, "tool", {"ok": True})
    state.status = "completed"
    await store.save(lease, state)
    log = RunEventStore(state.run_id, runs_dir=str(tmp_path / "public"))
    publisher = RunEventPublisher(log)

    class CrashAfterAppend:
        async def publish(self, *args, **kwargs):
            await publisher.publish(*args, **kwargs)
            raise RuntimeError("crash before cursor")

    with pytest.raises(RuntimeError):
        await store.deliver_public(lease, CrashAfterAppend())
    await store.deliver_public(lease, publisher)
    await store.deliver_public(lease, publisher)
    assert [event.type for event in log.read()] == [
        "run.usage.updated",
        "run.completed",
    ]
    assert log.project().status == "completed"


@pytest.mark.parametrize("error_type", ["validation_error", "network_error"])
async def test_tool_rejection_releases_budget_but_uncertain_write_is_quarantined(
    store, error_type
):
    from test_research_migration import Empty

    from open_deep_research.tools.base import ToolEffect, ToolOrigin, build_tool
    from open_deep_research.tools.governance import (
        GovernedToolCallResult,
        ToolError,
        ToolOutcomeMessage,
    )

    state, lease = await create(store, limits={"tool_calls": 1})

    async def unused(*args):
        raise AssertionError("unused")

    tool = build_tool(
        name="write",
        input_schema=Empty,
        description="write",
        call=unused,
        origin=ToolOrigin.SYSTEM,
        effect=ToolEffect.EXTERNAL_WRITE,
    )

    async def handler():
        error = ToolError(error_type=error_type, tool_name="write", message="fixture")
        return GovernedToolCallResult(
            ToolOutcomeMessage("error", "write", "c"), error=error
        )

    session = RecoverySession(store, lease, state)
    if error_type == "network_error":
        with pytest.raises(UnknownOperation):
            await session.tool(tool, "c", {}, handler)
        assert (await store.budget(state.run_id, "owner"))["reserved"][
            "tool_calls"
        ] == 1
    else:
        await session.tool(tool, "c", {}, handler)
        budget = await store.budget(state.run_id, "owner")
        assert budget["reserved"]["tool_calls"] == 0
        assert budget["used"]["tool_calls"] == 0


async def test_public_approval_projection_retains_unresolved_item(store, tmp_path):
    from open_deep_research.events.public import RunEventPublisher, RunEventStore

    state, lease = await create(store)
    state.approvals = {
        key: {"kind": "tool", "payload": {"tool_name": "write"}}
        for key in ("one", "two")
    }
    state.status = "waiting"
    await store.save(lease, state)
    log = RunEventStore(state.run_id, runs_dir=str(tmp_path / "public"))
    publisher = RunEventPublisher(log)
    await store.deliver_public(lease, publisher)
    await store.submit_decision(
        state.run_id, "owner", "cmd", "one", {"action": "approve"}
    )
    session = RecoverySession(store, lease, state)
    flow = ResearchPipeline(
        state, Stages(), session.save, config_fingerprint="frozen", recovery=session
    )
    await session.consume_decisions(flow)
    await store.deliver_public(lease, publisher)
    assert [
        item["approval_id"] for item in log.project().pending_security_approvals
    ] == ["two"]


async def test_model_settlement_counts_every_physical_attempt_and_keeps_details(store):
    """每个物理调用只计一次：尝试数、失败已计费 usage 与缓存 token 分别入账。"""
    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.model import ChatUsage, ChatResponse

    state, lease = await create(store, limits={"model_calls": 3})
    session = RecoverySession(store, lease, state)

    class BilledFailure(Exception):
        pass

    billed_failure = BilledFailure("truncated")
    billed_failure.completion = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2)
    )
    physical = {"count": 0}

    def final_response():
        response = ChatResponse(content=[TextBlock(text="ok")], is_last=True)
        response.usage = ChatUsage(
            input_tokens=5, output_tokens=6, cache_input_tokens=4, time=0.1
        )
        return response

    async def call():
        # 模拟策略层：第一次物理尝试失败（携带已计费 usage），重试成功。
        physical["count"] += 1
        if physical["count"] == 1:
            try:
                raise billed_failure
            except BilledFailure:
                pass
        response = final_response()
        response.metadata["model_attempts"] = {
            "physical_attempts": 2,
            "attempt_failures": [
                {
                    "error_type": "BilledFailure",
                    "billed_usage": {"input_tokens": 7, "output_tokens": 2},
                }
            ],
        }
        return response

    with session.scope("researching", 1):
        await session.model("researcher", [UserMsg("user", "q")], call)
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"] == {"model_calls": 2, "input_tokens": 12, "output_tokens": 8}
    assert all(amount == 0 for amount in budget["reserved"].values())
    record = await store.operation_record(lease, "researching:1:pipeline:model:researcher:0")
    assert record["result"]["usage_details"]["cached_input_tokens"] == 4
    assert record["result"]["usage_details"]["physical_attempts"] == 2
    await session.close()


async def test_model_settlement_without_usage_keeps_conservative_reservation(store):
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse

    state, lease = await create(store, limits={"model_calls": 3})
    session = RecoverySession(store, lease, state)

    async def call():
        response = ChatResponse(content=[TextBlock(text="ok")], is_last=True)
        response.metadata["model_attempts"] = {"physical_attempts": 2, "attempt_failures": []}
        return response

    with session.scope("researching", 1):
        await session.model(
            "researcher", [UserMsg("user", "q")], call, max_tokens=9
        )
    budget = await store.budget(state.run_id, "owner")
    # usage 缺失：保守按预留结算（输出按 max_tokens），不当作零。
    assert budget["used"]["model_calls"] == 2
    assert budget["used"]["output_tokens"] == 9
    await session.close()


async def test_tool_settlement_uses_reported_physical_fetches(store):
    from types import SimpleNamespace

    state, lease = await create(store, limits={"tool_calls": 3, "fetch_calls": 9})
    session = RecoverySession(store, lease, state)
    tool = SimpleNamespace(
        name="web_research", effect=ToolEffect.READ_ONLY, supports_idempotency=True
    )

    async def reported():
        return GovernedToolCallResult(
            ToolOutcomeMessage("done", "web_research", "call-1"),
            ToolResult(output="out", metadata={"physical_fetches": 3}),
            None,
        )

    async def unreported():
        return GovernedToolCallResult(
            ToolOutcomeMessage("done", "web_research", "call-2"),
            ToolResult(output="out"),
            None,
        )

    with session.scope("researching", 1):
        await session.tool(tool, "call-1", {}, reported)
        await session.tool(tool, "call-2", {}, unreported)
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"] == {"tool_calls": 2, "fetch_calls": 4}
    await session.close()

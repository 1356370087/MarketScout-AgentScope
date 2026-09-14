"""Failure boundaries for run interruption and durable human decisions."""

import asyncio

import pytest

from open_deep_research import server
from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.run_control import RunControlStore


def make_engine(tmp_path, monkeypatch):
    engine = QueryEngine(
        {
            "configurable": {
                "runs_dir": str(tmp_path),
                "query_session_persistence_enabled": True,
                "enable_human_in_loop": True,
                "event_log_enabled": False,
                "observability_enabled": False,
                "search_api": "none",
            },
            "metadata": {"run_id": "pause-review", "owner": "u1"},
        }
    )
    if not engine.context_store.manifest_path.exists():
        engine.context_store.initialize("u1", engine.config)

    async def no_publish(*args, **kwargs):
        pass

    monkeypatch.setattr(engine, "_publish_public", no_publish)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan", "outline"])
async def test_revision_survives_replay(tmp_path, monkeypatch, kind):
    engine = make_engine(tmp_path, monkeypatch)
    state = {"research_brief": "Research objective"}
    await engine._persist_update(channel="lead", stage="seed", update=state)
    flow = getattr(engine, f"_maybe_await_{kind}_approval")(state)
    first = await anext(flow)
    engine.handle_human_action(
        first["data"]["pending_human_action"]["action_id"],
        "revise",
        "ONLY investigate Antarctica",
    )
    second = await anext(flow)
    if kind == "plan":
        second = await anext(flow)
    assert (
        "Antarctica"
        in second["data"]["pending_human_action"]["payload"][
            "research_plan" if kind == "plan" else "report_outline"
        ]
    )
    replay = engine.context_store.replay()
    restored = make_engine(tmp_path, monkeypatch)
    restored.human_feedback = replay.state.get("human_feedback", [])
    resumed_flow = getattr(restored, f"_maybe_await_{kind}_approval")(replay.state)
    restored_pending = await anext(resumed_flow)
    await flow.aclose()
    await resumed_flow.aclose()
    restored_text = restored_pending["data"]["pending_human_action"]["payload"][
        "research_plan" if kind == "plan" else "report_outline"
    ]
    assert "Antarctica" in restored_text, (
        "Restarted approval silently discarded the accepted revision"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan", "outline"])
async def test_pending_approval_id_survives_restart(tmp_path, monkeypatch, kind):
    engine = make_engine(tmp_path, monkeypatch)
    state = {"research_brief": "Research objective"}
    await engine._persist_update(channel="lead", stage="seed", update=state)
    flow = getattr(engine, f"_maybe_await_{kind}_approval")(state)
    first = await anext(flow)
    old_id = first["data"]["pending_human_action"]["action_id"]
    inbox = RunControlStore(engine.run_id, runs_dir=str(tmp_path))
    await inbox.enqueue(
        "human_action",
        {"action_id": old_id, "action": "approve"},
        command_id=f"human-action-{old_id}",
    )
    replay = engine.context_store.replay()
    restored = make_engine(tmp_path, monkeypatch)
    restored.human_feedback = replay.state.get("human_feedback", [])
    resumed_flow = getattr(restored, f"_maybe_await_{kind}_approval")(replay.state)
    pending = await anext(resumed_flow)
    new_id = pending["data"]["pending_human_action"]["action_id"]
    await flow.aclose()
    await resumed_flow.aclose()
    assert new_id == old_id, (
        "Previously accepted inbox command now addresses an obsolete action ID"
    )


@pytest.mark.asyncio
async def test_decision_is_durable_before_wait_returns(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    engine.status = "awaiting_clarification"
    pending = engine._open_human_action("clarification", {"question": "Which market?"})
    await engine._persist_checkpoint(
        "awaiting_clarification",
        "clarification_wait",
        status=engine.status,
        payload={"pending_human_action": pending},
    )
    result = engine.handle_human_action(pending["action_id"], "answer", "Antarctica")
    assert result["status"] == "accepted"
    decision = await engine._wait_for_human_action()
    assert decision["message"] == "Antarctica"
    # Crash boundary before _await_clarification persists its state update.
    replay = engine.context_store.replay()
    assert replay.manifest.pending_human_action["action_id"] == pending["action_id"]
    restored = make_engine(tmp_path, monkeypatch)
    flow = restored._await_clarification(
        replay.state, restored_action=replay.manifest.pending_human_action
    )
    await anext(flow)
    await asyncio.wait_for(anext(flow), 2)
    await flow.aclose()
    committed = restored.context_store.replay()
    assert committed.manifest.next_stage == "write_research_brief"
    assert committed.manifest.pending_human_action is None
    assert [message.content for message in committed.state["messages"]] == [
        "Antarctica"
    ]


@pytest.mark.asyncio
async def test_shutdown_retains_pending_clarification(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    flow = engine._await_clarification({"messages": []})
    event = await anext(flow)
    pending = event["data"]["pending_human_action"]
    waiter = asyncio.create_task(anext(flow))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    replay = engine.context_store.replay()
    assert engine.pending_human_action is None
    assert replay.manifest.pending_human_action["action_id"] == pending["action_id"]
    assert replay.manifest.next_stage == "clarification_wait"


@pytest.mark.asyncio
async def test_failed_decision_write_does_not_acknowledge_or_wake(
    tmp_path, monkeypatch
):
    engine = make_engine(tmp_path, monkeypatch)
    action = engine._open_human_action("plan_approval", {"research_plan": "plan"})

    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(engine.context_store, "write_json_atomic", fail_write)
    with pytest.raises(OSError, match="disk unavailable"):
        engine.handle_human_action(action["action_id"], "approve")
    assert not engine._pending_action_future.done()


@pytest.mark.asyncio
async def test_redelivered_decision_does_not_affect_next_approval(
    tmp_path, monkeypatch
):
    engine = make_engine(tmp_path, monkeypatch)
    flow = engine._maybe_await_plan_approval({"research_brief": "objective"})
    first = await anext(flow)
    action_id = first["data"]["pending_human_action"]["action_id"]
    engine.handle_human_action(action_id, "revise", "Antarctica")
    await anext(flow)
    second = await anext(flow)
    new_id = second["data"]["pending_human_action"]["action_id"]
    assert new_id != action_id
    assert (
        engine.handle_human_action(action_id, "revise", "Antarctica")["status"]
        == "accepted"
    )
    assert not engine._pending_action_future.done()
    with pytest.raises(ValueError, match="different decision"):
        engine.handle_human_action(action_id, "approve")
    await flow.aclose()


@pytest.mark.asyncio
async def test_plan_revision_limit_survives_restart(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    engine.config["configurable"]["hitl_max_plan_revisions"] = 1
    flow = engine._maybe_await_plan_approval({"research_brief": "objective"})
    first = await anext(flow)
    engine.handle_human_action(
        first["data"]["pending_human_action"]["action_id"], "revise", "Antarctica"
    )
    await anext(flow)
    await anext(flow)
    replay = engine.context_store.replay()
    await flow.aclose()
    assert replay.state["hitl_plan_revisions"] == 1
    restored = make_engine(tmp_path, monkeypatch)
    restored.config["configurable"]["hitl_max_plan_revisions"] = 1
    restored.human_feedback = replay.state["human_feedback"]
    resumed = restored._maybe_await_plan_approval(replay.state)
    event = await anext(resumed)
    restored.handle_human_action(
        event["data"]["pending_human_action"]["action_id"], "revise", "another revision"
    )
    with pytest.raises(RuntimeError, match="revision limit exceeded"):
        await anext(resumed)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plan", "outline"])
@pytest.mark.parametrize("delivery", ["direct", "inbox"])
async def test_loaded_run_consumes_pre_restart_approval(
    tmp_path, monkeypatch, kind, delivery
):
    from langchain_core.messages import HumanMessage

    from tests.test_hitl import _collect_until, _install_basic_graph
    from tests.test_query_persistence import _config

    calls = await _install_basic_graph(monkeypatch)
    config = _config(
        tmp_path,
        "restart-approval",
        enable_human_in_loop=True,
        hitl_require_plan_approval=kind == "plan",
        hitl_require_outline_approval=kind == "outline",
        task_state_backend="memory",
    )
    engine = QueryEngine(config)
    queue = asyncio.Queue()

    async def run():
        async for event in engine.stream_message([HumanMessage(content="research")]):
            await queue.put(event)

    task = asyncio.create_task(run())
    try:
        pending = await asyncio.wait_for(
            _collect_until([], f"hitl.{kind}_pending", queue), 10
        )
        action_id = pending["data"]["pending_human_action"]["action_id"]
        if delivery == "direct":
            # Cancel before the scheduled Future callback can apply the decision.
            engine.handle_human_action(action_id, "approve")
        else:
            await RunControlStore(engine.run_id, runs_dir=str(tmp_path)).enqueue(
                "human_action",
                {"action_id": action_id, "action": "approve"},
                command_id=f"human-action-{action_id}",
            )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    restored = QueryEngine.load(engine.run_id, runs_dir=str(tmp_path), config=config)
    # The HTTP resume endpoint acquires ownership before starting its listener.
    await restored.acquire_run_lease()
    record = server.RunRecord(run_id=restored.run_id, engine=restored)
    listener = asyncio.create_task(
        server._run_control_listener(record, restored.config)
    )
    try:
        result = await asyncio.wait_for(restored.resume(), 15)
        assert result["result"]["status"] == "success"
        assert calls == {"supervisor": 1, "final_report": 1}
        assert restored.context_store.load_manifest().pending_human_action is None
        assert (
            restored.context_store.load_human_decision(action_id)["action"] == "approve"
        )
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_cancellation_preserves_resumable_query():
    from langchain_core.messages import AIMessage, HumanMessage

    from open_deep_research.agents.query import QueryParams, query
    from open_deep_research.agents.query_state import InMemoryQueryCheckpointSink
    from open_deep_research.runtime_control import CancellationScope
    from tests.test_query_runtime import FakeModel, _config

    started = asyncio.Event()
    sink = InMemoryQueryCheckpointSink()
    scope = CancellationScope()

    async def slow_model(messages):
        started.set()
        await asyncio.Event().wait()

    async def consume():
        return [
            event
            async for event in query(
                QueryParams(
                    messages=[HumanMessage(content="research")],
                    system_prompt="system",
                    model=FakeModel([]),
                    config=_config(),
                    call_model=slow_model,
                    cancellation_scope=scope,
                    checkpoint_sink=sink,
                )
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()  # Same raw task cancellation as server graceful shutdown.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not scope.is_cancelled  # No user cancellation was requested.
    called = []

    async def resumed_model(messages):
        called.append(True)
        return AIMessage(content="Research completed")

    events = [
        event
        async for event in query(
            QueryParams(
                messages=[],
                system_prompt="system",
                model=FakeModel([]),
                config=_config(),
                call_model=resumed_model,
                initial_state=sink.states[-1],
            )
        )
    ]
    assert called, [(event.type, event.data.get("transition")) for event in events]

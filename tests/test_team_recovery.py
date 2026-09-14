"""Failure-boundary regressions for team inputs and researcher cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.tasks.registry import TaskRecord, TaskRegistry, TaskStatus
from open_deep_research.tasks.state import TaskSnapshot


@pytest.mark.asyncio
async def test_disabled_quality_still_admits_completed_task(monkeypatch):
    from open_deep_research.agents import deep_researcher as graph
    snapshot = TaskSnapshot(task_id="t", run_id="r", status=TaskStatus.COMPLETED)
    store = SimpleNamespace(get=AsyncMock(return_value=snapshot))
    monkeypatch.setattr(graph, "get_task_state_store", lambda _: store)
    admit = AsyncMock(return_value="admitted")
    monkeypatch.setattr(graph, "_admit_completed_async_output", admit)
    result = await graph._admit_completed_tasks_from_messages(
        {}, {"metadata": {"run_id": "r"}},
        configurable=Configuration(quality_evaluation_enabled=False), publisher=None,
        messages=[SimpleNamespace(type="task_completed", payload={"task_id": "t"})],
    )
    assert result == ["admitted"]
    admit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admitted_result_replays_without_judge(monkeypatch):
    from open_deep_research.agents import deep_researcher as graph
    judge = AsyncMock(side_effect=AssertionError("must reuse committed decision"))
    monkeypatch.setattr(graph, "evaluate_subagent_handoff", judge)
    output = {"task_id": "t", "compressed_research": "verified result"}
    snapshot = TaskSnapshot(task_id="t", run_id="r", status=TaskStatus.COMPLETED,
                            admission_status="accepted")
    result = await graph._admit_completed_async_output(
        output, state={}, config={"metadata": {"run_id": "r"}},
        configurable=Configuration(), publisher=None, snapshot=snapshot,
        state_store=SimpleNamespace(),
    )
    assert result.accepted_output == output
    judge.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_cancellation_drains_researcher(monkeypatch):
    from open_deep_research.tasks import executor
    monkeypatch.setattr(executor, "publish_task_update", AsyncMock())
    registry = TaskRegistry()
    record = registry.restore(TaskRecord(task_id="cancel-team", run_id="r", research_topic="topic"))
    started, drained = asyncio.Event(), asyncio.Event()

    async def research(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    config = {"configurable": {"task_state_backend": "memory", "task_checkpoint_enabled": False,
                               "event_log_enabled": False, "task_timeout_seconds": 60},
              "metadata": {"run_id": "r"}}
    task = asyncio.create_task(executor.run_task_with_control(
        record, config, registry, research, run_id="r", event_log_enabled=False,
    ))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()


def test_consumed_inputs_survive_checkpoint_and_compaction():
    from dataclasses import replace

    from open_deep_research.agents.query_state import QueryLoopState
    from open_deep_research.tools.governance import AgentRole
    state = QueryLoopState(state_key="researcher:t", role=AgentRole.RESEARCHER,
                           messages=(), consumed_input_ids=("message-1",))
    restored = QueryLoopState.from_snapshot(replace(state, messages=()).to_snapshot())
    assert restored.consumed_input_ids == ("message-1",)


@pytest.mark.asyncio
async def test_completed_artifact_recovery_skips_model(monkeypatch, tmp_path):
    from open_deep_research.run_context import RunContextStore
    from open_deep_research.tasks import executor
    from open_deep_research.tasks.recovery import (
        CheckpointManager,
        ResearcherCheckpoint,
    )
    monkeypatch.setattr(executor, "publish_task_update", AsyncMock())
    result = {"compressed_research": "already verified", "raw_notes": [], "metrics": {}}
    context = RunContextStore("artifact-run", runs_dir=str(tmp_path))
    digest = context.persist_task_result("artifact-task", result)
    checkpoints = CheckpointManager(runs_dir=str(tmp_path), run_id="artifact-run")
    checkpoints.save(ResearcherCheckpoint(task_id="artifact-task", run_id="artifact-run",
        phase="researching", completed_result_ref={"sha256": digest}))
    registry = TaskRegistry()
    record = registry.restore(TaskRecord(task_id="artifact-task", run_id="artifact-run", research_topic="topic"))
    research = AsyncMock(side_effect=AssertionError("completed artifact must be reused"))
    config = {"configurable": {"task_state_backend": "memory", "runs_dir": str(tmp_path),
                               "task_checkpoint_enabled": True, "event_log_enabled": False},
              "metadata": {"run_id": "artifact-run"}}
    await executor.run_task_with_control(record, config, registry, research,
        run_id="artifact-run", runs_dir=str(tmp_path), event_log_enabled=False,
        checkpoint_manager=checkpoints)
    assert record.status is TaskStatus.COMPLETED
    assert record.result["compressed_research"] == "already verified"
    research.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_bridge_rejects_another_run(monkeypatch):
    from pydantic import ValidationError

    from open_deep_research.tasks import registry as registry_module
    from open_deep_research.tasks.team_bridge import TeamWorkerRequest, host_request
    registry = TaskRegistry()
    registry.restore(TaskRecord(task_id="owned", run_id="owner-run", research_topic="topic",
        assigned_teammate_id="member", status=TaskStatus.RUNNING))
    monkeypatch.setattr(registry_module, "get_task_registry", lambda: registry)
    with pytest.raises(PermissionError, match="active_member_task_required"):
        await host_request(SimpleNamespace(config={"metadata": {"run_id": "other-run"}}),
                           "owned", "input", {})
    with pytest.raises(ValidationError):
        TeamWorkerRequest(run_id="owner-run", task_id="owned", action="input", member_id="lead")

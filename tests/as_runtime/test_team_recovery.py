"""Recovery guarantees migrated from the retired executor to native workers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from team_worker_fixture import Model, workers
from test_team import env
from test_team_worker import assignment

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff
from open_deep_research.agentscope_runtime.team_worker import _Worker

pytestmark = pytest.mark.asyncio


async def test_disabled_semantic_quality_still_admits_verified_completed_task(env, tmp_path):
    team = await env[0]()
    host, _, _ = await workers(team, env[1], tmp_path, quality_enabled=False)
    item, contract = assignment()
    try:
        outcome = await host.dispatch(item, contract)
        assessment = outcome.assessment["handoff"]
        assert assessment["accepted"] and assessment["deterministic_checks"]["passed"]
        assert "semantic_quality_disabled" in assessment["caveats"]
    finally:
        await host.aclose()


async def test_admitted_handoff_replays_without_judge(env, tmp_path):
    team = await env[0]()
    host, _, _ = await workers(team, env[1], tmp_path)
    item, contract = assignment()
    try:
        await host.dispatch(item, contract)
        row = (await team.service.tasks(team.lease.run_id))[0]
        quality = SimpleNamespace(handoff=AsyncMock(side_effect=AssertionError("must reuse committed admission")))
        receipt = await team.admit_handoff("handoff:" + item.task_id, row["owner"],
            ResearchHandoff.model_validate(row["_native_handoff"]), contract, quality)
        assert receipt["accepted"]
        quality.handoff.assert_not_awaited()
    finally:
        await host.aclose()


async def test_parent_cancellation_drains_native_model_call(env, tmp_path, monkeypatch):
    team = await env[0]()
    host, factory, _ = await workers(team, env[1], tmp_path, slow=True)
    item, contract = assignment()
    drained = asyncio.Event()
    original = Model._call_api

    async def tracked(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        finally:
            drained.set()

    monkeypatch.setattr(Model, "_call_api", tracked)
    running = asyncio.create_task(host.dispatch(item, contract))
    try:
        await asyncio.wait_for(factory.entered.wait(), 10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert drained.is_set() and not host._active
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await host.aclose()


async def test_consumed_feedback_reloads_from_sql_without_duplicate_application(env, tmp_path):
    team = await env[0]()
    host, _, _ = await workers(team, env[1], tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    worker = _Worker(host, item.task_id)
    await worker.claim()
    worker.session = SimpleNamespace(snapshot=SimpleNamespace(feedback_by_task={}))
    sent = await team.say(team.leader, "feedback", worker.member_id, "Keep the original year constraint")
    await worker.consume_inputs()
    expected = ["Keep the original year constraint"]
    assert worker.session.snapshot.feedback_by_task[item.task_id] == expected
    worker.session.snapshot.feedback_by_task.clear()
    await worker.consume_inputs()
    assert worker.session.snapshot.feedback_by_task[item.task_id] == expected
    assert not any(e.event_id == sent["event_id"] for e in await team.pending(worker.member_id))
    await host.aclose()


async def test_completed_artifact_recovery_skips_model_and_tools(env, tmp_path):
    team = await env[0]()
    host, factory, marker = await workers(team, env[1], tmp_path)
    item, contract = assignment()
    try:
        original = await host.dispatch(item, contract)
        calls = sum(len(model.calls) for model in factory.instances)
        replay = await host.execute(item.task_id)
        assert replay == original and replay.compressed_research
        assert sum(len(model.calls) for model in factory.instances) == calls
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        await host.aclose()


async def test_native_task_session_rejects_foreign_run_and_member(env, tmp_path):
    team, foreign = await env[0](), await env[0](user="another-owner")
    host, _, _ = await workers(team, env[1], tmp_path)
    item, contract = assignment()
    await host.prepare(item, contract)
    row = (await team.service.tasks(team.lease.run_id))[0]
    with pytest.raises(PermissionError, match="not been claimed"):
        await foreign.task_session(item.task_id, row["owner"])
    with pytest.raises(PermissionError, match="not been claimed"):
        await team.task_session(item.task_id, "another-member")
    assert (await team.service.tasks(team.lease.run_id))[0]["owner"] == row["owner"]
    await host.aclose()

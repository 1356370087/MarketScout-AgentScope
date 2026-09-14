"""Fetch grants pause the outer run and preserve consumed budgets."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage

from open_deep_research.agents.query_engine import QueryEngine
from open_deep_research.configuration import Configuration
from open_deep_research.tools.web_research import pipeline
from tests.test_hitl import _collect_until, _config, _install_basic_graph


def test_classifier_alias_is_authorized_even_when_quality_gate_is_off():
    cfg = Configuration(quality_evaluation_enabled=False, egress_classifier_model="if-egress-custom")
    assert "if-egress-custom" in QueryEngine._litellm_allowed_models(cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
async def test_budget_hitl_before_report_with_quality_and_plan_hitl_disabled(monkeypatch, decision):
    calls = await _install_basic_graph(monkeypatch)

    async def supervisor(self, state):
        calls["supervisor"] += 1
        assert self.config["metadata"]["sandbox_egress_intent"] == "Human: research"
        if calls["supervisor"] == 2:
            assert state["notes"] == ["retained evidence"]
            assert self.config["metadata"]["fetch_budget_extension"]["extra_fetches"] > 0
        return {
            "notes": {"type": "override", "value": ["retained evidence"]},
            "completion_decision": {"type": "override", "value": {
                "action": "complete_partial" if calls["supervisor"] == 1 else "complete",
                "reason": "fetch_budget_exhausted" if calls["supervisor"] == 1 else "explicit_completion",
                "gaps": [],
            }},
        }

    monkeypatch.setattr(QueryEngine, "_run_supervisor", supervisor)
    engine = QueryEngine(_config(enable_human_in_loop=False, quality_evaluation_enabled=False))
    queue = asyncio.Queue()

    async def run():
        async for event in engine.stream_message([HumanMessage(content="research")]):
            await queue.put(event)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(_collect_until([], "hitl.fetch_budget_pending", queue), 5)
        assert not task.done()
        assert calls["final_report"] == 0
        assert engine.status == "awaiting_fetch_budget_approval"
        action_id = engine.pending_human_action["action_id"]
        with pytest.raises(ValueError, match="does not match"):
            engine.handle_human_action(action_id, "revise", "no")
        await asyncio.sleep(0.01)
        assert not task.done()
        engine.handle_human_action(action_id, decision)
        await asyncio.wait_for(task, 5)
        assert calls["supervisor"] == (2 if decision == "approve" else 1)
        assert calls["final_report"] == (0 if decision == "cancel" else 1)
        if decision == "deny":
            assert engine.final_state["completion_decision"]["reason"] == "fetch_budget_exhausted"
        if decision == "cancel":
            assert engine.status == "cancelled"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fetch_grant_increases_actual_pool_without_resetting_usage(monkeypatch):
    monkeypatch.setenv("MAX_FETCHES_PER_RUN", "2")
    monkeypatch.setenv("MAX_FETCHES_PER_RESEARCHER", "10")
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH_UNITS", "1")
    config = {"metadata": {"run_id": "grant-test", "task_id": "task", "research_wave_id": "wave-1"}}
    pipeline.clear_run_web_budget("grant-test")
    try:
        assert (await pipeline._reserve_fetch_budget(config, 2)).reserved == 2
        assert (await pipeline._reserve_fetch_budget(config, 1)).reserved == 0
        config["metadata"]["fetch_budget_extension"] = {"extra_fetches": 2}
        assert (await pipeline._reserve_fetch_budget(config, 2)).reserved == 2
        assert (await pipeline._reserve_fetch_budget(config, 1)).reserved == 0
    finally:
        pipeline.clear_run_web_budget("grant-test")


@pytest.mark.asyncio
async def test_budget_pending_reuses_persisted_action_after_restart(monkeypatch, tmp_path):
    engine = QueryEngine(_config(enable_human_in_loop=False))
    engine.context_store = SimpleNamespace(
        manifest_path=tmp_path / "unused.json",
        load_manifest=lambda: SimpleNamespace(pending_human_action={
            "action_id": "existing-action", "type": "fetch_budget_approval",
        }),
    )
    monkeypatch.setattr(engine, "_persist_update", AsyncMock())
    monkeypatch.setattr(engine, "_persist_checkpoint", AsyncMock())
    monkeypatch.setattr(engine, "_publish_public", AsyncMock())
    state = {"completion_decision": {"reason": "fetch_budget_exhausted"}}
    events = engine._await_fetch_budget_approval(state)
    await anext(events)
    assert engine.pending_human_action["action_id"] == "existing-action"
    engine.handle_human_action("existing-action", "deny")
    await anext(events)
    assert state["fetch_budget_resolution"] == "deny"
    assert state["completion_decision"]["action"] == "complete_partial"
    await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_budget_decision_replay_does_not_ask_twice(monkeypatch, decision):
    calls = await _install_basic_graph(monkeypatch)
    engine = QueryEngine(_config(enable_human_in_loop=False))
    state = {
        "messages": [HumanMessage(content="research")],
        "approved_research_plan": "plan", "notes": ["evidence"],
        "fetch_budget_resolution": decision,
        "fetch_budget_action_id": "resolved-budget-action",
        "completion_decision": {"action": "complete_partial", "reason": "fetch_budget_exhausted"} if decision == "deny" else {},
    }
    events = [e async for e in engine._stream_execution(state, "fetch_budget_approval")]
    assert not any(e['event'] == 'hitl.fetch_budget_pending' for e in events)
    assert calls["supervisor"] == (1 if decision == "approve" else 0)
    assert calls["final_report"] == 1

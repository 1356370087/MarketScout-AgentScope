"""Delivery-driven completion skips another paid Supervisor turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_efficiency import cache
from test_research_migration import Models, cfg, contract, evidence, tool_call

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_all_delivered_requirements_complete_without_another_model_turn(cache, tmp_path, asynchronous):
    coverage = contract("Explain deployment limits.")
    rid = coverage["requirements"][0]["requirement_id"]
    name = "TaskCreate" if asynchronous else "ConductResearch"
    models = Models({"supervisor": [
        [tool_call(name, "create", research_topic="Limits", requirement_ids=[rid])],
        [tool_call("WaitForTeamEvents" if asynchronous else "think_tool", "unnecessary", reflection="waiting")],
    ]})
    models.recovery = cache.recovery
    config = cfg(enable_async_research=asynchronous, async_research_mode="collaborator",
                 research_efficiency_mode="bounded", runs_dir=str(tmp_path))
    config["metadata"]["run_config_schema_version"] = 21
    worker = SimpleNamespace(run=AsyncMock(side_effect=lambda a, *_: ResearchHandoff(
        **a.model_dump(), compressed_research="retained", evidence_registry=[evidence()])))
    supervisor = Supervisor(models, lambda: config, worker, completion_policy=True, run_id="efficiency")
    supervisor.persist_findings = AsyncMock()
    result, state = await supervisor.run("brief", coverage)
    assert len(result) == 1
    assert state["middle_context"]["completion_signal"]["tool"] == "ResearchComplete"
    assert state["middle_context"]["business_completion"]["action"] == "complete"
    supervisor.persist_findings.assert_awaited()
    if not asynchronous:
        assert len(models.created[0][2].calls) == 1


@pytest.mark.asyncio
async def test_closed_budget_stops_before_first_task_planning(cache, tmp_path):
    await cache.recovery.store.close_research(cache.recovery.lease, "report_time_reserved")
    models = Models({"supervisor": [[tool_call("ConductResearch", research_topic="must not run")]]})
    models.recovery = cache.recovery
    config = cfg(research_efficiency_mode="bounded", runs_dir=str(tmp_path))
    config["metadata"]["run_config_schema_version"] = 21
    worker = SimpleNamespace(run=AsyncMock())
    result, state = await Supervisor(models, lambda: config, worker, run_id="efficiency",
                                     completion_policy=True).run("brief", contract())
    worker.run.assert_not_awaited()
    assert result == []
    assert not models.created[0][2].calls
    assert state["middle_context"]["business_completion"]["action"] == "complete_partial"

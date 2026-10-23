"""A dependent synthesis can reuse accepted facts without reopening research."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_efficiency import cache
from test_research_migration import Models, cfg, evidence, tool_call

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor
from open_deep_research.agentscope_runtime.teams_tools import TaskCreateInput, dependency_synthesis_ids
from open_deep_research.quality.contract import CoverageRequirement, ResearchCoverageContract, validate_requirement_ids


def coverage():
    return ResearchCoverageContract(schema_version=4, original_query_sha256="fixture", requirements=tuple(
        CoverageRequirement(requirement_id=rid, text=text, kind="factual", dimension_id=dimension,
                            source_message_index=0, source_start=0, source_end=len(text))
        for rid, text, dimension in (("vacuum", "VACUUM disk reclamation", "DIM-V"),
                                     ("analyze", "ANALYZE planner statistics", "DIM-A"))
    ))


def synthesis(**patch):
    return TaskCreateInput.model_validate({"subject": "汇总研究", "description": "读取前置工件并比较两项研究结论。",
        "requirement_ids": ["vacuum", "analyze"], "owner": "", "blockedBy": ["first", "second"], **patch})


def predecessors():
    return [{"task_id": "first", "requirement_ids": ["vacuum"]},
            {"task_id": "second", "requirement_ids": ["analyze"]}]


def test_cross_dimension_scope_is_allowed_only_for_matching_predecessors():
    contract = coverage()
    with pytest.raises(ValueError, match="cross_dimension"):
        validate_requirement_ids(["vacuum", "analyze"], contract, required=True)
    assert dependency_synthesis_ids(synthesis(), predecessors(), contract) == ["vacuum", "analyze"]
    assert dependency_synthesis_ids(synthesis(blockedBy=[]), predecessors(), contract) is None
    assert dependency_synthesis_ids(synthesis(blockedBy=["first"]), predecessors(), contract) is None
    with pytest.raises(ValueError, match="dependency_task_not_found"):
        dependency_synthesis_ids(synthesis(blockedBy=["foreign-run"]), predecessors(), contract)


@pytest.mark.asyncio
async def test_supervisor_schedules_synthesis_after_both_facts_are_already_covered(cache):
    contract = coverage()
    await cache.progress({"requirements": {rid: {"status": "supported", "rounds": 99, "stagnant": 99}
                                           for rid in ("vacuum", "analyze")}})
    models = Models({"supervisor": [
        [tool_call("TaskCreate", "synthesis", **{**synthesis().model_dump(exclude={"research_topic"}), "owner": ""})],
        [tool_call("WaitForTeamEvents", "wait")],
        [tool_call("ResearchComplete", "done")],
    ]})
    models.recovery = cache.recovery
    config = cfg(enable_async_research=True, async_research_mode="teams", research_efficiency_mode="bounded")
    rows = predecessors()
    host = SimpleNamespace(recovery=cache.recovery, prepare=AsyncMock(), consume_leader_inputs=AsyncMock(),
        finish_team=AsyncMock(), team=SimpleNamespace(leader=None, lease=SimpleNamespace(run_id="efficiency"),
            service=SimpleNamespace(tasks=AsyncMock(return_value=rows))))

    async def dispatch(assignment, contract, feedback):
        from open_deep_research.agentscope_runtime.research_quality import skipped_handoff_assessment
        return ResearchHandoff(**assignment.model_dump(), compressed_research="The two accepted findings compared.",
                               evidence_registry=[evidence()], assessment={"handoff": skipped_handoff_assessment()})

    async def wait():
        async with asyncio.timeout(5):
            while not (await cache.progress()).get("tasks"):
                await asyncio.sleep(0)

    host.dispatch, host.wait_for_updates = dispatch, wait
    supervisor = Supervisor(models, lambda: config, None, run_id="efficiency", team_workers=host)
    supervisor.initial_coverage_ledger = {rid: {"status": "supported"} for rid in ("vacuum", "analyze")}
    results, _ = await supervisor.run("Compare the accepted findings", contract.model_dump(mode="json"))
    host.prepare.assert_awaited_once()
    assert host.prepare.await_args.args[0].requirement_ids == ["vacuum", "analyze"]
    assert host.prepare.await_args.kwargs["blocked_by"] == ["first", "second"]
    assert len(results) == 1 and results[0]["compressed_research"]
    host.finish_team.assert_awaited_once()

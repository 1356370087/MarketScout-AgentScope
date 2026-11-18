"""Exercise the new gates inside native reasoning loops, beyond DTO checks."""

import pytest
from test_research_migration import Models, cfg, contract, evidence, research_tools, tool_call
from open_deep_research.agentscope_runtime.adaptive_research import AdaptiveResearch
from open_deep_research.agentscope_runtime.research_agents import Researcher, ResearchHandoff, Supervisor
from open_deep_research.quality.gate import HandoffAssessment
from open_deep_research.quality.contract import RequirementCoverage, ResearchCoverageContract, research_delegation_scopes


class Quality:
    def __init__(self, accepted=True):
        self.accepted, self.handoffs = accepted, []

    async def batch(self, *args):
        return {"accepted": True, "decision": "complete", "deterministic_checks": {"passed": True}, "gaps": []}

    async def handoff(self, result, coverage):
        self.handoffs.append(result.task_id)
        return HandoffAssessment(accepted=self.accepted, relevance=5, source_quality=5, evidence_coverage=5,
            groundedness=5, reason="fixture assessment", requirement_coverage=[RequirementCoverage(requirement_id=rid,
                status="supported", evidence_ids=["ev1"], explanation="fixture evidence") for rid in result.requirement_ids])


def configuration(**values):
    result = cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=1, max_researcher_iterations=4, skills=[], **values)
    result["metadata"]["run_config_schema_version"] = 24
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_adaptive_main_runs_handoff_and_completion_gate(accepted):
    models = Models({"researcher": [[tool_call("web_research", "read")],
        [tool_call("ResearchComplete", "finish", summary="Supported growth [ev1].")]]})
    quality = Quality(accepted)
    flow = AdaptiveResearch(Researcher(models, lambda: configuration(), research_tools, run_id="gate", quality=quality))
    findings, state = await flow.run("研究市场规模。", contract("研究市场规模。"))
    assert quality.handoffs == ["main-research"]
    assert findings[0]["evidence_registry"]
    assert findings[0]["assessment"]["handoff"]["evaluation_status"] == "evaluated"
    assert (state["middle_context"]["business_completion"]["action"] == "complete") is accepted


@pytest.mark.asyncio
async def test_delegated_supervisor_merges_admitted_coverage_before_completing():
    coverage = contract("研究市场规模。")
    scopes = research_delegation_scopes(ResearchCoverageContract.model_validate(coverage))
    arguments = {"research_topic": "研究市场规模。"}
    if scopes:
        arguments["scope_id"] = scopes[0]["scope_id"]
    else:
        arguments["requirement_ids"] = [row["requirement_id"] for row in coverage["requirements"]]
    models = Models({"supervisor": [[tool_call("ConductResearch", "delegate", **arguments)], [tool_call("ResearchComplete", "finish")]]})
    class Worker:
        async def run(self, assignment, contract, feedback):
            return ResearchHandoff(**assignment.model_dump(), compressed_research="Supported growth [ev1].", evidence_registry=[evidence()], termination="research_complete")
    quality = Quality()
    flow = Supervisor(models, lambda: configuration(research_execution_strategy="delegated"), Worker(), quality=quality, run_id="gate", completion_policy=True)
    findings, state = await flow.run("研究市场规模。", coverage)
    assert quality.handoffs and findings[0]["assessment"]["handoff"]["accepted"]
    assert state["middle_context"]["business_completion"]["action"] == "complete"
    assert all(value["status"] == "supported" for value in state["middle_context"]["coverage_ledger"].values())

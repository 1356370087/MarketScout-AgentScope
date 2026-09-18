"""Quality feedback and candidate accumulation through the native Agent loop."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from test_research_migration import Models, cfg, contract, evidence, tool_call
from test_research_quality import Judge

from open_deep_research.agentscope_runtime.research_agents import Researcher, ResearchAssignment, _Observations
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
from open_deep_research.tools.base import ToolOrigin, ToolResult, ToolExecutionZone, build_tool

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("premature_completion", [False, True])
async def test_parallel_sources_are_assessed_once_and_feedback_reaches_researcher(premature_completion):
    class Fetch(BaseModel):
        index: int

    async def fetch(input, context, progress):
        record = {**evidence(), "evidence_id": f"ev{input.index}", "document_id": f"doc{input.index}",
                  "source_url": f"https://example.test/source{input.index}"}
        return ToolResult(output={"evidence": [record], "documents": [record]})

    async def tools(_):
        return [build_tool(name="fetch_url", input_schema=Fetch, description="Fetch", prompt=lambda _: "fetch",
                           origin=ToolOrigin.SEARCH, call=fetch, concurrency_safe=True,
                           execution_zone=ToolExecutionZone.HOST_CONTROL)]

    script = [[tool_call("fetch_url", str(i), index=i) for i in range(1, 4)],
              [tool_call("ResearchComplete", "done")]]
    if premature_completion:
        script.insert(0, [tool_call("ResearchComplete", "premature")])
    models = Models({"researcher": script})
    config = lambda: cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=3)
    judge = Judge()
    quality = NativeResearchQuality(judge, config)
    result = await Researcher(models, config, tools, run_id="run", quality=quality).run(
        ResearchAssignment(research_topic="市场"), contract())
    assert len(judge.calls) == 1
    assert len(result.evidence_registry) == 3 and result.termination == "research_complete"
    assert result.assessment["tool_batches"][0]["deterministic_checks"]["source_count"] == 3
    second_input = models.created[0][2].calls[-1]
    assert "本轮受信质量反馈" in str(second_input)
    if premature_completion:
        assert "ResearchComplete 未获准" in str(models.created[0][2].calls[1])


async def test_low_batch_score_retains_candidates_for_later_corroboration():
    async def low(_):
        return {"accepted": False, "decision": "continue", "missing_information": ["more sources"]}
    observations = _Observations(assess=low, contract=contract())
    for index in (1, 2):
        outcome = SimpleNamespace(error=None, result=ToolResult(output={"evidence": [
            {**evidence(), "evidence_id": f"ev{index}", "document_id": f"doc{index}"}]}),
            message=SimpleNamespace(content="fetched"))
        await observations.capture("fetch_url", str(index), outcome)
        await observations.assess_pending()
    assert len(observations.evidence) == 2
    assert all(not item["accepted"] for item in observations.assessments)


@pytest.mark.parametrize("fail_open,decision", [(True, "continue"), (False, "complete")])
async def test_completed_invalid_judge_obeys_quality_policy(fail_open, decision):
    class BrokenJudge:
        async def structured(self, *args):
            raise ModelOutputProtocolError("'relevance' is a required property")

    quality = NativeResearchQuality(BrokenJudge(), lambda: cfg(
        quality_evaluation_enabled=True, quality_evaluation_min_sources=1, quality_evaluation_fail_open=fail_open))
    result = await quality.batch(ResearchAssignment(research_topic="市场"), contract(),
                                 [{"name": "fetch_url", "content": "fetched"}], [evidence()])
    assert result["decision"] == decision
    assert result["evaluator_error"] and result["accepted"] is False

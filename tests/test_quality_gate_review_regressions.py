"""Regressions from the quality-gate runtime and prompt review."""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from open_deep_research.agents import deep_researcher
from open_deep_research.agents.query_engine import ResearcherQueryEngine
from open_deep_research.quality.gate import (
    HANDOFF_EVALUATION_PROMPT_V4,
    ToolResultAssessment,
    _bounded_quality_payload,
    deterministic_tool_checks,
    evaluate_tool_results,
)
from tests.test_researcher_query_runtime import (
    FakeResearchModel,
    _config,
    research_echo,
)


def _evidence(count):
    return [{"evidence_id": f"ev-{i}", "claim": "Requested fact",
             "supporting_excerpt": "Supporting official passage",
             "security_status": "accepted",
             "source_url": f"https://source-{i}.example/page"} for i in range(count)]


@pytest.mark.parametrize("count,passed", [(0, False), (2, True)])
@pytest.mark.parametrize("batch", [[], [{
    "name": "web_research", "content": '{"error":"timeout"}', "error": True,
}]])
def test_cumulative_evidence_survives_empty_or_failed_batch(count, passed, batch):
    checks = deterministic_tool_checks(batch, min_sources=2, evidence_registry=_evidence(count))
    assert checks["source_count"] == count
    assert checks["passed"] is passed
    assert checks["batch_failures"]


def test_failed_batch_does_not_allow_quarantined_evidence():
    records = _evidence(2)
    for record in records:
        record["security_status"] = "quarantined"
    checks = deterministic_tool_checks(
        [{"name": "web_research", "content": '{"error":"timeout"}', "error": True}],
        min_sources=2, evidence_registry=records,
    )
    assert not checks["passed"]
    assert "all_tools_failed" in checks["failures"]


@pytest.mark.parametrize("field", [
    "owned_requirement_ids", "shared_requirement_ids", "exclusive_requirement_ids",
    "requirement_ids", "evidence_ids", "batch_failures",
])
def test_payload_never_truncates_protocol_arrays(field):
    payload = {field: ["COV-01-0123456789abcdef"]}
    size = len(json.dumps({**payload, "input_truncated": False}))
    with pytest.raises(ValueError, match="quality_payload_budget_too_small"):
        _bounded_quality_payload(payload, max_chars=size - 12)
    bounded = _bounded_quality_payload({**payload, "reason": "long prose " * 100}, max_chars=size + 50)
    assert bounded[field] == payload[field]
    assert bounded["input_truncated"]


def test_handoff_prompt_acceptance_uses_exclusive_scope():
    rules = HANDOFF_EVALUATION_PROMPT_V4.split("Propose accepted", 1)[1]
    assert "every owned user requirement" not in rules
    assert "every requirement in exclusive_requirement_ids" in rules
    assert "Missing or partial shared requirements alone do not prevent acceptance" in rules


@pytest.mark.asyncio
@pytest.mark.parametrize("judge_decision", ["complete", "continue"])
async def test_cumulative_evidence_does_not_override_judge_coverage(monkeypatch, judge_decision):
    async def judge(*args, **kwargs):
        return ToolResultAssessment(decision=judge_decision, relevance=4, source_quality=4,
            evidence_coverage=4, corroboration=4, reason="coverage assessment",
            missing_information=[] if judge_decision == "complete" else ["Unanswered requirement"])

    monkeypatch.setattr("open_deep_research.quality.gate._evaluate_json", judge)
    result = await evaluate_tool_results("topic", [{"name": "web_research",
        "content": '{"error":"timeout"}', "error": True}],
        _config(), evidence_registry=_evidence(2))
    assert result.decision == judge_decision
    assert result.deterministic_checks["batch_failures"] == ["all_tools_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluator_error", [None, "judge unavailable"])
async def test_research_loop_honors_quality_stop(monkeypatch, evaluator_error):
    model = FakeResearchModel([
        AIMessage(content="", tool_calls=[{"name": "research_echo", "args": {"text": "fact"}, "id": "tool-1"}]),
        AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "done-1"}]),
    ])

    async def tools(_config):
        return [research_echo, *deep_researcher.build_supervisor_tools({})[-2:-1]]

    async def judge(*args, **kwargs):
        return ToolResultAssessment(decision="complete", relevance=3, source_quality=3,
            evidence_coverage=3, corroboration=3, reason="stop", evaluator_error=evaluator_error)

    async def compress(state, _config):
        return {"compressed_research": "compressed", "raw_notes": []}

    monkeypatch.setattr(deep_researcher, "configurable_model", model)
    monkeypatch.setattr(deep_researcher, "get_all_tools", tools)
    monkeypatch.setattr(deep_researcher, "evaluate_tool_results", judge)
    monkeypatch.setattr(deep_researcher, "compress_research", compress)
    result = await ResearcherQueryEngine(_config(quality_evaluation_enabled=True,
        quality_evaluation_fail_open=False, event_log_enabled=False,
        query_session_persistence_enabled=False)).ainvoke({
            "researcher_messages": [HumanMessage(content="topic")], "research_topic": "topic",
            "evidence_registry": _evidence(2),
        })
    assert len(model.calls) == 1
    assert result["completion_decision"]["action"] == ("terminate" if evaluator_error else "complete")
    assert result["completion_decision"]["reason"] == (
        "quality_evaluator_unavailable" if evaluator_error else "quality_complete"
    )

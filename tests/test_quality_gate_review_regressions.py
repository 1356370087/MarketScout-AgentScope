"""Regressions from the quality-gate runtime and prompt review."""
import json

import pytest

from open_deep_research.quality.gate import (
    HANDOFF_EVALUATION_PROMPT_V4,
    ToolResultAssessment,
    _bounded_quality_payload,
    deterministic_tool_checks,
    evaluate_tool_results,
)
from tests.quality_helpers import config as _config



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

"""Judge responses must not control runtime errors or admission diagnostics."""
import json

import pytest
from langchain_core.messages import AIMessage

from open_deep_research.quality import gate
from tests.test_quality_gate_review_regressions import _evidence
from tests.test_researcher_query_runtime import _config

RUNTIME_FIELDS = {
    "deterministic_checks", "evaluator_error", "protocol_errors",
    "protocol_repair_count", "evaluator_model", "policy_version",
    "evaluation_epoch", "quality_rigor", "quality_thresholds",
}


@pytest.mark.parametrize("schema", [gate.ToolResultAssessment, gate.HandoffAssessment])
def test_judge_schema_excludes_runtime_metadata_but_persistence_keeps_it(schema):
    fields = RUNTIME_FIELDS | {"hard_rejection_reasons"}
    assert not fields.intersection(schema.model_json_schema()["properties"])
    values = dict(relevance=4, source_quality=4, evidence_coverage=4,
                  corroboration=4, groundedness=4, decision="complete",
                  accepted=True, reason="Supported", evaluator_error="real failure")
    assert schema(**values).model_dump()["evaluator_error"] == "real failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["litellm", "legacy", "replay"])
@pytest.mark.parametrize("error_value", ["null", "", "invented failure"])
@pytest.mark.parametrize("schema,prompt", [
    (gate.ToolResultAssessment, gate.TOOL_RESULT_EVALUATION_PROMPT),
    (gate.HandoffAssessment, gate.HANDOFF_EVALUATION_PROMPT_V4),
])
async def test_model_metadata_is_ignored(monkeypatch, backend, error_value, schema, prompt):
    raw = dict(relevance=4, source_quality=4, evidence_coverage=4,
               corroboration=4, groundedness=4, decision="complete", accepted=True,
               reason="Supported", evaluator_error=error_value,
               protocol_errors=["invented"], protocol_repair_count=42,
               evaluator_model="spoofed", deterministic_checks={"passed": False},
               hard_rejection_reasons=["invented"], quality_thresholds={"floor": 1})

    async def complete(*args, **kwargs):
        values = raw if backend == "replay" else kwargs["output_payload_transform"](raw)
        return kwargs["output_schema"].model_validate(values)

    async def legacy(*args, **kwargs):
        return AIMessage(content=json.dumps(raw))

    monkeypatch.setattr(gate, "complete_model", complete)
    monkeypatch.setattr(gate, "invoke_with_model_fallback", legacy)
    result = await gate._evaluate_json(schema, prompt, {},
        _config(model_backend="legacy" if backend == "legacy" else "litellm"),
        span_name="test.judge")
    assert result.evaluator_error is None
    assert result.protocol_errors == []
    assert result.protocol_repair_count == 0
    assert result.evaluator_model == ""
    assert result.deterministic_checks == {}
    assert result.quality_thresholds == {}
    assert getattr(result, "hard_rejection_reasons", []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_activity_distinguishes_model_null_from_real_exception(monkeypatch, failure):
    events = []

    async def complete(*args, **kwargs):
        if failure:
            raise RuntimeError("test transport failure")
        return gate.ToolResultAssessment(decision="complete", relevance=4,
            source_quality=4, evidence_coverage=4, corroboration=4,
            reason="Supported", evaluator_error="null")

    async def publish(config, event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(gate, "complete_model", complete)
    monkeypatch.setattr(gate, "publish_task_activity", publish)
    result = await gate.evaluate_tool_results("topic", [],
        _config(model_backend="litellm", quality_evaluation_fail_open=True),
        evidence_registry=_evidence(3))
    assert bool(result.evaluator_error) is failure
    assert result.decision == ("continue" if failure else "complete")
    assert events[-1][0] == ("quality.failed" if failure else "quality.completed")
    if failure:
        assert events[-1][1]["error_class"] == "RuntimeError"

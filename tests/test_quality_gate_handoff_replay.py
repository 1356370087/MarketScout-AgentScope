"""Regressions minimized from run eda86136's quality-gate repair handoff."""

import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from open_deep_research.events.task_activity import TaskActivityStore
from open_deep_research.models.codec import MessageCodecError
from open_deep_research.models.gateway import ModelGatewayError
from open_deep_research.quality import gate
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.report import assembly, build_report, orchestrator
from open_deep_research.report.models import SourceRef


def test_final_brief_structure_is_not_a_researchable_fact():
    contract = build_research_coverage_contract([
        HumanMessage(content="1. 核查监管要求。最终简报包含摘要、五维分析、横向对比表与证据局限。")
    ])
    structure = next(row for row in contract.requirements if "最终简报" in row.text)
    assert structure.kind == "deliverable"
    assert structure.requirement_id not in contract.delegable_requirement_ids()


@pytest.mark.asyncio
async def test_quality_gate_preserves_writer_number_to_source_binding(monkeypatch, tmp_path):
    for key, value in {
        "QUALITY_EVALUATION_ENABLED": "true",
        "WEB_PIPELINE_MODE": "enforced",
        "REPORT_REVIEW_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    writer_markdown = (
        "# Report\n\nEU regulation [1]. US project [2].\n\n"
        "### Sources\n"
        "[1] EU regulation: https://eur-lex.europa.eu/eli/reg/2023/1542/oj\n"
        "[2] US project: https://www.energy.gov/lpo/redwood-materials\n"
    )

    async def fake_invoke(*_args, **_kwargs):
        return AIMessage(content=writer_markdown)

    monkeypatch.setattr(assembly, "invoke_model_with_retry_observability", fake_invoke)
    update = await build_report(
        {
            "messages": [],
            "research_brief": "EU regulation and US project",
            # Evidence arrival order differs from the writer's citation order.
            "notes": [
                "[US project](https://www.energy.gov/lpo/redwood-materials)",
                "[EU regulation](https://eur-lex.europa.eu/eli/reg/2023/1542/oj)",
            ],
        },
        {"configurable": {"runs_dir": str(tmp_path)}, "metadata": {"run_id": "citation-replay"}},
    )
    markdown = update["final_report"]
    eu_number = re.search(r"EU regulation \[(\d+)\]", markdown).group(1)
    assert f"[{eu_number}] EU regulation: https://eur-lex.europa.eu/eli/reg/2023/1542/oj" in markdown
    assert update["canonical_report"]["sources"][int(eu_number) - 1]["url"].startswith("https://eur-lex")


def test_citation_mapping_preserves_code_links_and_adjacent_references():
    markdown = (
        "Claim [1][2]. [1](https://b.test) and `array[7]`.\n"
        "```python\narray[9]\n```\n"
        "## 参考资料\n[1] B: https://b.test\n[2] A: https://a.test\n"
        "[3] Unused: https://unused.test\n"
    )
    remapped = orchestrator._remap_numbered_citations(
        markdown, [SourceRef(url="https://a.test"), SourceRef(url="https://b.test")],
    )
    assert "Claim [2][1]. [1](https://b.test) and `array[7]`." in remapped
    assert "```python\narray[9]\n```" in remapped


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [
    "",  # A number alone cannot establish the writer's intended URL.
    "### Sources\n[1] EU: https://eur-lex.europa.eu/eli/reg/2023/1542/oj",
    "### Sources\n[1] US: https://www.energy.gov/lpo/redwood-materials\n[1] Other: https://other.test",
])
async def test_unresolvable_citation_uses_evidence_limited_recovery(monkeypatch, tmp_path, references):
    monkeypatch.setenv("QUALITY_EVALUATION_ENABLED", "true")
    monkeypatch.setenv("REPORT_REVIEW_ENABLED", "false")
    body = "# Draft\n\nEU regulation [1].\n\n" + references

    async def fake_invoke(*_args, **_kwargs):
        return AIMessage(content=body)

    async def restricted(records, **_kwargs):
        assert len(records) == 1
        return "# Partial\n\nUS project [EV-1].\n\n## 来源\n[EV-1] [US](https://www.energy.gov/lpo/redwood-materials)"

    monkeypatch.setattr(assembly, "invoke_model_with_retry_observability", fake_invoke)
    monkeypatch.setattr(orchestrator, "build_evidence_limited_report", restricted)
    update = await build_report(
        {
            "messages": [], "notes": ["US project"],
            "evidence_registry": [{
                "evidence_id": "EV-1", "claim": "US project", "supporting_excerpt": "US project",
                "source_title": "US", "source_url": "https://www.energy.gov/lpo/redwood-materials",
                "security_status": "accepted",
            }],
        },
        {"configurable": {"runs_dir": str(tmp_path)}, "metadata": {"run_id": "citation-gap"}},
    )
    assert "EU regulation" not in update["final_report"]
    assert update["quality_gate"]["status"] == "degraded"
    assert "report_unresolved_citations" in update["quality_gate"]["reason_codes"]


@pytest.mark.asyncio
async def test_partial_run_does_not_expand_missing_evidence_into_a_full_report(monkeypatch, tmp_path):
    monkeypatch.setenv("QUALITY_EVALUATION_ENABLED", "true")
    monkeypatch.setenv("REPORT_REVIEW_ENABLED", "false")
    calls = []

    async def unrestricted(_ctx):
        calls.append("unrestricted")
        return assembly.AssemblyResult(body_markdown=(
            "We partner with over 700 companies. US project [1].\n\n"
            "### Sources\n[1] US: https://www.energy.gov/lpo/redwood-materials"
        ))

    async def restricted(records, **kwargs):
        calls.append("restricted")
        assert [row["evidence_id"] for row in records] == ["EV-US"]
        assert kwargs["uncovered_requirement_ids"] == ["COV-EU"]
        return "# Partial\n\nUS project [EV-US]. EU evidence missing.\n\n## 来源\n[EV-US] [US](https://www.energy.gov/lpo/redwood-materials)"

    monkeypatch.setattr(orchestrator, "assemble", unrestricted)
    monkeypatch.setattr(orchestrator, "build_evidence_limited_report", restricted)
    update = await build_report({
        "messages": [], "notes": ["US project"],
        "completion_decision": {"action": "complete_partial", "reason": "max_turns_drained", "gaps": ["coverage_gaps:COV-EU"]},
        "coverage_contract": {"original_query_sha256": "replay", "requirements": [{
            "requirement_id": "COV-EU", "text": "EU regulation", "kind": "factual",
            "source_message_index": 0, "source_start": 0, "source_end": 13,
        }]},
        "evidence_registry": [{
            "evidence_id": "EV-US", "claim": "US project", "supporting_excerpt": "US project",
            "source_title": "US", "source_url": "https://www.energy.gov/lpo/redwood-materials",
            "security_status": "accepted",
        }],
    }, {"configurable": {"runs_dir": str(tmp_path)}, "metadata": {"run_id": "partial-replay"}})
    assert "700" not in update["final_report"]
    assert calls == ["restricted"]
    assert update["completion_decision"]["value"]["reason"] == "max_turns_drained"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [
    (TimeoutError("authorization=secret-value upstream timed out"), "timeout"),
    (gate.QualityProtocolError(["complete_requires_evidence"]), "quality_protocol_invalid"),
    (MessageCodecError("invalid structured output"), "quality_protocol_invalid"),
    (ModelGatewayError("gateway_unavailable"), "gateway_unavailable"),
])
async def test_failed_tool_judge_exposes_safe_diagnostics(monkeypatch, tmp_path, error, code):
    monkeypatch.setenv("QUALITY_EVALUATION_FAIL_OPEN", "true")

    async def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(gate, "_evaluate_json", unavailable)
    config = {
        "configurable": {"runs_dir": str(tmp_path)},
        "metadata": {"run_id": "judge-replay", "task_id": "task-1"},
    }
    result = await gate.evaluate_tool_results("US market", [], config)
    assert result.evaluator_error is not None
    events = TaskActivityStore("judge-replay", "task-1", runs_dir=str(tmp_path)).read()
    failure = next(event for event in events if event.type == "quality.failed")
    assert failure.payload["error_code"] == code
    assert failure.payload["error_class"] == type(error).__name__
    assert failure.payload["decision"] == result.decision
    assert "secret-value" not in str(failure.public_dict())

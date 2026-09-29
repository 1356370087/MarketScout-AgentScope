"""Native report inputs retain full domain records across window changes."""

import copy
import json
from types import SimpleNamespace

import pytest
from agentscope.model import StructuredResponse

from open_deep_research.agentscope_runtime.report import _ReportRun
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.configuration import Configuration
from open_deep_research.report.models import ReportDraft, ReportReview
from open_deep_research.report.reviewer import (
    _invoke_reviewer,
    _revision_prompt,
    build_reviewer_payload,
    review_report,
)
from open_deep_research.report.runtime import native_report
from open_deep_research.report.writing import fit_writing_messages, writing_messages
from tests.as_runtime.test_report_native import Factory, state


def records():
    return [{"evidence_id": f"EV-{index}", "claim": "完整结论" * 160,
             "supporting_excerpt": "原始证据" * 160 + f"TAIL-{index}",
             "source_url": f"https://example.com/{index}", "security_status": "accepted"}
            for index in range(20)]


@pytest.fixture
def native_port():
    factory = Factory()
    factory.run = SimpleNamespace(get=lambda name: {
        "openai:gpt-4.1": {"context_window": 24000}
    } if name == "model_catalog_snapshot" else {})
    port = _ReportRun(ResearchModels(factory), state())
    token = native_report.set(port)
    try:
        yield port
    finally:
        native_report.reset(token)


def test_frozen_window_and_repeated_fits_preserve_records_and_omissions(native_port):
    evidence = records()
    before = copy.deepcopy(evidence)
    messages = writing_messages("Write the report", {"brief": "问题"}, evidence)
    cfg = Configuration(model_context_window_overrides={"openai:gpt-4.1": 1000000})
    fitted, count = fit_writing_messages(messages, "openai:gpt-4.1", cfg, output_tokens=1024)
    assert 0 < count < len(evidence)
    smaller, count2 = fit_writing_messages(
        fitted, "openai:gpt-4.1", cfg, output_tokens=2048, context_window=14000,
    )
    payload = json.loads(smaller[-1].content)
    assert 0 < count2 < count
    assert payload["omitted_record_count"] + count2 == len(evidence)
    assert all(item in before for item in payload["records"])
    assert evidence == before
    assert sum(len(m.content.encode("utf8")) + 16 for m in smaller) <= 14000 - 2048 - 700


@pytest.mark.asyncio
async def test_review_preserves_draft_tail_and_full_evidence(native_port):
    draft = ReportDraft(markdown="正文" * 800 + "DRAFT-END")
    evidence = records()
    domain = {"research_brief": "原问题", "evidence_registry": evidence}
    cfg = Configuration(report_review_max_input_chars=1000)
    payload = build_reviewer_payload(draft, domain, {"configurable": cfg.model_dump()})
    assert payload["draft_markdown"] == draft.markdown
    assert payload["evidence_registry"][0]["supporting_excerpt"] == evidence[0]["supporting_excerpt"]

    async def review(messages, schema):
        fields = json.loads(messages[1].get_text_content())
        selected = json.loads(messages[2].get_text_content())
        assert fields["draft_markdown"] == draft.markdown
        assert 0 < len(selected["records"]) < len(evidence)
        assert len(selected["records"]) + selected["omitted_record_count"] == len(evidence)
        assert all(item["supporting_excerpt"].endswith("TAIL-" + item["evidence_id"].split("-")[1])
                   for item in selected["records"])
        return StructuredResponse(content={"decision": "revise"})

    native_port.models.factory.generate_structured_output = review
    result = await _invoke_reviewer(payload, {}, cfg, attempt=1)
    assert result.decision == "revise"


def test_revision_keeps_full_draft_and_issue_tail(native_port):
    draft = ReportDraft(markdown="完整草稿" * 2000 + "END")
    # Stay within the validated domain field limit; the prompt must not cut it again.
    review = ReportReview(issues=[{"description": "修订依据" * 400 + "ISSUE-END"}])
    messages = _revision_prompt(draft, review, {"evidence_registry": records()},
                                {"configurable": {"report_review_max_input_chars": 1000}})
    payload = json.loads(messages[1].content)
    assert payload["draft_markdown"] == draft.markdown
    assert payload["review"]["issues"][0]["description"].endswith("ISSUE-END")
    assert len(json.loads(messages[2].content)["records"]) == 20


@pytest.mark.asyncio
async def test_oversize_draft_fails_before_model_instead_of_silently_cutting(native_port):
    draft = ReportDraft(markdown="不可截断" * 10000)
    with pytest.raises(RuntimeError, match="report_fixed_context_exceeds_budget"):
        await review_report(draft, {"evidence_registry": records()},
                            {"configurable": {"report_review_fail_open": True}})
    assert native_port.models.factory.calls == []


@pytest.mark.asyncio
async def test_candidate_window_rebudgets_structured_review(native_port):
    factory = native_port.models.factory
    factory.context_size = 14000
    evidence = records()
    payload = build_reviewer_payload(ReportDraft(markdown="完整正文"), {"evidence_registry": evidence})

    async def review(messages, schema):
        assert sum(len(m.get_text_content().encode("utf8")) + 16 for m in messages) <= 14000 - 1024 - 700
        data = json.loads(messages[-1].get_text_content())
        assert len(data["records"]) + data["omitted_record_count"] == len(evidence)
        return StructuredResponse(content={"decision": "revise"})

    factory.generate_structured_output = review
    await _invoke_reviewer(payload, {}, Configuration(), attempt=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["budget", "lease", "unknown", "deadline"])
async def test_review_fail_open_does_not_swallow_control_errors(native_port, kind):
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost, UnknownOperation
    from open_deep_research.budgets import BudgetDimension, BudgetExhausted, DeadlineExceeded

    error = {"budget": BudgetExhausted(BudgetDimension.MODEL_CALLS),
             "lease": FenceLost("expired"), "unknown": UnknownOperation("uncertain"),
             "deadline": DeadlineExceeded("expired")}[kind]

    async def fail(messages, schema):
        raise error

    native_port.models.factory.generate_structured_output = fail
    with pytest.raises(type(error)) as captured:
        await review_report(ReportDraft(markdown="正文"), {"evidence_registry": records()},
                            {"configurable": {"report_review_fail_open": True}})
    assert captured.value is error


@pytest.mark.asyncio
async def test_missing_native_runtime_cannot_become_fail_open_review():
    from open_deep_research.report.runtime import NativeReportRuntimeMissing

    token = native_report.set(None)
    try:
        with pytest.raises(NativeReportRuntimeMissing, match="native_report_runtime_required"):
            await review_report(ReportDraft(markdown="完整草稿"), {"evidence_registry": records()},
                                {"configurable": {"report_review_fail_open": True}})
    finally:
        native_report.reset(token)

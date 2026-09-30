"""Contract tests for the final-report Reviewer -> Revisor loop.

These tests deliberately use fakes at the stage boundary.  They exercise the
orchestration contract without requiring a provider API key, network access, or
an actual report-generation model.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

import pytest
from open_deep_research.config_types import RuntimeConfig as RunnableConfig
from pydantic import ValidationError

from open_deep_research.configuration import Configuration
from open_deep_research.report import orchestrator
from open_deep_research.report import reviewer as reviewer_module
from open_deep_research.report.models import (
    ReportCitationReview,
    ReportCoverageReview,
    ReportDimensionScores,
    ReportDraft,
    ReportReview,
    ReportReviewIssue,
    SourceRef,
)


def _config(**overrides: Any) -> RunnableConfig:
    """Build a small runnable config for reviewer tests."""
    values: dict[str, Any] = {
        "quality_evaluation_enabled": False,
        "report_review_enabled": True,
        "report_review_max_revisions": 1,
        "report_review_fail_open": True,
        "report_type": "default",
        "output_format": "markdown",
        "reference_style": "numbered",
        "final_report_model": "openai:test",
    }
    values.update(overrides)
    return {
        "configurable": values,
        "metadata": {
            "run_id": "report-review-test",
            "quality_policy_version": "quality-gate-v4",
            "quality_evaluation_epoch": "test-epoch",
        },
    }


def _state() -> dict[str, Any]:
    """Return the minimum state projection accepted by report stages."""
    return {
        "messages": [],
        "research_brief": "Compare the two options and recommend one.",
        "notes": [],
        "raw_notes": [],
        "completed_task_outputs": [],
        "coverage_contract": {
            "schema_version": 1,
            "requirements": [
                {
                    "requirement_id": "COV-01",
                    "text": "Compare the options.",
                },
            ],
        },
        "coverage_ledger": {},
        "evidence_registry": [
            {
                "evidence_id": "EV-01",
                "claim": "Option A is supported.",
                "supporting_excerpt": "The source supports option A.",
                "source_title": "Approved source",
                "source_url": "https://approved.example/source",
                "security_status": "accepted",
            },
        ],
        "handoff_assessments": [],
        "supervisor_messages": [],
    }


def _draft(markdown: str = "# Draft\n\nOption A [1].\n\n### Sources\n[1] Approved source: https://approved.example/source") -> ReportDraft:
    """Create a canonical draft with explicit provenance for assertions."""
    return ReportDraft(
        markdown=markdown,
        body_markdown=markdown,
        sources=[
            SourceRef(
                title="Approved source",
                url="https://approved.example/source",
            )
        ],
        sections=[],
        report_type="default",
        output_format="markdown",
        reference_style="numbered",
        profile_name="default",
        evaluation_snapshot={"schema_version": "1.0"},
        coverage_checklist=["COV-01"],
        provenance={"model": "openai:test", "policy_version": "quality-gate-v4"},
        attempt=0,
    )


def _review(
    decision: str = "pass",
    *,
    status: str = "completed",
    issues: list[ReportReviewIssue] | None = None,
    deterministic_failures: list[str] | None = None,
    attempt: int = 0,
    draft_sha256: str = "",
) -> ReportReview:
    """Create a fully populated review result used by orchestration fakes."""
    dimensions = ReportDimensionScores(
        coverage=1.0,
        citation_correctness=1.0,
        contradictions=1.0,
        unsupported_claims=1.0,
        redundancy=1.0,
        executive_readability=1.0,
    )
    return ReportReview(
        schema_version="1.0",
        decision=decision,
        dimensions=dimensions,
        coverage=[
            ReportCoverageReview(
                requirement_id="COV-01",
                status="covered",
                explanation="The draft compares both options.",
            )
        ],
        citation_audit=[
            ReportCitationReview(
                claim="Option A is supported.",
                citation_target="https://approved.example/source",
                supported=True,
                evidence_ids=["EV-01"],
            )
        ],
        issues=issues or [],
        summary="The draft is ready for delivery.",
        status=status,
        hard_failures=[],
        deterministic_failures=deterministic_failures or [],
        attempt=attempt,
        draft_sha256=draft_sha256,
        provenance={"model": "openai:test", "policy_version": "quality-gate-v4"},
    )


def test_report_review_models_normalize_scores_and_close_decisions() -> None:
    """The structured review protocol normalizes scores and closes decisions."""
    scores = ReportDimensionScores(
        coverage=0.0,
        citation_correctness=0.5,
        contradictions=1.0,
        unsupported_claims=0.75,
        redundancy=0.25,
        executive_readability=0.9,
    )
    assert scores.coverage == 0.0
    assert scores.executive_readability == 0.9

    # Provider responses occasionally use the legacy 1..5 scale; the protocol
    # normalizes it into the public 0..1 range before applying the gate.
    normalized = ReportDimensionScores(
        coverage=5,
        citation_correctness=2.5,
        contradictions=0,
        unsupported_claims=1,
        redundancy=4,
        executive_readability=3,
    )
    assert normalized.coverage == 1.0
    assert normalized.citation_correctness == 0.5
    assert normalized.redundancy == 0.8

    with pytest.raises(ValidationError):
        _review(decision="approve")


def test_report_review_issue_and_citation_protocol_carry_stable_ids() -> None:
    """Issues and citation audits retain requirement/evidence provenance."""
    issue = ReportReviewIssue(
        category="unsupported_claims",
        severity="critical",
        location="Conclusion, paragraph 2",
        requirement_id="COV-01",
        evidence_ids=["EV-01"],
        citation_target="https://approved.example/source",
        description="A claim lacks support.",
        revision_instruction="Remove or qualify the claim.",
    )
    citation = ReportCitationReview(
        claim="Option A is supported.",
        citation_target="https://approved.example/source",
        supported=True,
        evidence_ids=["EV-01"],
    )
    assert issue.requirement_id == "COV-01"
    assert issue.evidence_ids == ["EV-01"]
    assert citation.evidence_ids == ["EV-01"]


def _model_review_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid provider-shaped review response for gateway fakes."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "decision": "pass",
        "dimensions": {
            "coverage": 1.0,
            "citation_correctness": 1.0,
            "contradictions": 1.0,
            "unsupported_claims": 1.0,
            "redundancy": 1.0,
            "executive_readability": 1.0,
        },
        "coverage": [
            {
                "requirement_id": "COV-01",
                "status": "covered",
                "explanation": "Answered.",
            }
        ],
        "citation_audit": [
            {
                "claim": "Option A is supported.",
                "citation_target": "https://approved.example/source",
                "supported": True,
                "evidence_ids": ["EV-01"],
            }
        ],
        "issues": [],
        "summary": "Ready.",
    }
    payload.update(overrides)
    return payload


def test_reviewer_payload_is_scoped_to_accepted_evidence_and_stable_requirements() -> None:
    """Reviewer input must exclude raw notes, rejected handoffs, and quarantined evidence."""
    state = _state()
    state["notes"] = [
        "RAW_TOOL_SECRET=do-not-send",
        "rejected_by_supervisor_quality_gate: do-not-send",
    ]
    state["evidence_registry"] = [
        state["evidence_registry"][0],
        {
            "evidence_id": "EV-QUARANTINED",
            "claim": "QUARANTINED_SECRET",
            "supporting_excerpt": "[quarantined external content]",
            "source_url": "https://quarantined.example/source",
            "security_status": "quarantined",
        },
        {
            "evidence_id": "EV-TAMPERED",
            "claim": "TAMPERED_SECRET",
            "source_url": "https://tampered.example/source",
            "security_status": "accepted",
            "hash_verified": False,
        },
    ]

    payload = reviewer_module.build_reviewer_payload(
        _draft(), state, _config(report_review_max_input_chars=20_000)
    )

    assert [row["evidence_id"] for row in payload["evidence_registry"]] == ["EV-01"]
    assert payload["coverage_contract"]["requirements"] == [
        {"requirement_id": "COV-01", "text": "Compare the options."}
    ]
    serialized = str(payload)
    assert "RAW_TOOL_SECRET" not in serialized
    assert "rejected_by_supervisor_quality_gate" not in serialized
    assert "QUARANTINED_SECRET" not in serialized
    assert "TAMPERED_SECRET" not in serialized


@pytest.mark.asyncio
async def test_reviewer_recomputes_hard_gate_for_unknown_ids_and_urls(monkeypatch) -> None:
    """A model cannot pass a report by inventing evidence or source URLs."""
    async def fake_invoke(*_args, **_kwargs):
        return _model_review_payload(
            citation_audit=[
                {
                    "claim": "Unsupported claim.",
                    "citation_target": "https://outside.example/source",
                    "supported": True,
                    "evidence_ids": ["EV-NOT-ACCEPTED"],
                }
            ],
            issues=[
                {
                    "category": "coverage",
                    "severity": "high",
                    "requirement_id": "COV-UNKNOWN",
                    "description": "Unknown requirement.",
                }
            ],
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    result = await reviewer_module.review_report(_draft(), _state(), _config())

    assert result.decision == "fail"
    assert "unknown_evidence_id" in result.deterministic_failures
    assert "unknown_requirement_id" in result.deterministic_failures
    assert any("outside the accepted source allowlist" in issue.description for issue in result.issues)
    assert result.hard_failures


@pytest.mark.asyncio
async def test_reviewer_fails_closed_on_disallowed_fenced_code_url(monkeypatch) -> None:
    """URLs inside fenced code are checked fail-closed instead of rewritten."""
    async def fake_invoke(*_args, **_kwargs):
        return _model_review_payload()

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    draft = _draft(
        "# Draft\n\nOption A [1].\n\n"
        "```text\nhttps://outside.example/in-code\n```"
    )
    result = await reviewer_module.review_report(draft, _state(), _config())

    assert result.decision == "fail"
    assert any(issue.location == "fenced_code" for issue in result.issues)
    assert any("citation_correctness" in code for code in result.hard_failures)


@pytest.mark.asyncio
async def test_reviewer_model_failure_obeys_fail_open_policy(monkeypatch) -> None:
    """Provider failures are explicitly degraded (open) or failed (closed)."""
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", unavailable)
    open_result = await reviewer_module.review_report(
        _draft(), _state(), _config(report_review_fail_open=True)
    )
    closed_result = await reviewer_module.review_report(
        _draft(), _state(), _config(report_review_fail_open=False)
    )

    assert open_result.status == "degraded"
    assert open_result.decision != "pass"
    assert "model_error" in open_result.provenance
    assert closed_result.status == "failed"
    assert closed_result.decision == "fail"
    assert "report_reviewer_unavailable" in closed_result.hard_failures


@pytest.mark.asyncio
async def test_fail_open_does_not_waive_deterministic_source_failures(monkeypatch) -> None:
    """Reviewer availability cannot publish a draft with an unaccepted URL."""
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", unavailable)
    result = await reviewer_module.review_report(
        _draft("# Draft\n\nUnsupported [link](https://outside.example/source)."),
        _state(),
        _config(report_review_fail_open=True),
    )

    assert result.status == "failed"
    assert result.decision == "fail"
    assert result.skipped is False
    assert result.hard_failure is True
    assert "report_reviewer_unavailable" in result.hard_failures


@pytest.mark.asyncio
async def test_reviewer_requires_all_dimensions_and_accepts_flattened_scores(monkeypatch) -> None:
    """Missing scores fail closed while complete flattened adapters remain compatible."""
    payload = _model_review_payload()
    payload.pop("dimensions")

    async def missing_dimensions(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", missing_dimensions)
    missing = await reviewer_module.review_report(_draft(), _state(), _config())
    assert missing.decision != "pass"
    assert "review_dimensions_missing" in missing.deterministic_failures

    coverage_rows = payload.pop("coverage")
    payload["coverage_reviews"] = coverage_rows
    payload.update(
        coverage=1.0,
        citation_correctness=1.0,
        contradictions=1.0,
        unsupported_claims=1.0,
        redundancy=1.0,
        executive_readability=1.0,
    )

    async def flattened_dimensions(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", flattened_dimensions)
    flattened = await reviewer_module.review_report(_draft(), _state(), _config())
    assert flattened.decision == "pass"
    assert flattened.dimensions.coverage == 1.0


@pytest.mark.asyncio
async def test_supported_citation_requires_matching_evidence_binding(monkeypatch) -> None:
    """A citation cannot be declared supported without a source-matched evidence ID."""
    state = _state()
    state["evidence_registry"].append(
        {
            "evidence_id": "EV-02",
            "claim": "A different source supports option B.",
            "supporting_excerpt": "Different evidence.",
            "source_title": "Other approved source",
            "source_url": "https://other.example/source",
            "security_status": "accepted",
        }
    )
    async def empty_binding(*_args, **_kwargs):
        return _model_review_payload(
            citation_audit=[
                {
                    "claim": "Option A is supported.",
                    "citation_target": "https://approved.example/source",
                    "supported": True,
                    "evidence_ids": [],
                }
            ]
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", empty_binding)
    empty_result = await reviewer_module.review_report(_draft(), state, _config())
    assert empty_result.decision != "pass"
    assert any(issue.category == "citation_correctness" for issue in empty_result.issues)

    async def mismatched_binding(*_args, **_kwargs):
        return _model_review_payload(
            citation_audit=[
                {
                    "claim": "Option A is supported.",
                    "citation_target": "https://approved.example/source",
                    "supported": True,
                    "evidence_ids": ["EV-02"],
                }
            ]
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", mismatched_binding)
    mismatched = await reviewer_module.review_report(_draft(), state, _config())
    assert mismatched.decision != "pass"
    assert mismatched.hard_failure is True


@pytest.mark.asyncio
async def test_revisor_prompt_and_output_are_scoped_and_sanitized(monkeypatch) -> None:
    """Revisor receives accepted evidence; invalid links remain visible to review."""
    captured: dict[str, str] = {}

    async def fake_reviser(prompt, *_args, **_kwargs):
        captured["messages"] = prompt
        captured["prompt"] = "\n".join(message.content for message in prompt)
        return (
            "# Revised\n\nOption A remains supported [1]. "
            "[Unsafe](https://outside.example/secret)\n"
            "<script>alert('x')</script>"
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviser", fake_reviser)
    state = _state()
    state["notes"] = ["RAW_NOTE_SECRET=do-not-send"]
    review = _review(
        "revise",
        issues=[
            ReportReviewIssue(
                category="unsupported_claims",
                severity="high",
                description="Remove unsupported claim.",
                revision_instruction="Delete it.",
            )
        ],
    )

    revised = await reviewer_module.revise_report(_draft(), review, state, _config())

    assert "RAW_NOTE_SECRET" not in captured["prompt"]
    assert "rejected_by_supervisor_quality_gate" not in captured["prompt"]
    assert "EV-01" in captured["prompt"]
    assert "Remove unsupported claim." in captured["prompt"]
    assert "outside.example" in revised
    assert "<script>" not in revised
    assert "Option A remains supported" in revised
    async def fake_review(*_args, **_kwargs):
        return _model_review_payload()

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_review)
    checked = await reviewer_module.review_report(_draft(revised), state, _config())
    assert checked.hard_failure
    assert checked.decision != "pass"


@pytest.mark.asyncio
async def test_revisor_canonicalizes_sources_before_the_next_review(monkeypatch) -> None:
    """Finalization publishes the exact canonical Markdown seen by Reviewer."""
    async def fake_reviser(*_args, **_kwargs):
        return "# Revised\n\nOption A remains supported [1].\n\n### Sources\n[1] Approved: https://approved.example/source"

    monkeypatch.setattr(reviewer_module, "_invoke_reviser", fake_reviser)
    draft = _draft()
    draft.report_type = "comparison_matrix"
    draft.profile_name = "comparison_matrix"
    config = _config(report_type="comparison_matrix")

    revised = await reviewer_module.revise_report(
        draft,
        _review("revise"),
        _state(),
        config,
    )
    finalized = await orchestrator.finalize_report(
        draft.model_copy(
            update={
                "markdown": revised,
                "body_markdown": revised,
            }
        ),
        _state(),
        config,
    )

    assert "# Sources" in revised
    assert "https://approved.example/source" in revised
    assert finalized["final_report"] == revised


@pytest.mark.asyncio
async def test_revisor_keeps_complete_input_for_native_window_budgeting(monkeypatch) -> None:
    """The native model window budgets whole records without clipping the draft."""
    captured: dict[str, str] = {}

    async def fake_reviser(prompt, *_args, **_kwargs):
        captured["messages"] = prompt
        captured["prompt"] = "\n".join(message.content for message in prompt)
        return "# Revised\n\nOption A remains supported [1]."

    monkeypatch.setattr(reviewer_module, "_invoke_reviser", fake_reviser)
    state = _state()
    state["research_brief"] = "B" * 20_000
    state["evidence_registry"][0]["supporting_excerpt"] = "E" * 20_000
    issue = ReportReviewIssue(
        category="redundancy",
        severity="high",
        description="D" * 20_000,
        revision_instruction="R" * 20_000,
    )

    await reviewer_module.revise_report(
        _draft("# Draft\n\n" + "word " * 10_000 + "[1]."),
        _review("revise", issues=[issue]),
        state,
        _config(report_review_max_input_chars=1_000),
    )

    assert "word " * 10_000 in captured["prompt"]
    assert "E" * 20_000 in captured["prompt"]
    assert issue.revision_instruction in captured["prompt"]
    assert "EV-01" in captured["prompt"]


@pytest.mark.asyncio
async def test_revisor_rejects_empty_model_output(monkeypatch) -> None:
    """An empty revision is a hard execution error, never a final report."""
    async def empty_reviser(*_args, **_kwargs):
        return "  "

    monkeypatch.setattr(reviewer_module, "_invoke_reviser", empty_reviser)
    with pytest.raises(RuntimeError, match="report_revision_empty_output"):
        await reviewer_module.revise_report(_draft(), _review("revise"), _state(), _config())


@pytest.mark.asyncio
async def test_reviewer_requires_native_port_for_every_backend():
    from open_deep_research.report.runtime import NativeReportRuntimeMissing, native_report
    token = native_report.set(None)
    try:
        for backend in ("legacy", "litellm"):
            with pytest.raises(NativeReportRuntimeMissing):
                await reviewer_module.review_report(_draft(), _state(), _config(model_backend=backend))
    finally:
        native_report.reset(token)


@pytest.mark.asyncio
async def test_reviewer_and_revisor_share_native_sql_budget(tmp_path):
    from agentscope.model import StructuredResponse
    from open_deep_research.agentscope_runtime.recovery import ApprovalPending, RecoverySession
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.agentscope_runtime.report import _ReportRun
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.report.runtime import native_report
    from tests.as_runtime.test_report_native import Factory, state

    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "budget.db").as_posix())
    await store.create_tables()
    snapshot = state()
    await store.create_run("owner", snapshot, limits={"model_calls": 1})
    recovery = await RecoverySession.open(store, snapshot.run_id, "owner")
    factory = Factory()

    async def review(messages, schema):
        return StructuredResponse(content=_review("pass").model_dump(mode="json"))

    factory.generate_structured_output = review
    token = native_report.set(_ReportRun(ResearchModels(factory, recovery=recovery), snapshot))
    try:
        cfg = Configuration()
        await reviewer_module._invoke_reviewer({}, {}, cfg, attempt=1)
        prompt = reviewer_module._revision_prompt(_draft(), _review("revise"), _state(), {})
        with pytest.raises(ApprovalPending):
            await reviewer_module._invoke_reviser(prompt, {}, cfg)
        assert (await store.budget(snapshot.run_id, "owner"))["used"]["model_calls"] == 1
        assert (await store.budget(snapshot.run_id, "owner"))["reserved"].get("model_calls", 0) == 0
    finally:
        native_report.reset(token)
        await recovery.close()
        await store.aclose()


@pytest.mark.asyncio
async def test_reviewer_uses_frozen_rigor_thresholds_for_dimension_aggregation(monkeypatch) -> None:
    """Strict runs must not be reinterpreted with the balanced default floor."""
    async def fake_invoke(*_args, **_kwargs):
        return _model_review_payload(
            dimensions={
                "coverage": 0.85,
                "citation_correctness": 0.85,
                "contradictions": 0.85,
                "unsupported_claims": 0.85,
                "redundancy": 0.85,
                "executive_readability": 0.85,
            }
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    result = await reviewer_module.review_report(
        _draft(), _state(), _config(quality_evaluation_rigor="very_strict")
    )
    assert result.decision == "revise"
    assert "score_below_critical_floor:coverage" in result.hard_failures


@pytest.mark.asyncio
async def test_reviewer_cannot_pass_when_model_marks_a_contract_requirement_missing(monkeypatch) -> None:
    """Coverage row statuses override an optimistic model decision and score."""
    async def fake_invoke(*_args, **_kwargs):
        return _model_review_payload(
            coverage=[
                {
                    "requirement_id": "COV-01",
                    "status": "missing",
                    "explanation": "Not addressed.",
                }
            ]
        )

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    result = await reviewer_module.review_report(_draft(), _state(), _config())
    assert result.decision == "revise"
    assert "coverage_requirement_missing" in result.deterministic_failures


def test_noncritical_readability_issue_is_not_counted_as_critical() -> None:
    """Presentation defects remain revisable but do not become hard evidence failures."""
    review = _review(
        "revise",
        issues=[
            ReportReviewIssue(
                category="executive_readability",
                severity="high",
                description="The summary is too diffuse.",
            )
        ],
    )
    assert review.critical_issue_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("severity,decision", [("info", "pass"), ("low", "revise")])
async def test_informational_review_notes_do_not_override_a_valid_pass(monkeypatch, severity, decision):
    async def fake_invoke(*_args, **_kwargs):
        return _model_review_payload(issues=[{
            "category": "unsupported_claims", "severity": severity,
            "description": "A bounded inference is explicitly labelled.",
        }])

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    result = await reviewer_module.review_report(_draft(), _state(), _config())
    assert result.decision == decision
    assert result.issues[0].severity == severity
    assert not result.degraded


@pytest.mark.asyncio
async def test_reviewer_repairs_unknown_requirement_id_without_rewriting_draft(monkeypatch):
    calls = []

    async def fake_invoke(payload, *_args, **_kwargs):
        calls.append(payload)
        result = _model_review_payload()
        if len(calls) == 1:
            result["coverage"][0]["requirement_id"] = "COV-unknown"
        return result

    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", fake_invoke)
    draft = _draft()
    result = await reviewer_module.review_report(draft, _state(), _config())
    assert result.decision == "pass"
    assert len(calls) == 2
    assert calls[0]["draft_markdown"] == calls[1]["draft_markdown"] == draft.markdown
    assert "unknown_requirement_id" in calls[1]["review_protocol_feedback"]["errors"]


@pytest.mark.asyncio
async def test_aggregate_only_gate_failure_degrades_without_evidence_recovery(
    monkeypatch,
) -> None:
    """Presentation scores may degrade at the limit without replacing the report."""
    draft = _draft()
    aggregate_only = _review("revise")
    aggregate_only.dimensions = ReportDimensionScores(
        coverage=1.0,
        citation_correctness=1.0,
        contradictions=1.0,
        unsupported_claims=1.0,
        redundancy=0.0,
        executive_readability=0.0,
    )
    aggregate_only.hard_failures = ["score_below_aggregate_floor"]
    calls: list[str] = []

    async def fake_build(*_args):
        calls.append("draft")
        return draft

    async def fake_review(*_args, **_kwargs):
        calls.append("review")
        return aggregate_only

    async def fake_finalize(value, *_args):
        calls.append("finalize")
        return {"final_report": value.markdown}

    async def unexpected_recovery(*_args, **_kwargs):
        raise AssertionError("aggregate-only failure must not trigger recovery")

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    monkeypatch.setattr(orchestrator, "finalize_report", fake_finalize)
    monkeypatch.setattr(orchestrator, "recover_report_draft", unexpected_recovery)

    result = await orchestrator.build_report(
        _state(),
        _config(report_review_max_revisions=0),
    )

    assert aggregate_only.hard_failure is False
    assert calls == ["draft", "review", "finalize"]
    assert result["report_review"]["status"] == "degraded"
    assert result["final_report"] == draft.markdown


def test_report_stage_interfaces_are_async_and_public() -> None:
    """All four stage functions are awaitable public orchestration boundaries."""
    for name in (
        "build_report_draft",
        "review_report",
        "revise_report",
        "finalize_report",
    ):
        function = getattr(orchestrator, name)
        assert inspect.iscoroutinefunction(function), name


@pytest.mark.asyncio
async def test_build_report_draft_records_canonical_hash_and_omits_raw_state(monkeypatch) -> None:
    """Draft conversion persists only the canonical report product metadata."""
    async def fake_legacy(_state, _config):
        return {
            "final_report": "# Canonical\n\nAnswer [1].",
            "sources": {
                "type": "override",
                "value": [
                    {
                        "title": "Approved source",
                        "url": "https://approved.example/source",
                    }
                ],
            },
            "coverage_checklist": {"type": "override", "value": ["COV-01"]},
            "evaluation_snapshot": {"schema_version": "1.0"},
            "quality_gate": {"status": "passed"},
            "notes": ["RAW_NOTE_SECRET"],
        }

    monkeypatch.setattr(orchestrator, "_build_report_legacy", fake_legacy)
    draft = await orchestrator.build_report_draft(_state(), _config())

    assert draft.markdown == "# Canonical\n\nAnswer [1]."
    assert draft.body_markdown == draft.markdown
    assert draft.sha256 == hashlib.sha256(draft.markdown.encode()).hexdigest()
    assert draft.coverage_checklist == ["COV-01"]
    assert draft.sources[0].url == "https://approved.example/source"
    assert "RAW_NOTE_SECRET" not in draft.model_dump_json()


@pytest.mark.asyncio
async def test_max_revision_limit_publishes_only_noncritical_degraded_review(monkeypatch) -> None:
    """A noncritical unresolved review is explicitly marked degraded at the limit."""
    draft = _draft()
    calls: list[str] = []
    low_issue = ReportReviewIssue(
        category="redundancy",
        severity="low",
        description="One sentence is repetitive.",
        revision_instruction="Trim the duplicate sentence.",
    )

    async def fake_build(*_args):
        calls.append("draft")
        return draft

    async def fake_review(*_args, **_kwargs):
        calls.append("review")
        return _review("revise", issues=[low_issue])

    async def fake_revise(*_args):
        calls.append("revise")
        return draft.markdown

    async def fake_finalize(*_args):
        calls.append("finalize")
        return {"final_report": draft.markdown}

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    monkeypatch.setattr(orchestrator, "revise_report", fake_revise)
    monkeypatch.setattr(orchestrator, "finalize_report", fake_finalize)

    result = await orchestrator.build_report(
        _state(), _config(report_review_max_revisions=1)
    )

    assert calls == ["draft", "review", "revise", "review", "finalize"]
    assert result["report_review"]["status"] == "degraded"
    assert result["report_revision_count"] == 1
    assert result["final_report"] == draft.markdown


@pytest.mark.asyncio
async def test_critical_revision_limit_uses_evidence_limited_recovery(monkeypatch) -> None:
    """A blocked product gets one accepted-evidence recovery and a fresh review."""
    draft = _draft()
    recovered = _draft("# Evidence-limited\n\nOption A is supported [1].")
    recovered.provenance = {
        "source": "report_review_evidence_limited_recovery",
        "report_review_recovery": True,
    }
    critical = ReportReviewIssue(
        category="unsupported_claims",
        severity="critical",
        description="The recommendation overstates the evidence.",
        revision_instruction="Remove the unsupported recommendation.",
    )
    reviews = iter((_review("revise", issues=[critical]), _review("pass")))
    calls: list[str] = []

    async def fake_build(*_args):
        calls.append("draft")
        return draft

    async def fake_review(*_args, **_kwargs):
        calls.append("review")
        return next(reviews)

    async def fake_recover(*_args, **_kwargs):
        calls.append("recover")
        return recovered

    async def fake_finalize(value, *_args):
        calls.append("finalize")
        return {"final_report": value.markdown}

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    monkeypatch.setattr(orchestrator, "recover_report_draft", fake_recover)
    monkeypatch.setattr(orchestrator, "finalize_report", fake_finalize)

    result = await orchestrator.build_report(
        _state(),
        _config(report_review_max_revisions=0),
    )

    assert calls == ["draft", "review", "recover", "review", "finalize"]
    assert result["final_report"] == recovered.markdown
    assert result["report_review"]["status"] == "degraded"
    assert result["report_revision_count"] == 0


@pytest.mark.asyncio
async def test_evidence_limited_recovery_uses_only_admitted_evidence(monkeypatch) -> None:
    """Recovery excludes quarantined evidence and records a partial provenance path."""
    captured: dict[str, Any] = {}

    async def fake_limited_writer(records, **_kwargs):
        captured["records"] = records
        return "# Evidence-limited\n\nOption A is supported [EV-01]."

    monkeypatch.setattr(
        orchestrator,
        "build_evidence_limited_report",
        fake_limited_writer,
    )
    state = _state()
    state["evidence_registry"].append(
        {
            "evidence_id": "EV-QUARANTINED",
            "claim": "Do not use this.",
            "source_url": "https://outside.example/source",
            "security_status": "quarantined",
        }
    )
    state["evidence_registry"].append(
        {
            "evidence_id": "EV-TAMPERED",
            "claim": "Do not use this either.",
            "source_url": "https://tampered.example/source",
            "security_status": "accepted",
            "hash_status": "sha256_mismatch",
        }
    )

    recovered = await orchestrator.recover_report_draft(
        _draft(),
        state,
        _config(),
        reason_codes=["unsupported_claim"],
    )

    assert [item["evidence_id"] for item in captured["records"]] == ["EV-01"]
    assert recovered.provenance["report_review_recovery"] is True
    assert recovered.finalization["completion_decision"]["value"]["action"] == "complete_partial"
    assert [source.url for source in recovered.sources] == [
        "https://approved.example/source"
    ]


@pytest.mark.asyncio
async def test_critical_review_without_eligible_evidence_fails_insufficient(monkeypatch) -> None:
    """A terminal report defect cannot recover from rejected or absent evidence."""
    critical = ReportReviewIssue(
        category="unsupported_claims",
        severity="critical",
        description="No claim has accepted support.",
    )

    async def fake_build(*_args):
        return _draft()

    async def fake_review(*_args, **_kwargs):
        return _review("revise", issues=[critical])

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    state = _state()
    state["evidence_registry"] = []

    with pytest.raises(RuntimeError, match="insufficient_evidence"):
        await orchestrator.build_report(
            state,
            _config(report_review_max_revisions=0),
        )


@pytest.mark.asyncio
async def test_failed_review_never_calls_finalizer_or_publishes_report(monkeypatch) -> None:
    """Unrecoverable report-review failures stop before finalization."""
    draft = _draft()
    finalized = False

    async def fake_build(*_args):
        return draft

    async def fake_review(*_args, **_kwargs):
        return _review("fail", status="failed")

    async def fake_finalize(*_args):
        nonlocal finalized
        finalized = True
        return {"final_report": draft.markdown}

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    monkeypatch.setattr(orchestrator, "finalize_report", fake_finalize)

    with pytest.raises(RuntimeError, match="report_review_failed"):
        await orchestrator.build_report(_state(), _config())
    assert finalized is False


@pytest.mark.asyncio
async def test_finalize_report_renders_non_markdown_artifacts_from_reviewed_markdown(monkeypatch) -> None:
    """Every alternate artifact must use the exact reviewed Markdown string."""
    captured: dict[str, str] = {}

    def fake_render(result, _fmt, _ctx):
        captured["markdown"] = result.body_markdown
        return {"structured": {"body": result.body_markdown}}

    monkeypatch.setattr(orchestrator, "render_artifacts", fake_render)
    draft = _draft("# Reviewed\n\nFinal answer [1].")
    draft.output_format = "structured_json"
    result = await orchestrator.finalize_report(draft, _state(), _config())

    assert captured["markdown"] == draft.markdown
    assert result["final_report"] == draft.markdown
    assert result["report_artifacts"]["structured"]["body"] == draft.markdown


@pytest.mark.asyncio
async def test_finalize_report_never_reuses_a_pre_review_artifact(monkeypatch) -> None:
    """Renderer failure is terminal instead of returning a stale legacy artifact."""
    def fail_render(*_args, **_kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(orchestrator, "render_artifacts", fail_render)
    draft = _draft("# Reviewed\n\nFinal answer [1].")
    draft.output_format = "structured_json"
    draft.finalization = {"report_artifacts": {"structured": {"body": "OLD DRAFT"}}}

    with pytest.raises(RuntimeError, match="renderer unavailable"):
        await orchestrator.finalize_report(draft, _state(), _config())


@pytest.mark.asyncio
@pytest.mark.parametrize("first_decision", ["revise", "fail"])
async def test_enabled_build_report_runs_reviewer_revisor_then_finalizer(monkeypatch, first_decision) -> None:
    """A revise result must loop back through review before finalization."""
    draft = _draft()
    revised = _draft("# Final\n\nOption A is recommended [1].")
    first_review = _review(
        first_decision,
        issues=[
            ReportReviewIssue(
                category="executive_readability",
                severity="high",
                location="Summary",
                description="The recommendation is not explicit.",
                revision_instruction="State the recommendation directly.",
            )
        ],
    )
    second_review = _review("pass", draft_sha256=revised.sha256)
    if first_decision == "fail":
        first_review.hard_failures = ["citation_without_admitted_evidence"]
    reviews = iter((first_review, second_review))
    calls: list[str] = []

    async def fake_build(_state, _config):
        calls.append("draft")
        return draft

    async def fake_review(_draft_value, _state, _config, **_kwargs):
        calls.append("review")
        return next(reviews)

    async def fake_revise(_draft_value, _review_value, _state, _config):
        calls.append("revise")
        return revised.markdown

    async def fake_finalize(_draft_value, _state, _config):
        calls.append("finalize")
        return {
            "final_report": revised.markdown,
            "report_review": second_review.model_dump(mode="json"),
        }

    monkeypatch.setattr(orchestrator, "build_report_draft", fake_build)
    monkeypatch.setattr(orchestrator, "review_report", fake_review)
    monkeypatch.setattr(orchestrator, "revise_report", fake_revise)
    monkeypatch.setattr(orchestrator, "finalize_report", fake_finalize)

    result = await orchestrator.build_report(_state(), _config())

    assert calls == ["draft", "review", "revise", "review", "finalize"]
    assert result["final_report"] == revised.markdown
    assert result["report_review"]["decision"] == "pass"
    assert result["report_review"]["degraded"] is False


@pytest.mark.asyncio
async def test_disabled_reviewer_keeps_legacy_path_and_does_not_call_review_stages(
    monkeypatch,
) -> None:
    """The default-off feature must not invoke Reviewer/Revisor stages."""
    calls: list[str] = []

    async def unexpected(*_args, **_kwargs):
        calls.append("unexpected")
        raise AssertionError("review stage called while disabled")

    for name in (
        "build_report_draft",
        "review_report",
        "revise_report",
        "finalize_report",
    ):
        monkeypatch.setattr(orchestrator, name, unexpected)

    async def fake_legacy(*_args, **_kwargs):
        return {"final_report": "legacy report"}

    monkeypatch.setattr(orchestrator, "_build_report_legacy", fake_legacy)

    # The legacy path is exercised by existing report tests; this assertion
    # only verifies that the new stage entry points are not selected by config.
    config = _config(report_review_enabled=False)
    cfg = Configuration.from_runnable_config(config)
    assert cfg.report_review_enabled is False
    result = await orchestrator.build_report(_state(), config)
    assert result["final_report"] == "legacy report"
    assert calls == []


def test_report_review_configuration_defaults_are_compatible() -> None:
    """Reviewer defaults are off, bounded, and fail-open for legacy runs."""
    cfg = Configuration()
    assert cfg.report_review_enabled is False
    assert cfg.report_review_max_revisions == 1
    assert cfg.report_review_fail_open is True
    assert 0 <= cfg.report_review_max_revisions <= 3


def test_report_review_state_is_separate_from_research_quality_gate():
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    snapshot = ResearchSnapshot(run_id="run", config_fingerprint="fixture",
        findings=[{"assessment": {"handoff": {"accepted": True}}}],
        report_product={"report_review": {"status": "degraded"}, "report_revision_count": 2})
    restored = ResearchSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.findings[0]["assessment"]["handoff"]["accepted"] is True
    assert restored.report_product["report_review"]["status"] == "degraded"
    assert restored.report_product["report_revision_count"] == 2
    # Both values remain JSON-compatible mappings; the dedicated keys keep the
    # product-review result from overwriting research-material gate metadata.


def test_v11_freezes_reviewer_configuration_but_v10_does_not() -> None:
    """Reviewer settings belong to the v11 contract; old manifests stay valid."""
    from open_deep_research.configuration import (
        RUN_CONFIG_FROZEN_FIELDS,
        RUN_CONFIG_FROZEN_FIELDS_V10,
        RUN_CONFIG_SCHEMA_VERSION,
    )

    reviewer_fields = {
        "report_review_enabled",
        "report_review_model",
        "report_review_model_max_tokens",
        "report_review_temperature",
        "report_review_max_input_chars",
        "report_review_max_revisions",
        "report_review_fail_open",
    }
    assert RUN_CONFIG_SCHEMA_VERSION == 14
    assert reviewer_fields <= set(RUN_CONFIG_FROZEN_FIELDS)
    assert reviewer_fields.isdisjoint(set(RUN_CONFIG_FROZEN_FIELDS_V10))

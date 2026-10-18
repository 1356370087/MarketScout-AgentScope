"""Versioned product review: factual blockers, editorial advice and issue closure."""

from copy import deepcopy

import pytest

from open_deep_research.report import reviewer
from open_deep_research.report.models import ReportDraft, SourceRef


URL = "https://docs.example/required-inputs"
DIMENSIONS = {name: 0.95 for name in (
    "coverage", "citation_correctness", "contradictions", "unsupported_claims", "redundancy", "executive_readability")}


def fixture(version="quality-gate-v6"):
    draft = ReportDraft(markdown=f"# Required inputs\n\nA previous backup manifest is required. [Guide]({URL})",
        body_markdown=f"A previous backup manifest is required. [Guide]({URL})", sources=[SourceRef(title="Guide", url=URL)])
    state = {"research_brief": "Explain the required backup inputs.",
        "coverage_contract": {"schema_version": 1, "requirements": [{"requirement_id": "COV-01", "text": "Explain the required backup inputs."}]},
        "evidence_registry": [{"evidence_id": "EV-01", "claim": "A previous backup manifest is required.",
            "supporting_excerpt": "A previous backup manifest is required.", "source_url": URL,
            "source_title": "Guide", "security_status": "accepted"}]}
    config = {"configurable": {"quality_evaluation_rigor": "balanced", "report_review_fail_open": False, "max_structured_output_retries": 1},
              "metadata": {"quality_policy_version": version, "run_config_schema_version": 19}}
    raw = {"decision": "pass", "dimensions": DIMENSIONS,
        "coverage": [{"requirement_id": "COV-01", "status": "covered", "explanation": "The required input is stated."}],
        "citation_audit": [{"citation_target": URL, "supported": True, "evidence_ids": ["EV-01"]}], "issues": []}
    return draft, state, config, raw


def issue(category="citation_correctness", severity="low", **kwargs):
    return {"category": category, "severity": severity, "location": "Required inputs paragraph",
        "description": "The inline source label contains an incorrect source count.",
        "revision_instruction": "Correct the count to match the body citation inventory.", **kwargs}


@pytest.mark.parametrize("version,expected", [("quality-gate-v5", "revise"), ("quality-gate-v6", "pass")])
@pytest.mark.asyncio
async def test_optional_editorial_advice_keeps_a_complete_grounded_report(monkeypatch, version, expected):
    draft, state, config, raw = fixture(version)
    raw.update(decision="revise", issues=[issue("redundancy", description="This sentence could be shorter.", revision_instruction="Optionally shorten the sentence without changing facts.")])

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, state, config)
    assert result.decision == expected
    assert not result.hard_failures
    if version == "quality-gate-v6":
        assert result.issues[0].blocking is False
        assert result.issues[0].issue_id


@pytest.mark.parametrize("category,severity,requirement", [
    ("citation_correctness", "low", None), ("unsupported_claims", "low", None),
    ("coverage", "medium", "COV-01"), ("executive_readability", "low", "COV-01"),
])
@pytest.mark.asyncio
async def test_factual_or_explicit_requirement_defects_still_block_model_pass(monkeypatch, category, severity, requirement):
    draft, state, config, raw = fixture()
    raw["issues"] = [issue(category, severity, requirement_id=requirement)]

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, state, config)
    assert result.decision == "revise"
    assert result.issues[0].blocking is True


@pytest.mark.asyncio
async def test_revision_must_close_previous_defect_before_passing(monkeypatch):
    draft, state, config, raw = fixture()
    raw["issues"] = [issue()]

    async def initial(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", initial)
    first = await reviewer.review_report(draft, state, config)
    identity = first.issues[0].issue_id
    state["report_review_history"] = [first.model_dump(mode="json")]
    raw["issues"] = []

    async def unchecked(payload, *args, **kwargs):
        assert payload["previous_issues"][0]["issue_id"] == identity
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", unchecked)
    missing = await reviewer.review_report(draft, state, config, attempt=2)
    assert missing.decision == "fail"
    assert "review_issue_resolution_missing_or_invalid" in missing.hard_failures
    raw["issue_resolutions"] = [{"issue_id": identity, "status": "resolved", "explanation": "The revised paragraph now has one source, matching the inventory."}]
    closed = await reviewer.review_report(draft, state, config, attempt=2)
    assert closed.decision == "pass"
    assert closed.issue_resolutions[0].issue_id == identity


@pytest.mark.asyncio
async def test_rephrasing_an_unresolved_defect_does_not_change_its_identity(monkeypatch):
    draft, state, config, raw = fixture()
    raw["issues"] = [issue()]

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    first = await reviewer.review_report(draft, state, config)
    identity = first.issues[0].issue_id
    state["report_review_history"] = [first.model_dump(mode="json")]
    raw["issues"][0].update(issue_id=identity, description="The same incorrect source count remains.")
    raw["issue_resolutions"] = [{"issue_id": identity, "status": "retained", "explanation": "The source count was not corrected."}]
    second = await reviewer.review_report(draft, state, config, attempt=2)
    assert second.decision == "revise"
    assert second.issues[0].issue_id == identity
    assert not second.hard_failures


@pytest.mark.parametrize("run_version", [19, 20])
@pytest.mark.asyncio
async def test_retained_informational_feedback_is_not_an_unresolved_blocker(monkeypatch, run_version):
    draft, state, config, raw = fixture()
    config["metadata"]["run_config_schema_version"] = run_version
    raw["coverage"][0].update(answer_excerpt="A previous backup manifest is required.",
        supporting_evidence=[{"evidence_id": "EV-01", "quote": "A previous backup manifest is required."}],
        missing_aspects=[])
    raw["issues"] = [issue("executive_readability", "info", requires_change=False,
        description="The paragraph is correctly qualified; no change is needed.", revision_instruction="")]

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    first = await reviewer.review_report(draft, state, config)
    assert first.decision == "pass"
    identity = first.issues[0].issue_id
    state["report_review_history"] = [first.model_dump(mode="json")]
    raw["issues"][0]["issue_id"] = identity
    raw["issue_resolutions"] = [{"issue_id": identity, "status": "retained",
        "explanation": "The same nonblocking observation remains valid."}]
    second = await reviewer.review_report(draft, state, config, attempt=2)
    assert ("review_issue_resolution_missing_or_invalid" in second.hard_failures) == (run_version == 19)
    if run_version == 20:
        assert second.decision == "pass" and not second.hard_failures


@pytest.mark.asyncio
async def test_editorial_label_cannot_bypass_critical_score_or_source_failure(monkeypatch):
    draft, state, config, raw = fixture()
    raw.update(dimensions={**DIMENSIONS, "coverage": 0.1}, issues=[issue("redundancy")])

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, state, config)
    assert result.decision != "pass"
    assert any(code.startswith("score_below") for code in result.hard_failures)


def test_citation_inventory_separates_body_sources_from_bibliography():
    draft, state, config, _ = fixture()
    draft.markdown += f"\n\n### Sources\n[1] Guide: {URL}\n[2] Background: https://docs.example/background\n"
    payload = reviewer.build_reviewer_payload(draft, state, config)
    assert payload["citation_inventory"]["body_source_count"] == 1
    assert len(payload["citation_inventory"]["listed_source_urls"]) == 2


@pytest.mark.asyncio
async def test_revision_schema_requires_explicit_resolution_with_known_id(monkeypatch):
    from types import SimpleNamespace
    from pydantic import ValidationError
    from open_deep_research.configuration import Configuration
    draft, state, config, raw = fixture()
    payload = reviewer.build_reviewer_payload(draft, state, config)
    payload["previous_issues"] = [{"issue_id": "issue-known", "blocking": True, **issue()}]

    async def invoke(role, messages, cfg, **kwargs):
        schema = kwargs["schema"]
        with pytest.raises(ValidationError):
            schema.model_validate(raw)
        with pytest.raises(ValidationError):
            schema.model_validate({**raw, "issue_resolutions": []})
        fields = schema.model_json_schema()
        resolution_ref = fields["properties"]["issue_resolutions"]["items"]["$ref"].split("/")[-1]
        assert fields["$defs"][resolution_ref]["properties"]["issue_id"]["enum"] == ["issue-known"]
        return schema.model_validate({**raw, "issue_resolutions": [{"issue_id": "issue-known",
            "status": "resolved", "explanation": "The missing inline citation was added."}]})

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer._invoke_reviewer(payload, config, Configuration.from_runnable_config(config), attempt=2)
    assert result.issue_resolutions[0].issue_id == "issue-known"


@pytest.mark.asyncio
async def test_verification_cannot_be_mislabeled_as_a_revision_defect(monkeypatch):
    draft, state, config, raw = fixture()
    raw.update(decision="revise", issues=[issue("coverage", requires_change=False,
        description="The cited statement is already correct.", revision_instruction="No correction is needed.")])

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    invalid = await reviewer.review_report(draft, state, config)
    assert "review_issue_not_actionable" in invalid.hard_failures
    raw["issues"][0]["severity"] = "info"
    corrected = await reviewer.review_report(draft, state, config)
    assert corrected.decision == "pass"
    assert corrected.issues[0].blocking is False


@pytest.mark.asyncio
async def test_native_evidence_marker_uses_same_contract_as_writer(monkeypatch):
    draft, state, config, raw = fixture()
    identity = "ev_367c17fa27be498433d8"
    state["evidence_registry"][0]["evidence_id"] = identity
    draft.markdown = f"# Backup inputs\n\nA previous backup manifest is required. [{identity}]"
    raw["citation_audit"][0].update(citation_target=f"[{identity}]", evidence_ids=[identity])

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, state, config)
    assert result.decision == "pass"
    raw["citation_audit"][0]["citation_target"] = "[ev_invented]"
    invalid = await reviewer.review_report(draft, state, config)
    assert invalid.decision == "fail"
    assert "unknown_evidence_id" in invalid.hard_failures


@pytest.mark.parametrize("heading", ["参考来源", "参考文献", "参考资料", "Sources", "References"])
@pytest.mark.asyncio
async def test_bibliography_alone_never_satisfies_body_citation(monkeypatch, heading):
    draft, state, config, raw = fixture()
    draft.markdown = f"# Required inputs\n\nA previous backup manifest is required.\n\n## {heading}\n\n[Guide]({URL})"

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, state, config)
    assert result.decision != "pass"
    assert "report_missing_verifiable_citations" in result.hard_failures


@pytest.mark.parametrize("patch", [
    {"uncovered_requirements": ("COV-missing",)}, {"active_task_count": 1},
    {"unresolved_conflicts": ("contradiction",)}, {"has_remaining_budget": False},
    {"research_stop_reason": "budget_exhausted"}, {"evidence_count": 0}, {"cancelled": True},
])
def test_completion_boundary_preserves_real_unresolved_conditions(patch):
    from open_deep_research.agentscope_runtime.research_completion import BusinessCompletion
    from open_deep_research.completion import CompletionDecision, CompletionPolicyContext
    facts = {"evidence_count": 4, "independent_source_count": 2, **patch}
    completion = BusinessCompletion(4, lambda: CompletionPolicyContext(**facts), accept_covered_on_exit=True)
    assert completion.evaluate(exhausted=True).action != CompletionDecision.COMPLETE


def test_iteration_exhaustion_does_not_discard_verified_complete_coverage():
    from open_deep_research.agentscope_runtime.research_completion import BusinessCompletion
    from open_deep_research.completion import CompletionDecision, CompletionPolicyContext
    facts = lambda: CompletionPolicyContext(evidence_count=4, independent_source_count=2)
    legacy = BusinessCompletion(4, facts)
    current = BusinessCompletion(4, facts, accept_covered_on_exit=True)
    assert legacy.evaluate(exhausted=True).action == CompletionDecision.COMPLETE_PARTIAL
    assert current.evaluate(exhausted=True).action == CompletionDecision.COMPLETE
    assert current.result.reason == "coverage_complete_at_iteration_limit"


@pytest.mark.parametrize("supported,attempts", [(True, 2), (False, 1)])
@pytest.mark.asyncio
async def test_only_summary_mismatch_is_repaired_without_new_research(monkeypatch, supported, attempts):
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime import research_agents, research_quality
    from open_deep_research.quality.gate import HandoffAssessment

    calls = []
    outcome = research_agents.ResearchHandoff(task_id="task", research_topic="Special exception handling",
        requirement_ids=["COV-01"], compressed_research="These evidence records are missing.",
        evidence_registry=[{"evidence_id": "ev-present", "claim": "The original exception is re-raised."}])

    async def evaluate(*args, **kwargs):
        calls.append(args[1]["compressed_research"])
        scores = {"relevance": 5, "source_quality": 5, "evidence_coverage": 5, "groundedness": 5, "reason": "Checked against source records."}
        if len(calls) == 2:
            return HandoffAssessment(accepted=True, admission_status="accepted", **scores)
        return HandoffAssessment(accepted=False, unsupported_claims=["The claimed absence contradicts the registry."], **scores,
            requirement_coverage=[{"requirement_id": "COV-01", "status": "supported" if supported else "partial",
                                   "evidence_ids": ["ev-present"], "explanation": "Source support checked."}],
            deterministic_checks={"passed": True}, hard_rejection_reasons=["unsupported_claims"])

    async def notes(*args):
        return "The original exception is re-raised. [ev-present]"

    monkeypatch.setattr(research_quality, "evaluate_subagent_handoff", evaluate)
    monkeypatch.setattr(research_agents, "_evidence_handoff_notes", notes)
    quality = research_quality.NativeResearchQuality(SimpleNamespace(), lambda: {"metadata": {"quality_policy_version": "quality-gate-v6"}})
    result = await quality.handoff(outcome, {})
    assert len(calls) == attempts
    assert result.accepted is supported
    if supported:
        assert outcome.assessment["summary_repair"]["original_summary"] == calls[0]
        assert outcome.assessment["summary_repair"]["accepted"]


@pytest.mark.parametrize("run_version", [19, 20])
@pytest.mark.asyncio
async def test_summary_repair_does_not_duplicate_cited_evidence_beyond_handoff_budget(monkeypatch, run_version):
    import hashlib
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime import research_agents, research_quality
    from open_deep_research.quality.gate import evaluate_subagent_handoff

    text = "A completed batch emits one receipt and exits. " + "Additional source context. " * 35
    evidence = [{"evidence_id": identity, "claim": text, "supporting_excerpt": text,
        "source_url": f"https://docs.example/source-{i}", "security_status": "accepted"}
        for i, identity in enumerate(["ev_aaaaaaaaaaaaaaaaaaaa", "ev_bbbbbbbbbbbbbbbbbbbb"])]
    original = deepcopy(evidence)
    outcome = research_agents.ResearchHandoff(task_id="task", research_topic="Explain finite batch completion",
        requirement_ids=["COV-01"], compressed_research="These source records are missing.", evidence_registry=evidence)
    contract = {"schema_version": 1, "original_query_sha256": hashlib.sha256(outcome.research_topic.encode()).hexdigest(),
        "requirements": [{"requirement_id": "COV-01", "text": outcome.research_topic, "kind": "factual",
            "source_message_index": 0, "source_start": 0, "source_end": len(outcome.research_topic)}]}
    cfg = {"configurable": {"quality_evaluation_max_input_chars": 6000, "handoff_context_target_tokens": 8000,
        "quality_evaluation_min_sources": 2, "quality_evaluation_fail_open": False},
        "metadata": {"run_config_schema_version": run_version, "quality_policy_version": "quality-gate-v6"}}
    calls = []

    async def judge(schema, system_prompt, payload, config, **kwargs):
        calls.append(payload)
        assert {row["evidence_id"] for row in payload["evidence_registry"]} == {row["evidence_id"] for row in evidence}
        return schema.model_validate({"accepted": len(calls) == 2,
            "relevance": 5, "source_quality": 5, "evidence_coverage": 5, "groundedness": 5,
            "unsupported_claims": ["The summary falsely claims the registered sources are missing."] if len(calls) == 1 else [],
            "requirement_coverage": [{"requirement_id": "COV-01", "status": "supported",
                "evidence_ids": [row["evidence_id"] for row in evidence], "explanation": "Both source records support completion."}],
            "reason": "The facts are supported; only the original summary is incorrect."})

    async def evaluate(*args, **kwargs):
        return await evaluate_subagent_handoff(*args, **{**kwargs, "evaluator": judge})

    async def count_tokens(messages, **kwargs):
        return len(messages[0].get_text_content()) // 4

    models = SimpleNamespace(agent_model=lambda *args: SimpleNamespace(context_size=32768, count_tokens=count_tokens))
    monkeypatch.setattr(research_quality, "evaluate_subagent_handoff", evaluate)
    if run_version == 19:
        from open_deep_research.quality.gate import QualityInputBudgetExceeded
        with pytest.raises(QualityInputBudgetExceeded, match="quality_cited_evidence_does_not_fit"):
            await research_quality.NativeResearchQuality(models, lambda: cfg).handoff(outcome, contract)
        assert len(calls) == 1
        return
    result = await research_quality.NativeResearchQuality(models, lambda: cfg).handoff(outcome, contract)
    assert result.accepted and len(calls) == 2
    assert outcome.evidence_registry == original
    assert len(outcome.compressed_research) < 600
    assert outcome.assessment["summary_repair"]["attempts"] == 1

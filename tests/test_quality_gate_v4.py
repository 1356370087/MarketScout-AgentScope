"""Regression tests for coverage-bound quality-gate v4 semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_deep_research.report.runtime import HumanMessage

from open_deep_research.quality.contract import (
    AdmissionStatus,
    canonicalize_requirement_ids,
    CoverageStatus,
    HandoffPolicyInput,
    RequirementCoverage,
    build_research_coverage_contract,
    classify_research_risk,
    merge_coverage_ledger,
    resolve_handoff_admission,
)
from open_deep_research.quality.gate import (
    HANDOFF_EVALUATION_PROMPT_V4,
    TOOL_RESULT_EVALUATION_PROMPT,
    HandoffAssessment,
    ToolResultAssessment,
    _bounded_quality_payload,
    deterministic_handoff_checks,
    deterministic_tool_checks,
    evaluate_subagent_handoff,
    evaluate_tool_results,
)


def test_contract_splits_final_chinese_conjunction_in_explicit_list() -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content=(
            "验证并比较三项能力：异步 I/O、"
            "B-tree skip scan（跳跃扫描）和虚拟生成列。"
        ))
    ])
    texts = [requirement.text for requirement in contract.requirements]

    assert any("B-tree skip scan" in text for text in texts)
    assert "虚拟生成列" in texts
    assert not any(
        "B-tree skip scan" in text and "虚拟生成列" in text
        for text in texts
    )


def test_short_unstructured_sentence_is_not_forced_into_a_dimension() -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content="说明中华人民共和国政策背景。")
    ])

    assert contract.dimensions == ()
    assert len(contract.delegable_requirement_ids()) == 1
    assert "中华人民共和国政策背景" in contract.requirements[0].text


def test_contract_fallback_never_copies_an_unbounded_full_message() -> None:
    original = "请比较成<b>本</b>、安全性。" + ("补充背景信息" * 300)

    contract = build_research_coverage_contract([
        HumanMessage(content=original)
    ])

    assert contract.requirements
    assert all(len(item.text) <= 500 for item in contract.requirements)
    assert all(item.text != original for item in contract.requirements)
    unlocated = next(item for item in contract.requirements if "成 本" in item.text)
    assert unlocated.source_start == unlocated.source_end == 0
    assert unlocated.source_located is False


def test_quality_prompts_explain_all_payload_truncation_markers() -> None:
    assert "input_truncated" in TOOL_RESULT_EVALUATION_PROMPT
    assert "input_truncated" in HANDOFF_EVALUATION_PROMPT_V4
    assert "compressed_research_truncated" in HANDOFF_EVALUATION_PROMPT_V4
    assert "raw_notes_truncated" in HANDOFF_EVALUATION_PROMPT_V4


def test_payload_budget_preserves_source_urls() -> None:
    source_url = "https://example.test/" + ("source/" * 110)
    payload = {
        "research_topic": "topic " + ("t" * 700),
        "evidence_registry": [{"source_url": source_url}],
    }

    bounded = _bounded_quality_payload(payload, max_chars=1_150)

    assert bounded["evidence_registry"][0]["source_url"] == source_url
    assert bounded["input_truncated"] is True


def test_payload_budget_fails_closed_instead_of_truncating_reason_codes() -> None:
    payload = {
        "deterministic_checks": {
            "failures": ["insufficient_traceable_sources"]
        }
    }
    encoded_chars = len(json.dumps({**payload, "input_truncated": False}))

    with pytest.raises(ValueError, match="quality_payload_budget_too_small"):
        _bounded_quality_payload(payload, max_chars=encoded_chars - 1)




def test_unique_coverage_ordinal_repairs_only_hash_suffix() -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content="比较能力 A、能力 B，并仅使用官方来源。")
    ])
    allowed = list(contract.requirement_ids())
    target = allowed[0]
    ordinal, _separator, suffix = target.rpartition("-")
    typo = f"{ordinal}-{'0' if suffix[-1] != '0' else '1'}{suffix[:-1]}"

    normalized = canonicalize_requirement_ids(
        [typo, "COV-99-deadbeef"],
        contract,
    )

    assert normalized[0] == target
    assert normalized[1] == "COV-99-deadbeef"






def test_accepted_empty_coverage_records_partial_ledger_entry() -> None:
    assessment = HandoffAssessment(
        accepted=True,
        admission_status=AdmissionStatus.ACCEPTED,
        relevance=5,
        source_quality=5,
        evidence_coverage=5,
        groundedness=5,
        reason="Legacy-compatible accepted handoff.",
    )

    ledger = merge_coverage_ledger(
        {},
        task_id="task-1",
        assessment=assessment,
        owned_requirement_ids=("COV-01",),
    )

    assert ledger == {
        "COV-01": {
            "status": "partial",
            "evidence_ids": [],
            "task_ids": ["task-1"],
            "caveats": ["coverage_mapping_missing"],
        }
    }


def test_parent_dimension_status_is_derived_from_atomic_ledger() -> None:
    from open_deep_research.quality.contract import aggregate_dimension_coverage

    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    china = next(
        dimension
        for dimension in contract.dimensions
        if dimension.label == "中国政策与市场"
    )
    first, second, *_rest = china.requirement_ids

    partial = aggregate_dimension_coverage(
        contract,
        {
            first: {"status": "supported"},
            second: {"status": "partial"},
        },
    )
    china_partial = next(
        summary for summary in partial if summary.dimension_id == china.dimension_id
    )
    assert china_partial.status is CoverageStatus.PARTIAL
    assert first not in china_partial.missing_requirement_ids
    assert second in china_partial.missing_requirement_ids

    supported = aggregate_dimension_coverage(
        contract,
        {
            requirement_id: {"status": "supported"}
            for requirement_id in china.requirement_ids
        },
    )
    china_supported = next(
        summary
        for summary in supported
        if summary.dimension_id == china.dimension_id
    )
    assert china_supported.status is CoverageStatus.SUPPORTED
    assert china_supported.missing_requirement_ids == ()


def test_report_coverage_context_uses_v2_atoms_and_derived_parent_status() -> None:
    from open_deep_research.report.coverage import (
        derive_state_coverage_checklist,
        render_state_coverage_checklist,
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    china = next(
        dimension
        for dimension in contract.dimensions
        if dimension.label == "中国政策与市场"
    )
    supported_id, missing_id, *_rest = china.requirement_ids
    state = {
        "coverage_contract": contract.model_dump(mode="json"),
        "coverage_ledger": {supported_id: {"status": "supported"}},
        "messages": [HumanMessage(content="this fallback must not be used")],
    }

    checklist = derive_state_coverage_checklist(state)
    rendered = render_state_coverage_checklist(state)

    assert len(checklist) == len(contract.requirements)
    assert any(item.startswith("中国政策与市场：") for item in checklist)
    assert (
        f"[{china.dimension_id}] 中国政策与市场: derived_status=partial"
        in rendered
    )
    assert "Unmet atomic requirements:" in rendered
    missing_requirement = next(
        requirement
        for requirement in contract.requirements
        if requirement.requirement_id == missing_id
    )
    assert f"中国政策与市场：{missing_requirement.text}" in rendered


@pytest.mark.asyncio
async def test_official_only_tool_gate_projects_out_third_party_candidates(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content=(
            "请仅依据 PostgreSQL 官方文档说明 skip scan，"
            "不得引用第三方来源。"
        ))
    ])
    captured: dict = {}

    async def capture_evaluation(
        _schema,
        _system_prompt,
        payload,
        _config,
        **_kwargs,
    ):
        captured.update(payload)
        return ToolResultAssessment(
            decision="complete",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            corroboration=5,
            reason="Official evidence is complete.",
        )

    async def ignore_activity(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        capture_evaluation,
    )
    monkeypatch.setattr(
        "open_deep_research.quality.gate.publish_task_activity",
        ignore_activity,
    )
    records = [
        {
            "evidence_id": "EV-OFFICIAL",
            "claim": "PostgreSQL 18 supports skip scan.",
            "supporting_excerpt": "Support for skip scan lookups.",
            "source_url": "https://www.postgresql.org/docs/release/18.0/",
            "security_status": "accepted",
        },
        {
            "evidence_id": "EV-BLOG",
            "claim": "A third-party explanation.",
            "supporting_excerpt": "Blog text.",
            "source_url": "https://example.com/postgresql-skip-scan",
            "security_status": "accepted",
        },
    ]
    tool_results = [{
        "name": "web_research",
        "content": json.dumps({"evidence": records}),
        "error": False,
    }]

    result = await evaluate_tool_results(
        "Advisory PostgreSQL task.",
        tool_results,
        {
            "configurable": {
                "quality_evaluation_min_sources": 1,
                "quality_evaluation_fail_open": False,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
                "run_id": "postgresql-source-scope",
            },
        },
        evidence_registry=records,
        coverage_contract=contract,
        requirement_ids=list(contract.requirement_ids()),
    )

    assert result.decision == "complete"
    assert captured["source_scope_enforced"] is True
    assert captured["deterministic_checks"]["source_count"] == 1
    assert [
        item["evidence_id"] for item in captured["cumulative_evidence"]
    ] == ["EV-OFFICIAL"]
    assert "example.com" not in json.dumps(captured["tool_results"])


def test_run3_artifact_replay_binds_acceptance_to_original_query() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "quality_gate_v4_run3_replay.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    contract = build_research_coverage_contract(
        [HumanMessage(content=fixture["original_query"])],
        advisory_dimensions=[fixture["supervisor_research_topic"]],
    )
    requirement_ids = contract.requirement_ids()
    assessment = fixture["v3_result_assessment"]

    assert all(
        "specific latest LangGraph version" not in requirement.text
        for requirement in contract.requirements
    )
    assert all(
        "langchain-ai.github.io/langgraph" not in requirement.text
        for requirement in contract.requirements
    )
    english_time_requirement = next(
        requirement
        for requirement in contract.requirements
        if requirement.text.lower() == "as of july 2026"
    )
    assert (
        fixture["original_query"][
            english_time_requirement.source_start
            : english_time_requirement.source_end
        ]
        == english_time_requirement.text
    )

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.REJECTED,
            requirement_coverage=tuple(
                RequirementCoverage(
                    requirement_id=requirement_id,
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=(f"ev-{index}",),
                    explanation="The original user requirement is supported.",
                )
                for index, requirement_id in enumerate(
                    requirement_ids,
                    start=1,
                )
            ),
            caveats=tuple(assessment["missing_information"]),
            missing_information=tuple(assessment["missing_information"]),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(
                assessment["relevance"],
                assessment["source_quality"],
                assessment["evidence_coverage"],
                assessment["corroboration"],
            ),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=requirement_ids,
    )

    assert result.admission_status.value == fixture["expected_v4"][
        "admission_when_owned_user_requirements_are_supported"
    ]
    assert result.hard_rejection_reasons == ()


def test_run3_contract_keeps_numbered_deliverables_atomic() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "quality_gate_v4_run3_replay.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    original_query = fixture["original_query"]

    contract = build_research_coverage_contract(
        [HumanMessage(content=original_query)]
    )
    texts = [requirement.text for requirement in contract.requirements]

    assert len(texts) <= 9
    assert not {"node results", "pending tasks", "database writes"} & set(texts)
    assert any(
        "Checkpointer state at super-step boundaries" in text
        and "pending tasks" in text
        for text in texts
    )
    assert any(
        "Interrupt and Command(resume=...) mechanics" in text
        and "restored vs. recalculated" in text
        for text in texts
    )
    assert any(
        "Idempotency design to avoid external side-effect duplication" in text
        and "database writes" in text
        and "file I/O" in text
        for text in texts
    )
    assert any(
        "officially guaranteed behavior versus engineering inference" in text
        for text in texts
    )
    assert any(
        "five-item Minimum Viable Reliability Checklist" in text
        for text in texts
    )
    for requirement in contract.requirements:
        assert (
            original_query[
                requirement.source_start : requirement.source_end
            ]
            == requirement.text
        )


def test_numbered_multiline_requirement_retains_exact_source_span() -> None:
    original_query = (
        "Please explain:\n"
        "1. Checkpoint state,\n"
        "   including pending tasks and metadata.\n"
        "2. Resume behavior and idempotency."
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content=original_query)]
    )
    requirement = next(
        item
        for item in contract.requirements
        if "Checkpoint state" in item.text
    )

    assert "\n" in requirement.text
    assert requirement.text != original_query
    assert (
        original_query[requirement.source_start : requirement.source_end]
        == requirement.text
    )


def test_coverage_contract_preserves_explicit_chinese_time_constraint() -> None:
    original_query = (
        "截至2026年7月，说明 LangGraph checkpointer 在 super-step "
        "边界保存哪些状态。"
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content=original_query)]
    )

    time_requirement = next(
        requirement
        for requirement in contract.requirements
        if "截至2026年7月" in requirement.text
    )
    assert (
        original_query[
            time_requirement.source_start : time_requirement.source_end
        ]
        == time_requirement.text
    )




def test_supervisor_advisory_requirement_cannot_hard_reject_handoff() -> None:
    contract = build_research_coverage_contract(
        [
            HumanMessage(
                content=(
                    "截至2026年7月，说明 LangGraph checkpointer 在 super-step "
                    "边界保存哪些状态，并区分官方保证与工程推断。"
                )
            )
        ],
        advisory_dimensions=[
            "必须给出精确 LangGraph 版本号",
            "必须只使用已迁移的旧文档域名",
        ],
    )
    requirement_ids = tuple(
        requirement.requirement_id for requirement in contract.requirements
    )

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.REJECTED,
            requirement_coverage=tuple(
                RequirementCoverage(
                    requirement_id=requirement_id,
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=("ev-1",),
                    explanation="Official documentation supports the requirement.",
                )
                for requirement_id in requirement_ids
            ),
            caveats=(
                "A precise package version was not stated.",
                "The advisory legacy documentation hostname was not used.",
            ),
            missing_information=(
                "A precise package version was not stated.",
            ),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(4, 5, 3, 4),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=requirement_ids,
    )

    assert result.admission_status is AdmissionStatus.ACCEPTED_WITH_CAVEATS
    assert result.accepted is True
    assert result.hard_rejection_reasons == ()


def test_explicit_user_requirement_remains_a_hard_gate() -> None:
    contract = build_research_coverage_contract(
        [
            HumanMessage(
                content=(
                    "请给出截至2026年7月的精确 LangGraph 版本号，并说明 "
                    "checkpointer 保存的字段。"
                )
            )
        ]
    )
    version_requirement = next(
        requirement
        for requirement in contract.requirements
        if "版本号" in requirement.text
    )

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.ACCEPTED,
            requirement_coverage=(
                RequirementCoverage(
                    requirement_id=version_requirement.requirement_id,
                    status=CoverageStatus.UNSUPPORTED,
                    evidence_ids=(),
                    explanation="No official version evidence was supplied.",
                ),
            ),
            caveats=(),
            missing_information=("The exact version is missing.",),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(4, 5, 4, 4),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=(version_requirement.requirement_id,),
    )

    assert result.admission_status is AdmissionStatus.REJECTED
    assert result.accepted is False
    assert result.hard_rejection_reasons == (
        f"required_coverage_missing:{version_requirement.requirement_id}",
    )


def test_unsupported_claim_never_enters_caveat_admission() -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content="说明 LangGraph 的 checkpoint 恢复机制。")]
    )
    requirement_id = contract.requirements[0].requirement_id

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.ACCEPTED_WITH_CAVEATS,
            requirement_coverage=(
                RequirementCoverage(
                    requirement_id=requirement_id,
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=("ev-1",),
                    explanation="Covered.",
                ),
            ),
            caveats=("One optional detail is unavailable.",),
            missing_information=(),
            unsupported_claims=("Checkpoint writes are globally atomic.",),
            deterministic_checks_passed=True,
            scores=(5, 5, 4, 4),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=(requirement_id,),
    )

    assert result.admission_status is AdmissionStatus.REJECTED
    assert "unsupported_claims" in result.hard_rejection_reasons


def test_researcher_is_judged_only_on_owned_requirement_ids() -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    china = next(
        dimension
        for dimension in contract.dimensions
        if dimension.label == "中国政策与市场"
    )
    owned, other = (
        next(
            requirement
            for requirement in contract.requirements
            if requirement.requirement_id == requirement_id
        )
        for requirement_id in china.requirement_ids[:2]
    )

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.ACCEPTED,
            requirement_coverage=(
                RequirementCoverage(
                    requirement_id=owned.requirement_id,
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=("ev-1",),
                    explanation="Covered by the assigned task.",
                ),
                RequirementCoverage(
                    requirement_id=other.requirement_id,
                    status=CoverageStatus.UNSUPPORTED,
                    evidence_ids=(),
                    explanation="Owned by a different task.",
                ),
            ),
            caveats=(),
            missing_information=(),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(5, 5, 5, 5),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=(owned.requirement_id,),
    )

    assert result.admission_status is AdmissionStatus.ACCEPTED


def test_atomic_contract_does_not_lower_balanced_dimension_floor() -> None:
    requirement_id = "COV-atomic"
    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.ACCEPTED,
            requirement_coverage=(
                RequirementCoverage(
                    requirement_id=requirement_id,
                    status=CoverageStatus.SUPPORTED,
                    evidence_ids=("ev-1",),
                    explanation="The atomic requirement is supported.",
                ),
            ),
            caveats=(),
            missing_information=(),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(4, 4, 2, 4),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=False,
        ),
        owned_requirement_ids=(requirement_id,),
    )

    assert result.admission_status is AdmissionStatus.REJECTED
    assert "score_below_dimension_floor" in result.hard_rejection_reasons


def test_high_risk_keyword_disables_caveat_admission() -> None:
    risk = classify_research_risk(
        "请根据症状给出诊断和处方剂量建议。",
        mode="auto",
    )
    coverage = RequirementCoverage(
        requirement_id="COV-01",
        status=CoverageStatus.SUPPORTED,
        evidence_ids=("ev-1",),
        explanation="Covered.",
    )

    result = resolve_handoff_admission(
        HandoffPolicyInput(
            requested_status=AdmissionStatus.ACCEPTED_WITH_CAVEATS,
            requirement_coverage=(coverage,),
            caveats=("One secondary source is unavailable.",),
            missing_information=(),
            unsupported_claims=(),
            deterministic_checks_passed=True,
            scores=(5, 5, 5, 5),
            dimension_floor=3,
            average_floor=3.0,
            caveat_admission_enabled=True,
            high_risk=risk.level == "high",
        ),
        owned_requirement_ids=("COV-01",),
    )

    assert risk.level == "high"
    assert any(
        rule_id.startswith("medical.")
        for rule_id in risk.matched_rule_ids
    )
    assert result.admission_status is AdmissionStatus.REJECTED
    assert "high_risk_caveats_disallowed" in result.hard_rejection_reasons


def test_trade_economics_is_not_misclassified_as_personal_finance_risk() -> None:
    economics = classify_research_risk(
        "Compare international trade flows and trading volumes in economic history.",
        mode="auto",
    )
    advice = classify_research_risk(
        "Recommend a trading strategy and whether I should buy or sell today.",
        mode="auto",
    )

    assert economics.level == "standard"
    assert advice.level == "high"
    assert "finance.trading" in advice.matched_rule_ids


def test_finance_risk_handles_securities_without_flagging_laptop_purchase() -> None:
    securities = classify_research_risk(
        "Should I buy or sell these securities?",
        mode="auto",
    )
    laptop = classify_research_risk(
        "Compare reviews before I buy a laptop now.",
        mode="auto",
    )

    assert securities.level == "high"
    assert laptop.level == "standard"


def test_plain_text_none_error_type_is_not_a_tool_failure() -> None:
    checks = deterministic_tool_checks(
        [{
            "name": "legacy_tool",
            "content": 'prefix {"error_type": "none"} suffix',
            "error": False,
        }],
        min_sources=0,
    )

    assert checks["passed"] is True
    assert checks["error_count"] == 0


@pytest.mark.asyncio
async def test_v4_fail_open_evaluator_error_does_not_admit_empty_coverage(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content="说明 LangGraph 的 checkpoint 恢复机制。")]
    )
    requirement_id = contract.requirements[0].requirement_id

    async def fail_judge(*_args, **_kwargs):
        raise TimeoutError("quality judge unavailable")

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fail_judge,
    )
    handoff = {
        "compressed_research": (
            "Detailed checkpoint evidence from two official sources. "
        )
        * 10,
        "evidence_registry": [
            {
                "evidence_id": "ev-a",
                "claim": "Checkpoint state is persisted.",
                "supporting_excerpt": "Checkpoint state is persisted.",
                "source_url": "https://docs.example/checkpoints",
                "source_title": "Official checkpoints",
                "security_status": "accepted",
            },
            {
                "evidence_id": "ev-b",
                "claim": "Resume restores persisted state.",
                "supporting_excerpt": "Resume restores persisted state.",
                "source_url": "https://api.example/checkpoints",
                "source_title": "Official API",
                "security_status": "accepted",
            },
        ],
        "metrics": {"sources_read": 2},
    }
    config = {
        "configurable": {
            "quality_evaluation_fail_open": True,
            "quality_evaluation_min_sources": 2,
        },
        "metadata": {
            "quality_policy_version": "quality-gate-v4",
            "runtime_config_frozen": True,
            "run_id": "v4-fail-open-empty-coverage",
        },
    }

    result = await evaluate_subagent_handoff(
        "Advisory checkpoint task.",
        handoff,
        config,
        coverage_contract=contract,
        requirement_ids=[requirement_id],
    )

    assert result.accepted is False
    assert result.admission_status is AdmissionStatus.REJECTED
    assert result.requirement_coverage == []
    assert result.evaluator_error == "quality judge unavailable"
    assert "quality_evaluator_unavailable" in result.hard_rejection_reasons
    assert "free-text handoff is not admitted" in result.reason


@pytest.mark.asyncio
async def test_v4_fail_open_does_not_bypass_required_coverage(
    monkeypatch,
) -> None:
    """An outer judge outage must not admit free text without coverage mapping."""
    contract = build_research_coverage_contract(
        [HumanMessage(content="说明 LangGraph 的 checkpoint 恢复机制。")]
    )
    requirement_id = contract.requirements[0].requirement_id

    async def fail_judge(*_args, **_kwargs):
        raise TimeoutError("quality judge unavailable")

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fail_judge,
    )
    handoff = {
        "compressed_research": "Detailed checkpoint evidence. " * 20,
        "evidence_registry": [
            {
                "evidence_id": "ev-a",
                "claim": "Checkpoint state is persisted.",
                "supporting_excerpt": "Checkpoint state is persisted.",
                "source_url": "https://docs.example/checkpoints",
                "source_title": "Official checkpoints",
                "security_status": "accepted",
            },
            {
                "evidence_id": "ev-b",
                "claim": "Resume restores persisted state.",
                "supporting_excerpt": "Resume restores persisted state.",
                "source_url": "https://api.example/checkpoints",
                "source_title": "Official API",
                "security_status": "accepted",
            },
        ],
        "metrics": {"sources_read": 2},
        "result_assessment": {
            "decision": "complete",
            "relevance": 5,
            "source_quality": 5,
            "evidence_coverage": 4,
            "corroboration": 4,
            "deterministic_checks": {"passed": True},
            "evaluator_error": None,
        },
    }
    config = {
        "configurable": {
            "quality_evaluation_fail_open": True,
            "quality_evaluation_min_sources": 2,
            "quality_caveat_admission_enabled": True,
        },
        "metadata": {
            "quality_policy_version": "quality-gate-v4",
            "runtime_config_frozen": True,
            "run_id": "v4-fail-open-inner-assessment",
        },
    }

    result = await evaluate_subagent_handoff(
        "Advisory checkpoint task.",
        handoff,
        config,
        coverage_contract=contract,
        requirement_ids=[requirement_id],
    )

    assert result.accepted is False
    assert result.admission_status is AdmissionStatus.REJECTED
    assert result.evaluator_error == "quality judge unavailable"
    assert any(
        reason.startswith("required_coverage_missing:")
        for reason in result.hard_rejection_reasons
    )
    assert "quality_evaluator_unavailable" in result.hard_rejection_reasons
    assert "admitting with caveats" not in result.reason


@pytest.mark.asyncio
async def test_v4_policy_without_contract_fails_closed_on_judge_outage(
    monkeypatch,
) -> None:
    async def fail_judge(*_args, **_kwargs):
        raise TimeoutError("quality judge unavailable")

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fail_judge,
    )
    result = await evaluate_subagent_handoff(
        "Advisory checkpoint task.",
        {
            "compressed_research": "Detailed checkpoint evidence. " * 20,
            "evidence_registry": [],
            "metrics": {"sources_read": 2},
        },
        {
            "configurable": {
                "quality_evaluation_fail_open": True,
                "quality_evaluation_min_sources": 0,
            },
            "metadata": {"quality_policy_version": "quality-gate-v4"},
        },
    )

    assert result.accepted is False
    assert result.admission_status is AdmissionStatus.REJECTED
    assert "quality_evaluator_unavailable" in result.hard_rejection_reasons


@pytest.mark.asyncio
async def test_v4_malformed_handoff_contract_is_rejected_not_raised(
    monkeypatch,
) -> None:
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("invalid contracts must not reach the Judge")

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fail_if_called,
    )
    result = await evaluate_subagent_handoff(
        "Advisory task.",
        {
            "compressed_research": "Grounded handoff. " * 30,
            "evidence_registry": [],
            "metrics": {"sources_read": 2},
        },
        {
            "configurable": {
                "quality_evaluation_fail_open": True,
                "quality_evaluation_min_sources": 0,
            },
            "metadata": {"quality_policy_version": "quality-gate-v4"},
        },
        coverage_contract={"requirements": "not-a-list"},
        requirement_ids=["COV-01"],
    )

    assert result.accepted is False
    assert result.admission_status is AdmissionStatus.REJECTED
    assert "coverage_contract_invalid" in result.hard_rejection_reasons


@pytest.mark.asyncio
async def test_v4_successful_handoff_judge_does_not_add_unavailable_caveat(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content="说明 LangGraph 的 checkpoint 恢复机制。")]
    )
    requirement_id = contract.requirements[0].requirement_id

    async def pass_judge(*_args, **_kwargs):
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": requirement_id,
                    "status": "supported",
                    "evidence_ids": ["ev-a"],
                    "explanation": "The official evidence supports it.",
                }
            ],
            reason="All owned requirements are supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        pass_judge,
    )
    handoff = {
        "compressed_research": "Detailed checkpoint evidence. " * 20,
        "evidence_registry": [
            {
                "evidence_id": "ev-a",
                "claim": "Checkpoint state is persisted.",
                "supporting_excerpt": "Checkpoint state is persisted.",
                "source_url": "https://docs.example/checkpoints",
                "source_title": "Official checkpoints",
                "security_status": "accepted",
            },
            {
                "evidence_id": "ev-b",
                "claim": "Resume restores persisted state.",
                "supporting_excerpt": "Resume restores persisted state.",
                "source_url": "https://api.example/checkpoints",
                "source_title": "Official API",
                "security_status": "accepted",
            },
        ],
        "metrics": {"sources_read": 2},
        "result_assessment": {
            "decision": "complete",
            "relevance": 5,
            "source_quality": 5,
            "evidence_coverage": 5,
            "corroboration": 5,
            "deterministic_checks": {"passed": True},
            "evaluator_error": None,
        },
    }
    config = {
        "configurable": {
            "quality_evaluation_fail_open": True,
            "quality_evaluation_min_sources": 2,
            "quality_caveat_admission_enabled": True,
        },
        "metadata": {
            "quality_policy_version": "quality-gate-v4",
            "runtime_config_frozen": True,
            "run_id": "v4-success-with-fail-open-enabled",
        },
    }

    result = await evaluate_subagent_handoff(
        "Advisory checkpoint task.",
        handoff,
        config,
        coverage_contract=contract,
        requirement_ids=[requirement_id],
    )

    assert result.accepted is True
    assert result.admission_status is AdmissionStatus.ACCEPTED
    assert result.evaluator_error is None
    assert "quality_evaluator_unavailable" not in result.caveats
    assert result.hard_rejection_reasons == []


@pytest.mark.asyncio
async def test_v4_structural_requirements_do_not_need_external_evidence_ids(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content=(
            "把下列内容作为一个不可拆分的单一研究任务；"
            "核验 PostgreSQL 18 skip scan；不得引用第三方来源。"
        ))
    ])
    factual_requirement = next(
        item for item in contract.requirements if "skip scan" in item.text
    )
    structural_ids = {
        item.requirement_id
        for item in contract.requirements
        if item.requirement_id != factual_requirement.requirement_id
    }
    captured: dict = {}

    async def pass_judge(_schema, _prompt, payload, _config, **_kwargs):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": item.requirement_id,
                    "status": "supported",
                    "evidence_ids": (
                        ["ev-a"]
                        if item.requirement_id
                        == factual_requirement.requirement_id
                        else []
                    ),
                    "explanation": "Satisfied by evidence or output structure.",
                }
                for item in contract.requirements
            ],
            reason="All requirements are supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        pass_judge,
    )
    handoff = {
        "compressed_research": "Grounded PostgreSQL skip scan evidence. " * 20,
        "evidence_registry": [
            {
                "evidence_id": "ev-a",
                "claim": "PostgreSQL 18 supports skip scan.",
                "supporting_excerpt": "Support for skip scan lookups.",
                "source_url": "https://www.postgresql.org/docs/release/18.0/",
                "security_status": "accepted",
            }
        ],
        "metrics": {"sources_read": 1},
    }
    result = await evaluate_subagent_handoff(
        "Advisory task.",
        handoff,
        {
            "configurable": {
                "quality_evaluation_fail_open": False,
                "quality_evaluation_min_sources": 1,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
            },
        },
        coverage_contract=contract,
        requirement_ids=list(contract.requirement_ids()),
    )

    assert result.accepted is True
    assert result.admission_status is AdmissionStatus.ACCEPTED
    assert set(captured["evidence_optional_requirement_ids"]) == structural_ids
    assert captured["owned_requirement_ids"] == [
        factual_requirement.requirement_id
    ]
    assert result.hard_rejection_reasons == []


@pytest.mark.asyncio
async def test_v4_parallel_delegation_and_final_table_are_run_level(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content=(
            "最终修复后 E2E：请并行委派两个 Subagent；"
            "A 仅根据 https://peps.python.org/pep-0703/ 总结状态与风险；"
            "不得引用其他 URL；最终用中文给出对照表；"
            "用中文输出；每个事实结论都附来源。"
        ))
    ])
    factual_requirement = next(
        item for item in contract.requirements if "peps.python.org" in item.text
    )
    citation_requirement = next(
        item for item in contract.requirements if "每个事实" in item.text
    )
    run_level_ids = {
        item.requirement_id
        for item in contract.requirements
        if (
            "并行委派" in item.text
            or "不得引用其他 URL" in item.text
            or "对照表" in item.text
            or "用中文输出" in item.text
        )
    }
    captured: dict = {}

    async def pass_judge(_schema, _prompt, payload, _config, **_kwargs):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": factual_requirement.requirement_id,
                    "status": "supported",
                    "evidence_ids": ["ev-a"],
                    "explanation": "Grounded leaf-task finding.",
                },
                {
                    "requirement_id": citation_requirement.requirement_id,
                    "status": "supported",
                    "evidence_ids": ["ev-a"],
                    "explanation": "Every factual leaf finding is cited.",
                },
            ],
            reason="The factual leaf requirement is supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        pass_judge,
    )
    result = await evaluate_subagent_handoff(
        "Advisory task A.",
        {
            "compressed_research": "Grounded PEP 703 finding [ev-a]. " * 20,
            "evidence_registry": [
                {
                    "evidence_id": "ev-a",
                    "claim": "PEP 703 defines the free-threading design.",
                    "source_url": "https://peps.python.org/pep-0703/",
                    "security_status": "accepted",
                }
            ],
            "metrics": {"sources_read": 1},
        },
        {
            "configurable": {
                "quality_evaluation_fail_open": False,
                "quality_evaluation_min_sources": 1,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
            },
        },
        coverage_contract=contract,
        requirement_ids=list(contract.requirement_ids()),
    )

    assert result.accepted is True
    assert run_level_ids <= set(captured["evidence_optional_requirement_ids"])
    assert captured["owned_requirement_ids"] == [
        factual_requirement.requirement_id,
        citation_requirement.requirement_id,
    ]


def test_explicit_url_leaf_handoff_does_not_require_global_source_floor() -> None:
    contract = build_research_coverage_contract([
        HumanMessage(content=(
            "A 仅根据 https://peps.python.org/pep-0703/ 总结状态；"
            "B 仅根据 https://numpy.org/doc/2.1/release/2.1.0-notes.html "
            "总结支持情况；不得引用其他 URL。"
        ))
    ])
    checks = deterministic_handoff_checks(
        {
            "compressed_research": "Grounded finding [ev-a]. " * 20,
            "evidence_registry": [
                {
                    "evidence_id": "ev-a",
                    "claim": "PEP 703 defines the design.",
                    "source_url": "https://peps.python.org/pep-0703/",
                    "security_status": "accepted",
                }
            ],
        },
        min_sources=3,
        coverage_contract=contract,
    )

    assert checks["passed"] is True
    assert checks["source_count"] == 1
    assert checks["required_source_count"] == 1


@pytest.mark.asyncio
async def test_v4_official_only_handoff_cannot_map_to_out_of_scope_evidence(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract(
        [
            HumanMessage(
                content=(
                    "Based solely on the LangGraph official documentation, "
                    "official API reference, and official GitHub repository, "
                    "explain checkpoint persistence."
                )
            )
        ]
    )
    requirement_ids = list(contract.requirement_ids())
    captured: dict = {}

    async def fake_evaluate(
        _schema,
        _prompt,
        payload,
        _config,
        **_kwargs,
    ):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": requirement_id,
                    "status": "supported",
                    "evidence_ids": ["EV-MINTLIFY"],
                    "explanation": "The temporary mirror supports this.",
                }
                for requirement_id in requirement_ids
            ],
            reason="All requirements are supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fake_evaluate,
    )
    handoff = {
        "compressed_research": (
            "MINTLIFY_FREE_TEXT_SECRET "
            "https://langchain-5e9cc07a.mintlify.app/oss/python/langgraph "
        )
        * 10,
        "raw_notes": ["MINTLIFY_RAW_NOTE_SECRET"],
        "evidence_registry": [
            {
                "evidence_id": "EV-DOCS",
                "claim": "Official checkpoint claim. " + ("c" * 200),
                "supporting_excerpt": "Official excerpt. " + ("e" * 200),
                "source_url": (
                    "https://docs.langchain.com/oss/python/langgraph/"
                    "persistence"
                ),
                "source_title": "LangGraph docs",
                "security_status": "accepted",
            },
            {
                "evidence_id": "EV-MINTLIFY",
                "claim": "Temporary mirror claim.",
                "supporting_excerpt": "Temporary mirror excerpt.",
                "source_url": (
                    "https://langchain-5e9cc07a.mintlify.app/oss/python/"
                    "langgraph/persistence"
                ),
                "source_title": "Temporary mirror",
                "security_status": "accepted",
            },
        ],
        "metrics": {"sources_read": 99},
    }
    config = {
        "configurable": {
            "quality_evaluation_fail_open": False,
            "quality_evaluation_min_sources": 1,
        },
        "metadata": {
            "quality_policy_version": "quality-gate-v4",
            "runtime_config_frozen": True,
            "run_id": "official-only-runtime-gate",
        },
    }

    result = await evaluate_subagent_handoff(
        "Advisory task.",
        handoff,
        config,
        coverage_contract=contract,
        requirement_ids=requirement_ids,
    )

    assert result.accepted is False
    assert result.admission_status is AdmissionStatus.REJECTED
    assert "deterministic_checks_failed" in result.hard_rejection_reasons
    assert all(
        reason.startswith("supported_requirement_has_invalid_evidence:")
        for reason in result.hard_rejection_reasons
        if reason != "deterministic_checks_failed"
    )
    assert [item["evidence_id"] for item in captured["evidence_registry"]] == [
        "EV-DOCS"
    ]
    assert "MINTLIFY_FREE_TEXT_SECRET" in captured["compressed_research"]
    assert captured["raw_notes"] == ""
    assert captured["deterministic_checks"]["source_count"] == 1
    assert captured["deterministic_checks"]["source_scope_enforced"] is True
    assert (
        "handoff_contains_out_of_scope_source_url"
        in captured["deterministic_checks"]["failures"]
    )
    assert captured["deterministic_checks"]["out_of_scope_source_count"] > 0


@pytest.mark.asyncio
async def test_v4_official_only_judge_can_evaluate_candidate_structure(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract(
        [
            HumanMessage(
                content=(
                    "Based solely on the LangGraph official documentation, "
                    "explain checkpoint persistence and provide a checklist "
                    "that labels guarantees versus engineering inference."
                )
            )
        ]
    )
    requirement_ids = list(contract.requirement_ids())
    captured: dict = {}

    async def fake_evaluate(
        _schema,
        _prompt,
        payload,
        _config,
        **_kwargs,
    ):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": requirement_id,
                    "status": "supported",
                    "evidence_ids": ["EV-DOCS"],
                    "explanation": "The official evidence supports it.",
                }
                for requirement_id in requirement_ids
            ],
            reason="All owned requirements are supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        fake_evaluate,
    )
    official_url = (
        "https://docs.langchain.com/oss/python/langgraph/persistence"
    )
    candidate = (
        "Officially Guaranteed: checkpoint state can be persisted. "
        "Engineering Inference: use durable storage in production. "
        "Five-item Checklist: configure, persist, resume, inspect, verify. "
        f"Source: {official_url}. "
    ) * 3
    handoff = {
        "compressed_research": candidate,
        "raw_notes": ["UNTRUSTED_RAW_NOTE"],
        "evidence_registry": [
            {
                "evidence_id": "EV-DOCS",
                "claim": "Checkpoint state can be persisted.",
                "supporting_excerpt": (
                    "Checkpointers save graph state at every super-step."
                ),
                "source_url": official_url,
                "source_title": "LangGraph persistence",
                "security_status": "accepted",
            }
        ],
        "metrics": {"sources_read": 1},
    }
    config = {
        "configurable": {
            "quality_evaluation_fail_open": False,
            "quality_evaluation_min_sources": 1,
        },
        "metadata": {
            "quality_policy_version": "quality-gate-v4",
            "runtime_config_frozen": True,
            "run_id": "official-candidate-structure",
        },
    }

    result = await evaluate_subagent_handoff(
        "Advisory task.",
        handoff,
        config,
        coverage_contract=contract,
        requirement_ids=requirement_ids,
    )

    assert result.accepted is True
    assert result.admission_status is AdmissionStatus.ACCEPTED
    assert captured["compressed_research"] == candidate
    assert captured["raw_notes"] == ""
    assert [
        item["evidence_id"] for item in captured["evidence_registry"]
    ] == ["EV-DOCS"]
    assert captured["deterministic_checks"]["passed"] is True
    assert captured["deterministic_checks"]["out_of_scope_source_count"] == 0






_E2E_DELIVERABLE_QUERY = (
    "调查 Python 3.13 自由线程模式的官方定位与生产可用性。"
    "最终用中文输出执行摘要、风险矩阵、生产上线前检查清单，"
    "并为关键事实提供可点击引用。不需要澄清，直接执行。"
)


def _kinds_by_text(contract) -> dict[str, str]:
    return {
        requirement.text: requirement.kind
        for requirement in contract.requirements
    }


def test_requirement_kinds_classified_at_compilation() -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content=_E2E_DELIVERABLE_QUERY)]
    )
    kinds = _kinds_by_text(contract)

    deliverable_texts = [
        text for text, kind in kinds.items() if kind == "deliverable"
    ]
    assert any("执行摘要" in text for text in deliverable_texts)
    assert any("风险矩阵" in text for text in deliverable_texts)
    assert any("检查清单" in text for text in deliverable_texts)
    assert any("可点击" in text for text in deliverable_texts)

    process_texts = [text for text, kind in kinds.items() if kind == "process"]
    assert any("澄清" in text for text in process_texts)
    assert any("直接执行" in text for text in process_texts)

    # The researchable question itself stays factual and delegable.
    factual_texts = [text for text, kind in kinds.items() if kind == "factual"]
    assert any("Python 3.13" in text for text in factual_texts)


def test_legacy_payloads_without_kind_classify_via_patterns() -> None:
    from open_deep_research.quality.contract import is_delegable_requirement

    assert is_delegable_requirement({"text": "风险矩阵"}) is False
    assert is_delegable_requirement({"text": "生产上线前检查清单"}) is False
    assert is_delegable_requirement({"text": "不需要澄清"}) is False
    assert is_delegable_requirement({"text": "直接执行"}) is False
    assert is_delegable_requirement({"text": "调查 NumPy 2.1 的支持状态"}) is True
    # Explicit non-factual kinds stay authoritative even for odd text.
    assert is_delegable_requirement({"kind": "deliverable", "text": "杂项"}) is False
    assert is_delegable_requirement({"kind": "process", "text": "杂项"}) is False


_BREVITY_QUERY = (
    "调查 2025 年全球电动汽车电池回收的政策与技术趋势，"
    "结果控制在简短报告内，篇幅不超过两页，给出简短总结。"
)


def test_brevity_constraints_classify_as_deliverable() -> None:
    """Length/brevity output constraints must not be factual coverage."""
    from open_deep_research.quality.contract import classify_requirement_kind

    assert classify_requirement_kind("控制在简短报告内") == "deliverable"
    assert classify_requirement_kind("篇幅不超过两页") == "deliverable"
    assert classify_requirement_kind("给出简短总结") == "deliverable"
    assert classify_requirement_kind("keep it short") == "deliverable"
    assert classify_requirement_kind("no more than 500 words") == "deliverable"
    assert classify_requirement_kind("将升温控制在1.5°C以内") == "factual"
    # A topical research question mentioning brevity context stays factual.
    assert classify_requirement_kind(
        "调查 2025 年全球电动汽车电池回收的政策趋势"
    ) == "factual"

    contract = build_research_coverage_contract(
        [HumanMessage(content=_BREVITY_QUERY)]
    )
    kinds = _kinds_by_text(contract)
    assert any(
        "简短" in text or "两页" in text
        for text, kind in kinds.items()
        if kind == "deliverable"
    )
    # The delegable set contains no brevity requirement.
    delegable_ids = set(contract.delegable_requirement_ids())
    for requirement in contract.requirements:
        if "简短" in requirement.text or "两页" in requirement.text:
            assert requirement.requirement_id not in delegable_ids


_LONG_ENUMERATED_QUERY = (
    "截至2025年，深度研究全球电动车电池回收："
    "覆盖中国政策、欧盟法规、欧盟委员会配套法案、美国监管要求、"
    "市场规模、增长率、回收产能、技术路线、湿法冶金、火法冶金、"
    "直接回收、梯次利用、再利用产业、白名单企业、Redwood Materials、"
    "Li-Cycle、GEM、宁德时代、Umicore 以及竞争格局，"
    "结果控制在简短报告内，篇幅不超过两页。"
)


_BRACKETED_SECTIONS_REPLAY_QUERY = (
    Path(__file__).parent
    / "fixtures"
    / "quality_gate_bracketed_sections_replay.txt"
).read_text(encoding="utf-8").rstrip("\r\n")


_MIXED_NUMBERED_QUERY = (
    "请对2024-2026年全球动力电池回收产业进行深度研究，并以中文决策简报输出。必须覆盖三个并行任务维度："
    "1. 中国政策与市场：研究中国2024-2026年动力电池回收相关政策，包括工信部法规、生产者责任延伸制度执行与市场规模。"
    "2. 欧盟法规与市场：研究EU Battery Regulation 2023/1542落地、再生材料含量目标与主要企业。"
    "3. 全球市场规模与技术趋势：基于IEA等国际机构数据给出2024-2026年市场规模预测，比较湿法冶金与直接回收技术路线。"
    "关键对比要求：比较中国与欧盟的法规体系、生产者责任制度与再生材料目标。"
    "来源要求：优先使用中国政府官网与IEA报告，每个事实性结论必须给出可点击引用链接。"
    "报告结构：必须包含执行摘要、对比表与来源列表。"
    "整体为精炼、信息密度高的简短报告。"
    "时间覆盖：覆盖2024年初至2026年8月的最新政策和市场变化。"
)


_FD1AAEC3_COMPACT_QUERY = (
    "请对2024-2026年全球动力电池回收与再利用产业进行一次真实深度研究，并以中文决策简报输出。"
    "必须覆盖五个并行任务维度："
    "1. 中国政策与市场：研究中国2024-2026年动力电池回收相关政策，包括工信部、发改委法规与管理办法、"
    "生产者责任延伸制度执行、再生材料目标、市场规模和主要参与者。"
    "2. 欧盟法规与市场：研究欧盟2024-2026年相关法规，包括EU Battery Regulation 2023/1542落地、"
    "生产者责任延伸、钴锂镍最低再生材料含量、碳足迹要求、市场规模和主要企业。"
    "3. 美国政策与市场：研究美国2024-2026年EPA、DOE政策和资金支持，包括IRA对电池供应链激励、"
    "生产者责任模式、再生材料政策、市场规模和主要参与者。"
    "4. 主要企业竞争格局：分析宁德时代、格林美、邦普循环、Umicore、Li-Cycle、"
    "Redwood Materials、ACCUREC等企业的产能规模、技术路线、市场份额与合作并购动态。"
    "5. 全球市场规模与技术趋势：基于IEA等国际机构数据，给出2024-2026年全球市场规模预测，"
    "比较湿法冶金、火法冶金、直接回收等技术路线，并分析技术趋势与创新方向。"
    "关键对比要求：比较中国、欧盟、美国三地法规体系、生产者责任制度、再生材料目标、主要参与者，"
    "以及2025-2026并展望至2027年初的机会和风险。"
    "来源要求：优先使用中国政府官网、欧盟委员会和EUR-Lex、美国EPA和DOE、IEA报告及企业官网。"
    "报告结构：必须包含执行摘要、三地对比表、关键结论、研究限制与来源列表。"
    "整体为精炼、信息密度高的简短报告。"
    "时间覆盖：基于今天日期，覆盖2024年初至2026年8月的最新政策和市场变化。"
)


def test_numbered_items_split_from_trailing_constraints() -> None:
    """编号列表末项吸收尾部全局约束时，factual 维度必须保住硬覆盖。

    E2E 50373da5：最后一个编号项吞掉其后的约束句（对比/来源/结构/篇幅），
    混合段整体命中 brevity 正则被判 deliverable，"全球市场规模与技术趋势"
    维度因此没有任何任务硬持有，Supervisor 只能错误绑定企业需求。
    """
    contract = build_research_coverage_contract(
        [HumanMessage(content=_MIXED_NUMBERED_QUERY)]
    )
    kinds = _kinds_by_text(contract)
    delegable_ids = set(contract.delegable_requirement_ids())

    dimensions = {dimension.label: dimension for dimension in contract.dimensions}

    # 父维度保留展示语义，但只有其原子子需求进入 delegable 硬契约。
    for label in (
        "中国政策与市场",
        "欧盟法规与市场",
        "全球市场规模与技术趋势",
    ):
        assert label in dimensions
        assert dimensions[label].requirement_ids
        assert set(dimensions[label].requirement_ids) <= delegable_ids
        assert not any(
            requirement.text.startswith(f"{label}：")
            for requirement in contract.requirements
            if requirement.requirement_id in delegable_ids
        )
    # 尾部全局约束按句独立分类，不再拖垮维度句。引用链接要求在无标签
    # 句中是 deliverable；在"来源要求："标签句内随整句归 process——两者
    # 都不可委派，语义等价。
    assert any(
        "可点击引用链接" in text and kind != "factual"
        for text, kind in kinds.items()
    )
    assert any(
        "简短报告" in text and kind == "deliverable"
        for text, kind in kinds.items()
    )
    # 过程性约束标签句（来源偏好/时间范围/报告结构）不是可研究维度，
    # 不得进入 delegable 集——否则 Supervisor 会把它们塞给每个任务，
    # Judge 按未覆盖维度压分（E2E af84fda9：9/9 因 evidence_coverage 硬拒）。
    assert any(
        text.startswith("来源要求") and kind == "process"
        for text, kind in kinds.items()
    )
    assert any(
        text.startswith("时间覆盖") and kind == "process"
        for text, kind in kinds.items()
    )
    for requirement in contract.requirements:
        if "简短报告" in requirement.text or "可点击引用链接" in requirement.text:
            assert requirement.requirement_id not in delegable_ids
        if requirement.text.startswith(("来源要求", "时间覆盖", "报告结构")):
            assert requirement.requirement_id not in delegable_ids


def test_numbered_dimensions_compile_to_atomic_child_requirements() -> None:
    """A coarse numbered dimension must not remain one handoff contract."""
    contract = build_research_coverage_contract(
        [HumanMessage(content=_MIXED_NUMBERED_QUERY)]
    )

    dimensions = {
        dimension.label: dimension for dimension in contract.dimensions
    }
    china = dimensions["中国政策与市场"]
    china_children = [
        requirement
        for requirement in contract.requirements
        if requirement.dimension_id == china.dimension_id
    ]

    assert len(china_children) >= 3
    assert china.requirement_ids == tuple(
        requirement.requirement_id for requirement in china_children
    )
    assert any("市场规模" in requirement.text for requirement in china_children)
    assert any(
        "生产者责任延伸" in requirement.text
        for requirement in china_children
    )
    assert not any(
        "中国政策与市场" in requirement.text
        and "生产者责任延伸" in requirement.text
        and "市场规模" in requirement.text
        for requirement in china_children
    )


def test_fd1aaec3_contract_replay_atomizes_all_six_dimensions() -> None:
    """Replay the failed E2E contract without model or network dependencies."""
    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    dimensions = {dimension.label: dimension for dimension in contract.dimensions}
    requirement_by_id = {
        requirement.requirement_id: requirement
        for requirement in contract.requirements
    }

    assert contract.schema_version == 2
    assert set(dimensions) == {
        "中国政策与市场",
        "欧盟法规与市场",
        "美国政策与市场",
        "主要企业竞争格局",
        "全球市场规模与技术趋势",
        "关键对比要求",
    }
    assert len(contract.delegable_requirement_ids()) == 33
    assert len(dimensions["主要企业竞争格局"].requirement_ids) == 7
    assert len(dimensions["关键对比要求"].requirement_ids) == 6
    assert all(
        attribute in dimensions["主要企业竞争格局"].text
        for attribute in ("产能规模", "技术路线", "市场份额", "合作并购动态")
    )

    comparison_children = {
        requirement_by_id[item].text
        for item in dimensions["关键对比要求"].requirement_ids
    }
    assert {"法规体系", "生产者责任制度", "再生材料目标", "主要参与者", "风险"} <= (
        comparison_children
    )
    assert any("机会" in item for item in comparison_children)

    for label in ("中国政策与市场", "欧盟法规与市场", "美国政策与市场"):
        assert len(dimensions[label].requirement_ids) >= 5

    market_requirements = [
        requirement_by_id[requirement_id]
        for label in ("中国政策与市场", "欧盟法规与市场", "美国政策与市场")
        for requirement_id in dimensions[label].requirement_ids
        if requirement_by_id[requirement_id].text == "市场规模"
    ]
    assert len(market_requirements) == 3
    assert len({item.dimension_id for item in market_requirements}) == 3

    global_children = [
        requirement_by_id[item].text
        for item in dimensions["全球市场规模与技术趋势"].requirement_ids
    ]
    assert any(
        all(route in text for route in ("湿法冶金", "火法冶金", "直接回收"))
        for text in global_children
    )
    assert all(not text.endswith("并") for text in global_children)

    for dimension in contract.dimensions:
        assert (
            _FD1AAEC3_COMPACT_QUERY[
                dimension.source_start : dimension.source_end
            ]
            == dimension.text
        )
        for requirement_id in dimension.requirement_ids:
            requirement = requirement_by_id[requirement_id]
            assert requirement.text != dimension.text
            assert (
                _FD1AAEC3_COMPACT_QUERY[
                    requirement.source_start : requirement.source_end
                ]
                == requirement.text
            )


def test_bracketed_multiline_contract_replay_preserves_source_structure() -> None:
    """Replay Run 146dad43's exact query, not an Arabic-numbered rewrite."""
    first = build_research_coverage_contract(
        [HumanMessage(content=_BRACKETED_SECTIONS_REPLAY_QUERY)]
    )
    second = build_research_coverage_contract(
        [HumanMessage(content=_BRACKETED_SECTIONS_REPLAY_QUERY)]
    )
    dimensions = {dimension.label: dimension for dimension in first.dimensions}
    requirement_by_id = {
        requirement.requirement_id: requirement
        for requirement in first.requirements
    }

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.original_query_sha256 == (
        "127c00a40125999e94f39684aca343f68fe05469de7e8f56e697160d509af53e"
    )
    assert set(dimensions) == {
        "中国政策与市场",
        "欧盟法规与市场",
        "美国政策与市场",
        "主要企业竞争格局",
        "全球市场规模与技术趋势",
        "关键对比要求",
    }
    assert len(dimensions["关键对比要求"].requirement_ids) == 5
    assert len(dimensions["主要企业竞争格局"].requirement_ids) == 7

    dimensioned_texts = {
        requirement.text
        for requirement in first.requirements
        if requirement.dimension_id
    }
    assert "再生材料（镍、钴、锂）目标要求" in dimensioned_texts
    assert "钴、锂、镍最低再生材料含量要求及时间节点" in dimensioned_texts
    assert "EPA和DOE相关政策与资金支持" in dimensioned_texts
    assert (
        "主要参与者（宁德时代、格林美、邦普循环等）的市场份额与动态"
        in dimensioned_texts
    )
    assert "再生材料（镍" not in dimensioned_texts
    assert not any("主要参、者" in text for text in dimensioned_texts)
    assert not any("【维度" in text for text in dimensioned_texts)

    for dimension in first.dimensions:
        assert dimension.requirement_ids
        assert (
            _BRACKETED_SECTIONS_REPLAY_QUERY[
                dimension.source_start : dimension.source_end
            ]
            == dimension.text
        )
        for requirement_id in dimension.requirement_ids:
            requirement = requirement_by_id[requirement_id]
            assert requirement.dimension_id == dimension.dimension_id
            assert requirement.source_located is True

    for requirement in first.requirements:
        assert requirement.source_located is True
        assert (
            _BRACKETED_SECTIONS_REPLAY_QUERY[
                requirement.source_start : requirement.source_end
            ]
            == requirement.text
        )

    constraint_kinds = {
        heading: next(
            requirement.kind
            for requirement in first.requirements
            if requirement.text.startswith(heading)
        )
        for heading in ("【来源要求】", "【报告结构要求】", "【时间范围】")
    }
    assert constraint_kinds == {
        "【来源要求】": "process",
        "【报告结构要求】": "deliverable",
        "【时间范围】": "process",
    }
    delegable = set(first.delegable_requirement_ids())
    assert all(
        requirement.kind != "factual"
        for requirement in first.requirements
        if not requirement.dimension_id
    )
    assert all(
        requirement.requirement_id not in delegable
        for requirement in first.requirements
        if requirement.text.startswith(tuple(constraint_kinds))
    )


@pytest.mark.parametrize("separator", ["", "\n"])
@pytest.mark.parametrize(
    "materials",
    ["再生材料（镍、钴、锂）目标要求", "再生材料(镍、钴、锂)目标要求"],
)
def test_bracketed_dimension_keeps_nested_lists_atomic(
    separator: str,
    materials: str,
) -> None:
    participant = "主要参与者（宁德时代、格林美、邦普循环等）的市场份额与动态"
    query = (
        f"【维度一：中国政策与市场】{separator}"
        f"研究中国政策，包括：{materials}；{participant}。"
    )

    contract = build_research_coverage_contract([HumanMessage(content=query)])
    dimension = contract.dimensions[0]
    children = [
        requirement.text
        for requirement in contract.requirements
        if requirement.dimension_id == dimension.dimension_id
    ]

    assert dimension.label == "中国政策与市场"
    assert children == [materials, participant]


@pytest.mark.parametrize(
    ("first_marker", "second_marker"),
    [("(1)", "(2)"), ("（1）", "（2）")],
)
def test_bracketed_comparison_accepts_ascii_and_fullwidth_item_markers(
    first_marker: str,
    second_marker: str,
) -> None:
    query = (
        "【关键对比要求】必须比较三地的以下维度："
        f"{first_marker}法规体系与监管框架；"
        f"{second_marker}产业机会与风险。"
    )

    contract = build_research_coverage_contract([HumanMessage(content=query)])
    dimension = contract.dimensions[0]
    children = [
        requirement.text
        for requirement in contract.requirements
        if requirement.dimension_id == dimension.dimension_id
    ]

    assert dimension.label == "关键对比要求"
    assert children == ["法规体系与监管框架", "产业机会与风险"]


def test_bracketed_dimensions_locate_repeated_children_in_each_parent() -> None:
    query = (
        "【维度一：中国政策】研究中国政策，包括：市场规模；市场份额。\n"
        "【维度二：欧盟政策】研究欧盟政策，包括：市场规模；市场份额。"
    )

    contract = build_research_coverage_contract([HumanMessage(content=query)])
    repeated = [
        requirement
        for requirement in contract.requirements
        if requirement.text == "市场规模"
    ]

    assert len(contract.dimensions) == 2
    assert len(repeated) == 2
    assert len({requirement.dimension_id for requirement in repeated}) == 2
    assert len({requirement.source_start for requirement in repeated}) == 2
    for requirement in repeated:
        assert query[requirement.source_start : requirement.source_end] == "市场规模"


def test_bracketed_dimension_cap_falls_back_to_exact_parent_spans() -> None:
    query = (
        "【维度一：中国政策】研究中国政策，包括：市场规模；市场份额。\n"
        "【维度二：欧盟政策】研究欧盟政策，包括：市场规模；市场份额。"
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content=query)],
        max_requirements=2,
    )
    requirement_by_id = {
        requirement.requirement_id: requirement
        for requirement in contract.requirements
    }

    assert len(contract.dimensions) == 2
    assert len(contract.delegable_requirement_ids()) == 2
    for dimension in contract.dimensions:
        assert len(dimension.requirement_ids) == 1
        requirement = requirement_by_id[dimension.requirement_ids[0]]
        assert requirement.text == dimension.text
        assert requirement.source_located is True
        assert query[requirement.source_start : requirement.source_end] == dimension.text


def test_v1_contract_payload_loads_without_parent_dimensions() -> None:
    from open_deep_research.quality.contract import ResearchCoverageContract

    payload = {
        "schema_version": 1,
        "original_query_sha256": "legacy",
        "requirements": [
            {
                "requirement_id": "COV-01-legacy",
                "text": "研究旧版需求",
                "kind": "factual",
                "source_message_index": 0,
                "source_start": 0,
                "source_end": 6,
                "source_located": True,
            }
        ],
        "advisory_dimensions": [],
    }

    contract = ResearchCoverageContract.model_validate(payload)

    assert contract.schema_version == 1
    assert contract.dimensions == ()
    assert contract.requirements[0].dimension_id is None
    assert contract.model_dump(mode="json")["schema_version"] == 1




def test_dimension_cap_falls_back_per_parent_without_dropping_dimensions() -> None:
    query = (
        "1. 中国政策与市场：包括政策法规、生产者责任、市场规模和主要参与者。"
        "2. 欧盟法规与市场：包括法规落地、生产者责任、市场规模和主要企业。"
    )
    contract = build_research_coverage_contract(
        [HumanMessage(content=query)],
        max_requirements=2,
    )

    assert len(contract.dimensions) == 2
    assert len(contract.delegable_requirement_ids()) == 2
    for dimension in contract.dimensions:
        assert len(dimension.requirement_ids) == 1
        requirement = next(
            item
            for item in contract.requirements
            if item.requirement_id == dimension.requirement_ids[0]
        )
        assert requirement.text == dimension.text


def test_constraints_survive_the_factual_cap_with_clause_aggregation() -> None:
    """Over-cap enumerations aggregate to clause granularity, not drops.

    E2E round 6: a 20-item factual enumeration squeezed the trailing brevity
    constraint out of the contract entirely.
    """
    contract = build_research_coverage_contract(
        [HumanMessage(content=_LONG_ENUMERATED_QUERY)],
        max_requirements=20,
    )
    kinds = _kinds_by_text(contract)

    # The trailing deliverable constraints survive regardless of the cap.
    assert any("简短" in text for text, kind in kinds.items() if kind == "deliverable")
    assert any("两页" in text for text, kind in kinds.items() if kind == "deliverable")

    factual = [text for text, kind in kinds.items() if kind == "factual"]
    assert len(factual) <= 20
    # At least one late clause aggregated the enumeration overflow into one
    # requirement instead of dropping it beyond the cap.
    assert any("、" in text for text in factual)


def test_coverage_units_group_items_by_clause() -> None:
    from open_deep_research.report.coverage import derive_coverage_units

    units = derive_coverage_units(
        "研究中国政策、欧盟法规与美国监管要求，并给出简短总结。"
    )
    assert len(units) == 2
    first_items = [item for item in units[0]]
    assert "欧盟法规" in " ".join(first_items) or "欧盟法规" in first_items
    assert any("简短总结" in item for item in units[1])


def test_coverage_units_keep_nested_parenthetical_lists_atomic() -> None:
    from open_deep_research.report.coverage import derive_coverage_units

    units = derive_coverage_units(
        "比较再生材料（镍、钴、锂）目标要求、市场规模。"
    )

    assert units == [["再生材料（镍、钴、锂）目标要求", "市场规模"]]


def test_deliverable_and_process_requirements_are_evidence_optional() -> None:
    from open_deep_research.quality.gate import (
        _evidence_optional_requirement_ids,
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content=_E2E_DELIVERABLE_QUERY)]
    )
    expected = {
        requirement.requirement_id
        for requirement in contract.requirements
        if requirement.kind != "factual"
    }
    assert expected  # the query must actually produce non-factual requirements
    assert set(_evidence_optional_requirement_ids(contract)) == expected


def test_non_factual_requirements_are_not_delegable() -> None:
    from open_deep_research.quality.contract import (
        coverage_bound_input_schema,
        validate_requirement_ids,
    )
    from open_deep_research.agentscope_runtime.research_agents import _Topic as ConductResearch
    from open_deep_research.agentscope_runtime.teams_tools import TaskCreateInput as StartResearchTask

    contract = build_research_coverage_contract(
        [HumanMessage(content=_E2E_DELIVERABLE_QUERY)]
    )
    factual_ids = list(contract.delegable_requirement_ids())
    non_factual_ids = [
        requirement.requirement_id
        for requirement in contract.requirements
        if requirement.kind != "factual"
    ]
    assert factual_ids and non_factual_ids

    for base_schema in (ConductResearch, StartResearchTask):
        schema = coverage_bound_input_schema(base_schema, contract)
        enum = schema.model_fields["requirement_ids"].json_schema_extra["items"][
            "enum"
        ]
        assert set(enum) == set(factual_ids)
        assert (
            schema.model_json_schema()["properties"]["requirement_ids"][
                "maxItems"
            ]
            == 3
        )

    mixed = validate_requirement_ids(
        factual_ids[:1] + non_factual_ids, contract, required=True
    )
    assert mixed == factual_ids[:1]

    with pytest.raises(ValueError, match="non_delegable_requirement_ids_only"):
        validate_requirement_ids(non_factual_ids, contract, required=True)


def test_research_task_rejects_more_than_three_atomic_requirements() -> None:
    from open_deep_research.quality.contract import validate_requirement_ids

    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )

    with pytest.raises(ValueError, match="too_many_coverage_requirement_ids:3"):
        validate_requirement_ids(
            list(contract.delegable_requirement_ids()[:4]),
            contract,
            required=True,
        )


def test_research_task_rejects_ids_spanning_parent_dimensions() -> None:
    """Atomic children from two parent dimensions must not re-form one task."""
    from open_deep_research.quality.contract import validate_requirement_ids

    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    dimensions = {
        dimension.label: dimension for dimension in contract.dimensions
    }
    china_ids = dimensions["中国政策与市场"].requirement_ids
    eu_id = dimensions["欧盟法规与市场"].requirement_ids[0]

    validate_requirement_ids([china_ids[0]], contract, required=True)
    validate_requirement_ids(list(china_ids[:3]), contract, required=True)

    with pytest.raises(ValueError, match="cross_dimension_requirement_ids"):
        validate_requirement_ids(
            [china_ids[0], eu_id], contract, required=True
        )


def test_owned_projection_keeps_company_dimension_attributes() -> None:
    """The Judge projection must carry the parent dimension's source text.

    Company-roster children are bare company names; their shared aspects
    (产能规模/技术路线/市场份额/合作并购动态) live only in the parent clause.
    Dropping it from the projection let any company fact pass the gate.
    """
    from open_deep_research.quality.gate import _owned_coverage_contract_projection

    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    company = next(
        dimension
        for dimension in contract.dimensions
        if dimension.label == "主要企业竞争格局"
    )
    projection = _owned_coverage_contract_projection(
        contract, company.requirement_ids
    )

    projected_dimensions = projection["dimensions"]
    assert len(projected_dimensions) == 1
    assert projected_dimensions[0]["text"] == company.text
    for attribute in ("产能规模", "技术路线", "市场份额", "合作并购动态"):
        assert attribute in projected_dimensions[0]["text"]
    assert {item["text"] for item in projection["requirements"]} == {
        requirement.text
        for requirement in contract.requirements
        if requirement.requirement_id in set(company.requirement_ids)
    }






def test_contract_without_delegable_requirements_allows_empty_assignment() -> None:
    from open_deep_research.quality.contract import (
        validate_requirement_ids,
    )

    contract = build_research_coverage_contract(
        [HumanMessage(content="不需要澄清，直接执行。")]
    )
    assert not contract.delegable_requirement_ids()
    assert (
        validate_requirement_ids([], contract, required=True) == []
    )


@pytest.mark.asyncio
async def test_v4_handoff_ignores_deliverable_and_process_requirements(
    monkeypatch,
) -> None:
    # Regression for the 2026-08-20 E2E: deliverable-format and process
    # requirements used to be owned by subtasks and could never cite
    # evidence, so every handoff was structurally rejected.
    contract = build_research_coverage_contract(
        [HumanMessage(content=_E2E_DELIVERABLE_QUERY)]
    )
    factual_requirement = next(
        requirement
        for requirement in contract.requirements
        if requirement.kind == "factual"
    )
    non_factual_ids = [
        requirement.requirement_id
        for requirement in contract.requirements
        if requirement.kind != "factual"
    ]
    captured: dict = {}

    async def pass_judge(_schema, _prompt, payload, _config, **_kwargs):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": factual_requirement.requirement_id,
                    "status": "supported",
                    "evidence_ids": ["ev-a"],
                    "explanation": "Grounded in official Python docs.",
                },
            ],
            reason="The factual requirement is supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        pass_judge,
    )
    handoff = {
        "compressed_research": "Python 3.13 free-threading evidence. " * 20
        + "\n## 风险矩阵\n- 实验性：高\n## 生产上线前检查清单\n- 验证 C 扩展",
        "evidence_registry": [
            {
                "evidence_id": "ev-a",
                "claim": "Free-threaded builds are experimental in 3.13.",
                "supporting_excerpt": "The free-threaded build is experimental.",
                "source_url": "https://docs.python.org/3.13/howto/free-threading-python.html",
                "security_status": "accepted",
            }
        ],
        "metrics": {"sources_read": 1},
    }
    result = await evaluate_subagent_handoff(
        "Advisory task.",
        handoff,
        {
            "configurable": {
                "quality_evaluation_fail_open": False,
                "quality_evaluation_min_sources": 1,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
            },
        },
        coverage_contract=contract,
        # The supervisor delegates every requirement ID it can see; non-factual
        # IDs must be filtered out of the owned set, not reject the handoff.
        requirement_ids=list(contract.requirement_ids()),
    )

    assert result.accepted is True
    assert result.admission_status is AdmissionStatus.ACCEPTED
    assert result.hard_rejection_reasons == []
    assert captured["owned_requirement_ids"] == [factual_requirement.requirement_id]
    assert set(captured["evidence_optional_requirement_ids"]) == set(
        non_factual_ids
    )


@pytest.mark.asyncio
async def test_handoff_judge_projection_excludes_unowned_dimension_siblings(
    monkeypatch,
) -> None:
    contract = build_research_coverage_contract(
        [HumanMessage(content=_FD1AAEC3_COMPACT_QUERY)]
    )
    china = next(
        dimension
        for dimension in contract.dimensions
        if dimension.label == "中国政策与市场"
    )
    owned_id, sibling_id, *_rest = china.requirement_ids
    owned = next(
        requirement
        for requirement in contract.requirements
        if requirement.requirement_id == owned_id
    )
    captured: dict = {}

    async def pass_judge(_schema, _prompt, payload, _config, **_kwargs):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status="accepted",
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": owned_id,
                    "status": "supported",
                    "evidence_ids": ["ev-owned"],
                    "explanation": "The assigned atomic requirement is supported.",
                }
            ],
            reason="The assigned atomic requirement is supported.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        pass_judge,
    )
    result = await evaluate_subagent_handoff(
        "Investigate one China atomic requirement.",
        {
            "compressed_research": (
                "已核验的中国政策事实 [ev-owned]。" * 20
                + f"\n### Coverage Checklist\n- {owned_id}: supported [ev-owned]"
            ),
            "evidence_registry": [
                {
                    "evidence_id": "ev-owned",
                    "claim": "The assigned China policy requirement is supported.",
                    "supporting_excerpt": "Official policy text.",
                    "source_url": "https://gov.example/policy",
                    "security_status": "accepted",
                }
            ],
            "metrics": {"sources_read": 1},
        },
        {
            "configurable": {
                "quality_evaluation_fail_open": False,
                "quality_evaluation_min_sources": 1,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
            },
        },
        coverage_contract=contract,
        requirement_ids=[owned_id],
    )

    projected = captured["coverage_contract"]
    assert result.accepted is True
    assert [item["requirement_id"] for item in projected["requirements"]] == [
        owned_id
    ]
    assert projected["dimensions"] == [
        {
            "dimension_id": china.dimension_id,
            "label": "中国政策与市场",
            "text": china.text,
            "requirement_ids": [owned_id],
        }
    ]
    assert sibling_id not in json.dumps(projected, ensure_ascii=False)
    assert captured["owned_requirements"] == [
        {
            "requirement_id": owned_id,
            "text": f"中国政策与市场：{owned.text}",
        }
    ]


def test_bound_compressed_research_keeps_head_and_tail() -> None:
    from open_deep_research.quality.gate import (
        _COMPRESSED_TRUNCATION_MARKER,
        _bound_compressed_research,
    )

    full = (
        "FINDINGS free-threaded build is experimental. " * 1200
        + "\n## 风险矩阵\n实验性:高;生态:中\n"
        + "\n## 生产上线前检查清单\n- 项 1\n- 项 2\n"
    )
    bounded = _bound_compressed_research(full, 12_000)

    assert len(bounded) <= 12_000
    assert bounded.startswith("FINDINGS")  # head (findings) preserved
    assert "风险矩阵" in bounded  # tail (deliverables) preserved
    assert "生产上线前检查清单" in bounded
    assert "项 2" in bounded  # the very end survives
    assert _COMPRESSED_TRUNCATION_MARKER.strip() in bounded

    assert _bound_compressed_research(full, len(full) + 5) == full


def test_bound_compressed_research_prioritizes_deliverable_sections() -> None:
    # Structured handoff (findings first, deliverables late): the section-aware
    # bound must keep the deliverable sections even when a positional cut would
    # place them in the omitted middle.
    from open_deep_research.quality.gate import _bound_compressed_research

    findings = "\n\n".join(
        f"**{n}. Findings Section {n}**\n\n" + ("Grounded official evidence. " * 120)
        for n in range(1, 9)
    )
    full = (
        findings
        + "\n\n**9. Executive Summary (draft for final report)**\n\n"
        + "实验性定位与生产建议摘要。"
        + "\n\n**10. Risk Matrix**\n\n"
        + "| R1 | C-extension 生态 | 高 | 中 |\n| R2 | 单线程回退 | 中 | 高 |"
        + "\n\n**11. Pre-Production Checklist**\n\n"
        + "- [ ] 验证 C 扩展\n- [ ] 基准测试"
        + "\n\n**Coverage Checklist**\n\n"
        + "| COV-07 (风险矩阵) | supported | Section 10 |"
    )
    bounded = _bound_compressed_research(full, 12_000)

    assert len(bounded) <= 12_000
    assert len(bounded) < len(full)  # truncation actually happened
    # Deliverable sections survive with their content, not just headings.
    assert "10. Risk Matrix" in bounded and "| R1 |" in bounded
    assert "11. Pre-Production Checklist" in bounded and "验证 C 扩展" in bounded
    assert "Executive Summary" in bounded
    assert "COV-07" in bounded
    # Leading findings fill the remaining budget in document order.
    assert "1. Findings Section 1" in bounded


def test_global_constraint_labels_classify_as_process() -> None:
    """约束标签句是研究过程指令，不是可委派的事实维度。"""
    from open_deep_research.quality.contract import classify_requirement_kind

    assert classify_requirement_kind(
        "来源要求：优先使用中国政府官网与IEA报告及企业官网。"
    ) == "process"
    assert classify_requirement_kind(
        "时间覆盖：基于今天日期，覆盖2024年初至2026年8月的最新政策和市场变化"
    ) == "process"
    assert classify_requirement_kind(
        "报告结构：必须包含执行摘要、三地对比表与来源列表。"
    ) == "process"
    assert classify_requirement_kind(
        "输出形式：精炼的表格清单"
    ) == "process"
    # 真实维度句不受标签规则影响。
    assert classify_requirement_kind(
        "中国政策与市场：研究中国2024-2026年动力电池回收相关政策。"
    ) == "factual"
    assert classify_requirement_kind(
        "关键对比要求：比较中国、欧盟、美国三地法规体系与再生材料目标。"
    ) == "factual"

"""Cross-cutting regressions for research quality and coverage handoffs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.agents import deep_researcher
from open_deep_research.quality.contract import (
    AdmissionStatus,
    build_research_coverage_contract,
)
from open_deep_research.quality.gate import (
    HandoffAssessment,
    _bound_compressed_research,
    _handoff_protocol_errors,
    evaluate_subagent_handoff,
)
from open_deep_research.quality.policy import get_run_quality_rigor_policy
from open_deep_research.report.coverage import derive_coverage_checklist
from open_deep_research.tools.supervisor.conduct_research import (
    definition as conduct_research_definition,
)


@pytest.mark.asyncio
async def test_first_research_batch_after_planning_uses_wave_zero(
    monkeypatch,
) -> None:
    """Supervisor planning turns must not consume research-wave ordinals."""
    observed_wave_ids: list[str] = []

    async def fake_researcher_runtime(state, config):
        observed_wave_ids.append(str(config["metadata"]["research_wave_id"]))
        return {
            "research_topic": state["research_topic"],
            "researcher_messages": [],
            "compressed_research": "Grounded finding [ev-wave].",
            "raw_notes": [],
            "evidence_registry": [
                {
                    "evidence_id": "ev-wave",
                    "claim": "Grounded finding.",
                    "source_url": "https://example.test/wave",
                    "security_status": "accepted",
                }
            ],
            "metrics": {"sources_read": 1},
        }

    monkeypatch.setattr(
        deep_researcher.researcher_runtime,
        "ainvoke",
        fake_researcher_runtime,
    )

    class FakeArtifactPath:
        def __truediv__(self, _part):
            return self

        def stat(self):
            return SimpleNamespace(st_size=128)

    class FakeRunContextStore:
        artifacts: dict[str, dict] = {}
        run_dir = FakeArtifactPath()

        def __init__(self, *_args, **_kwargs):
            pass

        def bind_fence_token(self, *_args, **_kwargs):
            return None

        def persist_task_result(self, task_id, artifact):
            self.artifacts[str(task_id)] = dict(artifact)
            return "a" * 64

        def load_task_result(self, task_id, **_kwargs):
            return dict(self.artifacts[str(task_id)])

    class DummyPublisher:
        async def publish(self, *_args, **_kwargs):
            return SimpleNamespace()

    async def ignore_activity(*_args, **_kwargs):
        return None

    async def no_public_summary(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        conduct_research_definition,
        "RunContextStore",
        FakeRunContextStore,
    )
    monkeypatch.setattr(deep_researcher, "RunContextStore", FakeRunContextStore)
    monkeypatch.setattr(
        deep_researcher,
        "event_publisher_from_config",
        lambda _config: DummyPublisher(),
    )
    monkeypatch.setattr(deep_researcher, "publish_task_activity", ignore_activity)
    monkeypatch.setattr(
        deep_researcher,
        "summarize_public_findings",
        no_public_summary,
    )
    state = {
        "enable_async_research": False,
        # Two Supervisor turns have elapsed, but the first only planned.
        "research_iterations": 2,
        "supervisor_messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "think_tool",
                        "args": {"reflection": "Plan the first research batch."},
                        "id": "think-before-research",
                    }
                ],
            ),
            ToolMessage(
                content="Planning complete.",
                tool_call_id="think-before-research",
                name="think_tool",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ConductResearch",
                        "args": {"research_topic": "First evidence batch"},
                        "id": "first-research-task",
                    }
                ],
            ),
        ],
    }

    await deep_researcher._execute_supervisor_tools(
        state,
        {
            "configurable": {
                "quality_evaluation_enabled": False,
            },
            "metadata": {"run_id": "wave-ordinal-regression"},
        },
    )

    assert observed_wave_ids == ["wave-0"]


@pytest.mark.asyncio
async def test_compression_retries_non_evidence_citation_aliases(
    monkeypatch,
) -> None:
    """Only accepted evidence IDs may survive as compression citations."""
    responses = iter(
        [
            AIMessage(
                content=(
                    "The finding is supported [1] [src_discovery] [doc_source].\n\n"
                    "### Sources\n[1] Source: https://example.test/source"
                )
            ),
            AIMessage(
                content=(
                    "The finding is supported [ev-supported, doc_source].\n\n"
                    "### Sources\n[ev-supported, doc_source] "
                    "Source: https://example.test/source"
                )
            ),
            AIMessage(
                content=(
                    "The finding is supported [ev-supported].\n\n"
                    "### Sources\n[ev-supported] "
                    "Source: https://example.test/source"
                )
            ),
        ]
    )
    call_count = 0

    async def fake_invoke(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return next(responses)

    monkeypatch.setattr(
        deep_researcher,
        "invoke_model_with_retry_observability",
        fake_invoke,
    )
    update = await deep_researcher.compress_research(
        {
            "research_topic": "Verify one finding.",
            "researcher_messages": [],
            "document_registry": [
                {
                    "document_id": "doc_source",
                    "candidate_id": "src_discovery",
                    "title": "Source",
                    "final_url": "https://example.test/source",
                }
            ],
            "evidence_registry": [
                {
                    "evidence_id": "ev-supported",
                    "document_id": "doc_source",
                    "claim": "The finding is supported.",
                    "supporting_excerpt": "The finding is supported by the source.",
                    "source_title": "Source",
                    "source_url": "https://example.test/source",
                    "security_status": "accepted",
                }
            ],
        },
        {
            "configurable": {"compression_model": "openai:deepseek-v4-flash"},
            "metadata": {"run_id": "compression-citation-regression"},
        },
    )

    assert call_count == 3
    assert "[ev-supported]" in update["compressed_research"]
    assert "[1]" not in update["compressed_research"]
    assert "src_discovery" not in update["compressed_research"]
    assert "doc_source" not in update["compressed_research"]


def test_coverage_does_not_split_the_word_participant_on_yu() -> None:
    query = (
        "比较三地法规、生产者责任、再生材料目标、"
        "主要参与者以及未来两年的机会与风险。"
    )
    checklist = derive_coverage_checklist(query)
    contract = build_research_coverage_contract([HumanMessage(content=query)])
    requirement_texts = {
        requirement.text
        for requirement in contract.requirements
        if requirement.kind == "factual"
    }

    assert "主要参与者" in checklist
    assert "未来两年的机会" in checklist
    assert "风险" in checklist
    assert {"主要参与者", "未来两年的机会", "风险"} <= requirement_texts
    assert "主要参" not in checklist
    assert not any(item.startswith("者") for item in checklist)


def test_coverage_does_not_split_gongheguo_word_internally() -> None:
    checklist = derive_coverage_checklist(
        "比较经济、中华人民共和国政策和监管环境。"
    )

    assert "中华人民共和国政策" in checklist
    assert "监管环境" in checklist
    assert "中华人民共" not in checklist
    assert "国政策" not in checklist


def test_handoff_projection_preserves_chinese_comparison_table() -> None:
    leading_findings = "\n\n".join(
        f"**发现 {index}**\n\n" + ("有证据支持的区域发现。" * 140)
        for index in range(1, 7)
    )
    # The failed Run emitted the comparison as an unlabelled Markdown table,
    # not under a heading containing "对比表".
    comparison = (
        "| Dimension | China | EU | US |\n|---|---|---|---|\n"
        "| Regulation Framework | 政策甲 | 政策乙 | 政策丙 |\n"
        "| Producer Responsibility | 责任甲 | 责任乙 | 责任丙 |\n"
        "| Recycled Content Targets | 目标甲 | 目标乙 | 目标丙 |\n"
        "| Main Participants | 企业甲 | 企业乙 | 企业丙 |"
    )
    trailing_findings = "\n\n".join(
        f"**补充发现 {index}**\n\n" + ("补充证据。" * 160)
        for index in range(1, 5)
    )
    full = (
        leading_findings
        + "\n\n"
        + comparison
        + "\n\n"
        + trailing_findings
        + "\n\n**Coverage Checklist**\n\nCOV-01: supported [ev-one]"
    )

    bounded = _bound_compressed_research(full, 5_000)

    assert len(bounded) <= 5_000
    assert "| Dimension | China | EU | US |" in bounded
    assert "| Main Participants | 企业甲 | 企业乙 | 企业丙 |" in bounded


@pytest.mark.asyncio
async def test_global_handoff_bound_keeps_chinese_comparison_table(
    monkeypatch,
) -> None:
    """The final whole-payload bound must not undo section-aware bounding."""
    captured: dict = {}
    limit = 12_000
    contract = build_research_coverage_contract(
        [
            HumanMessage(
                content=(
                    "比较中国、欧盟、美国的政策、市场规模、主要参与者与未来机会，"
                    "并给出三地对比表。"
                )
            )
        ]
    )
    owned_id = next(
        requirement.requirement_id
        for requirement in contract.requirements
        if requirement.kind == "factual"
    )
    leading = "\n\n".join(
        f"**区域发现 {index}**\n\n" + ("有证据支持的区域发现。" * 180)
        for index in range(1, 6)
    )
    table = (
        "| Dimension | China | EU | US |\n|---|---|---|---|\n"
        "| Regulation Framework | 政策甲 | 政策乙 | 政策丙 |\n"
        "| Producer Responsibility | 责任甲 | 责任乙 | 责任丙 |\n"
        "| Recycled Content Targets | 目标甲 | 目标乙 | 目标丙 |\n"
        "| Main Participants | 企业甲 | 企业乙 | 企业丙 |"
    )
    trailing = "\n\n".join(
        f"**补充发现 {index}**\n\n" + ("补充证据。" * 220)
        for index in range(1, 4)
    )
    compressed = (
        leading
        + "\n\n"
        + table
        + "\n\n"
        + trailing
        + f"\n\n**Coverage Checklist**\n\n{owned_id}: supported [ev-0]"
    )
    evidence = [
        {
            "evidence_id": f"ev-{index}",
            "claim": f"Region finding {index}. " + ("grounded claim " * 30),
            "supporting_excerpt": "Official supporting excerpt. " * 30,
            "source_title": f"Official source {index}",
            "source_url": f"https://official.example.test/source-{index}",
            "security_status": "accepted",
            "confidence": 0.95,
        }
        for index in range(8)
    ]

    async def capture_evaluation(
        _schema,
        _prompt,
        payload,
        _config,
        **_kwargs,
    ):
        captured.update(payload)
        return HandoffAssessment(
            accepted=True,
            admission_status=AdmissionStatus.ACCEPTED,
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            requirement_coverage=[
                {
                    "requirement_id": owned_id,
                    "status": "supported",
                    "evidence_ids": ["ev-0"],
                    "explanation": "The comparison is grounded.",
                }
            ],
            reason="Grounded handoff.",
        )

    monkeypatch.setattr(
        "open_deep_research.quality.gate._evaluate_json",
        capture_evaluation,
    )
    await evaluate_subagent_handoff(
        "Compare the three regions.",
        {
            "compressed_research": compressed,
            "raw_notes": [],
            "evidence_registry": evidence,
            "metrics": {"sources_read": len(evidence)},
        },
        {
            "configurable": {
                "quality_evaluation_max_input_chars": limit,
                "quality_evaluation_min_sources": 0,
            },
            "metadata": {
                "quality_policy_version": "quality-gate-v4",
                "runtime_config_frozen": True,
            },
        },
        coverage_contract=contract,
        requirement_ids=[owned_id],
    )

    assert len(json.dumps(captured, ensure_ascii=False)) <= limit
    assert "| Dimension | China | EU | US |" in captured["compressed_research"]
    assert (
        "| Main Participants | 企业甲 | 企业乙 | 企业丙 |"
        in captured["compressed_research"]
    )


def test_caveat_acceptance_with_unsupported_claim_is_downgraded() -> None:
    assessment = HandoffAssessment(
        accepted=True,
        admission_status=AdmissionStatus.ACCEPTED_WITH_CAVEATS,
        relevance=5,
        source_quality=5,
        evidence_coverage=4,
        groundedness=4,
        caveats=["One limitation remains."],
        unsupported_claims=["One claim lacks accepted evidence."],
        reason="Accept with caveats.",
    )

    assert assessment.accepted is False
    assert assessment.admission_status is AdmissionStatus.REJECTED
    errors = _handoff_protocol_errors(
        assessment,
        checks={"passed": True, "failures": []},
        policy=get_run_quality_rigor_policy(
            "balanced",
            policy_version="quality-gate-v4",
        ),
    )
    assert "caveat_acceptance_contains_unsupported_claim" not in errors

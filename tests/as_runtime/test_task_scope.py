"""Owned-subject negative controls and bounded evaluator protocol repair."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.agentscope_runtime.teams_planning import plan_source_checks
from open_deep_research.quality.contract import CoverageRequirement, ResearchCoverageContract
from open_deep_research.quality.planning import compile_planned_requirements
from open_deep_research.quality.task_scope import (
    ScopedToolResultAssessment, TaskEvaluationScope, derive_feedback, protocol_errors,
)
from test_research_migration import evidence

AWS = "Amazon OpenSearch Service 的预置域"
ELASTIC = "Elastic Cloud Hosted"


def contract():
    return ResearchCoverageContract(schema_version=4, original_query_sha256="fixture", requirements=(
        CoverageRequirement(requirement_id="aws-price", text="核查定价和区域限制", kind="factual",
                            subject=AWS, source_message_index=0, source_start=0, source_end=9),
        CoverageRequirement(requirement_id="elastic-price", text="核查定价和区域限制", kind="factual",
                            subject=ELASTIC, source_message_index=0, source_start=0, source_end=9),
        CoverageRequirement(requirement_id="premise", text="每月按730小时计算。", kind="process",
                            source_message_index=0, source_start=10, source_end=21),
    ))


def result(*, foreign=False, complete=False):
    return ScopedToolResultAssessment(
        decision="complete" if complete else "retry", relevance=4, source_quality=4,
        evidence_coverage=4, corroboration=4, reason="自由描述建议继续查 Elastic Cloud Hosted。",
        requirement_checks=[{"requirement_id": "aws-price", "subject": AWS,
                             "status": "supported" if complete else "partial", "evidence_ids": ["ev1"]}],
        gaps=[] if complete else [{
            "requirement_id": "aws-price", "kind": "factual", "target_subjects": [AWS],
            "basis_quote": "定价", "checked_evidence_ids": ["ev1"],
            "reason": "缺少 Elastic Cloud Hosted 的定价" if foreign else "缺少当前服务在目标区域的单价",
            "next_query": "Elastic Cloud Hosted pricing" if foreign else "Amazon OpenSearch Service pricing",
        }],
    )


def test_same_requirement_id_cannot_hide_another_competitor_in_the_gap():
    scope = TaskEvaluationScope.build(contract(), ["aws-price"])
    assert any("foreign_subject" in e for e in protocol_errors(result(foreign=True), scope, ["ev1"]))
    assert protocol_errors(result(), scope, ["ev1"]) == []
    feedback = derive_feedback(result())
    assert ELASTIC not in feedback.reason and ELASTIC not in " ".join(feedback.suggested_queries)
    assert ELASTIC in feedback.raw_reason  # Retained for audit, not execution.
    assert scope.fingerprint() != TaskEvaluationScope.build(contract(), ["elastic-price"]).fingerprint()
    projected = scope.contract_view(contract())
    assert {r["requirement_id"] for r in projected["requirements"]} == {"aws-price", "premise"}


@pytest.mark.parametrize("problem", ["missing_check", "unknown_evidence", "wrong_subject", "unsupported_complete", "unowned_basis"])
def test_scoped_protocol_rejects_invalid_bindings(problem):
    value = result()
    if problem == "missing_check":
        value.requirement_checks.clear()
    elif problem == "unknown_evidence":
        value.requirement_checks[0].evidence_ids = ["not-submitted"]
    elif problem == "wrong_subject":
        value.gaps[0].target_subjects = [ELASTIC]
    elif problem == "unsupported_complete":
        value.decision = "complete"
    else:
        value.gaps[0].basis_quote = "另一个厂商的增长率"
    assert protocol_errors(value, TaskEvaluationScope.build(contract(), ["aws-price"]), ["ev1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [True, False])
async def test_scope_error_repairs_without_search_or_fake_scores(repair):
    models = SimpleNamespace(recovery=None, structured=AsyncMock(side_effect=[
        result(foreign=True), result(foreign=not repair),
    ]))
    cfg = {"configurable": {"research_efficiency_mode": "bounded", "quality_evaluation_min_sources": 1,
                           "quality_evaluation_fail_open": False, "max_structured_output_retries": 2},
           "metadata": {"run_config_schema_version": 21}}
    assessment = await NativeResearchQuality(models, lambda: cfg).batch(
        ResearchAssignment(research_topic="只核查AWS", requirement_ids=["aws-price"]),
        contract().model_dump(mode="json"), [{"name": "web_research", "content": "evidence"}], [evidence()],
    )
    assert models.structured.await_count == 2
    if repair:
        assert assessment["evaluator_error"] is None
        assert assessment["gaps"][0]["target_subjects"] == [AWS]
        assert ELASTIC not in assessment["reason"]
    else:
        assert assessment["evaluator_error"] == "quality_scope_violation"
        assert not any(key in assessment for key in ("relevance", "source_quality", "evidence_coverage", "corroboration"))
        assert not assessment["suggested_queries"]


def test_explicit_comparison_keeps_both_owners():
    scope = TaskEvaluationScope.build(contract(), ["aws-price", "elastic-price"])
    assert scope.mode == "comparison" and set(scope.subjects) == {AWS, ELASTIC}
    value = result()
    value.requirement_checks.append(value.requirement_checks[0].model_copy(update={
        "requirement_id": "elastic-price", "subject": ELASTIC, "status": "supported",
    }))
    assert not protocol_errors(value, scope, ["ev1"])


def test_v21_premises_and_report_goal_do_not_become_web_obligations():
    goal = f"请进行竞品深度研究，比较{AWS}和{ELASTIC}。"
    scenario = "统一业务场景：200 个租户，每月3000万查询。"
    mixed = "希望同时支持中英文和文档级权限，检索 p95 低于800ms是待验证的目标，不是已测结论。"
    price = "核查定价和区域限制。"
    text = goal + "\n" + scenario + "\n" + mixed + price
    atoms = [{"source_message_index": 0, "source_text": q, "kind": "factual"} for q in [goal, scenario, mixed, price]]
    value = compile_planned_requirements([{"role": "user", "content": text}], atoms,
        atomic=True, preserve_scope=True, task_scopes=True, entities=[{"name": AWS}, {"name": ELASTIC}])
    assert any(r.text == goal and r.kind == "deliverable" for r in value.requirements)
    assert not any(r.kind == "factual" and ("200" in r.text or "p95" in r.text) for r in value.requirements)
    assert any(r.kind == "factual" and "文档级权限" in r.text for r in value.requirements)
    assert any(r.kind == "factual" and "定价" in r.text for r in value.requirements)
    assert all(text[r.source_start:r.source_end] == r.text for r in value.requirements)


def test_plan_sources_use_real_subdomain_boundary():
    c = contract().model_dump(mode="json")
    c["source_selection"] = {"mode": "specific", "sources": [{"type": "domain", "domain": "elastic.co"}]}
    checks = plan_source_checks({"sources": ["https://docs.elastic.co/guide", "https://opensearch.org/",
                                            "https://elastic.co.evil.example/"]}, c, {})
    assert checks["allowed"] == ["https://docs.elastic.co/guide"]
    assert len(checks["denied"]) == 2

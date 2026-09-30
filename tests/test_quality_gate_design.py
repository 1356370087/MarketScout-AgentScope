"""Regressions for the four gate defects reproduced from real E2E receipts."""

import json
from pathlib import Path

import pytest
from open_deep_research.configuration import Configuration
from open_deep_research.evidence import required_source_count, source_scoped_evidence_records
from open_deep_research.quality.contract import build_research_coverage_contract, task_coverage_contract, coverage_requirement_display_text
from open_deep_research.quality.gate import (
    HandoffAssessment, ToolResultAssessment, QualityInputBudgetExceeded,
    evaluate_subagent_handoff, evaluate_tool_results, _tool_protocol_errors, _quality_policy,
)

CASES = json.loads((Path(__file__).parent / "fixtures/quality-gate-real-handoffs.json").read_text(encoding="utf-8"))
CONFIG = {"configurable": {"quality_evaluation_enabled": True, "quality_evaluation_min_sources": 3,
                          "quality_evaluation_max_input_chars": 30000, "quality_evaluation_fail_open": True},
          "metadata": {"quality_policy_version": "quality-gate-v4"}}


def hybrid_contract():
    case = CASES["hybrid"]
    return build_research_coverage_contract(case["user_messages"]).model_copy(
        update={"source_selection": task_coverage_contract(case["contract"], []).source_selection})


@pytest.mark.asyncio
async def test_hybrid_document_leaf_admits_real_evidence_without_lowering_run_diversity():
    case = CASES["hybrid"]
    contract = hybrid_contract()
    ids = [r.requirement_id for r in contract.requirements if r.kind == "factual" and "目标响应时间" in r.text]
    assert len(ids) == 1
    leaf = task_coverage_contract(contract, ids)
    assert leaf.source_selection.mode == "documents"
    assert required_source_count(3, contract) == 3
    assert required_source_count(3, leaf, leaf=True) == 1
    assert not source_scoped_evidence_records([{**case["evidence"][0], "document_id": "foreign", "source_url": "/documents/foreign?chunk=x"}], leaf)
    captured = {}
    async def judge(schema, prompt, payload, config, **kwargs):
        captured.update(payload)
        return HandoffAssessment(accepted=True, relevance=5, source_quality=4, evidence_coverage=5,
            groundedness=5, reason="Recorded source supports the assigned numeric fact.",
            requirement_coverage=[{"requirement_id": ids[0], "status": "supported", "evidence_ids": [case["evidence"][0]["evidence_id"]]}])
    result = await evaluate_subagent_handoff(case["handoff"]["research_topic"],
        {**case["handoff"], "compressed_research": case["compressed_research"], "evidence_registry": case["evidence"]},
        CONFIG, coverage_contract=contract, requirement_ids=ids, evaluator=judge)
    assert result.accepted, result.model_dump()
    assert result.deterministic_checks["configured_min_sources"] == 3
    assert result.deterministic_checks["required_source_count"] == 1
    assert captured["coverage_contract"]["source_selection"]["mode"] == "documents"
    mixed = [r.requirement_id for r in contract.requirements if r.kind == "factual"]
    assert task_coverage_contract(contract, mixed).source_selection.mode == "hybrid"


def test_source_and_writing_directives_stay_whole_and_out_of_factual_coverage():
    contract = hybrid_contract()
    kinds = {r.text: r.kind for r in contract.requirements}
    assert kinds["分别保留资料与网页引用"] == "deliverable"
    assert kinds["不将测试目标当作数据库性能基准"] == "process"
    c = build_research_coverage_contract([{"role": "user", "content": "说明 PostgreSQL 17 的查询执行、备份和权限管理各一项改进。不将发布说明视为性能基准。"}])
    assert next(r for r in c.requirements if r.text == "不将发布说明视为性能基准").kind == "process"
    factual = [r for r in c.requirements if r.kind == "factual"]
    assert len(factual) == 3
    for requirement in factual:
        display = coverage_requirement_display_text(c, requirement)
        assert "PostgreSQL 17" in display and "各一项改进" in display
    mixed = build_research_coverage_contract([{"role":"user", "content":"给出市场份额并提供引用。"}])
    assert any("市场份额" in r.text for r in mixed.requirements if r.kind == "factual")


def test_upgrade_verification_checklist_is_owned_by_report_not_web_research():
    contract = build_research_coverage_contract([{
        "role": "user",
        "content": "研究 PostgreSQL 17 增量备份的适用条件与限制，并据此给出升级前验证清单。",
    }])
    checklist = next(r for r in contract.requirements if "验证清单" in r.text)
    assert checklist.kind == "deliverable"
    assert checklist.requirement_id not in contract.delegable_requirement_ids()
    assert any("适用条件" in r.text for r in contract.requirements if r.kind == "factual")
    assert any("限制" in r.text for r in contract.requirements if r.kind == "factual")


def test_performance_writing_constraints_do_not_become_web_evidence_tasks():
    contract = build_research_coverage_contract([{
        "role": "user",
        "content": "研究 PostgreSQL 17 的性能变化。不要把发布说明当作性能基准，不推测未公开的性能数字。",
    }])
    kinds = {r.text: r.kind for r in contract.requirements}
    assert kinds["不要把发布说明当作性能基准"] == "process"
    assert kinds["不推测未公开的性能数字"] == "process"
    assert any("性能变化" in r.text for r in contract.requirements if r.kind == "factual")
    question = build_research_coverage_contract([{
        "role": "user", "content": "分析未公开性能数字带来的评估风险。",
    }])
    assert question.delegable_requirement_ids()


def test_selected_named_documents_and_summary_are_not_factual_obligations():
    contract = build_research_coverage_contract([{
        "role": "user",
        "content": "仅依据所选三份 PostgreSQL 17 官方文档，说明增量备份恢复流程。包含摘要。",
    }])
    kinds = {r.text: r.kind for r in contract.requirements}
    assert kinds["仅依据所选三份 PostgreSQL 17 官方文档"] == "process"
    assert kinds["包含摘要"] == "deliverable"
    assert any("恢复流程" in r.text for r in contract.requirements if r.kind == "factual")


@pytest.mark.asyncio
async def test_query_execution_owner_keeps_atomic_text_separate_from_sibling_context():
    """A live PG17 handoff wrongly inherited backup and planning-example gaps."""
    contract = build_research_coverage_contract([{
        "role": "user",
        "content": "说明 PostgreSQL 17 的查询执行、备份和权限管理各一项改进。",
    }])
    owned = next(r for r in contract.requirements if "查询执行" in r.text)
    case = CASES["web_review_long"]
    captured = {}

    async def judge(schema, prompt, payload, config, **kwargs):
        captured.update(payload)
        return HandoffAssessment(
            accepted=False, relevance=1, source_quality=1,
            evidence_coverage=1, groundedness=1, reason="Projection-only test",
        )

    await evaluate_subagent_handoff(
        "查询执行，例如排序内存、窗口函数优化",
        {**case["handoff"], "compressed_research": case["compressed_research"],
         "evidence_registry": case["evidence"]},
        CONFIG, coverage_contract=contract,
        requirement_ids=[owned.requirement_id], evaluator=judge,
    )
    assert captured["owned_requirements"] == [{
        "requirement_id": owned.requirement_id,
        "text": owned.text,
        "dimension_id": owned.dimension_id,
    }]
    assert "备份" not in captured["owned_requirements"][0]["text"]
    dimension = captured["coverage_contract"]["dimensions"][0]
    assert "各一项改进" in dimension["text"]
    assert dimension["requirement_ids"] == [owned.requirement_id]
    assert "窗口函数" not in json.dumps(captured["coverage_contract"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_real_long_handoff_is_not_reduced_to_a_title():
    case = CASES["web_review_long"]
    captured = {}
    async def judge(schema, prompt, payload, config, **kwargs):
        captured.update(payload)
        return HandoffAssessment(accepted=False, relevance=1, source_quality=1, evidence_coverage=1,
                                  groundedness=1, reason="Projection-only test", missing_information=["not graded"])
    await evaluate_subagent_handoff(case["handoff"]["research_topic"],
        {**case["handoff"], "compressed_research": case["compressed_research"], "evidence_registry": case["evidence"]},
        CONFIG, coverage_contract=case["contract"], requirement_ids=case["handoff"]["requirement_ids"], evaluator=judge)
    assert len(case["compressed_research"]) == 2431
    assert captured["compressed_research"] == case["compressed_research"]
    assert not captured["compressed_research_truncated"]
    assert len(json.dumps(captured, ensure_ascii=False)) <= 30000
    originals = {r["evidence_id"]:r for r in case["evidence"]}
    for record in captured["evidence_registry"]:
        assert record["supporting_excerpt"] == originals[record["evidence_id"]]["supporting_excerpt"]


@pytest.mark.asyncio
async def test_unfit_fixed_input_is_a_budget_error_not_a_research_request():
    case = CASES["web_review_long"]
    async def forbidden(*args, **kwargs):
        pytest.fail("A Judge must not receive silently damaged fixed input")
    with pytest.raises(QualityInputBudgetExceeded):
        await evaluate_subagent_handoff("topic", {"compressed_research": "不可截断的研究正文。" * 6000,
            "evidence_registry": case["evidence"]}, CONFIG, coverage_contract=case["contract"],
            requirement_ids=case["handoff"]["requirement_ids"], evaluator=forbidden)


def test_feedback_requires_owned_structured_gap_and_cannot_search_for_projection_loss():
    policy = _quality_policy(Configuration(), CONFIG)
    def validate(gaps):
        result = ToolResultAssessment(decision="continue", relevance=5, source_quality=5,
            evidence_coverage=5, corroboration=5, reason="Follow-up", missing_information=["Optional supervisor hypothesis"], gaps=gaps)
        return _tool_protocol_errors(result, checks={"passed":True,"failures":[]}, policy=policy,
                                     owned_requirement_ids=("owned",), evidence_ids=("e1",))
    assert "research_follow_up_requires_owned_gap" in validate([])
    assert "gap_requirement_not_owned:foreign" in validate([{"requirement_id":"foreign","kind":"factual","reason":"gap"}])
    assert "input_projection_cannot_request_research" in validate([{"requirement_id":"owned","kind":"input_projection","reason":"missing input","next_query":"search again"}])
    assert validate([{"requirement_id":"owned","kind":"factual","reason":"Missing numeric value", "checked_evidence_ids":["e1"],"next_query":"annual report revenue"}]) == []


@pytest.mark.asyncio
async def test_source_shortage_is_repaired_instead_of_becoming_input_budget_failure():
    from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality

    contract = build_research_coverage_contract([{"role": "user", "content": "核查 PostgreSQL 17 查询变化"}])
    owned = contract.delegable_requirement_ids()[0]
    evidence = CASES["web_review_long"]["evidence"][:2]
    calls = []

    class Models:
        async def structured(self, role, prompt, schema, state, *, messages):
            calls.append(messages[:])
            return ToolResultAssessment(
                decision="continue", relevance=4, source_quality=4,
                evidence_coverage=3, corroboration=3, reason="Need a third source",
                missing_information=["A third official source"],
                gaps=[{
                    "requirement_id": owned,
                    "kind": "input_projection" if len(calls) == 1 else "factual",
                    "reason": "Only two sources are present",
                    "checked_evidence_ids": [evidence[0]["evidence_id"]],
                    "next_query": "" if len(calls) == 1 else "PostgreSQL 17 official documentation",
                }],
            )

    quality = NativeResearchQuality(Models(), lambda: CONFIG)
    result = await evaluate_tool_results(
        "PostgreSQL 17 查询变化", [], CONFIG, evidence_registry=evidence,
        coverage_contract=contract, requirement_ids=[owned], evaluator=quality.evaluate,
    )
    assert len(calls) == 2
    assert "input_projection_without_omitted_evidence" in calls[1][-1].get_text_content()
    assert result.gaps[0].kind == "factual"
    assert result.protocol_repair_count == 1
    assert result.evaluator_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["repeat", "input_budget"])
async def test_native_researcher_stops_identical_queries_without_new_evidence(failure):
    from pydantic import BaseModel
    from open_deep_research.agentscope_runtime.research_agents import Researcher, ResearchAssignment
    from open_deep_research.tools.base import ToolOrigin, ToolExecutionZone, ToolResult, build_tool
    from tests.as_runtime.test_research_migration import Models, cfg, tool_call
    contract = build_research_coverage_contract([{"role":"user", "content":"核查营收"}])
    owned = list(contract.delegable_requirement_ids())
    calls = []
    class Input(BaseModel):
        pass
    async def fetch(input, context, on_progress=None):
        calls.append(True)
        return ToolResult(output={"evidence": CASES["web_review_long"]["evidence"][:1]})
    tool = build_tool(name="fetch_url", input_schema=Input, call=fetch, description="Fixture",
                      origin=ToolOrigin.SEARCH, execution_zone=ToolExecutionZone.HOST_CONTROL)
    async def catalog(assignment):
        return [tool]
    class Quality:
        async def batch(self, assignment, contract, rows, evidence):
            if failure == "input_budget":
                raise QualityInputBudgetExceeded("quality_payload_budget_too_small")
            return {"decision":"continue", "accepted":False, "deterministic_checks":{"passed":True},
                    "gaps":[{"requirement_id":owned[0],"kind":"factual", "reason":"Missing revenue",
                             "checked_evidence_ids":[evidence[0]["evidence_id"]],"next_query":"same query"}]}
    models = Models({"researcher":[[tool_call("fetch_url", "first")], [tool_call("fetch_url", "second")],
                                     [tool_call("fetch_url", "must-not-run")]]})
    researcher = Researcher(models, lambda: cfg(quality_evaluation_enabled=True), catalog,
                            run_id="repeat", quality=Quality())
    assignment = ResearchAssignment(research_topic="核查营收", requirement_ids=owned)
    if failure == "input_budget":
        with pytest.raises(QualityInputBudgetExceeded):
            await researcher.run(assignment, contract.model_dump(mode="json"))
        assert len(calls) == 1
    else:
        outcome = await researcher.run(assignment, contract.model_dump(mode="json"))
        assert len(calls) == 2
        assert outcome.termination == "research_no_progress"
        assert outcome.evidence_registry


def test_document_fact_does_not_narrow_an_unrelated_hybrid_sibling():
    original = hybrid_contract()
    c = build_research_coverage_contract([{"role":"user", "content":"依据所选资料说明目标响应时间。比较其他产品价格。"}]).model_copy(update={"source_selection":original.source_selection})
    unrelated = [r.requirement_id for r in c.requirements if "其他产品价格" in r.text]
    assert unrelated
    assert task_coverage_contract(c, unrelated).source_selection.mode == "hybrid"

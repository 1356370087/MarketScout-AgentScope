"""Regressions for the competitive RAG Teams E2E, without live model calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.model import StructuredResponse
from test_efficiency import cache
from test_quality_bypass import marked_snapshot
from test_report_native import Factory, config
from test_research_migration import Models, cfg, contract, evidence, tool_call

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.report import NativeReportWriter, _ReportRun
from open_deep_research.quality.planning import compile_planned_requirements


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,shared_topic", [("collaborator", False), ("teams", False), ("teams", True)])
async def test_distinct_competitor_tasks_are_not_merged_by_shared_requirement_ids(cache, mode, shared_topic, tmp_path):
    coverage = contract("Compare the providers' prices.")
    rid = coverage["requirements"][0]["requirement_id"]
    release = asyncio.Event()
    started = []
    executions = []

    async def research(assignment, *_):
        from open_deep_research.agentscope_runtime.research_quality import skipped_handoff_assessment

        started.append(assignment)
        executions.append(asyncio.current_task())
        await release.wait()
        return ResearchHandoff(**assignment.model_dump(), compressed_research=assignment.research_topic,
                               evidence_registry=[evidence()],
                               assessment={"handoff": skipped_handoff_assessment()})

    def create(call_id, topic, owner):
        fields = ({"subject": topic, "description": topic, "owner": owner, "blockedBy": []}
                  if mode == "teams" else {"research_topic": topic})
        return tool_call("TaskCreate", call_id, requirement_ids=[rid], **fields)

    topics = {owner: "Research assigned provider pricing" if shared_topic else f"Research {name} pricing"
              for owner, name in [("elastic", "Elastic"), ("aws", "AWS"), ("azure", "Azure")]}
    calls = [create(owner, topic, owner) for owner, topic in topics.items()]
    calls.append(create("repeat-aws", topics["aws"], "aws"))
    models = Models({"supervisor": [calls, [tool_call("ResearchComplete", "done")]]})
    models.recovery = cache.recovery
    build_model = models.agent_model

    def model_for(*args):
        model = build_model(*args)
        original = model._call_api

        async def respond(*a, **kw):
            if model.calls:
                release.set()
                async with asyncio.timeout(10):
                    while not started or sum(row.get("status") == "completed" for row in
                                             (await cache.progress()).get("tasks", {}).values()) < len(started):
                        await asyncio.sleep(0)
                    await asyncio.gather(*executions)
            return await original(*a, **kw)

        model._call_api = respond
        return model

    models.agent_model = model_for
    host = None
    if mode == "teams":
        host = SimpleNamespace(recovery=cache.recovery, prepare=AsyncMock(), dispatch=research,
                               consume_leader_inputs=AsyncMock(), finish_team=AsyncMock(),
                               team=SimpleNamespace(leader=None))
    outcomes, _ = await Supervisor(
        models, lambda: cfg(enable_async_research=True, async_research_mode=mode,
                            research_efficiency_mode="bounded", runs_dir=str(tmp_path)),
        SimpleNamespace(run=research), run_id="efficiency", team_workers=host,
    ).run("Compare provider pricing", coverage)
    assert {row["research_topic"] for row in outcomes} == set(topics.values())
    assert len(outcomes) == 3
    assert (await cache.progress())["counters"]["duplicate_tasks_suppressed"] == 1
    if host:
        assert sorted(call.kwargs["owner"] for call in host.prepare.await_args_list) == ["aws", "azure", "elastic"]
        assert all(call.args[0].research_topic == topics[call.kwargs["owner"]] for call in host.prepare.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["report_time_reserved", "report_budget_reserved"])
@pytest.mark.parametrize("quality_enabled", [True, False])
async def test_reserved_report_budget_is_used_for_writing_and_review(tmp_path, reason, quality_enabled):
    snapshot = marked_snapshot()
    snapshot.completion_outcome = {"action": "complete_partial", "reason": reason, "gaps": ["unfinished_research"]}
    factory = Factory()

    async def review(messages, schema):
        factory.calls.append(schema.__name__)
        return StructuredResponse(content={
            "decision": "pass",
            "dimensions": {key: 1.0 for key in (
                "coverage", "citation_correctness", "contradictions", "unsupported_claims",
                "redundancy", "executive_readability",
            )},
            "citation_audit": [{"claim": "Supported finding",
                                "citation_target": "https://example.com/source",
                                "supported": True, "evidence_ids": ["ev1"]}],
            "coverage": [{"requirement_id": row["requirement_id"], "status": "covered"}
                         for row in snapshot.coverage_contract["requirements"]],
            "issue_resolutions": [],
        })

    factory.generate_structured_output = review
    report = await NativeReportWriter(ResearchModels(factory))(
        snapshot, config(tmp_path, report_review_enabled=True,
                         quality_evaluation_enabled=quality_enabled, report_review_fail_open=False),
    )
    assert "final_report" in factory.calls and "ReportReview" in factory.calls
    assert "Supported finding" in report and "证据恢复报告" not in report
    product = snapshot.report_product
    assert product["report_review"]["decision"] == "pass"
    assert not product["report_review"].get("skipped"), product["report_review"]["summary"]
    assert product["completion_status"] == product["canonical_report"]["completion_status"] == "partial"
    assert product["stop_reason"] == reason
    assert product["research_gaps"] == ["unfinished_research"]
    assert product["quality_gate"]["skipped_stages"] == ["handoff", "supervisor_completion"]


@pytest.mark.asyncio
async def test_report_recovery_can_spend_remaining_time_below_three_maximum_call_timeouts(monkeypatch):
    from open_deep_research.configuration import Configuration

    monkeypatch.setattr("time.time", lambda: 1000)
    recovery = SimpleNamespace(store=SimpleNamespace(budget=AsyncMock(return_value={"deadline": 1540})),
                               lease=SimpleNamespace(run_id="run", user_id="owner"))
    port = _ReportRun(SimpleNamespace(recovery=recovery), marked_snapshot())
    await port.check_recovery_budget(Configuration(model_call_timeout_seconds=360))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["report_time_reserved", "report_budget_reserved"])
async def test_reserved_budget_does_not_waive_failed_review(tmp_path, reason):
    snapshot = marked_snapshot()
    snapshot.completion_outcome = {"action": "complete_partial", "reason": reason}
    factory = Factory()
    with pytest.raises(RuntimeError, match="report_review_failed"):
        await NativeReportWriter(ResearchModels(factory))(
            snapshot, config(tmp_path, report_review_enabled=True, report_review_fail_open=False),
        )
    assert "ReportReview" in factory.calls
    assert snapshot.report_product == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["budget_exhausted", "deadline_exceeded"])
async def test_actual_report_budget_refusal_keeps_evidence_and_never_claims_review_pass(tmp_path, monkeypatch, reason):
    from open_deep_research.budgets import BudgetDimension, BudgetExhausted, DeadlineExceeded

    error = BudgetExhausted(BudgetDimension.MODEL_CALLS) if reason == "budget_exhausted" else DeadlineExceeded()
    monkeypatch.setattr("open_deep_research.report.orchestrator.build_report", AsyncMock(side_effect=error))
    snapshot = marked_snapshot()
    snapshot.completion_outcome = {"action": "complete_partial", "reason": "report_time_reserved"}
    await NativeReportWriter(ResearchModels(Factory()))(snapshot, config(tmp_path, report_review_enabled=True))
    product = snapshot.report_product
    assert "Supported finding" in product["final_report"]
    assert product["completion_status"] == "partial" and product["stop_reason"] == reason
    assert product["report_review"]["skipped"] and product["report_review"]["decision"] == "fail"
    assert "report_review_not_completed" in product["research_gaps"]


@pytest.mark.asyncio
async def test_report_recovery_still_rejects_expired_deadline(monkeypatch):
    from open_deep_research.budgets import DeadlineExceeded
    from open_deep_research.configuration import Configuration

    monkeypatch.setattr("time.time", lambda: 1000)
    recovery = SimpleNamespace(store=SimpleNamespace(budget=AsyncMock(return_value={"deadline": 999})),
                               lease=SimpleNamespace(run_id="run", user_id="owner"))
    with pytest.raises(DeadlineExceeded):
        await _ReportRun(SimpleNamespace(recovery=recovery), marked_snapshot()).check_recovery_budget(Configuration())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancelled", "fence_lost", "unknown"])
async def test_report_budget_handling_never_swallows_runtime_control(tmp_path, monkeypatch, failure):
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost, UnknownOperation

    error = {"cancelled": asyncio.CancelledError(), "fence_lost": FenceLost(), "unknown": UnknownOperation("call")}[failure]
    monkeypatch.setattr("open_deep_research.report.orchestrator.build_report", AsyncMock(side_effect=error))
    snapshot = marked_snapshot()
    snapshot.completion_outcome = {"action": "complete_partial", "reason": "report_time_reserved"}
    with pytest.raises(type(error)):
        await NativeReportWriter(ResearchModels(Factory()))(snapshot, config(tmp_path))
    assert snapshot.report_product == {}


@pytest.mark.asyncio
async def test_unknown_outcome_takes_precedence_over_report_budget_refusal(tmp_path, monkeypatch):
    from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation
    from open_deep_research.budgets import BudgetDimension, BudgetExhausted

    error = UnknownOperation("in-flight")
    models = SimpleNamespace(recovery=SimpleNamespace(problem=error))
    monkeypatch.setattr("open_deep_research.report.orchestrator.build_report",
                        AsyncMock(side_effect=BudgetExhausted(BudgetDimension.MODEL_CALLS)))
    snapshot = marked_snapshot()
    with pytest.raises(UnknownOperation):
        await NativeReportWriter(models)(snapshot, config(tmp_path))
    assert models.recovery.problem is error and snapshot.report_product == {}


def test_enumerated_aspect_does_not_drop_everything_before_a_nested_qualifier():
    text = ("对每家产品核查八个方面：关键词与向量混合检索及融合方式、过滤向量检索及租户隔离、"
            "中英文检索与分词、文档级与字段级权限、语义重排的条件和计费、私网及高可用与副本、"
            "容量/分区/服务配额、运维与迁移和授权限制。")
    compiled = compile_planned_requirements(
        [{"role": "user", "content": text}],
        [{"source_message_index": 0, "source_text": text, "kind": "factual"}],
        atomic=True, preserve_scope=True,
    )
    facts = [row for row in compiled.requirements if row.kind == "factual"]
    for aspect in ("关键词", "过滤向量检索", "中英文检索", "文档级", "语义重排", "私网", "容量", "运维"):
        assert any(aspect in row.text for row in facts), aspect
    assert all(text[row.source_start:row.source_end] == row.text for row in facts)


def test_compared_subjects_keep_exact_user_names_when_the_extractor_omits_a_particle():
    comparison = ("比较 Elastic Cloud Hosted、Amazon OpenSearch Service 的预置域，"
                  "以及 Azure AI Search 的付费标准层。")
    requirement = "核查各家的价格。"
    compiled = compile_planned_requirements(
        [{"role": "user", "content": comparison + requirement}],
        [{"source_message_index": 0, "source_text": requirement, "kind": "factual"}],
        atomic=True, preserve_scope=True,
        entities=[{"name": name} for name in (
            "Elastic Cloud Hosted", "Amazon OpenSearch Service 预置域", "Azure AI Search 付费标准层",
        )],
    )
    assert {row.subject for row in compiled.requirements if row.text == requirement} == {
        "Elastic Cloud Hosted", "Amazon OpenSearch Service 的预置域", "Azure AI Search 的付费标准层",
    }


def test_nonliteral_model_scenario_cannot_erase_the_users_original_capacity_assumptions():
    scenario = ("统一业务场景：200 个租户、10,000 名用户、2,000 万篇文档；"
                "正文平均 4 KiB，每篇一个 1,536 维 float32 向量。日均 100 万查询，10% 重排。")
    question = "比较三家的检索成本。"
    compiled = compile_planned_requirements(
        [{"role": "user", "content": scenario + "\n\n" + question}],
        [{"source_message_index": 0, "source_text": "统一业务场景：200 个租户……10% 重排。", "kind": "factual"},
         {"source_message_index": 0, "source_text": question, "kind": "factual"}],
        atomic=True, preserve_scope=True,
    )
    assert any(row.text == scenario and row.kind == "process" for row in compiled.requirements)
    assert not any("……" in row.text for row in compiled.requirements)
    assert any(row.text == question and row.kind == "factual" for row in compiled.requirements)

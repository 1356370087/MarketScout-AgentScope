"""Temporary handoff bypass preserves tool correction and truthful delivery state."""

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import ToolResultBlock
from test_research_migration import Models, cfg, contract, evidence, tool_call

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor
from open_deep_research.agentscope_runtime.research_completion import BusinessCompletion
from open_deep_research.agentscope_runtime.research_quality import skipped_handoff_assessment
from open_deep_research.completion import CompletionPolicyContext
from open_deep_research.quality.gate import HandoffAssessment, QualityInputBudgetExceeded


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "collaborator", "teams"])
@pytest.mark.parametrize("verdict", ["rejected", "unavailable", "input_budget"])
async def test_delivery_skips_judge_and_preserves_evidence_in_all_modes(mode, verdict):
    coverage = contract("Explain deployment limits.")
    ids = [row["requirement_id"] for row in coverage["requirements"]]
    if verdict == "rejected":
        handoff = AsyncMock(return_value=HandoffAssessment(
            accepted=False, relevance=1, source_quality=1, evidence_coverage=1, groundedness=1,
            reason="Rejected fixture",
        ))
    else:
        error = RuntimeError("Judge unavailable") if verdict == "unavailable" else QualityInputBudgetExceeded("too large")
        handoff = AsyncMock(side_effect=error)
    quality = SimpleNamespace(handoff=handoff)
    delivered = asyncio.Event()

    class Worker:
        async def run(self, assignment, contract, feedback):
            delivered.set()
            return ResearchHandoff(
                **assignment.model_dump(), compressed_research="Original findings",
                evidence_registry=[evidence()], termination="research_no_progress",
            )

    worker = Worker()
    workers = None
    task_args = {"research_topic": "Explain deployment limits.", "requirement_ids": ids}
    script = [[tool_call("ConductResearch" if mode == "sync" else "TaskCreate", **task_args)]]
    if mode != "sync":
        if mode == "teams":
            script[0] = [tool_call("TaskCreate", subject="Deployment", description=task_args["research_topic"],
                                   requirement_ids=ids, owner="", blockedBy=[])]

        async def dispatch(*args):
            result = await worker.run(*args)
            result.assessment["handoff"] = skipped_handoff_assessment()
            return result

        async def wait():
            await asyncio.wait_for(delivered.wait(), 2)
            await asyncio.sleep(0)

        workers = SimpleNamespace(
            dispatch=dispatch, wait_for_updates=wait, prepare=AsyncMock(),
            consume_leader_inputs=AsyncMock(), finish_team=AsyncMock(),
            recovery=SimpleNamespace(task=lambda _: nullcontext()),
            team=SimpleNamespace(leader=None, lease=SimpleNamespace(run_id="bypass"),
                                 service=SimpleNamespace(tasks=AsyncMock(return_value=[]))),
        )
        script.append([tool_call("WaitForTeamEvents", "wait")])
    script.append([tool_call("ResearchComplete", "done")])
    models = Models({"supervisor": script})
    config = cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=3,
                 enable_async_research=mode != "sync", async_research_mode="teams" if mode == "teams" else "collaborator")
    outcomes, state = await Supervisor(models, lambda: config, worker, run_id="bypass",
        quality=quality, completion_policy=True, team_workers=workers).run("brief", coverage)

    quality.handoff.assert_not_awaited()
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["evidence_registry"] == [evidence()]
    assert outcome["compressed_research"] == "Original findings"
    assert outcome["termination"] == "research_no_progress"
    assessment = outcome["assessment"]["handoff"]
    assert assessment["evaluation_status"] == "skipped"
    assert assessment["admission_status"] == "accepted_with_caveats"
    assert not {"relevance", "groundedness", "deterministic_checks", "requirement_coverage"} & assessment.keys()
    assert state["middle_context"]["coverage_ledger"] == {}
    assert state["middle_context"]["business_completion"]["action"] == "complete"
    assert state["middle_context"]["quality_gate"]["skipped_stages"] == ["handoff", "supervisor_completion"]
    if mode == "teams":
        workers.finish_team.assert_awaited_once()


@pytest.mark.parametrize("facts,explicit,exhausted,action,reason", [
    ({"independent_source_count": 0, "uncovered_requirements": ("missing",), "unresolved_conflicts": ("conflict",)}, True, False, "complete", "explicit_completion"),
    ({"active_task_count": 1}, True, False, "continue_with_gaps", "active_tasks"),
    ({"cancelled": True}, True, False, "terminate", "cancelled"),
    ({"has_remaining_budget": False}, True, False, "complete_partial", "budget_exhausted"),
    ({"research_stop_reason": "report_budget_reserved"}, True, False, "complete_partial", "report_budget_reserved"),
    ({"evidence_count": 0}, True, False, "complete_partial", "insufficient_evidence"),
    ({}, False, True, "complete_partial", "iteration_limit"),
    ({}, False, False, "continue_with_gaps", "explicit_completion_required"),
    ({}, True, True, "complete", "explicit_completion"),
])
def test_completion_bypasses_quality_but_preserves_lifecycle(facts, explicit, exhausted, action, reason):
    completion = BusinessCompletion(4, lambda: CompletionPolicyContext(**{"evidence_count": 1, **facts}), min_sources=3)
    result = completion.evaluate(explicit=explicit, exhausted=exhausted)
    assert result.action.value == action and result.reason == reason
    if action in {"terminate", "continue_with_gaps"} and explicit:
        with pytest.raises(ValueError):
            completion.request_completion()
        assert not completion.finished


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["report_budget_reserved", "report_time_reserved"])
async def test_worker_report_reservation_preserves_evidence_and_partial_status(reason):
    models = Models({"supervisor": [
        [tool_call("ConductResearch", research_topic="Limits")],
        [tool_call("ResearchComplete", "done")],
    ]})
    worker = SimpleNamespace(run=AsyncMock(side_effect=lambda assignment, *_: ResearchHandoff(
        **assignment.model_dump(), compressed_research="Available evidence", evidence_registry=[evidence()],
        termination=reason)))
    outcomes, state = await Supervisor(models, lambda: cfg(), worker, run_id="reserve", completion_policy=True).run("brief", contract())
    assert outcomes[0]["evidence_registry"] == [evidence()]
    assert state["middle_context"]["business_completion"]["action"] == "complete_partial"
    assert state["middle_context"]["business_completion"]["reason"] == reason


@pytest.mark.asyncio
async def test_delivered_scope_is_not_researched_again_for_an_empty_ledger():
    coverage = contract("Explain deployment limits.")
    ids = [row["requirement_id"] for row in coverage["requirements"]]
    coverage.update(schema_version=4, delegation_scopes={"S01": ids})
    worker = SimpleNamespace(run=AsyncMock(side_effect=lambda assignment, *_: ResearchHandoff(
        **assignment.model_dump(), compressed_research="Original finding", evidence_registry=[evidence()])))
    models = Models({"supervisor": [
        [tool_call("ConductResearch", "first", research_topic="Limits", scope_id="S01")],
        [tool_call("ConductResearch", "repeat", research_topic="Limits again", scope_id="S01")],
        [tool_call("ResearchComplete", "done")],
    ]})
    config = cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=3)
    config["metadata"]["quality_policy_version"] = "quality-gate-v6"
    outcomes, state = await Supervisor(models, lambda: config, worker, run_id="repeat", completion_policy=True).run("brief", coverage)
    worker.run.assert_awaited_once()
    assert len(outcomes) == 1 and state["middle_context"]["coverage_ledger"] == {}
    outputs = {block.id: json.loads("".join(part.text for part in block.output))
               for call in models.created[0][2].calls for message in call["messages"]
               for block in message.content if isinstance(block, ToolResultBlock) and block.name == "ConductResearch"}
    assert outputs["first"]["remaining_scopes"] == []
    assert outputs["repeat"]["reason"] == "requirements_already_delivered"


@pytest.mark.asyncio
async def test_replayed_team_assessment_and_existing_coverage_are_not_reinterpreted():
    coverage = contract("Explain deployment limits.")
    ids = [row["requirement_id"] for row in coverage["requirements"]]
    old_assessment = {"accepted": False, "admission_status": "rejected", "reason": "Historical rejection"}
    prior = ResearchHandoff(task_id="historical", research_topic="Limits", requirement_ids=ids,
                           compressed_research="", assessment={"handoff": old_assessment})
    models = Models({"supervisor": [[tool_call("ResearchComplete", "done")]]})
    supervisor = Supervisor(models, lambda: cfg(), None, run_id="history", completion_policy=True)
    supervisor.initial_findings = [prior.model_dump()]
    supervisor.initial_coverage_ledger = {ids[0]: {"status": "partial", "evidence_ids": [], "task_ids": ["historical"]}}
    outcomes, state = await supervisor.run("brief", coverage)
    assert outcomes[0]["assessment"]["handoff"] == old_assessment
    assert state["middle_context"]["coverage_ledger"] == supervisor.initial_coverage_ledger
    assert state["middle_context"]["quality_gate"]["skipped_stages"] == ["supervisor_completion"]


def marked_snapshot():
    from test_report_native import state

    snapshot = state()
    snapshot.findings[0]["evidence_registry"] = [{**evidence(), "source_url": "https://example.com/source",
                                                "claim": "Supported finding", "supporting_excerpt": "Supported finding"}]
    snapshot.coverage_contract = contract("Research the finding")
    snapshot.completion_outcome = {"action": "complete", "reason": "explicit_completion", "gaps": []}
    snapshot.agent_states["supervisor"] = {"middle_context": {"quality_gate": {
        "status": "skipped", "skipped_stages": ["handoff", "supervisor_completion"],
        "reason_codes": ["handoff_quality_gate_temporarily_disabled", "supervisor_completion_quality_gate_temporarily_disabled"],
    }}}
    return snapshot


@pytest.mark.asyncio
async def test_normal_report_records_unassessed_quality_without_becoming_partial(tmp_path):
    from test_report_native import Factory, config
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.api.projections import _stable_output

    snapshot = marked_snapshot()
    factory = Factory()
    report = await NativeReportWriter(ResearchModels(factory))(snapshot, config(tmp_path, quality_evaluation_enabled=True))
    assert "Supported finding" in report and "final_report" in factory.calls
    product = snapshot.report_product
    assert product["completion_status"] == product["canonical_report"]["completion_status"] == "success"
    gate = _stable_output(product)["quality_gate"]
    assert gate["status"] == "skipped" and len(gate["skipped_stages"]) == 2
    assert gate["unassessed_requirement_ids"]
    assert product["uncovered_requirements"] == []


@pytest.mark.asyncio
async def test_no_evidence_uses_existing_partial_report_and_keeps_bypass_marker(tmp_path):
    from test_report_native import Factory, config
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.agentscope_runtime.research_models import ResearchModels

    snapshot = marked_snapshot()
    snapshot.findings[0]["evidence_registry"] = []
    snapshot.completion_outcome = {"action": "complete_partial", "reason": "insufficient_evidence", "gaps": []}
    factory = Factory()
    report = await NativeReportWriter(ResearchModels(factory))(snapshot, config(tmp_path, report_review_enabled=True))
    assert report and not factory.calls
    assert snapshot.report_product["completion_status"] == "partial"
    assert snapshot.report_product["quality_gate"]["status"] == "partial"
    assert snapshot.report_product["quality_gate"]["skipped_stages"] == ["handoff", "supervisor_completion"]
    assert snapshot.report_product["report_review"]["decision"] == "fail"


@pytest.mark.asyncio
async def test_report_review_failure_is_not_overridden_by_bypassed_research_quality(tmp_path):
    from test_report_native import Factory, config
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.agentscope_runtime.research_models import ResearchModels

    snapshot = marked_snapshot()
    factory = Factory()
    with pytest.raises(RuntimeError):
        await NativeReportWriter(ResearchModels(factory))(snapshot, config(tmp_path,
            quality_evaluation_enabled=True, report_review_enabled=True, report_review_fail_open=False))
    assert "ReportReview" in factory.calls
    assert snapshot.report_product == {}


@pytest.mark.asyncio
async def test_report_degradation_and_its_reason_survive_skip_metadata(tmp_path, monkeypatch):
    from test_report_native import Factory, config
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.report import orchestrator

    snapshot = marked_snapshot()
    review = {"status": "skipped", "degraded": True, "decision": "pass", "summary": "Reviewer unavailable"}
    build = AsyncMock(return_value={
        "final_report": "Evidence-limited report", "report_review": review,
        "quality_gate": {"type": "override", "value": {
            "status": "degraded", "reason_codes": ["research_incomplete"], "reason": "Real report limitation",
        }},
        "completion_decision": {"action": "complete_partial", "reason": "research_incomplete"},
    })
    monkeypatch.setattr(orchestrator, "build_report", build)
    await NativeReportWriter(ResearchModels(Factory()))(snapshot, config(tmp_path))
    assert snapshot.report_product["report_review"] == review
    gate = snapshot.report_product["quality_gate"]
    assert gate["status"] == "degraded" and gate["reason"] == "Real report limitation"
    assert "research_incomplete" in gate["reason_codes"]
    assert gate["skipped_stages"] == ["handoff", "supervisor_completion"]
    assert snapshot.report_product["completion_status"] == "partial"

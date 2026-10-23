"""Report clock authorization and research closing use durable native receipts."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_recovery import store, create

from open_deep_research.agentscope_runtime.execution_budget import (
    APPROVAL_STAGE, ReportTimeApprovalPending, before_report_call, estimate_seconds,
    research_stop_reason, role_model,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict, UnknownOperation
from open_deep_research.agentscope_runtime.research_pipeline import PendingDecision, ResearchPipeline, STAGES
from open_deep_research.configuration import Configuration


def config(**values):
    return {"configurable": {"research_efficiency_mode": "bounded", "run_deadline_seconds": 1800,
                            "model_call_timeout_seconds": 360, "report_review_enabled": True,
                            "report_review_max_revisions": 1, **values},
            "metadata": {"run_config_schema_version": 21}}


def request(version=1, checkpoint="draft"):
    return PendingDecision(stage=APPROVAL_STAGE, question="延长报告执行时间？",
                           payload={"version": version, "checkpoint": checkpoint,
                                    "suggested_extension_seconds": 600, "remaining_seconds": 0,
                                    "allowed_actions": ["approve", "deny", "cancel"]})


def test_estimate_uses_same_model_p90_and_remaining_phases():
    cfg = Configuration.from_runnable_config(config())
    cold = estimate_seconds(cfg)
    assert cold == 555
    rows = [{"role": "final_report", "model": role_model(cfg, "final_report"), "seconds": value}
            for value in [1, 1, 240]]
    rows.append({"role": "final_report", "model": "another-model", "seconds": 999})
    assert estimate_seconds(cfg, rows) > cold
    assert estimate_seconds(cfg, rows, {"review_passed": True}) == 30
    progress = {"draft_checked": True, "completed_calls": {
        "draft": {"role": "final_report", "span": "lead.final_report", "step": "writing"},
    }}
    assert estimate_seconds(cfg, progress=progress) < cold


@pytest.mark.asyncio
@pytest.mark.parametrize("action,seconds", [("approve", 600), ("approve", None), ("deny", None), ("cancel", None)])
async def test_clock_pause_and_decision_are_atomic_and_idempotent(store, monkeypatch, action, seconds):
    clock = [1000.]
    monkeypatch.setattr(store, "_now", AsyncMock(side_effect=lambda _: clock[0]))
    state, lease = await create(store, deadline=1100, limits={"cost_micro_usd": 2000000, "fetch_calls": 40})
    # Long simulated approval wait does not require a live executor lease.
    await store.release(lease)
    lease = await store.acquire(state.run_id, "owner", ttl=10000)
    state.completed = list(STAGES[:7])
    state.revision_count = 3
    state.status = "waiting"
    state.pending = request()
    await store.save(lease, state)
    paused, _ = await store.load(state.run_id, "owner")
    assert paused.application["execution_budget"]["pause"]["remaining_seconds"] == 100
    with pytest.raises(RecoveryConflict, match="paused"):
        await store.begin_operation(lease, "should-not-run", "model:final_report", {})
    clock[0] = 5000
    payload = {"action": action, "expected_version": 1}
    if seconds is not None:
        payload["extension_seconds"] = seconds
    action_id = paused.pending.id
    await store.submit_decision(state.run_id, "owner", "decision", action_id, payload)
    session = RecoverySession(store, lease, paused)
    pipeline = ResearchPipeline(paused, None, session.save, config_fingerprint="frozen", recovery=session)
    await session.consume_decisions(pipeline)
    saved, _ = await store.load(state.run_id, "owner")
    budget = await store.budget(state.run_id, "owner")
    assert saved.pending is None and saved.revision_count == 3 and saved.completed == list(STAGES[:7])
    assert saved.config_fingerprint == "frozen"
    assert budget["limits"] == {"cost_micro_usd": 2000000, "fetch_calls": 40}
    assert budget["deadline"] == (5700 if action == "approve" else 5100 if action == "deny" else 1100)
    assert saved.status == ("cancelled" if action == "cancel" else "ready")
    assert len(saved.application["execution_budget"]["extension_history"]) == 1
    assert await store.submit_decision(state.run_id, "owner", "decision", action_id, payload) == "applied"
    assert (await store.budget(state.run_id, "owner"))["deadline"] == budget["deadline"]
    assert len((await store.load(state.run_id, "owner"))[0].application["execution_budget"]["extension_history"]) == 1


@pytest.mark.asyncio
async def test_approval_conflicts_and_unknown_effects_do_not_gain_time(store):
    state, lease = await create(store, deadline=time.time() + 100)
    state.pending, state.status = request(), "waiting"
    await store.begin_operation(lease, "uncertain", "gateway:model", {}, replay_safe=False)
    with pytest.raises(UnknownOperation):
        await store.save(lease, state)
    assert (await store.load(state.run_id, "owner"))[0].pending is None
    await store.resolve_operation(lease, "uncertain", not_executed=True)
    await store.save(lease, state)
    with pytest.raises(RecoveryConflict):
        await store.submit_decision(state.run_id, "owner", "stale", state.pending.id,
                                    {"action": "approve", "expected_version": 9})
    with pytest.raises(KeyError):
        await store.submit_decision(state.run_id, "another-user", "foreign", state.pending.id,
                                    {"action": "approve", "expected_version": 1})
    await store.submit_decision(state.run_id, "owner", "first", state.pending.id,
                                {"action": "approve", "expected_version": 1, "extension_seconds": 300})
    with pytest.raises(RecoveryConflict):
        await store.submit_decision(state.run_id, "owner", "second", state.pending.id,
                                    {"action": "approve", "expected_version": 1, "extension_seconds": 600})


@pytest.mark.asyncio
async def test_budget_stop_is_latched_and_does_not_change_spend_limits(store):
    state, lease = await create(store, deadline=time.time() + 120)
    session = RecoverySession(store, lease, state)
    models = SimpleNamespace(recovery=session)
    assert await research_stop_reason(models, config()) == "report_time_reserved"
    assert await research_stop_reason(models, config(report_review_enabled=False)) == "report_time_reserved"
    assert (await store.load(state.run_id, "owner"))[0].application["execution_budget"]["research_closing_reason"]
    assert (await store.budget(state.run_id, "owner"))["limits"] == {}


@pytest.mark.asyncio
async def test_report_request_is_before_dispatch_and_bounded_by_two_decisions(store):
    state, lease = await create(store, deadline=time.time() + 10)
    session = RecoverySession(store, lease, state)
    models = SimpleNamespace(recovery=session)
    async def check(checkpoint):
        await before_report_call(models, state, config(), role="final_report", span="draft",
                                 checkpoint=checkpoint, operation_key=None)
    with pytest.raises(ReportTimeApprovalPending) as pending:
        await check("draft")
    assert pending.value.pending.payload["suggested_extension_seconds"] % 60 == 0
    async with store.engine.connect() as conn:
        assert not (await conn.execute(select(store.ops))).first()
    state.application["execution_budget"].update(
        extension_history=[{"action": "approve"}, {"action": "approve"}],
        authorized_checkpoints=["review:1"],
    )
    # A deliberately short grant does not re-prompt at the same checkpoint.
    await check("review:1")
    from open_deep_research.budgets import DeadlineExceeded
    with pytest.raises(DeadlineExceeded, match="extension_limit"):
        await check("revision:1")


@pytest.mark.asyncio
async def test_checkpoint_save_cannot_erase_already_delivered_findings(store):
    state, lease = await create(store)
    state.inflight = "research_supervisor"
    state.application["configuration"] = {"contract": {"metadata": {"run_config_schema_version": 21}}}
    old = state.model_copy(deep=True)
    state.findings = [{"task_id": "one", "compressed_research": "retained", "evidence_registry": []}]
    await store.save(lease, state)
    old.status = "failed"
    await store.save(lease, old)
    assert (await store.load(state.run_id, "owner"))[0].findings == state.findings


@pytest.mark.asyncio
async def test_report_resume_replays_paid_draft_without_changing_operation_keys(store, tmp_path, monkeypatch):
    from agentscope.model import StructuredResponse
    from pydantic import BaseModel
    from test_quality_bypass import marked_snapshot
    from test_report_native import Factory, config as writer_config
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.report.models import ReportReview
    from open_deep_research.report.runtime import ReportMessage, native_report

    clock = [1000.]
    monkeypatch.setattr(store, "_now", AsyncMock(side_effect=lambda _: clock[0]))
    monkeypatch.setattr("time.time", lambda: clock[0])
    state = marked_snapshot()
    state.completed = list(STAGES[:7])
    await store.create_run("owner", state, deadline=2000)
    lease = await store.acquire(state.run_id, "owner", ttl=10000)
    session = RecoverySession(store, lease, state)
    factory = Factory()
    original = factory.complete_with_recovery

    async def write(*args, **kwargs):
        answer = await original(*args, **kwargs)
        clock[0] = 1950  # Draft is durably complete; review needs a grant.
        return answer

    factory.complete_with_recovery = write
    factory.generate_structured_output = AsyncMock(return_value=StructuredResponse(content={"decision": "pass"}))
    class Verdict(BaseModel):
        decision: str

    async def review(_draft, _state, runtime_config, *, attempt):
        verdict = await native_report.get().invoke(
            "report_review", [ReportMessage("Review the saved draft", type="system")],
            Configuration.from_runnable_config(runtime_config), span_name="lead.report_review", schema=Verdict,
        )
        return ReportReview(decision=verdict.decision, status="completed")

    monkeypatch.setattr("open_deep_research.report.orchestrator.review_report", review)
    cfg = writer_config(tmp_path, report_review_enabled=True)
    cfg["metadata"] = {"run_config_schema_version": 21}
    models = ResearchModels(factory, recovery=session)
    with session.scope("final_report_generation", 0), pytest.raises(ReportTimeApprovalPending) as pending:
        await NativeReportWriter(models)(state, cfg)
    assert factory.calls == ["final_report"]
    state.pending, state.status = pending.value.pending, "waiting"
    await session.save(state)
    before = await store.budget(state.run_id, "owner")
    clock[0] = 5000
    await store.submit_decision(state.run_id, "owner", "extend", state.pending.id,
                                {"action": "approve", "expected_version": 1, "extension_seconds": 600})
    pipeline = ResearchPipeline(session.snapshot, None, session.save, config_fingerprint=state.config_fingerprint, recovery=session)
    await session.consume_decisions(pipeline)
    restored = pipeline.state
    with session.scope("final_report_generation", 0):
        text = await NativeReportWriter(models)(restored, cfg)
    assert "Supported finding" in text
    assert factory.calls == ["final_report"]  # The completed writing call replayed.
    factory.generate_structured_output.assert_awaited_once()
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == before["used"]["model_calls"] + 1

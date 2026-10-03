"""SQL-backed regression coverage for native lifecycle progress boundaries."""

import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_deep_research.agentscope_runtime.native_security import NativeEventPublisher
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api import research_router
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.events.task_activity import (
    TaskActivityStore,
    publish_task_activity,
)
from open_deep_research.report import orchestrator
from open_deep_research.report import reviewer as reviewer_module
from open_deep_research.sandbox.gateway import GatewayRuntime
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal
from tests.test_report_reviewer_integration import (
    _config,
    _draft,
    _model_review_payload,
    _review,
    _state,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sql_progress(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {}})
    await store.create_from_config(
        "owner", "run", config, application={"configuration": config.snapshot()}
    )
    recovery = await RecoverySession.open(store, "run", "owner")
    service = NativeRuns(store, None, None, runs_dir=tmp_path)
    publisher = NativeEventPublisher(store, recovery.lease)
    app = FastAPI()
    app.include_router(research_router.build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal("owner")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture"
        ) as client:
            yield store, recovery, service, publisher, client
    finally:
        await recovery.close()
        await service.aclose()
        await store.aclose()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("after_all", [False, True])
async def test_terminal_sse_closes_after_late_usage_reconciliation(
    sql_progress, status, after_all
):
    store, recovery, service, _, client = sql_progress
    await store.begin_operation(
        recovery.lease, "model", "gateway:model", {}, replay_safe=False,
        reserve={"model_calls": 1},
    )
    await store.commit_operation(
        recovery.lease, "model",
        {"outcome": {"status": "completed", "usage": {}},
         "usage_status": "estimated", "cost_status": "estimated"},
        actual={"model_calls": 1, "input_tokens": 2, "output_tokens": 3},
    )
    state = recovery.snapshot.model_copy(deep=True)
    state.status = status
    await recovery.save(state)
    await store.reconcile_model_usage(
        recovery.lease, "model", receipt_id="verified-fixture", input_tokens=2,
        output_tokens=3, cost_micro_usd=1,
    )
    records = await service.events("run", "owner")
    assert records[-1].type == "run.usage.updated"
    after = records[-1].sequence if after_all else 0
    response = await asyncio.wait_for(client.get(f"/runs/run/events?after={after}"), 1)
    assert response.status_code == 200
    if after_all:
        assert response.text == ""
    else:
        assert f"event: run.{status}" in response.text
        assert response.text.rfind("event: run.usage.updated") > response.text.index(
            f"event: run.{status}"
        )
    assert service.admission.connection_limiter.active == 0


async def test_restored_sse_does_not_end_on_historical_failed_event(
    sql_progress, monkeypatch
):
    _, recovery, service, _, _ = sql_progress
    state = recovery.snapshot.model_copy(deep=True)
    state.status = "failed"
    await recovery.save(state)
    state.status = "ready"
    await recovery.save(state)
    config = research_router.Configuration.from_runnable_config(None).model_copy(
        update={"sse_heartbeat_seconds": 0, "sse_poll_interval_ms": 1}
    )
    monkeypatch.setattr(
        research_router.Configuration, "from_runnable_config", lambda _: config
    )
    route = next(
        route for route in research_router.build_research_router(service).routes
        if getattr(route, "path", None) == "/runs/{run_id}/events"
    )
    response = await route.endpoint(
        "run", after=0, last_event_id=None, principal=research_principal("owner")
    )
    bodies = asyncio.Queue()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await bodies.put(message["body"])

    async def receive():
        await asyncio.Event().wait()

    task = asyncio.create_task(response(
        {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
    ))
    try:
        replay = b""
        while b": keep-alive\n\n" not in replay:
            replay += await asyncio.wait_for(bodies.get(), 1)
        assert b"event: run.failed" in replay
        assert not task.done()
        state.status = "completed"
        await recovery.save(state)
        await asyncio.wait_for(task, 2)
        assert b"event: run.completed" in await asyncio.wait_for(bodies.get(), 1)
        assert service.admission.connection_limiter.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(("task_id", "role", "is_research_task"), [
    ("supervisor", "supervisor", False),
    ("supervisor", "compression", False),
    ("pipeline", "message_summary", False),
    ("report:lead.final_report:0", "final_report", False),
    ("report:lead.report_review:3", "report_review", False),
    ("report:lead.report_revision:2", "final_report", False),
    ("evidence-task", "researcher", True),
    ("evidence-task", "quality_evaluation", True),
    ("evidence-task", "compression", True),
    ("evidence-task", "summarization", True),
])
async def test_model_activity_keeps_internal_spans_out_of_research_tasks(
    sql_progress, tmp_path, monkeypatch, task_id, role, is_research_task
):
    _, recovery, service, publisher, _ = sql_progress
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.delenv("SANDBOX_GATEWAY_PHYSICAL_PROCESS", raising=False)
    calls = []

    class Internal:
        def signed(self, _cls, **fields):
            return SimpleNamespace(**fields)

        async def post(self, _url, event):
            calls.append(event)
            result = await publish_task_activity(
                {"configurable": {"runs_dir": str(tmp_path)},
                 "metadata": {"run_id": "run", "task_id": event.task_id},
                 "_event_publisher": publisher},
                event.event_type, task_id=event.task_id,
                update_run_summary=event.update_run_summary,
                kind=event.kind, phase=event.phase, status=event.status,
                title=event.title, summary=event.summary, payload=event.payload,
                iteration=None, duration_ms=None, dedupe_key=event.dedupe_key,
            )
            assert result is not None

    gateway = GatewayRuntime.__new__(GatewayRuntime)
    gateway.internal = Internal()
    await publisher.publish(
        "research.task.started", stage="researching",
        payload={"task_id": "evidence-task", "status": "running"},
        dedupe_key="real-start",
    )
    request = SimpleNamespace(
        run_id="run", task_id=task_id, role=role, model="fixture",
        logical_operation_id="logical",
    )
    context = SimpleNamespace(fence_token=recovery.lease.fence)
    await gateway._model_activity_v2(request, context, "model.started")
    await gateway._model_activity_v2(
        request, context, "model.completed",
        outcome=SimpleNamespace(
            usage={"input_tokens": 2, "output_tokens": 3}, error_code=None
        ),
    )
    assert len(calls) == 2
    assert len(TaskActivityStore("run", task_id, runs_dir=str(tmp_path)).read()) == 2
    assert all(call.update_run_summary is is_research_task for call in calls)
    active = await service.snapshot("run", "owner")
    if is_research_task:
        assert active["progress"]["task_items"][task_id]["model_call_count"] == 1
    else:
        assert task_id not in active["progress"]["task_items"]
    await publisher.publish(
        "research.task.completed", stage="researching",
        payload={"task_id": "evidence-task", "status": "completed"},
        dedupe_key="real-done",
    )
    state = recovery.snapshot.model_copy(deep=True)
    state.status = "completed"
    await recovery.save(state)
    snapshot = await service.snapshot("run", "owner")
    assert snapshot["progress"]["tasks"]["cancelled"] == 0
    assert snapshot["progress"]["tasks"]["total"] == 1


@pytest.mark.parametrize("revisions", [0, 1, 3])
async def test_report_progress_replays_normalized_review_revision_boundaries(
    sql_progress, monkeypatch, revisions
):
    _, _, service, publisher, _ = sql_progress
    provider_calls = 0
    revision_calls = 0
    config = _config(report_review_max_revisions=3)
    config["_event_publisher"] = publisher
    draft = _draft()
    draft.sha256 = hashlib.sha256(draft.markdown.encode()).hexdigest()

    async def build(*_args):
        return draft

    async def invoke(*_args, **_kwargs):
        nonlocal provider_calls
        assert (await service.events("run", "owner"))[-1].type == "report.review.started"
        provider_calls += 1
        if provider_calls <= revisions:
            return _model_review_payload(decision="revise", issues=[{
                "category": "redundancy", "severity": "low",
                "description": "Make this concise.",
            }])
        return _model_review_payload()

    async def revise(current, *_args):
        nonlocal revision_calls
        assert (await service.events("run", "owner"))[-1].type == "report.revision.started"
        revision_calls += 1
        return current.markdown.replace("# Draft", f"# Revised {revision_calls}")

    async def finalize(current, *_args):
        return {"final_report": current.markdown}

    monkeypatch.setattr(orchestrator, "build_report_draft", build)
    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", invoke)
    monkeypatch.setattr(orchestrator, "revise_report", revise)
    monkeypatch.setattr(orchestrator, "finalize_report", finalize)
    result = await orchestrator.build_report(_state(), config)
    records = [e for e in await service.events("run", "owner") if e.type.startswith("report.")]
    expected = ["report.review.started", "report.review.completed"]
    for _ in range(revisions):
        expected.extend([
            "report.revision.started", "report.revision.completed",
            "report.review.started", "report.review.completed",
        ])
    assert [event.type for event in records] == expected
    assert provider_calls == revisions + 1
    assert revision_calls == revisions
    progress = (await service.snapshot("run", "owner"))["progress"]["report_review"]
    assert progress == {
        "phase": "reviewing", "status": "completed", "decision": "pass",
        "attempt": revisions + 1, "revision_count": revisions,
        "issue_count": 0, "critical_issue_count": 0,
        "draft_sha256": hashlib.sha256(result["final_report"].encode()).hexdigest(),
    }
    assert records[-1].payload["decision"] == result["report_review"]["decision"]
    assert all(set(event.payload) <= {
        "status", "decision", "attempt", "revision_count", "issue_count",
        "critical_issue_count", "draft_sha256",
    } for event in records)


async def test_report_completed_progress_uses_audited_failure_not_provider_pass(
    sql_progress, monkeypatch
):
    _, _, service, publisher, _ = sql_progress
    config = _config(report_review_max_revisions=0)
    config["_event_publisher"] = publisher

    async def build(*_args):
        return _draft()

    async def invoke(*_args, **_kwargs):
        return _model_review_payload(issues=[{
            "category": "other", "severity": "info",
            "requirement_id": "COV-UNKNOWN",
            "description": "Unvalidated provider pass.",
        }])

    async def finalize(*_args):
        pytest.fail("A protocol failure must never finalize the report")

    monkeypatch.setattr(orchestrator, "build_report_draft", build)
    monkeypatch.setattr(reviewer_module, "_invoke_reviewer", invoke)
    monkeypatch.setattr(orchestrator, "finalize_report", finalize)
    with pytest.raises(RuntimeError, match="report_review_failed"):
        await orchestrator.build_report(_state(), config)
    records = [e for e in await service.events("run", "owner") if e.type.startswith("report.")]
    assert [event.type for event in records] == [
        "report.review.started", "report.review.completed"
    ]
    assert records[-1].payload["decision"] == "fail"
    assert records[-1].payload["issue_count"] > 0


@pytest.mark.parametrize("kind", ["skipped", "revision_limit", "recovered"])
async def test_report_progress_matches_final_normalized_review(
    sql_progress, monkeypatch, kind
):
    _, _, service, publisher, _ = sql_progress
    config = _config(report_review_max_revisions=0)
    config["_event_publisher"] = publisher
    review = _review("revise", status="skipped" if kind == "skipped" else "completed")
    if kind == "skipped":
        review.skipped = True
    elif kind == "recovered":
        review.hard_failures = ["report_missing_verifiable_citations"]
    reviews = iter([review, _review("pass")])

    async def build(*_args):
        return _draft()

    async def assess(*_args, **_kwargs):
        return next(reviews)

    async def recover(*_args, **_kwargs):
        return _draft("# Recovered\n\nOption A [1].")

    async def finalize(current, *_args):
        return {"final_report": current.markdown}

    monkeypatch.setattr(orchestrator, "build_report_draft", build)
    monkeypatch.setattr(orchestrator, "review_report", assess)
    monkeypatch.setattr(orchestrator, "recover_report_draft", recover)
    monkeypatch.setattr(orchestrator, "finalize_report", finalize)
    result = await orchestrator.build_report(_state(), config)
    records = [e for e in await service.events("run", "owner") if e.type.startswith("report.")]
    assert [e.type for e in records] == [
        "report.review.started", "report.review.completed"
    ] * (2 if kind == "recovered" else 1)
    completed = records[-1].payload
    assert completed["status"] == result["report_review"]["status"]
    assert completed["decision"] == result["report_review"]["decision"]
    assert completed["attempt"] == (2 if kind == "recovered" else 1)

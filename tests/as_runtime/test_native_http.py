"""Native HTTP scheduling against real SQL checkpoints and the research pipeline."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
)
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.research_router import build_research_router
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def host(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    closed = []

    class Stages:
        async def execute(self, stage, state):
            if stage == "plan_approval":
                return PendingDecision(stage=stage, question="Confirm plan")
            if stage == "final_report_generation":
                state.final_report = "Native report"

    @asynccontextmanager
    async def factory(state, config, recovery):
        try:
            yield ResearchPipeline(
                state,
                Stages(),
                recovery.save,
                config_fingerprint=state.config_fingerprint,
                recovery=recovery,
            )
        finally:
            closed.append(state.run_id)

    async def prepare(request, principal):
        return {
            "configurable": request.configurable,
            "metadata": {"user_id": principal.user_id},
        }

    service = NativeRuns(store, factory, prepare, runs_dir=tmp_path / "archive")
    app = FastAPI()
    app.include_router(build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal(
        "alice"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield service, client, app, closed
    await service.aclose()
    await store.aclose()


async def settle(service):
    for task in list(service.tasks.values()):
        await asyncio.wait_for(asyncio.shield(task), 10)
    await asyncio.sleep(0)


async def test_native_task_activity_uses_sql_owner_and_replays(host, monkeypatch):
    from open_deep_research.api.activity_routes import ActivityRoutes
    from open_deep_research.api.streams import StreamOptions, _task_activity_iterator
    from open_deep_research.configuration import Configuration
    from fastapi import HTTPException
    from open_deep_research.agentscope_runtime.native_security import NativeEventPublisher
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.events.task_activity import TaskActivityStore

    service, client, app, _ = host
    monkeypatch.setenv("RUNS_DIR", str(service.runs_dir))
    def legacy_miss(*args):
        raise HTTPException(404, "Run not found")

    async def reserve(*args):
        return None

    async def authorize(_):
        return True

    options = StreamOptions(configuration=Configuration(), shutdown=asyncio.Event(),
                            authorize=authorize, reauth_interval=60)
    routes = ActivityRoutes(
        native_service=lambda: service, lookup_record=lambda _: None,
        require_run_owner=legacy_miss, require_record_owner=legacy_miss,
        reserve_sse_connection=reserve, limited_sse=lambda stream, _: stream,
        task_activity_iterator=lambda store, **kw: _task_activity_iterator(store, options=options, **kw),
        public_event_iterator=None,
    )
    app.include_router(routes.router)
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "Question"}]})
    run_id = response.json()["run_id"]
    await settle(service)
    recovery = await RecoverySession.open(service.store, run_id, "alice")
    try:
        await NativeEventPublisher(service.store, recovery.lease).publish(
            "research.task.started", stage="researching",
            payload={"task_id": "task-one", "title": "Evidence", "status": "running"},
            dedupe_key="task-one-started",
        )
    finally:
        await recovery.close()
    activity = TaskActivityStore(run_id, "task-one", runs_dir=str(service.runs_dir))
    await activity.append(
        "task.completed", kind="lifecycle", phase="terminal", status="success",
        title="Completed", summary="Safe evidence", iteration=1, duration_ms=12,
        payload={}, dedupe_key="done",
    )
    assert not (service.runs_dir / run_id / "context" / "manifest.json").exists()
    url = f"/runs/{run_id}/tasks/task-one/activity"
    page = await client.get(url)
    assert page.status_code == 200, page.text
    assert page.json()["items"][0]["type"] == "task.completed"
    stream = await client.get(url + "/stream")
    assert stream.status_code == 200 and "event: task.completed" in stream.text
    assert "dedupe_key" not in stream.text
    assert (await client.get(url + "/stream?after=8")).status_code == 409
    assert (await client.get(url + "/stream?after=1")).text == ""
    assert (await client.get(url.replace("task-one", "unknown"))).status_code == 404
    app.dependency_overrides[get_current_principal] = lambda: research_principal("foreign")
    assert (await client.get(url)).status_code == 404
    assert (await client.get(url + "/stream")).status_code == 404


async def test_native_feedback_budget_and_team_ownership(host, monkeypatch):
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    service, client, app, _closed = host
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "Question"}]})
    run_id = response.json()["run_id"]
    await settle(service)
    recovery = await RecoverySession.open(service.store, run_id, "alice")
    await service.store.register_task(recovery.lease, "supervisor")
    await recovery.close()
    uncreated_team = SimpleNamespace(members=AsyncMock(return_value=[]), say=AsyncMock())
    monkeypatch.setattr(service.pipeline_factory, "active", {run_id: {"team": uncreated_team}}, raising=False)
    body = {"type": "direction", "message": "Focus on evidence", "command_id": "feedback-1"}
    first = await client.post(f"/runs/{run_id}/feedback", json=body)
    second = await client.post(f"/runs/{run_id}/feedback", json=body)
    assert first.status_code == 200 and second.json() == first.json()
    uncreated_team.say.assert_not_called()
    assert (await client.get(f"/runs/{run_id}/budget")).status_code == 200
    app.dependency_overrides[get_current_principal] = lambda: research_principal("foreign")
    for suffix in ["budget", "team"]:
        assert (await client.get(f"/runs/{run_id}/{suffix}")).status_code == 404
    assert (await client.post(f"/runs/{run_id}/feedback", json=body)).status_code == 404


async def test_http_create_approve_snapshot_sse_and_idempotency(host):
    service, client, app, closed = host
    body = {"messages": [{"role": "user", "content": "A research question"}]}
    response = await client.post(
        "/runs", json=body, headers={"Idempotency-Key": "same"}
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    await settle(service)
    paused = await client.get("/runs/" + run_id)
    assert paused.json()["status"] == "awaiting_plan_approval", paused.text
    pending = paused.json()["pending_human_action"]
    assert pending["action_id"]
    duplicate = await client.post(
        "/runs", json=body, headers={"Idempotency-Key": "same"}
    )
    assert duplicate.json()["run_id"] == run_id
    conflict = await client.post(
        "/runs",
        json={"messages": [{"role": "user", "content": "other"}]},
        headers={"Idempotency-Key": "same"},
    )
    assert conflict.status_code == 409
    decision = await client.post(
        f"/runs/{run_id}/human-actions/{pending['action_id']}",
        json={"action": "approve"},
    )
    assert decision.status_code == 200, decision.text
    await settle(service)
    complete = (await client.get("/runs/" + run_id)).json()
    assert complete["status"] == "completed", complete
    assert complete["output"]["markdown"] == "Native report"
    assert "configuration" not in complete
    replay = await client.get(f"/runs/{run_id}/events")
    assert "event: run.completed" in replay.text
    assert (
        await client.get(
            f"/runs/{run_id}/events",
            headers={"Last-Event-ID": str(complete["last_event_id"])},
        )
    ).text == ""
    assert (await client.get(f"/runs/{run_id}/events?after=99999")).status_code == 409
    for cursor in ["bad", "-1"]:
        assert (await client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": cursor})).status_code == 400
    assert closed.count(run_id) == 2
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.get("/runs/" + run_id)).status_code == 404
    assert (await client.get(f"/runs/{run_id}/events")).status_code == 404
    assert (await client.get("/runs")).json()["items"] == []
    assert (await client.post("/runs/" + run_id + "/cancel")).status_code == 404


async def test_cancel_and_privileged_input_rejected(host):
    service, client, _app, _closed = host
    assert (
        await client.post(
            "/runs", json={"messages": [{"role": "system", "content": "forged"}]}
        )
    ).status_code == 422
    assert (
        await client.post(
            "/runs",
            json={
                "messages": [{"role": "user", "content": "ok"}],
                "metadata": {"user_id": "other"},
            },
        )
    ).status_code == 422
    run_id = (
        await client.post(
            "/runs", json={"messages": [{"role": "user", "content": "q"}]}
        )
    ).json()["run_id"]
    await settle(service)
    assert (await client.post(f"/runs/{run_id}/cancel")).status_code == 200
    assert (await client.get("/runs/" + run_id)).json()["status"] == "cancelled"
    assert (await client.post(f"/runs/{run_id}/resume", json={})).status_code == 409


async def test_history_cannot_resume_and_pagination_is_owner_scoped(host):
    from open_deep_research.run_context import RunManifest

    service, client, app, _closed = host
    context = service.runs_dir / "legacy-run" / "context"
    context.mkdir(parents=True)
    (context / "manifest.json").write_text(
        RunManifest(
            run_id="legacy-run", owner_id="alice", status="completed", created_at=1
        ).model_dump_json(),
        encoding="utf-8",
    )
    (context / "final_report.md").write_text("Old report", encoding="utf-8")
    assert (await client.get("/runs/legacy-run")).json()["read_only"] is True
    assert (await client.get("/runs/legacy-run/events")).json()["detail"] == (
        "event_stream_unavailable_legacy_run"
    )
    # Archived logs can end before a terminal event: replay still closes.
    from open_deep_research.events.public import PublicEvent

    event = PublicEvent(
        run_id="legacy-run",
        event_id="old-1",
        dedupe_key="old-started",
        sequence=1,
        timestamp="2026-09-17T00:00:00+00:00",
        type="run.started",
        payload={},
    )
    event_file = context.parent / "public_events.jsonl"
    event_file.write_text(event.model_dump_json() + "\n", encoding="utf-8")
    replay = await asyncio.wait_for(client.get("/runs/legacy-run/events"), 5)
    assert replay.status_code == 200 and "event: run.started" in replay.text
    event_file.write_text("broken tail\n", encoding="utf-8")
    assert (await client.get("/runs/legacy-run")).json()["detail"] == (
        "historical_artifact_corrupted"
    )
    assert (await client.get("/runs")).json()["items"] == []
    event_file.write_text(event.model_dump_json() + "\n", encoding="utf-8")
    assert (await client.post("/runs/legacy-run/resume", json={})).json()[
        "detail"
    ] == "legacy_checkpoint_read_only"
    run_id = (
        await client.post(
            "/runs", json={"messages": [{"role": "user", "content": "new"}]}
        )
    ).json()["run_id"]
    await settle(service)
    page = (await client.get("/runs?limit=1")).json()
    assert page["items"][0]["run_id"] == run_id
    older = (
        await client.get("/runs", params={"limit": 1, "cursor": page["next_cursor"]})
    ).json()
    assert older["items"][0]["run_id"] == "legacy-run"
    assert older["next_cursor"] is None
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.post("/runs/legacy-run/resume", json={})).status_code == 404


@pytest.mark.parametrize("cursor", ["bad", "e30", "W10", "bnVsbA"])
async def test_malformed_list_cursor_is_bad_request(host, cursor):
    _service, client, _app, _closed = host
    assert (await client.get("/runs", params={"cursor": cursor})).status_code == 400


async def test_native_publication_routes_owner_and_reuse(host, monkeypatch):
    monkeypatch.setenv("PUBLISHER_ENABLED", "true")
    service, client, app, _closed = host
    run_id = (
        await client.post(
            "/runs", json={"messages": [{"role": "user", "content": "report"}]}
        )
    ).json()["run_id"]
    await settle(service)
    assert (
        await client.post(f"/runs/{run_id}/publications", json={"format": "markdown"})
    ).status_code == 409
    pending = (await client.get(f"/runs/{run_id}")).json()["pending_human_action"]
    await client.post(
        f"/runs/{run_id}/human-actions/{pending['action_id']}",
        json={"action": "approve"},
    )
    await settle(service)
    response = await client.post(
        f"/runs/{run_id}/publications", json={"format": "markdown"}
    )
    assert response.status_code == 202, response.text
    publication = response.json()
    duplicate = await client.post(
        f"/runs/{run_id}/publications", json={"format": "markdown"}
    )
    assert duplicate.json()["reused"] is True
    assert duplicate.json()["publication_id"] == publication["publication_id"]
    assert (await client.get(publication["download_url"])).status_code == 409
    listed = (await client.get(f"/runs/{run_id}/publications")).json()
    assert listed["run_id"] == run_id and len(listed["items"]) == 1
    assert (
        await client.get(f"/runs/{run_id}/publications/events?after=999999")
    ).status_code == 409
    for cursor in ["bad", "-1"]:
        assert (await client.get(f"/runs/{run_id}/publications/events", headers={"Last-Event-ID": cursor})).status_code == 400
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.get(publication["status_url"])).status_code == 404
    assert (await client.get(f"/runs/{run_id}/publications/events")).status_code == 404
    assert (await client.post(publication["status_url"] + "/retry")).status_code == 404


async def test_historical_publication_download_and_replay_are_read_only(host, monkeypatch):
    import hashlib
    from tests.as_runtime.test_http_boundary import archive, hashes
    from open_deep_research.events.publications import PublicationEventStore
    from open_deep_research.report.models import PublisherTheme, RenderedArtifact
    from open_deep_research.report.publication_store import PublicationJobStore

    service, client, app, _closed = host
    archive(service.runs_dir)
    store = PublicationJobStore("old", runs_dir=service.runs_dir)
    content = "# 历史发布工件".encode()
    job, _ = store.enqueue(
        report_sha256=hashlib.sha256(content).hexdigest(),
        publication_format="markdown", theme=PublisherTheme(), max_attempts=2,
    )
    claimed = store.claim(job.publication_id, worker_id="fixture", lease_seconds=30)
    artifact = store.commit_file(
        claimed, RenderedArtifact(content=content, media_type="text/markdown", extension="md"),
        report_title="History", max_output_bytes=10000, worker_id="fixture",
    )
    store.complete(job.publication_id, worker_id="fixture", artifact=artifact)
    events = PublicationEventStore("old", runs_dir=service.runs_dir)
    # A historical stream can end before completion, without a final newline.
    event = events.append("publication.started", publication_id=job.publication_id,
                         payload={"format": "markdown", "status": "running", "attempt": 1},
                         dedupe_key="historical-start")
    events.path.write_bytes(events.path.read_bytes().rstrip(b"\n"))
    events.lock_path.unlink()
    before = hashes(service.runs_dir)
    base = f"/runs/old/publications/{job.publication_id}"
    listing = await client.get("/runs/old/publications")
    assert listing.status_code == 200 and len(listing.json()["items"]) == 1
    assert (await client.get(base)).json()["status"] == "completed"
    response = await client.get(base + "/download")
    assert response.status_code == 200 and response.content == content
    assert response.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'
    replay = await asyncio.wait_for(client.get("/runs/old/publications/events"), 5)
    assert replay.status_code == 200 and "event: publication.started" in replay.text
    assert (await client.get("/runs/old/publications/events",
                             headers={"Last-Event-ID": str(event.sequence)})).text == ""
    assert (await client.get("/runs/old/publications/events?after=99")).status_code == 409
    assert (await client.get("/runs/old/publications/events?after=-1")).status_code == 400
    monkeypatch.setenv("PUBLISHER_ENABLED", "true")
    assert (await client.post(base + "/retry")).status_code == 404
    assert (await client.post("/runs/old/publications", json={"format": "markdown"})).status_code == 404
    assert hashes(service.runs_dir) == before
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    for url in (base, base + "/download", "/runs/old/publications/events"):
        assert (await client.get(url)).status_code == 404
    app.dependency_overrides[get_current_principal] = lambda: research_principal("alice")
    events.path.write_bytes(events.path.read_bytes() + b"\n{broken")
    damaged = hashes(service.runs_dir)
    response = await client.get("/runs/old/publications/events")
    assert response.status_code == 409
    assert response.json()["detail"] == "historical_artifact_corrupted"
    assert hashes(service.runs_dir) == damaged


async def test_failed_run_hides_committed_report_and_rejects_publication(host, monkeypatch):
    monkeypatch.setenv("PUBLISHER_ENABLED", "true")
    service, client, _app, _closed = host

    class FailingStages:
        async def execute(self, stage, state):
            if stage == "final_report_generation":
                state.final_report = "Committed draft before terminal failure"
                state.report_product = {"result": {"status": "success"}}
            if stage == "memory_extract_and_write":
                raise RuntimeError("fixture terminal failure")

    @asynccontextmanager
    async def factory(state, config, recovery):
        yield ResearchPipeline(
            state, FailingStages(), recovery.save,
            config_fingerprint=state.config_fingerprint, recovery=recovery,
        )

    service.pipeline_factory = factory
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "Failure after draft"}]})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    await settle(service)
    durable, _ = await service.store.load(run_id, "alice")
    assert durable.status == "failed"
    assert durable.final_report == "Committed draft before terminal failure"
    snapshot = (await client.get(f"/runs/{run_id}")).json()
    assert snapshot["status"] == "failed"
    assert snapshot["output"]["markdown"] == ""
    assert snapshot["output"]["status"] != "success"
    listing = (await client.get("/runs")).json()
    item = next(row for row in listing["items"] if row["run_id"] == run_id)
    assert item["status"] == "failed" and item["output"]["markdown"] == ""
    assert (await client.post(f"/runs/{run_id}/publications", json={"format": "markdown"})).status_code == 409
    assert (await client.get(f"/runs/{run_id}/publications")).json()["items"] == []

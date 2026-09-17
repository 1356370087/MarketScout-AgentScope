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


async def test_native_feedback_budget_and_team_ownership(host):
    from open_deep_research.agentscope_runtime.recovery import RecoverySession

    service, client, app, _closed = host
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "Question"}]})
    run_id = response.json()["run_id"]
    await settle(service)
    recovery = await RecoverySession.open(service.store, run_id, "alice")
    await service.store.register_task(recovery.lease, "supervisor")
    await recovery.close()
    body = {"type": "direction", "message": "Focus on evidence", "command_id": "feedback-1"}
    first = await client.post(f"/runs/{run_id}/feedback", json=body)
    second = await client.post(f"/runs/{run_id}/feedback", json=body)
    assert first.status_code == 200 and second.json() == first.json()
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
    assert closed.count(run_id) == 2
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.get("/runs/" + run_id)).status_code == 404
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
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.get(publication["status_url"])).status_code == 404
    assert (await client.post(publication["status_url"] + "/retry")).status_code == 404

"""Native HTTP keeps deployment admission limits after legacy retirement."""

# ruff: noqa: F811 -- imported pytest fixtures

import asyncio

import pytest

from open_deep_research.api.admission import ApiAdmission, LimitedStreamingResponse
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.contracts import RunRequest
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal
from tests.as_runtime.test_native_http import host, settle  # noqa: F401

pytestmark = pytest.mark.asyncio
QUESTION = {"messages": [{"role": "user", "content": "Research this question"}]}


async def test_concurrent_admission_counts_durable_waiting_runs_after_restart(host, monkeypatch):
    service, client, _, _ = host
    monkeypatch.setenv("MAX_CONCURRENT_RUNS_PER_USER", "1")
    first, second = await asyncio.gather(
        client.post("/runs", json=QUESTION), client.post("/runs", json=QUESTION)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 429]
    await settle(service)
    assert not service.tasks
    restarted = NativeRuns(service.store, service.pipeline_factory, service.prepare_config)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as denied:
        await restarted.create(RunRequest(**QUESTION), research_principal("alice"))
    assert denied.value.detail == "concurrent_run_limit_reached"
    run_id = (first if first.status_code == 200 else second).json()["run_id"]
    await service.cancel(run_id, "alice")
    assert (await client.post("/runs", json=QUESTION)).status_code == 200
    await settle(service)


async def test_rate_limit_preserves_idempotent_replay_and_principal_isolation(host, monkeypatch):
    service, client, app, _ = host
    monkeypatch.setenv("API_RUN_CREATE_PER_MINUTE", "1")
    headers = {"Idempotency-Key": "one-request"}
    first = await client.post("/runs", json=QUESTION, headers=headers)
    await settle(service)
    replay = await client.post("/runs", json=QUESTION, headers=headers)
    assert first.json() == replay.json()
    blocked = await client.post("/runs", json=QUESTION)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "run_create_rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.post("/runs", json=QUESTION)).status_code == 200
    await settle(service)


async def test_sse_limit_is_shared_and_rejects_before_creating_run(host, monkeypatch):
    service, client, _, _ = host
    monkeypatch.setenv("MAX_CONCURRENT_SSE_CONNECTIONS", "1")
    first = await client.post("/runs", json=QUESTION)
    await settle(service)
    run_id = first.json()["run_id"]
    assert await service.admission.connection_limiter.acquire(1)
    try:
        for path in (f"/runs/{run_id}/events", f"/runs/{run_id}/publications/events"):
            response = await client.get(path)
            assert response.status_code == 429, response.text
        response = await client.post("/runs/stream", json=QUESTION)
        assert response.status_code == 429
        assert len((await service.list_runs("alice"))["items"]) == 1
    finally:
        await service.admission.connection_limiter.release(1)
    await service.cancel(run_id, "alice")
    assert (await client.get(f"/runs/{run_id}/events")).status_code == 200
    assert service.admission.connection_limiter.active == 0


async def test_failed_stream_creation_releases_connection(host):
    service, client, _, _ = host
    response = await client.post("/runs/stream", json={"messages": []})
    assert response.status_code == 422
    assert service.admission.connection_limiter.active == 0


@pytest.mark.parametrize("outcome", ["disconnect", "cancel", "error"])
async def test_response_releases_exactly_one_slot_on_every_exit(outcome):
    admission = ApiAdmission()
    assert await admission.connection_limiter.acquire(2)
    assert await admission.connection_limiter.acquire(2)
    started = asyncio.Event()

    async def source():
        started.set()
        if outcome == "error":
            raise RuntimeError("stream failed")
        await asyncio.Event().wait()
        yield "unreachable"

    async def receive():
        if outcome == "disconnect":
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        pass

    response = LimitedStreamingResponse(source(), admission=admission, release_token=2)
    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send))
    if outcome == "cancel":
        await started.wait()
        task.cancel()
    if outcome == "error":
        with pytest.raises(RuntimeError, match="stream failed"):
            await task
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert admission.connection_limiter.active == 1
    await admission.connection_limiter.release(2)

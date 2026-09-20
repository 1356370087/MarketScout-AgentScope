"""Exercise the native SQL stream rather than the historical file iterator."""

# ruff: noqa: F811 -- imported pytest fixtures

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_deep_research.api import research_router
from tests.as_runtime.test_native_http import host, settle  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_model_tokens_do_not_become_domain_completion_events():
    from open_deep_research.agentscope_runtime.recovery_events import public_events

    for kind in ["model.delta", "model.completed", "tool.completed"]:
        assert public_events({"sequence": 1, "payload": {"type": kind, "status": "completed"}}) == []


@pytest.mark.parametrize("stop", ["revocation", "shutdown"])
async def test_native_sql_stream_heartbeat_and_live_stop(host, monkeypatch, stop):
    service, client, app, _ = host
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "Research"}]})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    await settle(service)
    events = await service.events(run_id, "alice")
    assert events and events[-1].type == "approval.required"

    monkeypatch.setattr(research_router.Configuration, "from_runnable_config", lambda _: SimpleNamespace(
        sse_poll_interval_ms=1, sse_heartbeat_seconds=0, max_concurrent_sse_connections=1,
    ))
    monkeypatch.setattr(research_router, "get_settings", lambda: SimpleNamespace(sse_reauth_interval=0))

    @asynccontextmanager
    async def session():
        yield object()

    authorize = AsyncMock(return_value=object())
    monkeypatch.setattr(research_router, "session_scope", session)
    monkeypatch.setattr(research_router, "reauthorize_session", authorize)
    # Exercise the complete ASGI response, including admission release, while
    # observing each heartbeat without HTTPX's ASGI response buffering.
    route = next(route for route in research_router.build_research_router(service).routes
                 if getattr(route, "path", None) == "/runs/{run_id}/events")
    response = await route.endpoint(run_id, after=0, last_event_id=str(events[-1].sequence),
                                    principal=SimpleNamespace(user_id="alice", session_id="session-1"))
    bodies = asyncio.Queue()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await bodies.put(message["body"])

    async def receive():
        await asyncio.Event().wait()

    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    try:
        assert await asyncio.wait_for(bodies.get(), 2) == b": keep-alive\n\n"
        assert authorize.await_count == 1
        if stop == "revocation":
            authorize.return_value = None
        else:
            service.closed = True
        await asyncio.wait_for(task, 2)
        assert authorize.await_count == (2 if stop == "revocation" else 1)
        assert service.admission.connection_limiter.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

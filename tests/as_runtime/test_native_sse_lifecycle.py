"""Exercise the native SQL stream rather than the historical file iterator."""

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
        sse_poll_interval_ms=1, sse_heartbeat_seconds=0,
    ))
    monkeypatch.setattr(research_router, "get_settings", lambda: SimpleNamespace(sse_reauth_interval=0))

    @asynccontextmanager
    async def session():
        yield object()

    authorize = AsyncMock(return_value=object())
    monkeypatch.setattr(research_router, "session_scope", session)
    monkeypatch.setattr(research_router, "reauthorize_session", authorize)
    # Call the real route's body iterator so a deliberately open SSE response
    # is observable without HTTPX's ASGI response buffering.
    route = next(route for route in research_router.build_research_router(service).routes
                 if getattr(route, "path", None) == "/runs/{run_id}/events")
    response = await route.endpoint(run_id, after=0, last_event_id=str(events[-1].sequence),
                                    principal=SimpleNamespace(user_id="alice", session_id="session-1"))
    stream = response.body_iterator
    try:
        assert await asyncio.wait_for(anext(stream), 2) == ": keep-alive\n\n"
        assert authorize.await_count == 1
        if stop == "revocation":
            authorize.return_value = None
        else:
            service.closed = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 2)
        assert authorize.await_count == (2 if stop == "revocation" else 1)
    finally:
        await stream.aclose()

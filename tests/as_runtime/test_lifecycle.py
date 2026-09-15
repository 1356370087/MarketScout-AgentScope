"""T016 关闭边界与 T014 组合接入回归。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from open_deep_research.agentscope_runtime.app import ASRuntime
from open_deep_research.agentscope_runtime.lifecycle import (
    ShutdownGate,
    ShutdownStack,
    run_shutdown_sequence,
)
from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

pytestmark = pytest.mark.asyncio


async def test_cancel_persists_before_release_and_pool_close():
    gate, stack = ShutdownGate(), ShutdownStack()
    ready, saved = asyncio.Event(), asyncio.Event()
    order = []

    async def request():
        gate.begin()
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            order.append("saved")
            saved.set()
            await gate.end()

    async def release():
        assert saved.is_set()
        order.append("release")

    async def close():
        assert order[-1] == "release"
        order.append("pool")

    stack.push_locked_release("lock", release)
    stack.push_base("db", close)
    task = asyncio.create_task(request())
    await ready.wait()
    result = await run_shutdown_sequence(gate, stack, drain_timeout=0.01)
    assert not result["idle"] and task.cancelled()
    assert order == ["saved", "release", "pool"]
    with pytest.raises(RuntimeError, match="closed"):
        gate.begin()
    await stack.teardown()
    assert order == ["saved", "release", "pool"]


async def test_hook_failure_is_reported_and_other_resources_close():
    stack = ShutdownStack()
    close = AsyncMock()
    stack.push_base("pool", close)
    stack.push_drain("bad", AsyncMock(side_effect=RuntimeError("failed")))
    with pytest.raises(ExceptionGroup):
        await stack.teardown()
    close.assert_awaited_once()


async def test_runtime_gate_and_concurrent_close(monkeypatch):
    runtime = await ASRuntime.create(
        ASRuntimeSettings(None, "unused", True, "test_", drain_timeout=0.01)
    )
    app = runtime.build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            runtime.gate.close()
            assert (await client.get("/openapi.json")).status_code == 503
        await asyncio.gather(runtime.aclose(), runtime.aclose())
    assert runtime.shutdown_stack.order.count("storage") == 1
    assert runtime.shutdown_stack.order.index(
        "agentscope_services"
    ) < runtime.shutdown_stack.order.index("storage")


async def test_start_failure_closes_storage(monkeypatch):
    storage = AsyncMock()
    monkeypatch.setattr(
        "open_deep_research.agentscope_runtime.app.build_storage", lambda _: storage
    )
    monkeypatch.setattr(
        ASRuntime, "_start_bus", AsyncMock(side_effect=RuntimeError("startup"))
    )
    with pytest.raises(RuntimeError, match="startup"):
        await ASRuntime.create(ASRuntimeSettings(None, "unused", True, "test_"))
    storage.__aexit__.assert_awaited_once()


async def test_runtime_polls_durable_fact_without_signal(pg_url):
    runtime = await ASRuntime.create(
        ASRuntimeSettings(pg_url, "agentscope_runtime", True, "poll_")
    )
    done = asyncio.Event()

    async def apply(command_id, payload):
        done.set()

    try:
        await runtime.commands.submit("run-poll", "poll-command", {}, signal=False)
        runtime.start_command_consumer("run-poll", apply, poll_seconds=0.01)
        await asyncio.wait_for(done.wait(), 5)
    finally:
        await runtime.aclose()
    assert "commands:run-poll" in runtime.shutdown_stack.order


async def test_native_registry_shutdown_persists_pg_state_before_unlock(pg_url):
    from agentscope.app.storage._model._session import SessionConfig
    from agentscope.state import AgentState

    settings = ASRuntimeSettings(pg_url, "agentscope_runtime", True, "shutdown_native_")
    runtime = await ASRuntime.create(settings)
    record = await runtime.storage.upsert_session(
        user_id="shutdown-user",
        agent_id="shutdown-agent",
        config=SessionConfig(workspace_id="shutdown-ws"),
    )
    app = runtime.build_app()
    ready = asyncio.Event()

    async def run():
        async with runtime.message_bus.acquire_lock("shutdown-run"):
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await runtime.storage.update_session_state(
                    "shutdown-user",
                    "shutdown-agent",
                    record.id,
                    AgentState(session_id=record.id, summary="cancelled-and-saved"),
                )
                assert await runtime.message_bus.is_locked("shutdown-run")

    async with app.router.lifespan_context(app):
        task = app.state.chat_run_registry.spawn(run(), session_id=record.id)
        await asyncio.wait_for(ready.wait(), 5)
    assert task.cancelled()
    reopened = await ASRuntime.create(settings)
    try:
        saved = await reopened.storage.get_session(
            "shutdown-user", "shutdown-agent", record.id
        )
        assert saved.state.summary == "cancelled-and-saved"
        assert not await reopened.message_bus.is_locked("shutdown-run")
    finally:
        await reopened.aclose()

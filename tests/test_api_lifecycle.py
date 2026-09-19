"""Application lifespan orchestration and shutdown ownership."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from open_deep_research.api import lifecycle
from open_deep_research.agentscope_runtime import native_host
from open_deep_research.configuration import Configuration


@pytest.mark.asyncio
async def test_lifespan_mounts_native_and_drains_owned_resources(monkeypatch, tmp_path):
    config = Configuration(
        runs_dir=str(tmp_path), sandbox_enabled=False, model_backend="legacy",
        retention_sweep_interval_seconds=1, run_recovery_sweep_on_startup=True,
    )
    monkeypatch.setattr(lifecycle.Configuration, "from_runnable_config", lambda _: config)
    monkeypatch.setattr(lifecycle, "get_document_settings", lambda: SimpleNamespace(enabled=False))
    for name in ("startup_checks", "assert_schema_current", "close_document_pool", "close_embedding_clients", "shutdown_rbac"):
        monkeypatch.setattr(lifecycle, name, AsyncMock())
    service = SimpleNamespace(native_aclose=AsyncMock())
    monkeypatch.setattr(native_host, "native_engine_enabled", lambda: True)
    monkeypatch.setattr(native_host, "build_native_research_service", AsyncMock(return_value=service))
    mount = Mock()
    monkeypatch.setattr(native_host, "mount_native_research", mount)
    team_close = AsyncMock()
    monkeypatch.setitem(sys.modules, "open_deep_research.tasks.team_runtime", SimpleNamespace(team_runtime=SimpleNamespace(close=team_close)))
    state = {"service": None}
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def retention(_config):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    eviction = asyncio.create_task(asyncio.Event().wait())
    evictions = {"completed": eviction}
    recovery = AsyncMock(return_value=0)
    drain = AsyncMock()
    host = lifecycle.ApplicationLifecycle(
        get_native_service=lambda: state["service"],
        set_native_service=lambda value: state.update(service=value),
        recovery_sweep=recovery, retention_loop=retention,
        native_key_cleanup=AsyncMock(), drain_runs=drain,
        eviction_tasks=lambda: evictions,
    )
    app = FastAPI()
    async with host.lifespan(app):
        await asyncio.wait_for(entered.wait(), 2)
        assert state["service"] is service
        mount.assert_called_once_with(app, service)
        recovery.assert_awaited_once_with(config)
        assert not host.shutting_down.is_set()
        assert not host.sse_shutdown.is_set()
    drain.assert_awaited_once_with(config.shutdown_drain_timeout_seconds)
    assert host.shutting_down.is_set() and host.sse_shutdown.is_set()
    assert stopped.is_set() and host.retention_task is None
    assert eviction.cancelled() and not evictions
    assert state["service"] is None
    service.native_aclose.assert_awaited_once()
    team_close.assert_awaited_once()
    lifecycle.close_document_pool.assert_awaited_once()
    lifecycle.close_embedding_clients.assert_awaited_once()
    lifecycle.shutdown_rbac.assert_awaited_once()

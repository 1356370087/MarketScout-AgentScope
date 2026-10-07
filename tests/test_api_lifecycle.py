"""Application lifespan orchestration and shutdown ownership."""

import asyncio
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
    service = SimpleNamespace(native_aclose=AsyncMock(), recover_interrupted=AsyncMock())
    monkeypatch.setattr(native_host, "native_engine_enabled", lambda: True)
    monkeypatch.setattr(native_host, "build_native_research_service", AsyncMock(return_value=service))
    mount = Mock()
    monkeypatch.setattr(native_host, "mount_native_research", mount)
    state = {"service": None}
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def retention(_config):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    service.retention = SimpleNamespace(loop=retention)

    host = lifecycle.ApplicationLifecycle(
        get_native_service=lambda: state["service"],
        set_native_service=lambda value: state.update(service=value),
        native_key_cleanup=AsyncMock(),
    )
    app = FastAPI()
    async with host.lifespan(app):
        await asyncio.wait_for(entered.wait(), 2)
        assert state["service"] is service
        mount.assert_called_once_with(app, service)
        service.recover_interrupted.assert_awaited_once_with()
        lifecycle.assert_schema_current.assert_awaited_once_with("0018_knowledge_execution")
        assert not host.shutting_down.is_set()
        assert not host.sse_shutdown.is_set()
    assert host.shutting_down.is_set() and host.sse_shutdown.is_set()
    assert stopped.is_set() and host.retention_task is None
    assert state["service"] is None
    service.native_aclose.assert_awaited_once()
    lifecycle.close_document_pool.assert_awaited_once()
    lifecycle.close_embedding_clients.assert_awaited_once()
    lifecycle.shutdown_rbac.assert_awaited_once()

"""Acceptance coverage for the remaining operations-control SPEC workstreams."""

# ruff: noqa: F811 -- imported pytest fixtures

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from open_deep_research import server
from open_deep_research.api.admission import ApiAdmission
from tests.as_runtime.test_native_http import host, settle  # noqa: F401
from open_deep_research.api_governance import ConnectionLimiter, FixedWindowRateLimiter
from open_deep_research.configuration import Configuration
from open_deep_research.logging_config import (
    JSONFormatter,
    RequestContextFilter,
    bind_request_id,
)
from open_deep_research.memory import maintenance
from open_deep_research.observability.tracing import SQLiteTraceStore, TokenUsage
from security.rbac.principal import Principal


def _principal(user_id: str, *, admin: bool = False) -> Principal:
    return Principal(
        user_id=user_id,
        email=f"{user_id}@example.test",
        status="active",
        session_id="session",
        roles=frozenset({"admin"} if admin else {"researcher"}),
        permissions=frozenset({"research.run.control_own"}),
        authz_version=1,
    )








def test_json_logging_and_request_id_round_trip(monkeypatch) -> None:
    bind_request_id("test-123")
    record = logging.LogRecord("spec", logging.INFO, __file__, 1, "hello", (), None)
    RequestContextFilter().filter(record)
    payload = json.loads(JSONFormatter().format(record))
    assert payload["request_id"] == "test-123"

    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1024")
    client = TestClient(server.app)
    response = client.get("/healthz", headers={"X-Request-ID": "test-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-123"
    too_large = client.post(
        "/healthz",
        content=b"x" * 2048,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert too_large.status_code == 413

    def chunks():
        yield b"x" * 700
        yield b"y" * 700

    streamed = client.post(
        "/healthz",
        content=chunks(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert streamed.status_code == 413


@pytest.mark.asyncio
async def test_request_id_is_written_to_native_run_metadata(host):
    service, client, _, _ = host
    bind_request_id("run-request-123")
    response = await client.post("/runs", json={"messages": [{"role": "user", "content": "test"}]})
    await settle(service)
    state, _ = await service.store.load(response.json()["run_id"], "alice")
    assert state.application["request_metadata"]["request_id"] == "run-request-123"


def test_fixed_window_rate_limiter_rejects_eleventh_request() -> None:
    limiter = FixedWindowRateLimiter()
    for _ in range(10):
        assert limiter.allow("principal", 10, now=0) == (True, 0)
    allowed, retry_after = limiter.allow("principal", 10, now=0)
    assert not allowed
    assert retry_after == 60
    assert limiter.allow("principal", 10, now=61) == (True, 0)


def test_run_create_guard_rejects_eleventh_request() -> None:
    user = _principal("rate-owner")
    config = Configuration(
        api_run_create_per_minute=10,
        max_concurrent_runs_per_user=0,
        prometheus_enabled=False,
    )
    admission = ApiAdmission()
    for _ in range(10):
        admission.enforce_creation(user, config, 0)
    with pytest.raises(HTTPException) as limited:
        admission.enforce_creation(user, config, 0)
    assert limited.value.status_code == 429
    assert limited.value.headers["Retry-After"]
    # Native SQL lifecycle counting and slot release are covered by
    # tests/as_runtime/test_native_admission.py.


@pytest.mark.asyncio
async def test_connection_limiter_releases_terminal_slot() -> None:
    limiter = ConnectionLimiter()
    assert await limiter.acquire(1)
    assert not await limiter.acquire(1)
    await limiter.release(1)
    assert await limiter.acquire(1)


def test_run_limiter_failure_is_fail_open(monkeypatch) -> None:
    config = Configuration(
        api_run_create_per_minute=1,
        max_concurrent_runs_per_user=0,
        prometheus_enabled=False,
    )
    admission = ApiAdmission()
    monkeypatch.setattr(
        admission.rate_limiter,
        "allow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    admission.enforce_creation(_principal("owner"), config, 0)


def test_sqlite_busy_timeout_supports_concurrent_usage_writes(tmp_path) -> None:
    store = SQLiteTraceStore(str(tmp_path / "concurrent.sqlite3"))
    store.start_run("run", "owner", {})

    def write_usage(index: int) -> None:
        store.add_usage(
            "run",
            f"span-{index}",
            "test",
            "test",
            TokenUsage(input_tokens=1),
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(write_usage, range(100)))

    assert store.get_usage("run")["input_tokens"] == 100
    with store._connect() as conn:  # noqa: SLF001
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_memory_maintenance_loop_uses_configured_interval(monkeypatch) -> None:
    calls: list[int] = []
    delays: list[float] = []

    async def fake_daily(_args, _config):
        calls.append(1)
        return {"iteration": len(calls)}

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(maintenance, "_run_daily", fake_daily)
    args = argparse.Namespace(interval_hours=2, dry_run=True, user_id=None, loop=True)
    results = await maintenance._run_daily_loop(
        args,
        Configuration(),
        sleep=fake_sleep,
        max_iterations=2,
    )
    assert results == [{"iteration": 1}, {"iteration": 2}]
    assert delays == [7200]

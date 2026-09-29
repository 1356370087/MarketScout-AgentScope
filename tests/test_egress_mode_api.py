"""Public egress-mode API tests (GET/POST /runs/{run_id}/egress-mode)."""

import asyncio
from sqlalchemy.pool import NullPool
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.native_runs import NativeRuns

from fastapi.testclient import TestClient

from open_deep_research import server

_POLICY_AUTO = """version = 1
deployment_id = "egress-mode-test"
default_profile = "p"

[profiles.p.network]
unknown_target = "auto"

[profiles.p.runtime]
worker_image_digest = "sha256:{digest}"
"""

_POLICY_ASK = _POLICY_AUTO.replace('unknown_target = "auto"', 'unknown_target = "ask"')


def _write_policy(base_dir, document: str) -> str:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / "policy.toml"
    path.write_text(document.format(digest="0" * 64), encoding="utf-8")
    return str(path)


_owned_services = []


def _clear_native_runs():
    for service in _owned_services:
        asyncio.run(service.aclose())
        asyncio.run(service.store.aclose())
    _owned_services.clear()
    server._set_native_research_service(None)


def _live_run(run_id: str, tmp_path, policy_path: str, *, fence: int = 1):
    service = server._native_research_service

    async def create():
        run = RunConfig.compile({"configurable": {"runs_dir": str(service.runs_dir), "sandbox_policy_path": policy_path}})
        state = await service.store.create_from_config("local-dev-user", run_id, run,
                                                       application={"configuration": run.snapshot(),
                                                                    "request_configurable": {"sandbox_policy_path": policy_path}})
        for index in range(fence):
            lease = await service.store.acquire(run_id, "local-dev-user", ttl=120)
            if index + 1 < fence:
                await service.store.release(lease)
        state.status = "running"
        await service.store.save(lease, state)
        return state

    return asyncio.run(create())


def _bypass_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")
    monkeypatch.delenv("IAM_DATABASE_URL", raising=False)
    _clear_native_runs()
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "egress.db").as_posix(), engine_kwargs={"poolclass": NullPool})
    asyncio.run(store.create_tables())
    service = NativeRuns(store, None, None, runs_dir=tmp_path)
    _owned_services.append(service)
    server._set_native_research_service(service)
    return TestClient(server.app, raise_server_exceptions=False)


def test_get_reports_baseline_and_effective_mode(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-mode", tmp_path, policy)
    try:
        response = client.get("/runs/run-mode/egress-mode")
    finally:
        _clear_native_runs()
    assert response.status_code == 200
    body = response.json()
    assert body["baseline_mode"] == "auto"
    assert body["run_setting"] == "profile"
    assert body["override"] is None
    assert body["effective_mode"] == "auto"
    assert body["capped"] is False


def test_post_narrowing_override_applies_and_persists(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-mode", tmp_path, policy)
    try:
        switch = client.post("/runs/run-mode/egress-mode", json={"mode": "manual"})
        assert switch.status_code == 200
        assert switch.json()["effective_mode"] == "manual"
        # A second run of the same process reads the persisted override.
        report = client.get("/runs/run-mode/egress-mode")
        assert report.status_code == 200
        assert report.json()["override"] == "manual"
        assert report.json()["effective_mode"] == "manual"
    finally:
        _clear_native_runs()


def test_post_widening_past_baseline_is_refused(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path, _POLICY_ASK)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-mode", tmp_path, policy)
    try:
        refused = client.post("/runs/run-mode/egress-mode", json={"mode": "auto"})
        assert refused.status_code == 409
        assert "capped_by_baseline" in refused.json()["detail"]
        # open on an auto baseline is equally refused.
        policy_auto = _write_policy(tmp_path / "auto", _POLICY_AUTO)
        _live_run("run-auto", tmp_path / "auto", policy_auto)
        refused_open = client.post(
            "/runs/run-auto/egress-mode", json={"mode": "open"}
        )
        assert refused_open.status_code == 409
    finally:
        _clear_native_runs()


def test_post_requires_live_run(tmp_path, monkeypatch):
    from open_deep_research.run_context import RunContextStore

    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    context = RunContextStore("run-absent", runs_dir=str(tmp_path))
    context.initialize(
        "local-dev-user",
        {
            "configurable": {"runs_dir": str(tmp_path), "sandbox_policy_path": policy},
            "metadata": {"run_id": "run-absent", "owner": "local-dev-user"},
        },
    )
    context._update_manifest(  # noqa: SLF001 - persisted fence fixture
        allow_fence_advance=True,
        fence_token=1,
        fence_owner_id="test-owner",
    )
    response = client.post("/runs/run-absent/egress-mode", json={"mode": "manual"})
    assert response.status_code == 409


def test_post_rejects_unknown_mode(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-mode", tmp_path, policy)
    try:
        response = client.post(
            "/runs/run-mode/egress-mode", json={"mode": "sometimes"}
        )
    finally:
        _clear_native_runs()
    assert response.status_code == 422

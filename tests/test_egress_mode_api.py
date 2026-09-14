"""Public egress-mode API tests (GET/POST /runs/{run_id}/egress-mode)."""

from types import SimpleNamespace

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


def _live_run(run_id: str, tmp_path, policy_path: str, *, fence: int = 1):
    engine = SimpleNamespace(
        run_fence_token=fence,
        config={
            "configurable": {
                "runs_dir": str(tmp_path),
                "sandbox_policy_path": policy_path,
            },
            "metadata": {"owner": "local-dev-user"},
        },
    )
    record = server.RunRecord(run_id=run_id, engine=engine, status="running")
    server._runs[run_id] = record
    return record


def _bypass_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")
    monkeypatch.delenv("IAM_DATABASE_URL", raising=False)
    server._runs.clear()
    return TestClient(server.app, raise_server_exceptions=False)


def test_get_reports_baseline_and_effective_mode(tmp_path, monkeypatch):
    policy = _write_policy(tmp_path, _POLICY_AUTO)
    client = _bypass_client(monkeypatch, tmp_path)
    _live_run("run-mode", tmp_path, policy)
    try:
        response = client.get("/runs/run-mode/egress-mode")
    finally:
        server._runs.clear()
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
        server._runs.clear()


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
        server._runs.clear()


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
        server._runs.clear()
    assert response.status_code == 422

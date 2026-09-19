"""Product-facing API contract tests for the research frontend."""

import asyncio
import base64
import time

import httpx
from fastapi.testclient import TestClient

from open_deep_research import server
from open_deep_research.api import configuration_routes
from open_deep_research.events.public import RunEventStore
from open_deep_research.events.task_activity import TaskActivityStore
from open_deep_research.models.catalog import ModelCatalogEntry
from open_deep_research.run_context import RunContextStore
from open_deep_research.sandbox.approvals import SecurityApprovalStore
from security.auth import get_current_user
from tests.auth_helpers import research_principal


def _client(identity: str) -> TestClient:
    server.app.dependency_overrides[get_current_user] = lambda: research_principal(identity)
    return TestClient(server.app)


def test_capabilities_exposes_only_explicit_frontend_fields():
    client = _client("user-1")
    try:
        payload = client.get("/capabilities").json()
    finally:
        server.app.dependency_overrides.clear()

    keys = set(payload["editable_config_keys"])
    assert payload["public_event_schema_version"] == 2
    assert payload["public_task_activity_schema_version"] == 1
    assert payload["features"]["subagent_activity"] is True
    assert "research_model" in keys
    assert "mcp_config" not in keys
    assert "sandbox_allowed_domains" not in keys
    assert "langfuse_secret_key" not in keys
    assert payload["config_schema"]["additionalProperties"] is False


def test_capabilities_defaults_reflect_effective_environment(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL", "openai:deepseek-v4-flash")
    monkeypatch.setenv(
        "QUALITY_EVALUATION_MODEL",
        "openai:deepseek-v4-flash",
    )
    client = _client("user-1")
    try:
        payload = client.get("/capabilities").json()
    finally:
        server.app.dependency_overrides.clear()

    assert payload["defaults"]["research_model"] == "openai:deepseek-v4-flash"
    assert (
        payload["defaults"]["quality_evaluation_model"]
        == "openai:deepseek-v4-flash"
    )


def test_run_history_is_owner_scoped_sorted_and_legacy_title_falls_back(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    for run_id, owner, created_at, title in (
        ("old", "user-1", 1.0, None),
        ("new", "user-1", 2.0, "New research"),
        ("private", "user-2", 3.0, "Other user"),
    ):
        store = RunContextStore(run_id, runs_dir=str(tmp_path))
        store.initialize(owner, {"configurable": {"runs_dir": str(tmp_path)}})
        store._update_manifest(created_at=created_at, title=title)  # noqa: SLF001

    client = _client("user-1")
    try:
        response = client.get("/runs?limit=10")
    finally:
        server.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["items"]] == ["new", "old"]
    assert response.json()["items"][1]["title"] == "old"


def test_task_activity_is_owner_scoped_and_replays_terminal_stream(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    run_id = "activity-run"
    task_id = "task-1"
    context = RunContextStore(run_id, runs_dir=str(tmp_path))
    context.initialize(
        "user-1",
        {
            "configurable": {"runs_dir": str(tmp_path)},
            "metadata": {"run_id": run_id},
        },
    )
    run_events = RunEventStore(run_id, runs_dir=str(tmp_path))
    asyncio.run(run_events.append(
        "research.task.started",
        stage="researching",
        payload={
            "task_id": task_id,
            "wave_id": "wave-1",
            "title": "验证活动接口",
            "status": "running",
            "phase": "researching",
            "mode": "sync",
        },
        dedupe_key="task:started",
    ))
    activity = TaskActivityStore(run_id, task_id, runs_dir=str(tmp_path))
    asyncio.run(activity.append(
        "task.completed",
        kind="lifecycle",
        phase="terminal",
        status="success",
        title="Subagent 已完成",
        summary="任务安全完成。",
        iteration=1,
        duration_ms=12,
        payload={"mode": "sync", "wave_id": "wave-1"},
        dedupe_key="terminal",
    ))

    owner = _client("user-1")
    try:
        page = owner.get(f"/runs/{run_id}/tasks/{task_id}/activity")
        stream = owner.get(
            f"/runs/{run_id}/tasks/{task_id}/activity/stream?after=0"
        )
        stream_url = f"/runs/{run_id}/tasks/{task_id}/activity/stream"
        for cursor, status in [("bad", 400), ("-1", 400), ("99", 409)]:
            assert owner.get(stream_url, headers={"Last-Event-ID": cursor}).status_code == status
        assert owner.get(stream_url + "?after=99", headers={"Last-Event-ID": "1"}).text == ""
    finally:
        server.app.dependency_overrides.clear()
    assert page.status_code == 200
    assert page.json()["source"] == "native"
    assert page.json()["items"][0]["type"] == "task.completed"
    assert "dedupe_key" not in page.text
    assert stream.status_code == 200
    assert "event: task.completed" in stream.text

    other = _client("user-2")
    try:
        denied = other.get(f"/runs/{run_id}/tasks/{task_id}/activity")
        assert other.get(stream_url).status_code == 404
    finally:
        server.app.dependency_overrides.clear()
    assert denied.status_code == 404


def test_security_approvals_work_in_default_local_bypass_without_iam_db(
    tmp_path, monkeypatch
):
    run_id = "bypass-approval"
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LOCAL_DEV_AUTH_BYPASS", "true")
    monkeypatch.delenv("IAM_DATABASE_URL", raising=False)
    context = RunContextStore(run_id, runs_dir=str(tmp_path))
    context.initialize(
        "local-dev-user",
        {
            "configurable": {"runs_dir": str(tmp_path)},
            "metadata": {"run_id": run_id, "owner": "local-dev-user"},
        },
    )
    context._update_manifest(  # noqa: SLF001 - persisted fence fixture
        allow_fence_advance=True,
        fence_token=1,
        fence_owner_id="test-owner",
    )
    SecurityApprovalStore(run_id, runs_dir=str(tmp_path)).request(
        task_id="task-1",
        fence_token=1,
        kind="network",
        capability="tool.egress",
        target={"domain": "example.com", "port": 443},
        operation_id="operation-1",
        expires_at=time.time() + 60,
    )
    server._runs.clear()
    server.app.dependency_overrides.clear()
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get(f"/runs/{run_id}/security-approvals")

    assert response.status_code == 200
    assert len(response.json()["approvals"]) == 1


def test_legacy_run_without_public_event_log_returns_empty_approvals(
    tmp_path, monkeypatch
):
    run_id = "legacy-no-events"
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    context = RunContextStore(run_id, runs_dir=str(tmp_path))
    context.initialize(
        "user-1",
        {
            "configurable": {"runs_dir": str(tmp_path)},
            "metadata": {"run_id": run_id, "owner": "user-1"},
        },
    )
    server._runs.clear()
    server.app.dependency_overrides[get_current_user] = lambda: research_principal(
        "user-1"
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        response = client.get(f"/runs/{run_id}")
    finally:
        server.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["pending_security_approvals"] == []


def test_terminal_run_does_not_rehydrate_pending_security_approvals(
    tmp_path, monkeypatch
):
    run_id = "terminal-approval"
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    context = RunContextStore(run_id, runs_dir=str(tmp_path))
    context.initialize(
        "user-1",
        {
            "configurable": {"runs_dir": str(tmp_path)},
            "metadata": {"run_id": run_id, "owner": "user-1"},
        },
    )
    context._update_manifest(  # noqa: SLF001 - persisted terminal fixture
        status="completed",
        result={"status": "success"},
        allow_fence_advance=True,
        fence_token=1,
        fence_owner_id="test-owner",
    )
    SecurityApprovalStore(run_id, runs_dir=str(tmp_path)).request(
        task_id="task-1",
        fence_token=1,
        kind="network",
        capability="tool.egress",
        target={"domain": "example.com", "port": 443},
        operation_id="operation-terminal",
        expires_at=time.time() + 60,
    )
    asyncio.run(
        RunEventStore(run_id, runs_dir=str(tmp_path)).append(
            "run.completed",
            stage="finalizing",
            payload={
                "status": "completed",
                "result_ref": f"/runs/{run_id}",
                "termination_reason": "completed",
                "result_status": "success",
                "permission_denial_count": 0,
            },
            dedupe_key="run:terminal",
        )
    )
    server._runs.clear()
    server.app.dependency_overrides[get_current_user] = lambda: research_principal(
        "user-1"
    )
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        response = client.get(f"/runs/{run_id}")
    finally:
        server.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["pending_security_approvals"] == []
    assert SecurityApprovalStore(run_id, runs_dir=str(tmp_path)).list(
        status="pending"
    )[1] == []


def _set_litellm_run_key_env(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-proxy:4000/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test-master")
    monkeypatch.setenv("LITELLM_RUN_TEAM_ID", "insightforge-runs")
    monkeypatch.setenv(
        "LITELLM_RUN_KEY_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
    )
    monkeypatch.setenv("LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD", "1000000")
    monkeypatch.setenv("LITELLM_RUN_BUDGET_MAX_MICRO_USD", "2000000")


def _install_catalog_stub(monkeypatch, loader) -> None:
    class StubCatalogClient:
        def __init__(self, *, base_url: str, api_key: str) -> None:
            self.base_url = base_url
            self.api_key = api_key

        async def load(self):
            return await loader()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(configuration_routes, "LiteLLMModelCatalogClient", StubCatalogClient)


def test_models_returns_gateway_catalog_and_role_aliases(monkeypatch):
    monkeypatch.setattr(configuration_routes, "_model_catalog_cache", None)
    monkeypatch.setenv("MODEL_BACKEND", "litellm")
    monkeypatch.setenv("RESEARCH_MODEL", "if-research-v1")
    _set_litellm_run_key_env(monkeypatch)

    async def loader():
        return {
            "if-research-v1": ModelCatalogEntry(
                model_name="if-research-v1",
                base_model="deepseek/deepseek-v4-flash",
                context_window=128_000,
                max_output_tokens=8_192,
                input_cost_per_token=0.000001,
                output_cost_per_token=0.000004,
            ),
            "if-fallback-v1": ModelCatalogEntry(
                model_name="if-fallback-v1",
                base_model="deepseek/deepseek-v4-flash",
                context_window=128_000,
                max_output_tokens=8_192,
                input_cost_per_token=0.000001,
                output_cost_per_token=0.000004,
            ),
        }

    _install_catalog_stub(monkeypatch, loader)

    client = _client("user-1")
    try:
        payload = client.get("/models").json()
    finally:
        server.app.dependency_overrides.clear()

    assert payload["backend"] == "litellm"
    assert payload["stale"] is False
    assert payload["error"] is None
    assert [item["name"] for item in payload["models"]] == [
        "if-fallback-v1",
        "if-research-v1",
    ]
    entry = payload["models"][1]
    assert entry["context_window"] == 128_000
    assert entry["output_cost_per_token"] == 0.000004
    assert payload["role_aliases"]["research_model"] == "if-research-v1"


def test_models_serves_stale_cache_then_empty_when_gateway_unavailable(monkeypatch):
    monkeypatch.setattr(configuration_routes, "_model_catalog_cache", None)
    monkeypatch.setenv("MODEL_BACKEND", "litellm")
    _set_litellm_run_key_env(monkeypatch)

    fresh = {
        "if-research-v1": ModelCatalogEntry(
            model_name="if-research-v1",
            context_window=64_000,
            max_output_tokens=8_192,
            input_cost_per_token=0.000001,
            output_cost_per_token=0.000004,
        )
    }
    state = {"catalog": fresh, "fail": False}

    async def loader():
        if state["fail"]:
            raise httpx.ConnectError("gateway down")
        return state["catalog"]

    _install_catalog_stub(monkeypatch, loader)

    client = _client("user-1")
    try:
        first = client.get("/models").json()
        assert first["stale"] is False
        assert [item["name"] for item in first["models"]] == ["if-research-v1"]

        # Age the snapshot past the TTL so the next request must hit the
        # gateway again; the failing loader then exercises the stale path.
        assert configuration_routes._model_catalog_cache is not None
        configuration_routes._model_catalog_cache["loaded_at"] -= 120
        state["fail"] = True
        second = client.get("/models").json()
        assert second["stale"] is True
        assert [item["name"] for item in second["models"]] == ["if-research-v1"]

        configuration_routes._model_catalog_cache = None
        cold = client.get("/models").json()
    finally:
        server.app.dependency_overrides.clear()
        configuration_routes._model_catalog_cache = None

    assert cold["stale"] is True
    assert cold["models"] == []
    assert cold["error"] == "gateway_unavailable"


def test_models_reports_legacy_backend_without_touching_gateway(monkeypatch):
    monkeypatch.setattr(configuration_routes, "_model_catalog_cache", None)
    monkeypatch.setenv("MODEL_BACKEND", "legacy")

    def boom():
        raise AssertionError("catalog client must not be constructed on legacy")

    _install_catalog_stub(monkeypatch, boom)

    client = _client("user-1")
    try:
        payload = client.get("/models").json()
    finally:
        server.app.dependency_overrides.clear()

    assert payload["backend"] == "legacy"
    assert payload["models"] == []
    assert payload["stale"] is False

"""Health, graceful shutdown, and startup recovery tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from open_deep_research import server
from open_deep_research.api import operations
from open_deep_research.api.projections import _stable_output


@pytest.fixture(autouse=True)
def _reset_server_lifecycle_state():
    server._lifecycle.shutting_down.clear()
    server._lifecycle.sse_shutdown.clear()
    yield
    server._lifecycle.shutting_down.clear()
    server._lifecycle.sse_shutdown.clear()


def test_healthz_is_public_and_dependency_free() -> None:
    client = TestClient(server.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_terminal_snapshot_uses_sql_before_executor_callback_finishes(tmp_path):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.api.native_runs import NativeRuns

    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    service = NativeRuns(store, None, None)
    consumer = asyncio.create_task(asyncio.Event().wait())
    service.tasks["terminal-race"] = consumer
    try:
        await store.create_run("owner", ResearchSnapshot(
            run_id="terminal-race", config_fingerprint="fixture", status="completed",
            final_report="# Completed report",
            report_product={"final_report": "# Completed report", "result": {"status": "success"}},
        ))
        snapshot = await service.snapshot("terminal-race", "owner")
        assert not consumer.done()
        assert snapshot["status"] == "completed"
        assert snapshot["output"]["markdown"] == "# Completed report"
        assert snapshot["output"]["status"] == "success"
    finally:
        await service.aclose()
        await store.aclose()


def test_report_review_output_projection_is_bounded_and_redacted() -> None:
    output = _stable_output(
        {
            "final_report": "# Final",
            "report_review": {
                "schema_version": "1.0",
                "status": "degraded",
                "decision": "revise",
                "attempt": 2,
                "issue_count": 1,
                "critical_issue_count": 0,
                "draft_sha256": "abc123",
                "reason": "MODEL_PRIVATE_REASON",
                "issues": [{"description": "PRIVATE_ISSUE"}],
                "citation_audit": [{"evidence_ids": ["EV-SECRET"]}],
                "provenance": {"prompt": "PRIVATE_PROMPT"},
                "dimensions": {
                    "coverage": 0.9,
                    "executive_readability": 0.6,
                    "private_numeric_field": 42,
                },
            },
            "result": {"status": "success"},
        }
    )

    assert output["report_review"] == {
        "schema_version": "1.0",
        "status": "degraded",
        "decision": "revise",
        "attempt": 2,
        "issue_count": 1,
        "critical_issue_count": 0,
        "draft_sha256": "abc123",
        "dimensions": {
            "coverage": 0.9,
            "executive_readability": 0.6,
        },
    }
    serialized = json.dumps(output, ensure_ascii=False)
    assert "PRIVATE" not in serialized
    assert "EV-SECRET" not in serialized


@pytest.mark.asyncio
async def test_readyz_reports_degraded_search_without_failing(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    monkeypatch.setattr(operations, "_probe_runs_directory", lambda _config: None)
    monkeypatch.setattr(
        operations,
        "get_iam_settings",
        lambda: SimpleNamespace(database_url=""),
    )

    report, ready = await server._operational_routes._readiness_report()

    assert ready is True
    assert report["status"] == "degraded"
    assert report["components"]["search"]["reason"] == "api_key_missing"


@pytest.mark.asyncio
async def test_readyz_fails_when_runs_directory_is_not_writable(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    monkeypatch.setattr(
        operations,
        "_probe_runs_directory",
        lambda _config: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        operations,
        "get_iam_settings",
        lambda: SimpleNamespace(database_url=""),
    )

    report, ready = await server._operational_routes._readiness_report()

    assert ready is False
    assert report["status"] == "failed"
    assert report["components"]["runs_dir"] == {
        "status": "failed",
        "error_type": "PermissionError",
    }


@pytest.mark.asyncio
async def test_readyz_immediately_fails_while_shutting_down(monkeypatch) -> None:
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    monkeypatch.setattr(operations, "_probe_runs_directory", lambda _config: None)
    monkeypatch.setattr(
        operations,
        "get_iam_settings",
        lambda: SimpleNamespace(database_url=""),
    )
    server._lifecycle.shutting_down.set()

    response = await server._operational_routes.readyz()

    assert response.status_code == 503
    assert json.loads(response.body)["components"]["server"]["reason"] == "shutting_down"

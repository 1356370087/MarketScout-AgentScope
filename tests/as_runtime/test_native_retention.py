"""Native deletion owns SQL, document references, publications and local artifacts."""

import time
from dataclasses import replace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, update

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.research_router import build_research_router
from open_deep_research.configuration import Configuration
from open_deep_research.observability.tracing import SQLiteTraceStore
from open_deep_research.report.models import PublisherTheme
from open_deep_research.report.publication_store import PublicationJobStore
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def retention(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "false")
    monkeypatch.setenv("TRACE_STORE_PATH", str(tmp_path / "traces.db"))
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "recovery.db").as_posix())
    await store.create_tables()
    service = NativeRuns(store, None, None, runs_dir=tmp_path / "runs")
    app = FastAPI()
    app.include_router(build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal("alice")

    async def seed(run_id="run", status="completed", age=0, owner="alice", size=32):
        config = RunConfig.compile({"configurable": {}})
        state = await store.create_from_config(owner, run_id, config,
            application={"configuration": config.snapshot(), "created_at": time.time() - 100 * 86400})
        state.status = status
        state.final_report = "Completed research" if status == "completed" else ""
        lease = await store.acquire(run_id, owner)
        try:
            await store.save(lease, state)
            if age:
                async with store.transaction(lease) as (conn, row):
                    records = (await conn.execute(select(store.outbox).where(store.outbox.c.run_id == run_id))).mappings().all()
                    for record in records:
                        await conn.execute(update(store.outbox).where(store.outbox.c.run_id == run_id,
                            store.outbox.c.event_id == record["event_id"]).values(
                                payload={**record["payload"], "timestamp": time.time() - age}))
        finally:
            await store.release(lease)
        directory = service.runs_dir / run_id
        directory.mkdir(parents=True)
        (directory / "artifact").write_bytes(b"x" * size)
        return directory

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield service, client, app, seed
    await service.aclose()
    await store.aclose()


async def test_owned_deletion_dry_run_and_complete_receipt_cleanup(retention, monkeypatch, tmp_path):
    service, client, app, seed = retention
    directory = await seed()
    traces = SQLiteTraceStore(str(tmp_path / "traces.db"))
    traces.start_run("run", "alice", {})
    traces.finish_run("run", "completed")
    app.dependency_overrides[get_current_principal] = lambda: research_principal("bob")
    assert (await client.delete("/runs/run?force=true")).status_code == 404
    app.dependency_overrides[get_current_principal] = lambda: research_principal("alice")
    preview = await client.delete("/runs/run?dry_run=true")
    assert preview.status_code == 200 and preview.json()["status"] == "would_delete"
    assert directory.exists()
    response = await client.delete("/runs/run")
    assert response.status_code == 200, response.text
    assert response.json()["trace_rows_deleted"] > 0
    assert not directory.exists()
    assert traces.get_run("run") is None
    async with service.store.engine.connect() as conn:
        for table in service.store.meta.sorted_tables:
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
    assert (await client.get("/runs/run")).status_code == 404


async def test_force_cancels_active_run_and_admin_can_delete_other_owner(retention):
    service, client, app, seed = retention
    await seed(status="running")
    assert (await client.delete("/runs/run")).status_code == 409
    preview = await client.delete("/runs/run?force=true&dry_run=true")
    assert preview.status_code == 200
    assert (await service.store.load("run", "alice"))[0].status == "running"
    assert (await client.delete("/runs/run?force=true")).status_code == 200
    await seed("other", owner="bob")
    app.dependency_overrides[get_current_principal] = lambda: replace(research_principal("admin"), roles=frozenset({"admin"}))
    assert (await client.delete("/runs/other")).status_code == 200


async def test_deletion_respects_live_lease_and_queued_publication(retention):
    service, client, _, seed = retention
    directory = await seed()
    lease = await service.store.acquire("run", "alice")
    assert (await client.delete("/runs/run")).status_code == 409
    await service.store.release(lease)
    publications = PublicationJobStore("run", runs_dir=service.runs_dir)
    publications.enqueue(report_sha256="a" * 64, publication_format="markdown", theme=PublisherTheme(), max_attempts=1)
    response = await client.delete("/runs/run?force=true")
    assert response.status_code == 409
    assert response.json()["detail"] == "publication_in_progress"
    assert directory.exists()
    assert (await service.store.load("run", "alice"))[0].status == "completed"


async def test_external_cleanup_failure_preserves_run_for_retry(retention, monkeypatch):
    service, client, _, seed = retention
    directory = await seed()
    original = service.retention._release_external

    async def unavailable(*args):
        raise HTTPException(409, "run_key_cleanup_pending")

    monkeypatch.setattr(service.retention, "_release_external", unavailable)
    assert (await client.delete("/runs/run")).status_code == 409
    assert directory.exists()
    assert (await service.store.load("run", "alice"))[0].status == "completed"
    monkeypatch.setattr(service.retention, "_release_external", original)
    assert (await client.delete("/runs/run")).status_code == 200


async def test_retention_uses_completion_age_preserves_active_and_legacy(retention, tmp_path):
    service, _, _, seed = retention
    old = await seed("old", age=3 * 86400)
    recent = await seed("recent")
    active = await seed("active", status="running", age=3 * 86400)
    archive = service.runs_dir / "legacy/context"
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text('{"read_only": true}', encoding="utf-8")
    cfg = Configuration(run_retention_days=1, trace_retention_days=0,
                        runs_dir_max_bytes=0, trace_store_path=str(tmp_path / "traces.db"))
    result = await service.retention.sweep(cfg)
    assert result["deleted_by_age"] == 1
    assert not old.exists()
    assert recent.exists() and active.exists() and archive.exists()


async def test_quota_reclaims_oldest_completed_artifact_and_reports_unreclaimable_usage(retention, tmp_path):
    service, _, _, seed = retention
    old = await seed("old", age=20, size=100_000)
    recent = await seed("recent", age=10, size=100_000)
    cfg = Configuration(run_retention_days=0, trace_retention_days=0,
                        runs_dir_max_bytes=150_000, trace_store_path=str(tmp_path / "traces.db"))
    result = await service.retention.sweep(cfg)
    assert result["deleted_by_quota"] == 1
    assert not old.exists() and recent.exists() and not result["quota_exceeded"]
    active = await seed("active", status="running", size=200_000)
    result = await service.retention.sweep(cfg)
    assert result["quota_exceeded"] and active.exists()


async def test_trace_retention_only_touches_native_terminal_runs(retention, tmp_path):
    service, _, _, seed = retention
    directory = await seed("native", age=3 * 86400)
    traces = SQLiteTraceStore(str(tmp_path / "traces.db"))
    for name in ("native", "historical"):
        traces.start_run(name, "alice", {})
        traces.finish_run(name, "completed")
    cfg = Configuration(run_retention_days=0, trace_retention_days=1,
                        runs_dir_max_bytes=0, trace_store_path=str(tmp_path / "traces.db"))
    result = await service.retention.sweep(cfg)
    assert result["trace_runs_deleted"] == 1
    assert directory.exists() and traces.get_run("native") is None
    assert traces.get_run("historical") is not None

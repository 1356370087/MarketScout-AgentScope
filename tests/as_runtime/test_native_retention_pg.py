"""Real PostgreSQL deletion of domain teams and framework-owned sessions."""

# ruff: noqa: F811 -- imported pytest fixtures

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.configuration import Configuration
from tests.as_runtime.test_team import env, task  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_native_purge_removes_team_sessions_and_only_its_sql_rows(env, tmp_path, monkeypatch):
    build, store, storage, pool = env
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "false")
    team = await build()
    other = await build(user="other")
    member = await team.add_member("worker-a", "worker-a", "research")
    await task(team, "task-a")
    await team.command("claim-a", "task_claim", {"task_id": "task-a", "owner": member})
    await team.reconcile()
    worker_id = team.identity("member", member)
    assert await storage.get_agent("owner", worker_id) is not None
    state, _ = await store.load(team.lease.run_id, "owner")
    state.status = "completed"
    await store.save(team.lease, state)
    await store.release(team.lease)
    service = NativeRuns(store, SimpleNamespace(runtime=SimpleNamespace(storage=storage)), None, runs_dir=tmp_path)
    run_dir = tmp_path / team.lease.run_id
    run_dir.mkdir()
    (run_dir / "artifact").write_text("research", encoding="utf-8")
    cfg = Configuration(trace_store_path=str(tmp_path / "traces.db"))
    assert [row["run_id"] for row in await service.retention.candidates()] == [team.lease.run_id]
    result = await service.retention.purge(team.lease.run_id, "owner", cfg, reason="manual")
    assert result["status"] == "deleted" and not run_dir.exists()
    assert await storage.get_team("owner", team.team_id) is None
    assert await storage.get_agent("owner", team.leader_agent_id) is None
    assert await storage.get_agent("owner", worker_id) is None
    assert await storage.get_team("other", other.team_id) is not None
    assert await storage.get_agent("other", other.leader_agent_id) is not None
    async with store.engine.connect() as conn:
        for table in store.meta.sorted_tables:
            assert await conn.scalar(select(func.count()).select_from(table).where(table.c.run_id == team.lease.run_id)) == 0
    async with pool.acquire() as conn:
        for table in ("research_teams", "research_team_members", "research_team_tasks",
                      "research_team_dependencies", "research_team_plans", "research_team_proposals",
                      "research_coordination_transactions", "research_coordination_events"):
            assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE run_id=$1", team.lease.run_id) == 0
        assert await conn.fetchval("SELECT count(*) FROM research_teams WHERE run_id=$1", other.lease.run_id) == 1
    await service.aclose()

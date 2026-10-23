"""The budget boundary precedes paid member planning and keeps SQL guards."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from team_worker_fixture import workers
from test_team import env, task
from test_team_worker import assignment

from open_deep_research.agentscope_runtime.team_worker import TaskBudgetStopped
from open_deep_research.tasks.team_protocol import MemberIdentity
from open_deep_research.tasks.team_service import task_view

pytestmark = pytest.mark.asyncio


async def test_worker_budget_stop_never_calls_a_planner_or_fabricates_a_handoff(env, tmp_path):
    build, store, _, pool = env
    team = await build()
    host, factory, marker = await workers(team, store, tmp_path)
    config = host.researcher.config_provider()
    config["metadata"]["run_config_schema_version"] = 21
    config["configurable"]["research_efficiency_mode"] = "bounded"
    item, coverage = assignment()
    await host.prepare(item, coverage)
    async with pool.acquire() as db:
        await db.execute("UPDATE research_team_tasks SET execution_mode='plan_approval',phase='planning' WHERE run_id=$1",
                         team.lease.run_id)
    await store.close_research(team.lease, "report_time_reserved")
    with pytest.raises(TaskBudgetStopped, match="report_time_reserved"):
        await host.execute(item.task_id)
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["status"] == "cancelled" and not row.get("result")
    assert task_view(row)["admission_reason"] == "report_time_reserved"
    assert not marker.exists()
    async with store.engine.connect() as conn:
        calls = (await conn.execute(select(store.ops.c.key).where(
            store.ops.c.run_id == team.lease.run_id, store.ops.c.kind.like("model:%"),
        ))).scalars().all()
    assert calls == []
    await host.aclose()


async def test_pending_budget_close_is_leader_owned_and_replayable(env, tmp_path):
    build, store, _, _ = env
    team = await build()
    host, _, _ = await workers(team, store, tmp_path)
    await task(team, "queued")
    member = await team.add_member("member", "worker", "research")
    with pytest.raises(PermissionError):
        await team.command("unauthorized-close", "research_budget_close", {"reason": "report_time_reserved"},
                           member=MemberIdentity(run_id=team.lease.run_id, member_id=member, name="worker"))
    first = await host.close_pending_for_budget("report_time_reserved")
    second = await host.close_pending_for_budget("report_time_reserved")
    assert first == second and first["cancelled_task_ids"] == ["queued"]
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["status"] == "cancelled" and row["budget_stop_reason"] == "report_time_reserved"
    await host.aclose()


async def test_report_time_decision_transaction_on_remote_postgres(env, monkeypatch):
    from test_execution_budget import test_clock_pause_and_decision_are_atomic_and_idempotent as verify_clock
    _, store, _, _ = env
    await verify_clock(store, monkeypatch, "approve", 600)

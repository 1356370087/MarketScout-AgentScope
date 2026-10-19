"""Task contracts and lifecycle regressions against the native team service."""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from test_agent_teams import claim, member, setup
from test_team import env, task
from open_deep_research.agentscope_runtime.teams_tools import TaskCreateInput, TaskUpdateInput, lead_tools
from open_deep_research.tasks.team_service import task_view

pytestmark = pytest.mark.asyncio


async def test_task_schema_requires_named_content_and_rejects_ignored_updates():
    schema = TaskCreateInput.model_json_schema()
    assert {"subject", "description"} <= set(schema["required"])
    assert "research_topic" not in schema["properties"]
    base = {"subject": "核实市场规模", "description": "核对指定年份和地区，附官方来源。",
            "requirement_ids": ["COV-1"], "owner": "", "blockedBy": []}
    item = TaskCreateInput.model_validate(base)
    assert item.research_topic == base["description"] and item.owner is None
    for patch in ({"subject": "  "}, {"description": ""}, {"research_topic": "另一目标"}):
        with pytest.raises(ValidationError):
            TaskCreateInput.model_validate({**base, **patch})
    legacy = TaskCreateInput(research_topic="旧研究目标", requirement_ids=["COV-1"], owner="", blockedBy=[])
    assert legacy.subject == legacy.description == "旧研究目标"
    for patch in ({"status": "completed"}, {"description": "  "}, {"subject": ""}, {"owner": "null"}):
        with pytest.raises(ValidationError):
            TaskUpdateInput(task_id="one", version=1, **patch)
    assert "owner" not in TaskUpdateInput(task_id="one", version=1).model_dump(exclude_unset=True)
    for owner in ("", None):
        assert TaskUpdateInput(task_id="one", version=1, owner=owner).model_dump(exclude_unset=True)["owner"] is None


async def test_task_list_only_reports_unresolved_blockers_but_detail_preserves_graph():
    row = {"task_id": "down", "subject": "汇总结果", "description": "完整说明", "blockedBy": ["accepted", "rejected"],
           "blocked_by": ["accepted", "rejected"], "unresolvedBlockedBy": ["rejected"], "blocks": ["report"]}
    assert task_view(row, summary=True)["blockedBy"] == ["rejected"]
    detail = task_view(row)
    assert detail["blockedBy"] == ["accepted", "rejected"]
    assert detail["blocks"] == ["report"] and detail["description"] == "完整说明"


async def test_combined_task_update_roundtrip_unassign_and_atomic_rollback(env):
    team, pool = await setup(env)
    who, _ = await member(team, pool, "reviewer")
    await task(team, "up")
    await task(team, "down")
    update = next(tool for tool in lead_tools(SimpleNamespace(team=team), None) if tool.name == "TaskUpdate")
    result = await update.call(TaskUpdateInput(task_id="down", version=1, owner="reviewer", subject="核实数据",
        description="完整的新说明", activeForm="正在核实数据", metadata={"keep": 1, "remove": 2}, addBlockedBy=["up"]),
        SimpleNamespace(tool_call_id="combined"), None)
    rows = {r["task_id"]: r for r in await team.service.tasks(team.lease.run_id)}
    row = rows["down"]
    assert row["owner"] == who.member_id and row["subject"] == row["display_title"] == "核实数据"
    assert row["description"] == row["research_topic"] == "完整的新说明"
    assert row["activeForm"] == "正在核实数据" and row["metadata"] == {"keep": 1, "remove": 2}
    assert row["blockedBy"] == ["up"] and rows["up"]["blocks"] == ["down"]
    assert result.output["version"] == row["version"]
    # A cyclic graph edit must roll back content and ownership in the same command.
    with pytest.raises(ValueError, match="cycle"):
        await team.command("cycle", "task_update", {"task_id": "down", "version": row["version"],
            "owner": None, "subject": "must roll back", "addBlocks": ["up"]})
    assert {r["task_id"]: r for r in await team.service.tasks(team.lease.run_id)}["down"] == row
    result = await update.call(TaskUpdateInput(task_id="down", version=row["version"], owner="", metadata={"remove": None}),
        SimpleNamespace(tool_call_id="unassign"), None)
    row = {r["task_id"]: r for r in await team.service.tasks(team.lease.run_id)}["down"]
    assert row["owner"] is None and row["metadata"] == {"keep": 1}
    assert row["subject"] == "核实数据" and row["version"] == result.output["version"]
    async with pool.acquire() as db:
        # A checkpoint can retain old metadata; readers use the authoritative columns.
        await db.execute("UPDATE research_team_tasks SET snapshot=snapshot || $3::jsonb WHERE run_id=$1 AND task_id=$2",
                         team.lease.run_id, "down", json.dumps({"display_title": "old", "research_topic": "old"}))
    assert {r["task_id"]: r for r in await team.service.tasks(team.lease.run_id)}["down"]["description"] == "完整的新说明"


async def test_task_metadata_migration_backfills_existing_snapshots(env):
    team, pool = await setup(env)
    await task(team, "before-migration")
    async with pool.acquire() as db:
        await db.execute("ALTER TABLE research_team_tasks DROP COLUMN subject, DROP COLUMN description")
        await db.execute(importlib.import_module("security.rbac.migrations.versions.0022_team_task_fields").DDL)
    row = (await team.service.tasks(team.lease.run_id))[0]
    assert row["subject"] == row["description"] == "question before-migration"
    assert row["status"] == "pending" and row["version"] == 1


async def test_task_session_preserves_discussion_and_shutdown_request(env):
    team, pool = await setup(env)
    who, token = await member(team, pool, "analyst")
    async with pool.acquire() as db:
        await db.execute("UPDATE research_team_members SET session=session || $3::jsonb WHERE run_id=$1 AND member_id=$2",
                         team.lease.run_id, who.member_id, json.dumps({"discussion_summary": "保留已有发现", "discussion": ["保留已有发现"]}))
    await task(team, "one")
    assert (await claim(team, who, token, "one"))["claimed"]
    request = await team.send_message(team.leader, "stop-member", who.member_id, {"type": "shutdown_request"})
    await team.task_session("one", who.member_id)
    async with pool.acquire() as db:
        session = json.loads(await db.fetchval("SELECT session FROM research_team_members WHERE run_id=$1 AND member_id=$2", team.lease.run_id, who.member_id))
    assert session["discussion_summary"] == "保留已有发现"
    assert session["discussion"] == ["保留已有发现"] and session["shutdown_request"] == request["event_id"]
    assert session["task_id"] == "one"


async def test_busy_member_declines_shutdown_and_can_close_after_task_stop(env, tmp_path):
    from team_worker_fixture import workers
    from test_team_worker import assignment
    from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers

    team, pool = await setup(env)
    base, factory, _ = await workers(team, env[1], tmp_path, slow=True)
    host = TeamsWorkers(team, base.recovery, base.researcher, base.quality, tmp_path, ttl=2)
    member_id = await team.add_member("worker", "worker", "核实所有数据年份", max_members=2)
    item, contract = assignment()
    await host.prepare(item, contract, owner=member_id)
    running = asyncio.create_task(host.run_member(member_id))
    try:
        await asyncio.wait_for(factory.entered.wait(), 10)
        sent = await team.send_message(team.leader, "busy-stop", member_id, {"type": "shutdown_request"})
        async with asyncio.timeout(5):
            while True:
                replies = [e for e in await team.pending("lead") if e.payload.get("message", {}).get("request_id") == sent["event_id"]]
                if replies:
                    break
                await asyncio.sleep(0.05)
        assert replies[0].payload["message"]["approve"] is False
        assert next(m for m in await team.members() if m["member_id"] == member_id)["status"] == "executing"
        assert (await team.service.tasks(team.lease.run_id))[0]["status"] == "running"
        await host.stop(item.task_id)
        async with asyncio.timeout(5):
            while next(m for m in await team.members() if m["member_id"] == member_id)["status"] != "idle":
                await asyncio.sleep(0.05)
        await team.send_message(team.leader, "idle-stop", member_id, {"type": "shutdown_request"})
        await asyncio.wait_for(running, 5)
        assert next(m for m in await team.members() if m["member_id"] == member_id)["status"] == "closed"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await host.aclose()


async def test_shutdown_recovers_applied_request_and_releases_pending_assignment(env, tmp_path):
    from team_worker_fixture import workers
    from test_team_worker import assignment
    from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers

    team, pool = await setup(env)
    base, _, _ = await workers(team, env[1], tmp_path)
    host = TeamsWorkers(team, base.recovery, base.researcher, base.quality, tmp_path)
    member_id = await team.add_member("worker", "worker", "research", max_members=2)
    item, contract = assignment()
    await host.prepare(item, contract, owner=member_id)
    request = await team.send_message(team.leader, "stop", member_id, {"type": "shutdown_request"})

    async def old_consumer(db, event):
        pass

    await team.apply_input(member_id, request["event_id"], old_consumer)
    try:
        await asyncio.wait_for(host.run_member(member_id), 5)
        assert next(m for m in await team.members() if m["member_id"] == member_id)["status"] == "closed"
        row = (await team.service.tasks(team.lease.run_id))[0]
        assert row["owner"] is None and row["status"] == "pending"
        replies = [e for e in await team.pending("lead") if isinstance(e.payload.get("message"), dict)
                   and e.payload["message"].get("request_id") == request["event_id"]]
        assert len(replies) == 1 and replies[0].payload["message"]["approve"] is True
    finally:
        await host.aclose()

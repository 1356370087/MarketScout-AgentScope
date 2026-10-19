"""Real remote MQ delivery with native, member-owned tasks and SQL recovery."""
import asyncio
import os
from types import SimpleNamespace

import pytest
from team_worker_fixture import workers
from test_agent_teams import setup
from test_team import env
from test_team_worker import assignment

from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers
from open_deep_research.tasks.team_delivery import TeamDelivery

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv("AS_TEST_ROCKETMQ") != "1", reason="requires explicit remote RocketMQ acceptance endpoint")]


async def test_native_member_reuse_dependencies_messages_and_shutdown_over_mq(env, tmp_path):
    team, pool = await setup(env)
    settings = SimpleNamespace(rocketmq_endpoint=os.environ["AS_TEST_ROCKETMQ_ENDPOINT"],
        rocketmq_topic_prefix=os.environ["AS_TEST_ROCKETMQ_PREFIX"], rocketmq_access_key="", rocketmq_secret_key="", rocketmq_tls=False)
    relay = TeamDelivery(pool, settings)
    team.transport.reliable = True
    base, factory, marker = await workers(team, env[1], tmp_path)
    host = TeamsWorkers(team, base.recovery, base.researcher, base.quality, tmp_path)
    member_id = await team.add_member("worker", "source-reviewer", "核实所有来源的适用年份", max_members=2)
    first, contract = assignment()
    second = first.model_copy(update={"task_id": "second"})
    running = None
    try:
        await relay.start()
        await host.prepare(first, contract, owner=member_id)
        await host.prepare(second, contract, owner=member_id, blocked_by=[first.task_id])
        running = asyncio.create_task(host.run_member(member_id))
        async with asyncio.timeout(60):
            while not all(row["status"] == "completed" for row in await team.service.tasks(team.lease.run_id)):
                if running.done():
                    await running
                await asyncio.sleep(0.1)
        rows = await team.service.tasks(team.lease.run_id)
        assert all(row["admission_status"] in {"accepted", "accepted_with_caveats"} for row in rows)
        assert all(not row["unresolvedBlockedBy"] for row in rows)
        assert len(marker.read_text(encoding="utf-8").splitlines()) == 2
        assert any("核实所有来源的适用年份" in message.get_text_content()
                   for model in factory.instances for call in model.calls for message in call["messages"])
        async with asyncio.timeout(30):
            while len([event for event in await team.pending("lead") if event.type == "send_message"]) < 2:
                await asyncio.sleep(0.1)
        await host.finish_team()
        await asyncio.wait_for(running, 5)
        assert all(member["status"] == "closed" for member in await team.members())
        async with pool.acquire() as db:
            assert await db.fetchval("SELECT execution_epoch FROM research_team_members WHERE run_id=$1 AND member_id=$2", team.lease.run_id, member_id) == 1
            assert await db.fetchval("SELECT count(*) FROM research_coordination_receipts r JOIN research_coordination_events e USING(event_id) WHERE e.run_id=$1 AND r.applied", team.lease.run_id) > 0
    finally:
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await host.aclose()
        await relay.aclose()

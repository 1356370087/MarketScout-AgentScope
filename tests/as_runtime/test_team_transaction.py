"""Confirm deadlock rollback and exactly-once outcomes against remote PostgreSQL."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_team import env, task

from open_deep_research.tasks.team_protocol import TeamEvent


@pytest.mark.asyncio
async def test_deadlocked_team_transaction_retries_without_duplicate_events(env):
    team = await env[0]()
    pool = env[3]
    members = [await team.add_member("add:" + name, name, "research") for name in ("one", "two")]
    ready = [asyncio.Event(), asyncio.Event()]
    attempts = [0, 0]

    async def execute(index):
        event = TeamEvent(run_id=team.lease.run_id, operation_id=f"deadlock:{index}",
            sender="lead", recipients=["lead"], type="message", payload={}, fence_token=team.lease.fence)
        await team.transport.store.prepare(event)

        async def mutate(db):
            attempts[index] += 1
            await db.execute("UPDATE research_team_members SET version=version+1 WHERE run_id=$1 AND member_id=$2",
                             team.lease.run_id, members[index])
            if attempts[index] == 1:
                ready[index].set()
                await ready[1 - index].wait()
            await db.execute("UPDATE research_team_members SET version=version+1 WHERE run_id=$1 AND member_id=$2",
                             team.lease.run_id, members[1 - index])
            return {"index": index}

        result = await team.transport.store.commit(event, mutate, durable_recipients=True, outbox=True)
        # A committed replay cannot execute the mutation again.
        assert await team.transport.store.commit(event, mutate) == result
        return event

    async with pool.acquire() as db:
        versions = {r['member_id']: r['version'] for r in await db.fetch(
            "SELECT member_id,version FROM research_team_members WHERE run_id=$1 AND member_id=ANY($2::text[])",
            team.lease.run_id, members)}
    events = await asyncio.wait_for(asyncio.gather(execute(0), execute(1)), 15)
    assert sorted(attempts) == [1, 2]
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT member_id,version FROM research_team_members WHERE run_id=$1 AND member_id=ANY($2::text[])",
                              team.lease.run_id, members)
        assert all(row['version'] == versions[row['member_id']] + 2 for row in rows)
        for event in events:
            assert await db.fetchval("SELECT count(*) FROM research_coordination_events WHERE event_id=$1", event.event_id) == 1
            assert await db.fetchval("SELECT count(*) FROM research_coordination_receipts WHERE event_id=$1", event.event_id) == 1
            assert await db.fetchval("SELECT count(*) FROM research_coordination_outbox WHERE event_id=$1", event.event_id) == 1
            row = await db.fetchrow("SELECT state,result FROM research_coordination_transactions WHERE event_id=$1", event.event_id)
            assert row['state'] == 'COMMITTED' and 'index' in json.loads(row['result'])


@pytest.mark.asyncio
async def test_worker_guard_and_plan_update_do_not_form_a_member_task_lock_cycle(env):
    from open_deep_research.agentscope_runtime.teams_worker import MemberLoop

    team, pool = await env[0](), env[3]
    member = await team.add_member("add:worker", "worker", "research")
    await task(team, "work")
    worker = MemberLoop(SimpleNamespace(team=team), member)
    await worker.acquire()
    task_locked, second_guarded = asyncio.Event(), asyncio.Event()

    async def plan_update():
        async with pool.acquire() as db, db.transaction():
            await worker.guard(db)
            await db.execute("UPDATE research_team_tasks SET version=version+1 WHERE run_id=$1 AND task_id='work'",team.lease.run_id)
            task_locked.set()
            # With a shared run guard, the peer can hold a member SHARE lock
            # while waiting for this task, preventing this transaction's upgrade.
            try:
                await asyncio.wait_for(second_guarded.wait(),1)
            except TimeoutError:
                pass
            await db.execute("UPDATE research_team_members SET status='executing' WHERE run_id=$1 AND member_id=$2",team.lease.run_id,member)

    async def worker_checkpoint():
        await task_locked.wait()
        async with pool.acquire() as db, db.transaction():
            await worker.guard(db)
            second_guarded.set()
            await db.execute("UPDATE research_team_tasks SET version=version+1 WHERE run_id=$1 AND task_id='work'",team.lease.run_id)

    attempts = [asyncio.create_task(plan_update()),asyncio.create_task(worker_checkpoint())]
    try:
        await asyncio.wait_for(asyncio.gather(*attempts),15)
    finally:
        for attempt in attempts:
            attempt.cancel()
        await asyncio.gather(*attempts,return_exceptions=True)
    async with pool.acquire() as db:
        assert await db.fetchval("SELECT version FROM research_team_tasks WHERE run_id=$1 AND task_id='work'",team.lease.run_id) == 3

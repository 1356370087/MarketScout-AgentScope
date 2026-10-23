"""Retry only confirmed PostgreSQL transaction rollbacks, preserving receipts."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import asyncpg
import pytest

from open_deep_research.tasks.team_protocol import TeamEvent
from open_deep_research.tasks.team_store import TeamStore


class Pool:
    def __init__(self):
        self.rollbacks = 0
        self.commits = 0
        self.db = AsyncMock()
        self.db.fetchval.return_value = "event"
        self.db.transaction = self.transaction

    @asynccontextmanager
    async def acquire(self):
        yield self.db

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


def event():
    return TeamEvent(run_id="run", operation_id="operation", sender="lead", recipients=["member"],
                     type="message", payload={}, fence_token=1)


@pytest.mark.asyncio
async def test_rolled_back_deadlock_rechecks_mutation_before_committing_receipt():
    pool = Pool()
    mutate = AsyncMock(side_effect=[asyncpg.DeadlockDetectedError(), {"task_id": "task"}])
    result = await TeamStore(pool).commit(event(), mutate, durable_recipients=True, outbox=True)
    assert result == {"task_id": "task"}
    assert mutate.await_count == 2 and pool.rollbacks == pool.commits == 1
    sql = [call.args[0] for call in pool.db.execute.await_args_list]
    assert sum("INSERT INTO research_coordination_events" in value for value in sql) == 1
    assert sum("INSERT INTO research_coordination_outbox" in value for value in sql) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,attempts", [
    (asyncpg.DeadlockDetectedError(), 3), (ConnectionError("unknown commit"), 1),
    (PermissionError("ownership changed"), 1), (asyncio.CancelledError(), 1),
])
async def test_retry_is_bounded_and_never_repeats_other_failures(error, attempts):
    pool = Pool()
    mutate = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await TeamStore(pool).commit(event(), mutate)
    assert mutate.await_count == pool.rollbacks == attempts
    assert pool.commits == 0

"""T047: independent SQL clients share reservations across takeover and correction."""

import asyncio

import pytest
from test_recovery import create

from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryStore,
    UnknownOperation,
)
from open_deep_research.budgets import BudgetDimension, BudgetExhausted


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [item.value for item in BudgetDimension])
async def test_cross_client_budget_survives_unknown_result_and_takeover(pg_url, dimension):
    leader, worker = RecoveryStore(pg_url), RecoveryStore(pg_url)
    try:
        await leader.create_tables()
        state, lease = await create(leader, limits={dimension: 10})
        results = await asyncio.gather(*(
            client.begin_operation(lease, key, "tool", {}, reserve={dimension: 6})
            for client, key in ((leader, "leader"), (worker, "worker"))
        ), return_exceptions=True)
        assert sum(isinstance(item, BudgetExhausted) for item in results) == 1
        key = ("leader", "worker")[next(i for i, item in enumerate(results) if isinstance(item, dict))]
        await leader.release(lease)
        successor = await worker.acquire(state.run_id, "owner")
        with pytest.raises(FenceLost):
            await leader.commit_operation(lease, key, {"stale": True})
        with pytest.raises(UnknownOperation):
            await worker.begin_operation(successor, key, "tool", {})
        with pytest.raises(BudgetExhausted):
            await worker.begin_operation(successor, "next", "tool", {}, reserve={dimension: 5})
        await worker.resolve_operation(successor, key, not_executed=True)
        await worker.begin_operation(successor, "next", "tool", {}, reserve={dimension: 10})
        await worker.commit_operation(successor, "next", {"ok": True}, actual={dimension: 7})
        await leader.commit_operation(successor, "next", {"ok": True}, actual={dimension: 7})
        budget = await leader.budget(state.run_id, "owner")
        assert budget["used"] == {dimension: 7}
        assert budget["reserved"] == {dimension: 0}
        await worker.release(successor)
    finally:
        await leader.aclose()
        await worker.aclose()

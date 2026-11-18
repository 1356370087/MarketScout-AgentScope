import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from open_deep_research.intelligence import budget
from open_deep_research.intelligence.coordinator import Coordinator
from open_deep_research.intelligence.contracts import ItemAction
from open_deep_research.intelligence.router import item_action
from open_deep_research.intelligence.store import record
from open_deep_research.api.native_runs import idempotent_run_id


def run_identity(domain, item_id, number=1):
    return idempotent_run_id(domain.actor, f"intelligence:{item_id}:{number}")

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def queued(domain):
    identity = str(uuid4())
    await domain.pool.execute("""INSERT INTO intelligence_items(id,plan_id,authorization_id,owner_id,question,status,manual)
        VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'Verify pricing','queued',true)""",
        identity, domain.plan["id"], domain.plan["authorization_id"], domain.actor)
    return identity


async def test_same_budget_key_concurrently_reserves_once(domain):
    key = "same:" + str(uuid4())
    async def reserve():
        async with domain.pool.acquire() as c, c.transaction():
            return await budget.reserve(c, domain.plan, key, 3_000_000)
    rows = await asyncio.gather(reserve(), reserve())
    assert rows[0]["id"] == rows[1]["id"]
    assert await domain.pool.fetchval("SELECT reserved FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"]) == 3_000_000


async def test_create_response_loss_reuses_frozen_request_and_identity(domain):
    identity = await queued(domain)
    calls, logical = [], {}
    async def create(request, **kwargs):
        calls.append((request.model_dump(mode="json"), kwargs))
        key = (kwargs["item_id"], kwargs["execution_number"])
        if key not in logical:
            logical[key] = run_identity(domain, kwargs["item_id"], kwargs["execution_number"])
            raise ConnectionError("response lost after durable creation")
        return logical[key]
    coordinator = Coordinator(SimpleNamespace(create_automated=create), domain.store)
    await coordinator.dispatch()
    item, _ = await domain.store.item(identity)
    assert item["status"] == "blocked" and item["run_request"]
    assert "response lost after durable creation" not in item["reason"]
    await item_action(identity, ItemAction(expected_version=item["version"], command_id="retry", action="retry"), domain.principal)
    await coordinator.dispatch()
    item, _ = await domain.store.item(identity)
    assert item["run_id"] == run_identity(domain, identity) and len(logical) == 1
    assert calls[0] == calls[1]
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_item_runs WHERE item_id=$1::uuid", identity) == 1


async def test_cancel_while_run_creation_is_in_flight_cancels_created_run(domain):
    identity = await queued(domain)
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    async def create(*args, **kwargs):
        entered.set()
        await release.wait()
        return run_identity(domain, identity)
    async def cancel(run_id, owner, **kwargs):
        cancelled.append((run_id, owner))
    coordinator = Coordinator(SimpleNamespace(create_automated=create, cancel=cancel), domain.store)
    running = asyncio.create_task(coordinator.dispatch())
    await asyncio.wait_for(entered.wait(), timeout=10)
    item, _ = await domain.store.item(identity)
    await item_action(identity, ItemAction(expected_version=item["version"], command_id="cancel", action="cancel"), domain.principal)
    release.set()
    await running
    item, _ = await domain.store.item(identity)
    assert item["status"] == "cancelled" and cancelled == [(run_identity(domain, identity), domain.actor)]


async def test_supplement_keeps_prior_run_and_gets_new_execution_identity(domain):
    identity = await queued(domain)
    calls = []
    async def create(*args, **kwargs):
        calls.append(kwargs["execution_number"])
        return run_identity(domain, identity, kwargs["execution_number"])
    coordinator = Coordinator(SimpleNamespace(create_automated=create), domain.store)
    await coordinator.dispatch()
    await domain.pool.execute("UPDATE intelligence_items SET status='awaiting_review' WHERE id=$1::uuid", identity)
    await domain.pool.execute("UPDATE intelligence_item_runs SET result='{" + '"runtime_status":"completed"' + "}'::jsonb WHERE item_id=$1::uuid", identity)
    item, _ = await domain.store.item(identity)
    await item_action(identity, ItemAction(expected_version=item["version"], command_id="supplement", action="supplement", reason="Check changed package conditions"), domain.principal)
    await coordinator.dispatch()
    assert calls == [1, 2]
    assert await domain.pool.fetchval("SELECT count(*) FROM intelligence_item_runs WHERE item_id=$1::uuid", identity) == 2
    from open_deep_research.intelligence.router import item_runs, item_run
    from open_deep_research.intelligence.store import encode
    old_run = run_identity(domain, identity)
    await domain.pool.execute("UPDATE intelligence_item_runs SET result=$2::jsonb WHERE run_id=$1", old_run,
        encode({"report": "Published history. " * 100, "quality_status": "passed"}))
    listed = await item_runs(identity, domain.principal)
    old = next(r for r in listed["items"] if r["run_id"] == old_run)
    assert "report" not in old["result"] and len(old["result"]["summary"]) == 500
    assert len((await item_run(identity, old_run, domain.principal))["result"]["report"]) > 500


async def test_failed_current_run_keeps_budget_until_item_is_cancelled(domain):
    identity = await queued(domain)
    async def create(*args, **kwargs):
        return run_identity(domain, identity)
    async def load(*args):
        return SimpleNamespace(status="failed", final_report=""), 1
    async def usage(*args):
        return {"used": {"cost_micro_usd": 100_000, "model_calls": 1}, "reserved": {"cost_micro_usd": 0}}
    host = SimpleNamespace(create_automated=create, store=SimpleNamespace(load=load, budget=usage))
    coordinator = Coordinator(host, domain.store)
    await coordinator.dispatch()
    await domain.pool.execute("UPDATE intelligence_items SET status='blocked' WHERE id=$1::uuid", identity)
    await coordinator.reconcile_budgets()
    row = await domain.pool.fetchrow("SELECT reserved,settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"])
    assert tuple(row) == (1_000_000, 0)
    await domain.pool.execute("UPDATE intelligence_items SET status='cancelled' WHERE id=$1::uuid", identity)
    await coordinator.reconcile_budgets()
    row = await domain.pool.fetchrow("SELECT reserved,settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"])
    assert tuple(row) == (0, 100_000)


async def test_retry_reacquires_settled_envelope_and_defers_for_capacity(domain):
    identity = await queued(domain)
    attempts = []
    async def create(*args, **kwargs):
        return run_identity(domain, identity)
    async def resume(*args, automation=False):
        assert automation
        assert await domain.pool.fetchval("SELECT reserved FROM intelligence_budget_periods WHERE plan_id=$1::uuid", domain.plan["id"]) == 1_000_000
        attempts.append(True)
        return len(attempts) > 1
    coordinator = Coordinator(SimpleNamespace(create_automated=create, resume=resume), domain.store)
    await coordinator.dispatch()
    async with domain.pool.acquire() as c, c.transaction():
        await budget.settle(c, f"run:{identity}:1", 0)
    await domain.pool.execute("UPDATE intelligence_items SET status='blocked' WHERE id=$1::uuid", identity)
    item, _ = await domain.store.item(identity)
    await item_action(identity, ItemAction(expected_version=item["version"], command_id="retry-original", action="retry"), domain.principal)
    await coordinator.controls()
    assert (await domain.store.item(identity))[0]["status"] == "queued"
    await domain.pool.execute("UPDATE intelligence_jobs SET available_at=now() WHERE plan_id=$1::uuid AND kind='run_action'", domain.plan["id"])
    await coordinator.controls()
    assert (await domain.store.item(identity))[0]["status"] == "running"
    assert len(attempts) == 2


async def test_finished_or_superseded_item_cannot_execute_with_old_identity(domain):
    from open_deep_research.intelligence.authorization import check_work_item, check_budget_override
    from open_deep_research.intelligence.contracts import IntelligenceError
    identity = await queued(domain)
    async with domain.pool.acquire() as c, c.transaction():
        await budget.reserve(c, domain.plan, f"run:{identity}:1", 1_000_000)
    auth = {"item_id": identity, "plan_id": domain.plan["id"], "authorization_id": domain.plan["authorization_id"], "execution_number": 1}
    await check_work_item(auth)
    with pytest.raises(IntelligenceError, match="budget_change_requires"):
        await check_budget_override(auth, {"cost_micro_usd": 2_000_000})
    await domain.pool.execute("UPDATE intelligence_items SET status='completed' WHERE id=$1::uuid", identity)
    with pytest.raises(IntelligenceError, match="inactive"):
        await check_work_item(auth)


async def test_month_rollover_keeps_unknown_hold_and_settlement_in_its_period(domain, monkeypatch):
    monkeypatch.setattr(budget, "period", lambda plan: "2026-01")
    first, second = "january:" + str(uuid4()), "february:" + str(uuid4())
    async with domain.pool.acquire() as c, c.transaction():
        await budget.reserve(c, domain.plan, first, 3_000_000)
        await budget.settle(c, first, None)
    monkeypatch.setattr(budget, "period", lambda plan: "2026-02")
    async with domain.pool.acquire() as c, c.transaction():
        await budget.reserve(c, domain.plan, second, 3_000_000)
        await budget.settle(c, first, 100_000)
    rows = await domain.pool.fetch("SELECT period,reserved,settled FROM intelligence_budget_periods WHERE plan_id=$1::uuid ORDER BY period", domain.plan["id"])
    assert [tuple(row) for row in rows] == [("2026-01", 0, 100_000), ("2026-02", 3_000_000, 0)]

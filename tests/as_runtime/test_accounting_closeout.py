"""Accounting conflicts and external-result reconciliation crash boundaries."""

import json
import os
from pathlib import Path
from decimal import Decimal

import pytest
from test_recovery import create, store  # noqa: F401

from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict
from open_deep_research.agentscope_runtime.spend_reconciliation import reconcile_spend
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot

pytestmark = pytest.mark.asyncio


async def test_verified_result_resolution_rolls_back_entire_transaction(store, monkeypatch):
    state, lease = await create(store)
    await store.begin_operation(lease, "tool", "tool", {}, reserve={"tool_calls": 1})
    original = store._event

    async def fail(conn, row, kind, *args, **kwargs):
        if kind == "research.operation_resolved":
            raise RuntimeError("crash after receipt before resolution commit")
        return await original(conn, row, kind, *args, **kwargs)

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError, match="crash"):
        await store.resolve_operation(lease, "tool", result={"verified": True})
    assert (await store.operation_record(lease, "tool"))["state"] == "started"
    assert (await store.budget(state.run_id, "owner"))["reserved"] == {"tool_calls": 1}
    monkeypatch.setattr(store, "_event", original)
    for _ in range(2):
        await store.resolve_operation(lease, "tool", result={"verified": True})
    assert (await store.budget(state.run_id, "owner"))["used"] == {"tool_calls": 1}
    with pytest.raises(RecoveryConflict):
        await store.resolve_operation(lease, "tool", result={"verified": False})
    events = await store.events(state.run_id, "owner")
    assert sum(e["payload"]["type"] == "research.operation_resolved" for e in events) == 1


@pytest.mark.parametrize("bad", ["conflicting_id", "multiple_bills", "negative", "nan", "reported_conflict"])
async def test_bill_conflict_never_overwrites_or_blocks_other_operations(store, bad):
    state, lease = await create(store)
    for name in ("bad", "good"):
        await store.begin_operation(lease, "gateway:model:" + name, "gateway:model", {},
                                    reserve={"model_calls": 1, "input_tokens": 20})
        await store.commit_operation(lease, "gateway:model:" + name, {
            "outcome": {"usage": {"input_tokens": 7} if bad == "reported_conflict" and name == "bad" else {}}
        })
    bill = {"request_id": "bad", "request_tags": [f"run:{state.run_id}", "operation:bad"],
            "prompt_tokens": 4, "completion_tokens": 2, "spend": "0.000001"}
    logs = [bill]
    if bad == "conflicting_id":
        logs.append({**bill, "prompt_tokens": 5})
    elif bad == "multiple_bills":
        logs.append({**bill, "request_id": "another"})
    elif bad == "negative":
        bill["prompt_tokens"] = -1
    elif bad == "nan":
        bill["spend"] = "NaN"
    logs.append({**bill, "request_id": "good", "request_tags": [f"run:{state.run_id}", "operation:good"],
                 "prompt_tokens": 4, "spend": "0.000001"})
    for _ in range(2):
        result = await reconcile_spend(store, lease, logs)
        assert result == {"corrected": ["gateway:model:good"], "unresolved": ["gateway:model:bad"]}
    assert (await store.operation_record(lease, "gateway:model:bad"))["actual"]["input_tokens"] == 20
    assert (await store.operation_record(lease, "gateway:model:good"))["actual"]["cost_micro_usd"] == 1
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 2


async def test_historical_proxy_bills_reconcile_twice_without_new_calls(store):
    prefix = os.environ.get("ACCOUNTING_HISTORY_PREFIX")
    if not prefix:
        pytest.skip("requires sanitized historical bills and receipts")
    bills = [json.loads(x) for x in Path(prefix + "-bills.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    operations = [json.loads(x) for x in Path(prefix + "-ops.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    assert bills and operations
    run_id = next(t[4:] for t in bills[0]["request_tags"] if t.startswith("run:"))
    state = ResearchSnapshot(run_id=run_id, config_fingerprint="history-accounting")
    await store.create_run("owner", state)
    lease = await store.acquire(run_id, "owner", ttl=120)
    for op in operations:
        assert op["state"] == "committed"
        await store.begin_operation(lease, op["key"], "gateway:model", {}, reserve=op["reservation"])
        receipt = {k: v for k, v in op["result"].items() if k != "usage_correction"}
        await store.commit_operation(lease, op["key"], receipt)
    for _ in range(2):
        result = await reconcile_spend(store, lease, bills)
        assert not result["unresolved"]
        assert len(result["corrected"]) == len(operations)
    used = (await store.budget(run_id, "owner"))["used"]
    assert used["model_calls"] == len(operations)
    assert used["input_tokens"] == sum(b["prompt_tokens"] for b in bills)
    assert used["output_tokens"] == sum(b["completion_tokens"] for b in bills)
    assert used["cost_micro_usd"] == sum(int(Decimal(str(b["spend"])) * 1_000_000) for b in bills)
    for op in operations:
        assert (await store.operation_record(lease, op["key"]))["actual"] == op["actual"]


async def test_postgres_resolution_transaction_and_replay(pg_url, monkeypatch):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    database = RecoveryStore(pg_url)
    try:
        await database.create_tables()
        await test_verified_result_resolution_rolls_back_entire_transaction(database, monkeypatch)
    finally:
        await database.aclose()


async def test_bill_client_accepts_json_encoded_tags_without_matching_foreign_runs():
    from unittest.mock import AsyncMock
    from open_deep_research.models.spend import LiteLLMSpendClient
    client = object.__new__(LiteLLMSpendClient)
    client.spend_logs = AsyncMock(return_value=[
        {"request_tags": '["run:one", "operation:test"]'},
        {"request_tags": ["run:one"]},
        {"request_tags": '["run:other"]'},
        {"request_tags": 'broken'},
    ])
    assert len(await client.run_spend_logs("one")) == 2

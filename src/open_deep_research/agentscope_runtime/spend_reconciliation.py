"""Join trusted LiteLLM bills to SQL receipts without replaying unknown effects."""

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict, digest


async def reconcile_spend(store, lease, logs):
    """Require exact run/operation tags and a complete, uniquely identified bill.

    Proxy retries with multiple bills are reported for review, never silently
    collapsed into one upstream call. Unknown execution results remain isolated.
    """
    candidates = {}
    conflicts = set()
    for log in logs:
        tags = log.get("request_tags") or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                continue
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            continue
        if [tag for tag in tags if tag.startswith("run:")] != [f"run:{lease.run_id}"]:
            continue
        if f"run:{lease.run_id}" not in tags:
            continue
        operations = [
            tag.removeprefix("operation:")
            for tag in tags
            if tag.startswith("operation:")
        ]
        if len(operations) != 1 or not log.get("request_id"):
            continue
        key = "gateway:model:" + operations[0]
        bills = candidates.setdefault(key, {})
        prior = bills.get(log["request_id"])
        if prior is not None and any(prior.get(k) != log.get(k) for k in
                                     ("prompt_tokens", "completion_tokens", "spend")):
            conflicts.add(key)
        bills[log["request_id"]] = log
    result = {"corrected": [], "unresolved": []}
    async with store.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(store.ops).where(
                        store.ops.c.run_id == lease.run_id,
                        store.ops.c.kind == "gateway:model",
                    )
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        bills = list(candidates.get(row["key"], {}).values())
        if row["key"] in conflicts or len(bills) != 1 or row["state"] != "committed":
            result["unresolved"].append(row["key"])
            continue
        bill = bills[0]
        incoming, outgoing, cost = (
            bill.get("prompt_tokens"),
            bill.get("completion_tokens"),
            bill.get("spend"),
        )
        if (type(incoming) is not int or type(outgoing) is not int or cost is None
                or incoming < 0 or outgoing < 0):
            result["unresolved"].append(row["key"])
            continue
        try:
            amount = Decimal(str(cost))
        except InvalidOperation:
            result["unresolved"].append(row["key"])
            continue
        if not amount.is_finite() or amount < 0:
            result["unresolved"].append(row["key"])
            continue
        try:
            await store.reconcile_model_usage(
                lease,
                row["key"],
                receipt_id=digest(["litellm", bill["request_id"]]),
                input_tokens=incoming,
                output_tokens=outgoing,
                cost_micro_usd=int(amount * 1_000_000),
            )
        except RecoveryConflict:
            result["unresolved"].append(row["key"])
            continue
        result["corrected"].append(row["key"])
    return result


async def collect_spend(store, lease):
    """Fetch authenticated proxy bills; missing bills stay pending for later runs."""
    from open_deep_research.models.credentials import RunKeySettings
    from open_deep_research.models.spend import LiteLLMSpendClient

    client = LiteLLMSpendClient(RunKeySettings.from_env())
    try:
        return await reconcile_spend(
            store, lease, await client.run_spend_logs(lease.run_id)
        )
    finally:
        await client.aclose()


async def main_async(run_id, owner):
    """Operator command also handles late bills after an execution segment ends."""
    from open_deep_research.agentscope_runtime.app import ASRuntime

    runtime = await ASRuntime.create()
    try:
        store = await runtime.create_recovery_store()
        lease = await store.acquire(run_id, owner)
        try:
            print(json.dumps(await collect_spend(store, lease)))
        finally:
            await store.release(lease)
    finally:
        await runtime.aclose()


async def reconciliation_loop(service, interval=60):
    """Retry late bills for inactive runs; live segments reconcile in their owner."""
    import asyncio
    import logging

    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost

    while True:
        await asyncio.sleep(interval)
        store = service.store
        try:
            async with store.engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(
                                store.runs.c.run_id,
                                store.runs.c.user_id,
                                store.ops.c.result,
                            )
                            .join(store.ops, store.runs.c.run_id == store.ops.c.run_id)
                            .where(
                                store.ops.c.kind == "gateway:model",
                                store.ops.c.state == "committed",
                                # 等待用户的运行随时可能恢复，后台补账不得占用
                                # 它的执行租约，导致已入库审批返回 409。
                                store.runs.c.snapshot["status"].as_string().in_(
                                    ["completed", "failed", "cancelled"]
                                ),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            pending = {
                (r["run_id"], r["user_id"])
                for r in rows
                if not (r["result"] or {}).get("usage_correction")
            }
            for run_id, owner in pending:
                try:
                    recovery = await RecoverySession.open(store, run_id, owner)
                except FenceLost:
                    continue
                try:
                    await collect_spend(store, recovery.lease)
                finally:
                    await recovery.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- retry failed external reconciliation on the next cycle
            logging.getLogger(__name__).warning(
                "Native spend reconciliation pending: %s", type(exc).__name__
            )


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="核对原生运行的可信代理账单；未知执行结果保留隔离"
    )
    parser.add_argument("run_id")
    parser.add_argument("owner")
    args = parser.parse_args()
    asyncio.run(main_async(args.run_id, args.owner))

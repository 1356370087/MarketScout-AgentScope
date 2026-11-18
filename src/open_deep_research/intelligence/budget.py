"""Plan limits are reservations over existing bills, never a second bill."""

from datetime import datetime
from zoneinfo import ZoneInfo
from .contracts import IntelligenceError
from .store import record


def period(plan, at=None):
    return (at or datetime.now(ZoneInfo(plan["definition"]["timezone"]))).astimezone(
        ZoneInfo(plan["definition"]["timezone"])).strftime("%Y-%m")


async def reserve(c, plan, key, amount):
    if amount <= 0:
        raise IntelligenceError("plan_reservation_must_be_positive", 422)
    previous = record(await c.fetchrow("SELECT * FROM intelligence_budget_reservations WHERE operation_key=$1", key))
    if previous:
        if previous["plan_id"] != plan["id"] or previous["amount"] != amount:
            raise IntelligenceError("plan_budget_reservation_conflict")
        if previous["status"] != "released":
            return previous
    month = period(plan)
    inserted = await c.fetchrow("""INSERT INTO intelligence_budget_reservations
        (plan_id,operation_key,period,amount,created_at) VALUES($1::uuid,$2,$3,$4,$5::uuid)
        ON CONFLICT(operation_key) DO NOTHING RETURNING *""", plan["id"], key, month, amount, plan["owner_id"])
    if inserted is None:
        current = record(await c.fetchrow("SELECT * FROM intelligence_budget_reservations WHERE operation_key=$1", key))
        if current["plan_id"] != plan["id"] or current["amount"] != amount:
            raise IntelligenceError("plan_budget_reservation_conflict")
        if current["status"] != "released":
            return current
        inserted = await c.fetchrow("""UPDATE intelligence_budget_reservations SET status='reserved',period=$2,actual=NULL
            WHERE operation_key=$1 AND status='released' RETURNING *""", key, month)
        if inserted is None:
            return record(await c.fetchrow("SELECT * FROM intelligence_budget_reservations WHERE operation_key=$1", key))
    await c.execute("""INSERT INTO intelligence_budget_periods(plan_id,period,created_at)
        VALUES($1::uuid,$2,$3::uuid) ON CONFLICT(plan_id,period) DO NOTHING""", plan["id"], month, plan["owner_id"])
    changed = await c.fetchval("""UPDATE intelligence_budget_periods SET reserved=reserved+$3,update_time=now()
        WHERE plan_id=$1::uuid AND period=$2 AND reserved+settled+$3<=$4 RETURNING id""",
        plan["id"], month, amount, plan["definition"]["budget"]["month_micro_usd"])
    if not changed:
        raise IntelligenceError("plan_monthly_budget_exhausted", 429)
    return record(inserted)


async def settle(c, key, actual, *, confirmed_not_executed=False):
    if actual is None:
        await c.execute("UPDATE intelligence_budget_reservations SET status='unknown' WHERE operation_key=$1 AND status='reserved'", key)
        return
    if actual < 0 or (confirmed_not_executed and actual != 0):
        raise IntelligenceError("invalid_plan_settlement")
    status = "released" if confirmed_not_executed else "settled"
    row = record(await c.fetchrow("""UPDATE intelligence_budget_reservations SET status=$3,actual=$2,update_time=now()
        WHERE operation_key=$1 AND status IN ('reserved','unknown') RETURNING *""", key, actual, status))
    if row is None:
        old = await c.fetchrow("SELECT actual,status FROM intelligence_budget_reservations WHERE operation_key=$1", key)
        if old and (old["actual"] != actual or old["status"] != status):
            raise IntelligenceError("plan_settlement_conflict")
        return
    await c.execute("""UPDATE intelligence_budget_periods SET reserved=reserved-$3,settled=settled+$4,update_time=now()
        WHERE plan_id=$1::uuid AND period=$2""", row["plan_id"], row["period"], row["amount"], actual)


async def service_cost(job_id):
    from sqlalchemy import select, func
    from open_deep_research.platform.store import get_store
    from open_deep_research.platform import schema, runtime
    if not runtime.enabled():
        return None
    async with get_store().engine.connect() as c:
        rows = (await c.execute(select(schema.charges.c.status, schema.charges.c.actual).where(
            schema.charges.c.source == "intelligence:" + job_id))).all()
    if any(status in {"reserved", "unknown"} for status, _ in rows):
        return None
    return sum(actual or 0 for _, actual in rows)


async def hold_for_retry(c, key, actual):
    if actual is None:
        return await settle(c, key, None)
    await c.execute("""UPDATE intelligence_budget_reservations SET status='reserved',actual=$2,update_time=now()
        WHERE operation_key=$1 AND status IN ('reserved','unknown')""", key, actual)


async def reopen(c, plan, key):
    """Reacquire a prematurely settled envelope before any original-Run retry."""
    row = record(await c.fetchrow("""UPDATE intelligence_budget_reservations SET status='reserved',update_time=now()
        WHERE operation_key=$1 AND status='settled' RETURNING *""", key))
    if row is None:
        return
    if row["actual"] >= row["amount"]:
        raise IntelligenceError("original_run_budget_exhausted", 429)
    changed = await c.fetchval("""UPDATE intelligence_budget_periods SET reserved=reserved+$3,settled=settled-$4,update_time=now()
        WHERE plan_id=$1::uuid AND period=$2 AND reserved+settled+$3-$4<=$5 RETURNING id""",
        row["plan_id"], row["period"], row["amount"], row["actual"], plan["definition"]["budget"]["month_micro_usd"])
    if not changed:
        raise IntelligenceError("plan_monthly_budget_exhausted", 429)


async def correct(c, key, actual):
    """Apply a trusted native usage correction to the original billing period."""
    old = record(await c.fetchrow("SELECT * FROM intelligence_budget_reservations WHERE operation_key=$1", key))
    if old is None or old["status"] != "settled":
        return await settle(c, key, actual)
    if old["actual"] == actual:
        return
    if actual < 0:
        raise IntelligenceError("invalid_plan_settlement")
    changed = await c.fetchval("""UPDATE intelligence_budget_reservations SET actual=$2,update_time=now()
        WHERE operation_key=$1 AND status='settled' AND actual=$3 RETURNING id""", key, actual, old["actual"])
    if changed:
        await c.execute("UPDATE intelligence_budget_periods SET settled=settled+$3,update_time=now() WHERE plan_id=$1::uuid AND period=$2",
            old["plan_id"], old["period"], actual - old["actual"])

"""月度硬配额：调用前条件预留，回执幂等结算，未知结果继续占用。"""

from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, insert
from . import schema as s
from .store import PlatformError, audit_now, get_store, now
from open_deep_research.budgets import BudgetExhausted, BudgetDimension


class QuotaExceeded(BudgetExhausted):
    def __init__(self, available=None, required=None):
        super().__init__(BudgetDimension.COST_MICRO_USD, scope="account_month", available=available, required=required)
        self.detail, self.status = "monthly_quota_exhausted", 429


def current_period(at=None):
    return (at or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


async def runway(owner, *, store=None, at=None):
    store = store or get_store()
    period = current_period(at)
    async with store.engine.connect() as conn:
        limits = await store.limits(owner, conn)
        row = (await conn.execute(select(s.months.c.settled, s.months.c.reserved).where(
            s.months.c.owner_id == owner, s.months.c.period == period))).first()
    settled, reserved = row or (0, 0)
    ceiling = limits.get("monthly_cost_micro_usd")
    unlimited = limits.get("monthly_cost_unlimited", False)
    if ceiling is None and not unlimited:
        raise PlatformError("monthly_quota_not_configured", 503)
    return {"period": period, "limit": None if unlimited else ceiling, "settled": settled, "reserved": reserved,
            "available": None if unlimited else max(0, ceiling - settled - reserved)}


async def reserve(owner, operation_id, amount, source, *, store=None, at=None, detail=None):
    if type(amount) is not int or amount < 0:
        raise ValueError("费用预留必须为非负整数微美元")
    store = store or get_store()
    period = current_period(at)
    async with store.engine.begin() as conn:
        limits = await store.limits(owner, conn)
        ceiling = limits.get("monthly_cost_micro_usd")
        unlimited = limits.get("monthly_cost_unlimited", False)
        if ceiling is None and not unlimited:
            raise PlatformError("monthly_quota_not_configured", 503)
        created = (await conn.execute(store.upsert(s.charges).values(operation_id=operation_id,
            owner_id=owner, period=period, status="reserved", reserved=amount, actual=None,
            source=source, detail=detail or {}, create_time=audit_now(), update_time=audit_now(),
            created_at=owner, updated_at=owner).on_conflict_do_nothing()
            .returning(s.charges.c.operation_id))).scalar_one_or_none()
        if not created:
            old = (await conn.execute(select(s.charges).where(s.charges.c.operation_id == operation_id))).mappings().one()
            if old["owner_id"] != owner or old["reserved"] != amount:
                raise PlatformError("quota_operation_conflict", 409)
            if old["status"] != "released":
                return {**old, "created_reservation": False}
            changed = await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
                s.charges.c.status == "released").values(status="reserved", actual=None, period=period, detail=detail or {},
                update_time=audit_now(), updated_at=owner))
            if changed.rowcount != 1:
                raise PlatformError("quota_operation_conflict", 409)
        await conn.execute(store.upsert(s.months).values(owner_id=owner, period=period, reserved=0, settled=0).on_conflict_do_nothing())
        reservation = update(s.months).where(s.months.c.owner_id == owner, s.months.c.period == period)
        if not unlimited:
            reservation = reservation.where(s.months.c.reserved + s.months.c.settled + amount <= ceiling)
        changed = await conn.execute(reservation.values(reserved=s.months.c.reserved + amount))
        if changed.rowcount != 1:
            occupied = await conn.scalar(select(s.months.c.reserved + s.months.c.settled).where(
                s.months.c.owner_id == owner, s.months.c.period == period))
            raise QuotaExceeded(max(0, ceiling - occupied), amount)
        return {"operation_id": operation_id, "status": "reserved", "reserved": amount,
                "period": period, "created_reservation": True}


async def grant_dispatch(operation_id, detail, *, store=None):
    store = store or get_store()
    async with store.engine.begin() as conn:
        changed = await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status == "reserved", s.charges.c.detail["token"].as_string() == detail["token"],
            s.charges.c.detail["phase"].as_string() == "before_dispatch"
        ).values(detail={**detail, "phase": "dispatch_granted"}, update_time=audit_now()))
        if changed.rowcount != 1:
            raise PlatformError("quota_dispatch_grant_lost", 409)


async def settle(operation_id, actual, *, store=None):
    if type(actual) is not int or actual < 0:
        raise ValueError("结算金额必须为非负整数微美元")
    store = store or get_store()
    async with store.engine.begin() as conn:
        row = (await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status.in_(["reserved", "unknown"]))
            .values(status="settled", actual=actual, update_time=audit_now()).returning(s.charges))).mappings().one_or_none()
        if row is None:
            old = (await conn.execute(select(s.charges).where(s.charges.c.operation_id == operation_id))).mappings().one_or_none()
            if old and old["actual"] != actual:
                raise PlatformError("quota_settlement_conflict", 409)
            return
        await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
            s.months.c.period == row["period"]).values(reserved=s.months.c.reserved-row["reserved"],
            settled=s.months.c.settled+actual))


async def mark_unknown(operation_id, *, store=None, provisional_settlement=None):
    store = store or get_store()
    async with store.engine.begin() as conn:
        if provisional_settlement is not None:
            # Older gateways settled the full estimate even when they had no usage receipt.
            # Move only this exact estimate back to the hold. A verified bill wins a race.
            changed = (await conn.execute(update(s.charges).where(
                s.charges.c.operation_id == operation_id, s.charges.c.status == "settled",
                s.charges.c.reserved == provisional_settlement, s.charges.c.actual == provisional_settlement,
                s.charges.c.detail["bill_verified"].as_boolean().is_not(True),
            ).values(status="unknown", actual=None, update_time=audit_now()).returning(
                s.charges.c.owner_id, s.charges.c.period))).first()
            if changed:
                await conn.execute(update(s.months).where(s.months.c.owner_id == changed.owner_id,
                    s.months.c.period == changed.period).values(settled=s.months.c.settled - provisional_settlement,
                        reserved=s.months.c.reserved + provisional_settlement))
                return
        await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status == "reserved").values(status="unknown", update_time=audit_now()))


async def release(operation_id, *, store=None, before_dispatch_token=None):
    """仅在确认没有外部执行时释放；允许同一逻辑操作重新申请额度。"""
    store = store or get_store()
    async with store.engine.begin() as conn:
        statement = update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status.in_(["reserved", "unknown"]))
        if before_dispatch_token is not None:
            statement = statement.where(s.charges.c.detail["token"].as_string() == before_dispatch_token,
                s.charges.c.detail["phase"].as_string() == "before_dispatch")
        row = (await conn.execute(statement
            .values(status="released", actual=0, update_time=audit_now()).returning(s.charges))).mappings().one_or_none()
        if row:
            await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
                s.months.c.period == row["period"]).values(reserved=s.months.c.reserved-row["reserved"]))
        return row is not None


async def correct(operation_id, actual, *, store=None):
    """仅用于可信上游回执更正；与逻辑操作恢复保持独立。"""
    store = store or get_store()
    async with store.engine.begin() as conn:
        row = (await conn.execute(select(s.charges).where(s.charges.c.operation_id == operation_id))).mappings().one_or_none()
        if not row:
            return
        if row["status"] == "released":
            raise PlatformError("quota_receipt_conflicts_with_release", 409)
        previous = row["actual"] if row["status"] == "settled" else 0
        outstanding = row["reserved"] if row["status"] in {"reserved", "unknown"} else 0
        changed = await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status == row["status"], s.charges.c.actual == row["actual"])
            .values(status="settled", actual=actual, update_time=audit_now(),
                    detail={**row["detail"], "bill_verified": True}))
        if changed.rowcount != 1:
            current = (await conn.execute(select(s.charges.c.status, s.charges.c.actual).where(
                s.charges.c.operation_id == operation_id))).first()
            if current and current.status == "settled" and current.actual == actual:
                return
            raise PlatformError("quota_correction_conflict", 409)
        await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
            s.months.c.period == row["period"]).values(settled=s.months.c.settled + actual - previous,
                reserved=s.months.c.reserved - outstanding))


async def pending_native_runs():
    store = get_store()
    run = s.charges.c.detail["run_id"].as_string()
    async with store.engine.connect() as conn:
        return list(await conn.scalars(select(run).where(run.is_not(None),
            s.charges.c.status.in_(["reserved", "unknown"])).distinct()))


async def reconcile_run_reservations(native_store, lease):
    """Recover from a lost process using durable grant evidence from an older fence."""
    store = get_store()
    async with store.engine.connect() as conn:
        rows = (await conn.execute(select(s.charges).where(
            s.charges.c.detail["run_id"].as_string() == lease.run_id,
            s.charges.c.status.in_(["reserved", "unknown"])))).mappings().all()
    for row in rows:
        detail = row["detail"]
        if detail["fence"] >= lease.fence:
            continue
        key = detail["operation_key"]
        record = await native_store.operation_record(lease, key)
        if detail["phase"] == "before_dispatch":
            released = await release(row["operation_id"], store=store, before_dispatch_token=detail["token"])
            if released and record and record["state"] != "committed":
                await native_store.resolve_operation(lease, key, not_executed=True)
        elif record and record["state"] == "committed":
            await native_store._sync_monthly(lease, key)
        else:
            await mark_unknown(row["operation_id"], store=store)

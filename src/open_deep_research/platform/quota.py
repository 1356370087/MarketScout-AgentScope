"""月度硬配额：调用前条件预留，回执幂等结算，未知结果继续占用。"""

from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, insert
from . import schema as s
from .store import PlatformError, get_store, now
from open_deep_research.budgets import BudgetExhausted, BudgetDimension


class QuotaExceeded(BudgetExhausted):
    def __init__(self):
        super().__init__(BudgetDimension.COST_MICRO_USD)
        self.detail, self.status = "monthly_quota_exhausted", 429


def current_period(at=None):
    return (at or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


async def reserve(owner, operation_id, amount, source, *, store=None, at=None, detail=None):
    if type(amount) is not int or amount < 0:
        raise ValueError("费用预留必须为非负整数微美元")
    store = store or get_store()
    period = current_period(at)
    async with store.engine.begin() as conn:
        limits = await store.limits(owner, conn)
        ceiling = limits.get("monthly_cost_micro_usd")
        if ceiling is None:
            raise PlatformError("monthly_quota_not_configured", 503)
        created = (await conn.execute(store.upsert(s.charges).values(operation_id=operation_id,
            owner_id=owner, period=period, status="reserved", reserved=amount, actual=None,
            source=source, detail=detail or {}, created_at=now(), updated_at=now()).on_conflict_do_nothing()
            .returning(s.charges.c.operation_id))).scalar_one_or_none()
        if not created:
            old = (await conn.execute(select(s.charges).where(s.charges.c.operation_id == operation_id))).mappings().one()
            if old["owner_id"] != owner or old["reserved"] != amount:
                raise PlatformError("quota_operation_conflict", 409)
            if old["status"] != "released":
                return dict(old)
            changed = await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
                s.charges.c.status == "released").values(status="reserved", actual=None, period=period, updated_at=now()))
            if changed.rowcount != 1:
                raise PlatformError("quota_operation_conflict", 409)
        await conn.execute(store.upsert(s.months).values(owner_id=owner, period=period, reserved=0, settled=0).on_conflict_do_nothing())
        changed = await conn.execute(update(s.months).where(s.months.c.owner_id == owner,
            s.months.c.period == period, s.months.c.reserved + s.months.c.settled + amount <= ceiling)
            .values(reserved=s.months.c.reserved + amount))
        if changed.rowcount != 1:
            raise QuotaExceeded()
        return {"operation_id": operation_id, "status": "reserved", "reserved": amount, "period": period}


async def settle(operation_id, actual, *, store=None):
    if type(actual) is not int or actual < 0:
        raise ValueError("结算金额必须为非负整数微美元")
    store = store or get_store()
    async with store.engine.begin() as conn:
        row = (await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status.in_(["reserved", "unknown"]))
            .values(status="settled", actual=actual, updated_at=now()).returning(s.charges))).mappings().one_or_none()
        if row is None:
            old = (await conn.execute(select(s.charges).where(s.charges.c.operation_id == operation_id))).mappings().one_or_none()
            if old and old["actual"] != actual:
                raise PlatformError("quota_settlement_conflict", 409)
            return
        await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
            s.months.c.period == row["period"]).values(reserved=s.months.c.reserved-row["reserved"],
            settled=s.months.c.settled+actual))


async def mark_unknown(operation_id, *, store=None):
    store = store or get_store()
    async with store.engine.begin() as conn:
        await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status == "reserved").values(status="unknown", updated_at=now()))


async def release(operation_id, *, store=None):
    """仅在确认没有外部执行时释放；允许同一逻辑操作重新申请额度。"""
    store = store or get_store()
    async with store.engine.begin() as conn:
        row = (await conn.execute(update(s.charges).where(s.charges.c.operation_id == operation_id,
            s.charges.c.status.in_(["reserved", "unknown"]))
            .values(status="released", actual=0, updated_at=now()).returning(s.charges))).mappings().one_or_none()
        if row:
            await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
                s.months.c.period == row["period"]).values(reserved=s.months.c.reserved-row["reserved"]))


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
            .values(status="settled", actual=actual, updated_at=now()))
        if changed.rowcount != 1:
            raise PlatformError("quota_correction_conflict", 409)
        await conn.execute(update(s.months).where(s.months.c.owner_id == row["owner_id"],
            s.months.c.period == row["period"]).values(settled=s.months.c.settled + actual - previous,
                reserved=s.months.c.reserved - outstanding))

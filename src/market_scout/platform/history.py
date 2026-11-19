"""将已有原生回执按真实事件时间投影到月度账本，避免升级重置额度。"""

from datetime import datetime, timezone
from sqlalchemy import select, func, update
from . import schema as s
from .quota import current_period
from .store import audit_now, get_store, now


async def import_native_usage(native_store):
    store = get_store()
    operations, events, runs = native_store.ops, native_store.outbox, native_store.runs
    operation_key = events.c.payload["operation_key"].as_string()
    started = select(events.c.run_id.label("run_id"), operation_key.label("key"),
        func.max(events.c.payload["timestamp"].as_float()).label("started_at")).where(
        events.c.payload["type"].as_string() == "research.operation_started").group_by(
        events.c.run_id, operation_key).subquery()
    query = select(operations, runs.c.user_id, started.c.started_at).join(runs,
        runs.c.run_id == operations.c.run_id).outerjoin(started,
        (started.c.run_id == operations.c.run_id) & (started.c.key == operations.c.key)).where(
        operations.c.kind.in_(["model_attempt", "gateway:model"]))
    imported = undated = unpriced = 0
    offset = 0
    while True:
        async with native_store.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(operations.c.run_id, operations.c.key).offset(offset).limit(500))).mappings().all()
        if not rows:
            break
        offset += len(rows)
        async with store.engine.begin() as conn:
            for row in rows:
                if "cost_micro_usd" not in row["reservation"] and "cost_micro_usd" not in (row["actual"] or {}):
                    unpriced += 1
                    continue
                if row["started_at"] is None:
                    undated += 1
                    continue
                period = current_period(datetime.fromtimestamp(row["started_at"], timezone.utc))
                identity = native_store._monthly_key(row["run_id"], row["key"])
                result = row["result"] or {}
                unknown = row["state"] != "committed" or result.get("uncertain") or result.get("cost_status") == "unknown" or "cost_micro_usd" not in (row["actual"] or {})
                reserve = row["reservation"].get("cost_micro_usd", 0)
                actual = (row["actual"] or {}).get("cost_micro_usd", 0)
                created = (await conn.execute(store.upsert(s.charges).values(operation_id=identity,
                    owner_id=row["user_id"], period=period, status="unknown" if unknown else "settled",
                    reserved=reserve, actual=None if unknown else actual, source=row["kind"],
                    detail={"imported": True}, create_time=datetime.fromtimestamp(row["started_at"], timezone.utc),
                    update_time=audit_now(), created_at=row["user_id"], updated_at=row["user_id"]
                    ).on_conflict_do_nothing().returning(s.charges.c.operation_id))).scalar_one_or_none()
                if not created:
                    continue
                await conn.execute(store.upsert(s.months).values(owner_id=row["user_id"], period=period,
                    reserved=0, settled=0).on_conflict_do_nothing())
                await conn.execute(update(s.months).where(s.months.c.owner_id == row["user_id"], s.months.c.period == period)
                    .values(reserved=s.months.c.reserved+(reserve if unknown else 0), settled=s.months.c.settled+(0 if unknown else actual)))
                imported += 1
    return {"imported": imported, "undated": undated, "unpriced": unpriced}

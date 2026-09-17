"""Public sandbox approvals resolve the native SQL run and current epoch."""

from fastapi import HTTPException
from sqlalchemy import select

from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.configuration import Configuration


async def sandbox_context(service, run_id, *, require_live_fence=False):
    """Called after the public route has checked owner/read-any permissions."""
    store = service.store
    async with store.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(store.runs).where(store.runs.c.run_id == run_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        now = await store._now(conn)
    if require_live_fence and (
        not row["owner"]
        or row["expires"] <= now
        or row["snapshot"]["status"] in {"completed", "cancelled", "failed"}
    ):
        raise HTTPException(409, "stale_fence")
    application = row["snapshot"]["application"]
    config = RunConfig.restore(application["configuration"]).compatibility_projection()
    config["configurable"] = {
        **application.get("request_configurable", {}),
        **config["configurable"],
        "runs_dir": str(service.runs_dir),
    }
    config.setdefault("metadata", {}).update(
        run_id=run_id,
        run_fence_token=row["fence"],
        owner=row["user_id"],
        source_selection=application.get("source_selection", {}),
    )
    from open_deep_research.agentscope_runtime.recovery_store import RunLease

    config["_event_publisher"] = NativeEventPublisher(
        store, RunLease(run_id, row["user_id"], row["owner"], row["fence"])
    )
    return Configuration.from_runnable_config(config), row["fence"], config


async def cleanup_run_key(store, run_id, manager):
    """Skip live native keys; fence cleanup of abandoned execution segments."""
    from open_deep_research.agentscope_runtime.recovery_store import FenceLost

    async with store.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(store.runs).where(store.runs.c.run_id == run_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if row["owner"] and row["expires"] > await store._now(conn):
            return True
    try:
        lease = await store.acquire(run_id, row["user_id"])
    except FenceLost:
        return True
    try:
        async with store.transaction(lease):
            if not await manager.finalize(run_id):
                raise RuntimeError("native_run_key_cleanup_pending")
    finally:
        await store.release(lease)
    return True


class NativeEventPublisher:
    """Persist sandbox public events in the same SQL cursor as native research."""

    def __init__(self, store, lease):
        self.store, self.lease = store, lease

    async def publish(self, event_type, *, stage=None, payload=None, dedupe_key):
        from open_deep_research.agentscope_runtime.recovery_store import digest
        from open_deep_research.events.public import sanitize_public_payload

        event_id = digest([self.lease.run_id, dedupe_key])
        async with self.store.transaction(self.lease) as (conn, row):
            exists = await conn.scalar(
                select(self.store.outbox.c.event_id).where(
                    self.store.outbox.c.run_id == self.lease.run_id,
                    self.store.outbox.c.event_id == event_id,
                )
            )
            if not exists:
                await self.store._event(
                    conn,
                    row,
                    "research.public",
                    {
                        "public_type": event_type,
                        "stage": stage,
                        "public_payload": sanitize_public_payload(
                            event_type, payload or {}
                        ),
                    },
                    event_id=event_id,
                )

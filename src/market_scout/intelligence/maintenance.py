"""Retain every research/publication reference; sweep only unused observations."""

from .observations import root
from .settings import settings
from .store import Store
import time


async def sweep(store=None):
    store = store or Store()
    pool = await store.pool()
    async with pool.acquire() as c, c.transaction():
        rows = await c.fetch("""UPDATE intelligence_snapshots s SET is_deleted=true,update_time=now()
            WHERE s.id IN (SELECT o.id FROM intelligence_snapshots o JOIN intelligence_plans p ON p.id=o.plan_id
              WHERE NOT o.is_deleted AND p.status<>'active' AND o.observed_at<now()-make_interval(days=>$1)
              AND NOT EXISTS(SELECT 1 FROM intelligence_sources source WHERE source.snapshot_id=o.id AND NOT source.is_deleted)
              AND NOT EXISTS(SELECT 1 FROM intelligence_items item WHERE item.manifest->'snapshots'
                @> jsonb_build_array(jsonb_build_object('id',o.id::text)))
              AND NOT EXISTS(SELECT 1 FROM intelligence_item_runs run WHERE run.result->'manifest'->'snapshots'
                @> jsonb_build_array(jsonb_build_object('id',o.id::text)))
              AND NOT EXISTS(SELECT 1 FROM intelligence_events e WHERE e.status IN ('pending','assessing','needs_review')
                AND (e.snapshot_id=o.id OR e.previous_snapshot_id=o.id))
              AND NOT EXISTS(SELECT 1 FROM intelligence_item_events ie JOIN intelligence_events e ON e.id=ie.event_id
                WHERE e.snapshot_id=o.id OR e.previous_snapshot_id=o.id)
              ORDER BY o.observed_at FOR UPDATE OF o SKIP LOCKED LIMIT 100)
            RETURNING s.id""", settings().retention_days)
    # A crash between SQL retirement and file removal must remain resumable.
    rows = await pool.fetch("SELECT id,artifact FROM intelligence_snapshots WHERE is_deleted AND artifact<>'' LIMIT 100")
    storage_root = root().resolve()
    removed = 0
    for row in rows:
        directory = (storage_root / row["artifact"]).resolve()
        if not directory.is_relative_to(storage_root):
            raise ValueError("observation_artifact_outside_storage")
        for filename in ("raw.bin", "document.json"):
            (directory / filename).unlink(missing_ok=True)
        if directory.exists():
            directory.rmdir()
        await pool.execute("UPDATE intelligence_snapshots SET artifact='' WHERE id=$1", row["id"])
        removed += 1
    await pool.execute("DELETE FROM intelligence_oauth_states WHERE expires_at<now()-interval '1 day'")
    orphaned = 0
    # A fenced-out capture can leave files before its SQL insert commits.
    # Only old, unregistered artifacts are eligible; never recursively delete.
    cutoff = time.time() - settings().retention_days * 86400
    for candidate in storage_root.glob("*/*"):
        if not candidate.is_dir() or candidate.stat().st_mtime >= cutoff:
            continue
        directory = candidate.resolve()
        if not directory.is_relative_to(storage_root):
            continue
        relative = str(candidate.relative_to(storage_root))
        if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM intelligence_snapshots WHERE artifact=$1)", relative):
            continue
        if any(path.name not in {"raw.bin", "document.json"} for path in directory.iterdir()):
            continue
        for filename in ("raw.bin", "document.json"):
            (directory / filename).unlink(missing_ok=True)
        directory.rmdir()
        orphaned += 1
        if orphaned >= 100:
            break
    return {"removed_snapshots": removed, "removed_orphans": orphaned}

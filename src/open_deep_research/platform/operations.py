"""运营元数据与显式维护操作；不投影研究正文或资料内容。"""

import asyncio
import json
from datetime import datetime, timezone
from sqlalchemy import select, func, insert
from .store import get_store, PlatformError
from . import schema as s


async def list_operations(app):
    service = getattr(app.state, "native_research_service", None)
    runs = []
    if service is not None:
        table = service.store.runs
        async with service.store.engine.connect() as conn:
            rows = (await conn.execute(select(table.c.run_id, table.c.user_id,
                table.c.snapshot["status"].as_string().label("status"), table.c.used,
                table.c.snapshot["error"].as_string().label("error_code"))
                .order_by(table.c.run_id).limit(200))).mappings()
            runs = [{"id": row["run_id"], "owner_id": row["user_id"], "status": row["status"],
                "cost_micro_usd": row["used"].get("cost_micro_usd", 0), "error_code": row["error_code"]} for row in rows]
    from open_deep_research.documents.database import document_health, get_document_pool
    from open_deep_research.documents.settings import get_document_settings
    from open_deep_research.report.publication_store import worker_available, get_publisher_settings
    result = {"runs": runs, "documents": [], "knowledge_jobs": [], "publications": [], "workers": {
        "documents": await document_health(), "publisher": {"enabled": get_publisher_settings().enabled, "ready": worker_available()}}}
    from .components import config_directory
    state = await get_store().state()
    knowledge = {"enabled": get_document_settings().enabled, "ready": False}
    if state["active_revision"]:
        path = config_directory() / state["active_revision"] / "knowledge-worker.applied.json"
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            timestamp = receipt.get("time")
            knowledge.update(last_seen=timestamp, ready=bool(timestamp) and (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds() < 90)
    result["workers"]["knowledge"] = knowledge
    if get_document_settings().configured:
        pool = await get_document_pool()
        async with pool.acquire() as conn:
            result["documents"] = [dict(row) for row in await conn.fetch(
                "SELECT id,owner_id,status,created_at FROM research_documents WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 200")]
            result["knowledge_jobs"] = [dict(row) for row in await conn.fetch(
                "SELECT id,actor_id AS owner_id,status,kind,attempts,lease_until,updated_at FROM knowledge_jobs ORDER BY updated_at DESC LIMIT 200")]
    if service:
        from open_deep_research.report.publication_store import PublicationJobStore
        for row in runs:
            store = PublicationJobStore(row["id"], runs_dir=service.runs_dir)
            jobs = await asyncio.to_thread(store.list)
            result["publications"].extend({"id": job.publication_id, "run_id": row["id"],
                "owner_id": row["owner_id"], "status": job.status, "retryable": job.retryable}
                for job in jobs)
    return result


async def overview(app):
    store = get_store()
    state = await store.state()
    async with store.engine.connect() as conn:
        pending = await conn.scalar(select(func.count()).select_from(s.revisions).where(s.revisions.c.status == "pending"))
        total = (await conn.execute(select(func.coalesce(func.sum(s.months.c.settled), 0),
                                          func.coalesce(func.sum(s.months.c.reserved), 0)))).one()
    operations = await list_operations(app)
    return {"configuration": state, "pending_releases": pending, "cost_micro_usd": total[0],
            "reserved_micro_usd": total[1], "run_count": len(operations["runs"]),
            "workers": operations["workers"], "runtime_error": getattr(app.state, "platform_runtime_error", None),
            "history_import": getattr(app.state, "platform_history_import", None)}


async def maintain(app, user, body):
    from uuid import uuid4
    store = get_store()
    operation_id = str(uuid4())
    async with store.engine.begin() as conn:
        await store.audit(conn, user.user_id, "maintenance.requested", operation_id,
            {"action": body.action, "resource_id": body.resource_id, "reason": body.reason})
    owner = None
    try:
        service = getattr(app.state, "native_research_service", None)
        if body.action in {"cancel_run", "retry_publication"}:
            if service is None:
                raise PlatformError("runtime_unavailable", 503)
            run_id, _, publication_id = body.resource_id.partition("/")
            async with service.store.engine.connect() as conn:
                owner = await conn.scalar(select(service.store.runs.c.user_id).where(service.store.runs.c.run_id == run_id))
            if not owner:
                raise PlatformError("run_not_found", 404)
            if body.action == "cancel_run":
                await service.cancel(run_id, owner, command_id="platform:" + operation_id)
            else:
                from open_deep_research.agentscope_runtime.recovery import RecoverySession
                from open_deep_research.report.publication_store import PublicationJobStore
                from open_deep_research.events.publications import PublicationEventStore, publication_event_payload
                record, _ = await service.store.load(run_id, owner)
                if record.status != "completed":
                    raise PlatformError("run_not_completed", 409)
                recovery = await RecoverySession.open(service.store, run_id, owner)
                try:
                    async with service.store.transaction(recovery.lease):
                        def requeued(job):
                            PublicationEventStore(run_id, runs_dir=str(service.runs_dir)).append("publication.requeued",
                                publication_id=job.publication_id, payload=publication_event_payload(job),
                                dedupe_key=f"{job.publication_id}:retry:{job.attempt}:{job.max_attempts}")
                        await asyncio.to_thread(PublicationJobStore(run_id, runs_dir=service.runs_dir).retry,
                            publication_id, on_requeued=requeued)
                finally:
                    await recovery.close()
        else:
            from open_deep_research.documents.database import get_document_pool
            pool = await get_document_pool()
            async with pool.acquire() as conn:
                if body.action == "knowledge_maintenance":
                    row = await conn.fetchrow("""UPDATE knowledge_jobs SET status='queued',available_at=now(),
                        lease_until=NULL,update_time=now() WHERE id=$1::uuid AND status='failed' RETURNING actor_id""", body.resource_id)
                    if not row:
                        raise PlatformError("knowledge_job_not_retryable", 409)
                    owner = str(row["actor_id"])
                else:
                    owner = await conn.fetchval("SELECT owner_id FROM research_documents WHERE id=$1::uuid AND deleted_at IS NULL", body.resource_id)
                    if not owner:
                        raise PlatformError("document_not_found", 404)
                    owner = str(owner)
            if body.action == "retry_document":
                from open_deep_research.documents.repository import retry_document
                if not await retry_document(owner, body.resource_id):
                    raise PlatformError("document_not_retryable", 409)
            elif body.action == "reindex_document":
                from open_deep_research.documents.versioning import queue_reindex_generation
                await queue_reindex_generation(owner, body.resource_id)
    except Exception as exc:
        async with store.engine.begin() as conn:
            await store.audit(conn, user.user_id, "maintenance.failed", operation_id, {"error_code": type(exc).__name__})
        raise
    async with store.engine.begin() as conn:
        await store.audit(conn, user.user_id, "maintenance.completed", operation_id, {"owner_id": owner})
    return {"operation_id": operation_id, "status": "accepted", "owner_id": owner}

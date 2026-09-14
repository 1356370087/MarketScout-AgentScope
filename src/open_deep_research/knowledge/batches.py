"""Batch operations: per-item tracking with partial success (KB-14).

Each batch fixes its target document-id set upfront (≤100 by default), then
executes items one at a time — no wrapping transaction, so a failed item
never rolls back its siblings. Items record attempt counts, failure codes
and timestamps; "retry failed only" creates new attempts for failed items
without re-running successful ones.
"""

from __future__ import annotations

from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

MAX_BATCH_SIZE = 100

BATCH_OPERATIONS = frozenset({"reparse", "trash", "retry"})


class BatchError(RuntimeError):
    """Raised when a batch request is invalid."""


async def create_batch(
    actor_id: str,
    operation: str,
    document_ids: list[str],
    *,
    knowledge_base_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Create one parent batch with per-item rows; execution is separate."""
    if operation not in BATCH_OPERATIONS:
        raise BatchError(f"batch_operation_invalid:{operation}")
    if not document_ids:
        raise BatchError("batch_targets_empty")
    if len(document_ids) > MAX_BATCH_SIZE:
        raise BatchError(f"batch_size_exceeded:{len(document_ids)}:{MAX_BATCH_SIZE}")
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        batch_id = await connection.fetchval(
            """INSERT INTO knowledge_batches
                 (workspace_id, knowledge_base_id, operation, status,
                  total_items, created_by)
               VALUES ($1::uuid, $2::uuid, $3, 'pending', $4, $5::uuid)
             RETURNING id""",
            workspace_id,
            knowledge_base_id,
            operation,
            len(document_ids),
            actor_id,
        )
        await connection.executemany(
            """INSERT INTO knowledge_batch_items(batch_id, document_id)
               VALUES ($1::uuid, $2::uuid)""",
            [(batch_id, doc_id) for doc_id in document_ids],
        )
    return {"batch_id": str(batch_id), "operation": operation, "total": len(document_ids)}


async def get_batch(actor_id: str, batch_id: str) -> dict[str, Any] | None:
    """Return one batch with its item-level results."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        batch = await connection.fetchrow(
            """SELECT * FROM knowledge_batches
                WHERE id=$1::uuid AND created_by=$2::uuid""",
            batch_id,
            actor_id,
        )
        if not batch:
            return None
        items = await connection.fetch(
            """SELECT * FROM knowledge_batch_items
                WHERE batch_id=$1::uuid ORDER BY document_id""",
            batch_id,
        )
    return {
        "id": str(batch["id"]),
        "operation": batch["operation"],
        "status": batch["status"],
        "total": int(batch["total_items"]),
        "completed": int(batch["completed_count"]),
        "failed": int(batch["failed_count"]),
        "created_at": batch["created_at"].isoformat(),
        "items": [
            {
                "document_id": str(item["document_id"]),
                "attempt": int(item["attempt"]),
                "status": item["status"],
                "failure_code": item["failure_code"],
                "error_summary": item["error_summary"],
            }
            for item in items
        ],
    }


async def cancel_batch(actor_id: str, batch_id: str) -> dict[str, str]:
    """Cancel un-executed items; running items finish at their next checkpoint."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        batch = await connection.fetchrow(
            """SELECT * FROM knowledge_batches
                WHERE id=$1::uuid AND created_by=$2::uuid
                  AND status IN ('pending','running')""",
            batch_id,
            actor_id,
        )
        if not batch:
            return {"batch_id": batch_id, "status": "not_cancellable"}
        await connection.execute(
            """UPDATE knowledge_batch_items SET status='cancelled'
                WHERE batch_id=$1::uuid AND status='pending'""",
            batch_id,
        )
        await connection.execute(
            """UPDATE knowledge_batches
                  SET status='cancelled', updated_at=now()
                WHERE id=$1::uuid""",
            batch_id,
        )
    return {"batch_id": batch_id, "status": "cancelled"}


async def retry_failed(actor_id: str, batch_id: str) -> dict[str, Any]:
    """Reset failed items to pending; successful items are never re-run."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        batch = await connection.fetchrow(
            """SELECT * FROM knowledge_batches
                WHERE id=$1::uuid AND created_by=$2::uuid
                  AND status IN ('completed','failed','cancelled')""",
            batch_id,
            actor_id,
        )
        if not batch:
            return {"batch_id": batch_id, "status": "not_retryable"}
        reset = await connection.fetch(
            """UPDATE knowledge_batch_items
                  SET status='pending', failure_code=NULL, error_summary=NULL
                WHERE batch_id=$1::uuid AND status='failed'
             RETURNING document_id""",
            batch_id,
        )
        if not reset:
            return {"batch_id": batch_id, "status": "no_failed_items"}
        await connection.execute(
            """UPDATE knowledge_batches
                  SET status='pending', failed_count=0, updated_at=now()
                WHERE id=$1::uuid""",
            batch_id,
        )
    return {
        "batch_id": batch_id,
        "status": "pending",
        "retried": [str(row["document_id"]) for row in reset],
    }


async def execute_batch(actor_id: str, batch_id: str) -> dict[str, Any]:
    """Execute pending items one at a time (API or worker entry point)."""
    from open_deep_research.documents import versioning

    from .trash import trash_document

    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        batch = await connection.fetchrow(
            """SELECT * FROM knowledge_batches
                WHERE id=$1::uuid AND created_by=$2::uuid AND status='pending'""",
            batch_id,
            actor_id,
        )
        if not batch:
            return {"batch_id": batch_id, "status": "not_executable"}
        await connection.execute(
            "UPDATE knowledge_batches SET status='running', updated_at=now() WHERE id=$1::uuid",
            batch_id,
        )
        pending = await connection.fetch(
            """SELECT id, document_id FROM knowledge_batch_items
                WHERE batch_id=$1::uuid AND status='pending' ORDER BY document_id""",
            batch_id,
        )
    operation = batch["operation"]
    succeeded = failed = 0
    async with pool.acquire() as connection:
        for item in pending:
            item_id = str(item["id"])
            document_id = str(item["document_id"])
            await connection.execute(
                """UPDATE knowledge_batch_items
                      SET status='running', started_at=now(), attempt=attempt+1
                    WHERE id=$1::uuid""",
                item_id,
            )
            try:
                if operation == "trash":
                    result = await trash_document(actor_id, document_id)
                    if not result:
                        raise RuntimeError("document_not_found_or_already_trashed")
                elif operation == "reparse":
                    generation_id = await versioning.queue_reindex_generation(
                        actor_id, document_id
                    )
                    if not generation_id:
                        raise RuntimeError("document_not_found")
                elif operation == "retry":
                    from open_deep_research.documents.repository import retry_document

                    summary = await retry_document(actor_id, document_id)
                    if not summary:
                        raise RuntimeError("document_not_failed")
                await connection.execute(
                    """UPDATE knowledge_batch_items
                          SET status='succeeded', finished_at=now()
                        WHERE id=$1::uuid""",
                    item_id,
                )
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - per-item isolation
                code = str(exc).split(":", 1)[0][:96] or "batch_item_failed"
                await connection.execute(
                    """UPDATE knowledge_batch_items
                          SET status='failed', failure_code=$2,
                              error_summary=$3, finished_at=now()
                        WHERE id=$1::uuid""",
                    item_id,
                    code,
                    str(exc)[:500],
                )
                failed += 1
        await connection.execute(
            """UPDATE knowledge_batches
                  SET status='completed', completed_count=$2, failed_count=$3,
                      updated_at=now()
                WHERE id=$1::uuid""",
            batch_id,
            succeeded,
            failed,
        )
    return {"batch_id": batch_id, "succeeded": succeeded, "failed": failed}

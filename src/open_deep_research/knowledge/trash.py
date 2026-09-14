"""Recycle-bin semantics: deferred purge, restore, and reference protection.

Implements KB-15: soft delete records who and when with a 30-day retention;
trashed material exits retrieval but keeps versions, collections and audit
records. Physical cleanup only fires after the retention window AND when no
research run, fact evidence or Wiki citation still references the document.
Restore uses a guarded status transition so it can never race with an
in-flight purge.
"""

from __future__ import annotations

from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.documents.storage import delete_storage_key

RETENTION_DAYS = 30
_TRASH_STATUS = "trashed"


async def trash_document(
    actor_id: str, document_id: str, *, home_kb_id: str | None = None
) -> dict[str, Any] | None:
    """Move one document into the recycle bin (managers only, caller gates).

    Returns ``{"id", "deleted_at", "purge_after"}`` or ``None`` when the
    document is missing, foreign or already trashed. No physical cleanup
    job is queued here — the purge scheduler handles that separately.
    """
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if home_kb_id is None:
            home_kb_id = await connection.fetchval(
                "SELECT home_knowledge_base_id FROM research_documents WHERE id=$1::uuid",
                document_id,
            )
        row = await connection.fetchrow(
            f"""UPDATE research_documents
                   SET status='{_TRASH_STATUS}', deleted_at=now(), deleted_by=$2::uuid,
                       purge_after=now() + make_interval(days => {RETENTION_DAYS}),
                       updated_at=now()
                 WHERE id=$1::uuid AND deleted_at IS NULL
                RETURNING id, deleted_at, purge_after""",
            document_id,
            actor_id,
        )
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "deleted_at": row["deleted_at"].isoformat(),
        "purge_after": row["purge_after"].isoformat(),
    }


async def restore_document(actor_id: str, document_id: str) -> dict[str, Any] | None:
    """Restore one trashed document; the status guard prevents purge races.

    Returns the restored document summary or ``None`` when not found or not
    in the trash. The conditional ``WHERE status='trashed'`` ensures restore
    and physical cleanup are mutually exclusive: if a purge already flipped
    the row to 'deleting', this returns ``None`` instead of resurrecting.
    """
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """UPDATE research_documents
                  SET status='ready', deleted_at=NULL, deleted_by=NULL,
                      purge_after=NULL, updated_at=now()
                WHERE id=$1::uuid AND status='trashed'
             RETURNING *""",
            document_id,
        )
        if not row:
            return None
        # 恢复不自动发布原本未发布的草稿（方案 KB-15）。
        current = await connection.fetchval(
            "SELECT current_generation_id FROM research_documents WHERE id=$1::uuid",
            document_id,
        )
        if not current:
            row = await connection.fetchrow(
                """UPDATE research_documents SET status='ready', updated_at=now()
                 WHERE id=$1::uuid RETURNING *""",
                document_id,
            )
    return {"id": str(row["id"]), "status": row["status"]}


async def list_trash(
    actor_id: str, knowledge_base_id: str, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """List trashed documents in one knowledge base (managers, caller gates)."""
    actor_id = document_owner_id(actor_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        total = await connection.fetchval(
            """SELECT count(*) FROM research_documents
                WHERE home_knowledge_base_id=$1::uuid
                  AND status='trashed'""",
            knowledge_base_id,
        )
        rows = await connection.fetch(
            """SELECT id, filename, deleted_at, deleted_by, purge_after
                 FROM research_documents
                WHERE home_knowledge_base_id=$1::uuid AND status='trashed'
                ORDER BY deleted_at DESC LIMIT $2 OFFSET $3""",
            knowledge_base_id,
            limit,
            offset,
        )
    return {
        "items": [
            {
                "id": str(row["id"]),
                "filename": row["filename"],
                "deleted_at": row["deleted_at"].isoformat(),
                "deleted_by": str(row["deleted_by"]) if row["deleted_by"] else None,
                "purge_after": row["purge_after"].isoformat(),
            }
            for row in rows
        ],
        "total": int(total),
    }


async def _reference_check(connection, document_id: str) -> list[dict[str, str]]:
    """Check whether any citation or run still references this document."""
    references: list[dict[str, str]] = []
    runs = await connection.fetchval(
        "SELECT count(*) FROM research_run_sources WHERE document_id=$1::uuid",
        document_id,
    )
    if int(runs) > 0:
        references.append({"type": "research_run", "count": str(runs)})
    for table in ("knowledge_fact_evidence", "knowledge_page_citations"):
        count = await connection.fetchval(
            f"SELECT count(*) FROM {table} WHERE document_id=$1::uuid", document_id
        )
        if count:
            references.append({"type": table, "count": str(count)})
    exports = await connection.fetchval(
        """SELECT count(*) FROM knowledge_jobs j JOIN research_documents d
        ON d.home_knowledge_base_id=j.knowledge_base_id WHERE d.id=$1::uuid AND j.kind='export'
        AND j.status IN ('queued','running')""",
        document_id,
    )
    if exports:
        references.append({"type": "export", "count": str(exports)})
    return references


async def purge_due_documents(actor_id: str | None = None) -> list[str]:
    """Purge documents whose retention window has elapsed (worker entry).

    A document is only purged when: (a) ``purge_after`` has passed, (b) no
    research run / fact evidence / Wiki citation references it, and (c) the
    status guard flips it from 'trashed' to 'deleting' atomically — so a
    concurrent restore can never resurrect a purging row.
    Returns the purged document ids (for logging/audit).
    """
    pool = await get_document_pool()
    settings = get_document_settings()
    purged: list[str] = []
    async with pool.acquire() as connection:
        due = await connection.fetch(
            """SELECT id FROM research_documents
                WHERE status='trashed' AND purge_after < now()
                ORDER BY purge_after LIMIT 50"""
        )
        for row in due:
            document_id = str(row["id"])
            async with connection.transaction():
                references = await _reference_check(connection, document_id)
                if references:
                    await connection.execute(
                        """UPDATE research_documents
                              SET status='retained', updated_at=now()
                            WHERE id=$1::uuid AND status='trashed'""",
                        document_id,
                    )
                    continue
                flipped = await connection.fetchval(
                    """UPDATE research_documents
                          SET status='deleting', updated_at=now()
                        WHERE id=$1::uuid AND status='trashed'
                     RETURNING id""",
                    document_id,
                )
                if not flipped:
                    continue  # restored concurrently
            # 修复：收集 ALL 版本的 storage_key（含预览），不只当前。
            versions = await connection.fetch(
                """SELECT DISTINCT v.storage_key, v.id AS version_id
                     FROM research_document_versions v
                    WHERE v.document_id=$1::uuid""",
                document_id,
            )
            await connection.execute(
                "DELETE FROM research_documents WHERE id=$1::uuid", document_id
            )
            for version in versions:
                delete_storage_key(str(version["storage_key"]), settings)
                preview = (
                    settings.storage_dir / "previews" / f"{version['version_id']}.pdf"
                )
                if preview.is_file():
                    preview.unlink(missing_ok=True)
            purged.append(document_id)
    return purged

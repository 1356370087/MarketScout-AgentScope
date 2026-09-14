"""Owner-scoped persistence and durable ingestion jobs for local documents."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

from .contracts import (
    DocumentChunkView,
    DocumentStatus,
    DocumentSummary,
    SourceSelection,
)
from .database import get_document_pool
from .identity import document_owner_id
from .retrieval import locator_dict
from .settings import DocumentSettings
from .storage import StagedUpload


class DocumentConflictError(RuntimeError):
    """Raised when a document operation conflicts with durable state."""


class DocumentQuotaError(RuntimeError):
    """Raised when a per-owner document quota is exceeded."""


async def insert_first_version_and_generation(
    connection: asyncpg.Connection,
    *,
    document_id: str,
    staged: StagedUpload,
    storage_key: str,
) -> tuple[str, str]:
    """Create version 1 and its draft generation inside the upload transaction."""
    version_id = await connection.fetchval(
        """INSERT INTO research_document_versions
           (document_id, version_no, filename, media_type, size_bytes, sha256, storage_key)
           VALUES ($1::uuid, 1, $2, $3, $4, $5, $6) RETURNING id""",
        document_id,
        staged.filename,
        staged.media_type,
        staged.size_bytes,
        staged.sha256,
        storage_key,
    )
    generation_id = await connection.fetchval(
        """INSERT INTO research_document_generations(document_id, version_id)
           VALUES ($1::uuid, $2::uuid) RETURNING id""",
        document_id,
        version_id,
    )
    return str(version_id), str(generation_id)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _summary(row: asyncpg.Record) -> DocumentSummary:
    return DocumentSummary(
        id=str(row["id"]),
        filename=row["filename"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        status=DocumentStatus(row["status"]),
        failure_code=row["failure_code"],
        page_count=row["page_count"],
        chunk_count=row["chunk_count"],
        ocr_pages=row["ocr_pages"],
        created_at=_iso(row["created_at"]) or "",
        updated_at=_iso(row["updated_at"]) or "",
        deleted_at=_iso(row["deleted_at"]),
        current_generation_id=(
            str(row["current_generation_id"]) if "current_generation_id" in row.keys() and row["current_generation_id"] else None
        ),
    )


async def _home_knowledge_base(
    connection: asyncpg.Connection, owner_id: str, requested_kb_id: str | None
) -> tuple[str, str]:
    """Resolve the document's home knowledge base and its workspace.

    ``requested_kb_id`` must already be capability-checked by the caller;
    ``None`` falls back to the owner's personal default base (created on
    first use), matching plan §4.1: uploads default into the personal
    space's default library only.
    """
    workspace_id = await connection.fetchval(
        """INSERT INTO knowledge_workspaces(kind, name, created_by)
           VALUES ('personal', '个人空间', $1::uuid)
           ON CONFLICT DO NOTHING RETURNING id""",
        owner_id,
    )
    if workspace_id is None:
        workspace_id = await connection.fetchval(
            "SELECT id FROM knowledge_workspaces WHERE kind='personal' AND created_by=$1::uuid",
            owner_id,
        )
    await connection.execute(
        """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
           VALUES ($1::uuid, $2::uuid, 'owner') ON CONFLICT DO NOTHING""",
        workspace_id,
        owner_id,
    )
    kb_id = requested_kb_id
    if kb_id is None:
        kb_id = await connection.fetchval(
            """SELECT kb.id FROM knowledge_bases kb
                WHERE kb.workspace_id=$1::uuid ORDER BY kb.created_at, kb.id LIMIT 1""",
            workspace_id,
        )
        if kb_id is None:
            kb_id = await connection.fetchval(
                """INSERT INTO knowledge_bases
                     (owner_id, name, description, workspace_id, visibility, created_by)
                   VALUES ($1::uuid, '默认资料库', '', $2::uuid, 'team', $1::uuid)
                   RETURNING id""",
                owner_id,
                workspace_id,
            )
    else:
        kb_workspace = await connection.fetchval(
            "SELECT workspace_id FROM knowledge_bases WHERE id=$1::uuid", kb_id
        )
        if kb_workspace:
            workspace_id = kb_workspace
    return str(kb_id), str(workspace_id)


async def create_document(
    owner_id: str,
    staged: StagedUpload,
    storage_key: str,
    settings: DocumentSettings,
    *,
    knowledge_base_id: str | None = None,
) -> tuple[DocumentSummary, bool]:
    """Create an ingestion job or return the home base's existing document."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", owner_id)
        home_kb_id, workspace_id = await _home_knowledge_base(
            connection, owner_id, knowledge_base_id
        )
        existing = await connection.fetchrow(
            """SELECT * FROM research_documents
               WHERE home_knowledge_base_id=$1::uuid AND sha256=$2 AND deleted_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            home_kb_id,
            staged.sha256,
        )
        if existing:
            return _summary(existing), True
        usage = await connection.fetchrow(
            """SELECT count(*)::int AS count, coalesce(sum(size_bytes), 0)::bigint AS bytes
               FROM research_documents WHERE owner_id=$1::uuid AND deleted_at IS NULL""",
            owner_id,
        )
        if usage["count"] >= settings.max_documents_per_user:
            raise DocumentQuotaError("document_count_quota_exceeded")
        if usage["bytes"] + staged.size_bytes > settings.max_bytes_per_user:
            raise DocumentQuotaError("document_storage_quota_exceeded")
        row = await connection.fetchrow(
            """INSERT INTO research_documents
               (owner_id, filename, media_type, size_bytes, sha256, storage_key, status,
                workspace_id, home_knowledge_base_id, created_by)
               VALUES ($1::uuid,$2,$3,$4,$5,$6,'queued',$8::uuid,$7::uuid,$1::uuid)
               RETURNING *""",
            owner_id,
            staged.filename,
            staged.media_type,
            staged.size_bytes,
            staged.sha256,
            storage_key,
            home_kb_id,
            workspace_id,
        )
        await connection.execute(
            """INSERT INTO knowledge_document_links(knowledge_base_id, document_id)
               VALUES ($1::uuid, $2::uuid) ON CONFLICT DO NOTHING""",
            home_kb_id,
            row["id"],
        )
        await insert_first_version_and_generation(
            connection,
            document_id=str(row["id"]),
            staged=staged,
            storage_key=storage_key,
        )
        await connection.execute(
            "INSERT INTO research_document_jobs (document_id, kind, status) VALUES ($1,'ingest','queued')",
            row["id"],
        )
        await connection.execute(
            """INSERT INTO research_document_operations
               (owner_id, document_id, operation, actor_id, changes)
               VALUES ($1::uuid, $2::uuid, 'upload_version', $1::uuid,
                       $3::jsonb)""",
            owner_id,
            row["id"],
            json.dumps({"version_no": 1, "sha256": staged.sha256}, ensure_ascii=False),
        )
        return _summary(row), False


async def list_documents(
    owner_id: str,
    *,
    query: str = "",
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[DocumentSummary], int]:
    """List non-deleted owner documents with bounded filtering."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    clauses = ["owner_id=$1::uuid", "deleted_at IS NULL"]
    args: list[Any] = [owner_id]
    if query:
        args.append(f"%{query.strip()}%")
        clauses.append(f"filename ILIKE ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status=${len(args)}")
    where = " AND ".join(clauses)
    async with pool.acquire() as connection:
        total = await connection.fetchval(
            f"SELECT count(*) FROM research_documents WHERE {where}", *args
        )
        args.extend([limit, offset])
        rows = await connection.fetch(
            f"SELECT * FROM research_documents WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
            *args,
        )
    return [_summary(row) for row in rows], int(total)


async def get_document(
    owner_id: str, document_id: str, *, include_deleted: bool = False
) -> asyncpg.Record | None:
    """Fetch one document without leaking cross-owner existence."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    deleted = "" if include_deleted else " AND deleted_at IS NULL"
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            f"SELECT * FROM research_documents WHERE id=$1::uuid AND owner_id=$2::uuid{deleted}",
            document_id,
            owner_id,
        )


async def get_document_summary(
    owner_id: str, document_id: str
) -> DocumentSummary | None:
    """Return API-safe metadata for one non-deleted owner document."""
    row = await get_document(owner_id, document_id)
    return _summary(row) if row else None


async def get_chunk(
    owner_id: str, document_id: str, chunk_id: str
) -> DocumentChunkView | None:
    """Return one segment by id, including withdrawn history for old citations."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT s.*, g.document_id AS document_id
                 FROM research_document_segments s
                 JOIN research_document_generations g ON g.id=s.generation_id
                 JOIN research_documents d ON d.id=g.document_id
                WHERE s.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid""",
            chunk_id,
            document_id,
            owner_id,
        )
    if not row:
        return None
    locator = locator_dict(row["locator"])
    return DocumentChunkView(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        ordinal=int(row["ordinal"]),
        locator=str(locator.get("source") or ""),
        heading=locator.get("heading"),
        text=row["index_text"],
    )


async def list_chunks(
    owner_id: str, document_id: str, *, limit: int = 200, offset: int = 0
) -> list[DocumentChunkView] | None:
    """List the current generation's segments, or the newest when unpublished."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        owned = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM research_documents WHERE id=$1::uuid AND owner_id=$2::uuid)",
            document_id,
            owner_id,
        )
        if not owned:
            return None
        rows = await connection.fetch(
            """SELECT s.*, g.document_id AS document_id
                 FROM research_document_segments s
                 JOIN research_document_generations g ON g.id=s.generation_id
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.document_id=$1::uuid
                  AND s.generation_id=coalesce(
                        d.current_generation_id,
                        (SELECT newer.id FROM research_document_generations newer
                          WHERE newer.document_id=g.document_id
                          ORDER BY newer.created_at DESC LIMIT 1))
                ORDER BY s.ordinal LIMIT $2 OFFSET $3""",
            document_id,
            limit,
            offset,
        )
    return [
        DocumentChunkView(
            id=str(row["id"]),
            document_id=str(row["document_id"]),
            ordinal=int(row["ordinal"]),
            locator=locator_dict(row["locator"]).get("source") or "",
            heading=locator_dict(row["locator"]).get("heading"),
            text=row["index_text"],
        )
        for row in rows
    ]


async def retry_document(owner_id: str, document_id: str) -> DocumentSummary | None:
    """Queue a fresh ingestion attempt for one failed owner document."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """UPDATE research_documents SET status='queued', failure_code=NULL, updated_at=now()
               WHERE id=$1::uuid AND owner_id=$2::uuid AND deleted_at IS NULL AND status='failed'
               AND NOT EXISTS(
                 SELECT 1 FROM research_document_jobs j
                 WHERE j.document_id=research_documents.id
                   AND j.kind IN ('ingest','reindex')
                   AND j.status IN ('queued','running')
               )
               RETURNING *""",
            document_id,
            owner_id,
        )
        if not row:
            return None
        await connection.execute(
            """INSERT INTO research_document_jobs (document_id, kind, status)
               VALUES ($1,'ingest','queued')""",
            row["id"],
        )
    return _summary(row)


async def soft_delete_document(
    owner_id: str, document_id: str
) -> asyncpg.Record | None:
    """Move one document into the recycle bin (deferred purge, KB-15).

    The status guard flips to 'trashed' with a 30-day retention window;
    no physical cleanup job is queued here — the purge scheduler handles
    that separately after checking references.
    """
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """UPDATE research_documents
                  SET status='trashed', deleted_at=now(), deleted_by=$2::uuid,
                      purge_after=now() + make_interval(days => 30), updated_at=now()
                WHERE id=$1::uuid AND owner_id=$2::uuid AND deleted_at IS NULL
             RETURNING *""",
            document_id,
            owner_id,
        )
        return row


async def validate_selection(
    owner_id: str, selection: SourceSelection
) -> list[asyncpg.Record]:
    """Validate selected material atomically and preserve request order.

    Knowledge-base and collection references expand server-side into their
    linked documents (published generations bound by the same call), so a
    selection entry count limit never caps the expanded corpus (KB-03).
    """
    owner_id = document_owner_id(owner_id)
    document_ids = list(dict.fromkeys(selection.document_ids))
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if selection.knowledge_base_ids or selection.collection_ids:
            linked = await connection.fetch(
                """SELECT DISTINCT l.document_id
                     FROM knowledge_document_links l
                     JOIN knowledge_bases kb ON kb.id=l.knowledge_base_id
                    WHERE kb.owner_id=$1::uuid
                      AND (l.knowledge_base_id=ANY($2::uuid[])
                           OR l.collection_id=ANY($3::uuid[]))""",
                owner_id,
                selection.knowledge_base_ids,
                selection.collection_ids,
            )
            document_ids.extend(
                str(row["document_id"]) for row in linked
            )
            document_ids = list(dict.fromkeys(document_ids))
        if not document_ids:
            return []
        rows = await connection.fetch(
            """SELECT * FROM research_documents
               WHERE owner_id=$1::uuid AND id=ANY($2::uuid[]) AND deleted_at IS NULL""",
            owner_id,
            document_ids,
        )
    by_id = {str(row["id"]): row for row in rows}
    if any(document_id not in by_id for document_id in document_ids):
        raise KeyError("document_not_found")
    not_ready = [
        document_id
        for document_id in document_ids
        if by_id[document_id]["status"] != "ready"
    ]
    if not_ready:
        raise DocumentConflictError("document_not_ready:" + ",".join(not_ready))
    unpublished = [
        document_id
        for document_id in document_ids
        if not by_id[document_id]["current_generation_id"]
    ]
    if unpublished:
        raise DocumentConflictError("document_not_published:" + ",".join(unpublished))
    return [by_id[document_id] for document_id in document_ids]


async def run_source_document_ids(run_id: str) -> list[str]:
    """Return the document ids a run froze at creation (KB/collection refs)."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT document_id FROM research_run_sources WHERE run_id=$1", run_id
        )
    return [str(row["document_id"]) for row in rows]


async def bind_run_sources(
    run_id: str, owner_id: str, documents: Sequence[asyncpg.Record]
) -> None:
    """Persist immutable document and generation snapshots selected by one Run."""
    owner_id = document_owner_id(owner_id)
    if not documents:
        return
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.executemany(
            """INSERT INTO research_run_sources
               (run_id,owner_id,document_id,filename_snapshot,sha256_snapshot,generation_id)
               VALUES ($1,$2::uuid,$3,$4,$5,$6::uuid) ON CONFLICT DO NOTHING""",
            [
                (
                    run_id,
                    owner_id,
                    row["id"],
                    row["filename"],
                    row["sha256"],
                    row["current_generation_id"],
                )
                for row in documents
            ],
        )


async def release_run_sources(run_id: str) -> None:
    """Release document retention bindings owned by a purged Run."""
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        document_ids = await connection.fetch(
            "SELECT document_id FROM research_run_sources WHERE run_id=$1", run_id
        )
        await connection.execute(
            "DELETE FROM research_run_sources WHERE run_id=$1", run_id
        )
        for row in document_ids:
            await connection.execute(
                """INSERT INTO research_document_jobs(document_id,kind,status)
                   SELECT id,'delete','queued' FROM research_documents d
                   WHERE d.id=$1 AND d.deleted_at IS NOT NULL
                   AND NOT EXISTS(
                     SELECT 1 FROM research_document_jobs j WHERE j.document_id=d.id
                     AND j.kind='delete' AND j.status IN ('queued','running')
                   )""",
                row["document_id"],
            )


async def claim_job(worker_id: str, lease_seconds: int) -> asyncpg.Record | None:
    """Claim one eligible job with a renewable PostgreSQL lease."""
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """SELECT * FROM research_document_jobs
               WHERE (status='queued' AND available_at<=now())
                  OR (status='running' AND lease_expires_at<now())
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
        )
        if not row:
            return None
        claimed = await connection.fetchrow(
            """UPDATE research_document_jobs
               SET status='running', worker_id=$2, attempts=attempts+1,
                   lease_expires_at=now()+make_interval(secs=>$3), updated_at=now()
               WHERE id=$1 RETURNING *""",
            row["id"],
            worker_id,
            lease_seconds,
        )
        if claimed["kind"] in {"ingest", "reindex"}:
            await connection.execute(
                """UPDATE research_documents SET status='processing',updated_at=now()
                   WHERE id=$1 AND deleted_at IS NULL""",
                claimed["document_id"],
            )
        return claimed


async def heartbeat(worker_id: str) -> None:
    """Upsert one worker liveness timestamp."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO research_document_worker_heartbeats(worker_id,heartbeat_at)
               VALUES($1,now()) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at""",
            worker_id,
        )


async def renew_job_lease(
    job_id: str, worker_id: str, lease_seconds: int
) -> bool:
    """Renew a running job only while this worker still owns its lease."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        result = await connection.execute(
            """UPDATE research_document_jobs
               SET lease_expires_at=now()+make_interval(secs=>$3), updated_at=now()
               WHERE id=$1::uuid AND status='running' AND worker_id=$2""",
            job_id,
            worker_id,
            lease_seconds,
        )
    return result == "UPDATE 1"


async def record_job_upstream_task(job_id: str, worker_id: str, task_id: str) -> bool:
    """Persist the upstream conversion task while this worker owns the lease."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        result = await connection.execute(
            """UPDATE research_document_jobs
               SET upstream_task_id=$3, updated_at=now()
                WHERE id=$1::uuid AND status='running' AND worker_id=$2""",
            job_id,
            worker_id,
            task_id,
        )
    return result == "UPDATE 1"


async def load_job_document(job: asyncpg.Record) -> asyncpg.Record | None:
    """Load the document referenced by a claimed job."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            "SELECT * FROM research_documents WHERE id=$1", job["document_id"]
        )


async def complete_job(job_id: str, worker_id: str) -> bool:
    """Complete a job only if this worker still owns the active lease."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        result = await connection.execute(
            """UPDATE research_document_jobs
               SET status='completed', lease_expires_at=NULL, updated_at=now()
               WHERE id=$1::uuid AND status='running' AND worker_id=$2""",
            job_id,
            worker_id,
        )
    return result == "UPDATE 1"


async def fail_job(
    job: asyncpg.Record,
    failure_code: str,
    max_attempts: int,
    worker_id: str,
) -> bool:
    """Fail or reschedule a job only while this worker owns its lease."""
    pool = await get_document_pool()
    exhausted = int(job["attempts"]) >= max_attempts
    delay = min(300, 2 ** max(1, int(job["attempts"])))
    async with pool.acquire() as connection, connection.transaction():
        document_id = await connection.fetchval(
            """UPDATE research_document_jobs SET status=$2, error_code=$3,
               available_at=now()+make_interval(secs=>$4), lease_expires_at=NULL, updated_at=now()
               WHERE id=$1::uuid AND status='running' AND worker_id=$5
               RETURNING document_id""",
            job["id"],
            "failed" if exhausted else "queued",
            failure_code,
            delay,
            worker_id,
        )
        if document_id is None:
            return False
        await connection.execute(
            """UPDATE research_documents SET status=$2, failure_code=$3, updated_at=now()
               WHERE id=$1::uuid""",
            document_id,
            "failed" if exhausted else "queued",
            failure_code,
        )
    return True


async def delete_if_unreferenced(document_id: str) -> str | None:
    """Delete chunks and metadata only after every historical Run binding is gone."""
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            "SELECT * FROM research_documents WHERE id=$1::uuid FOR UPDATE", document_id
        )
        if not row:
            return None
        referenced = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM research_run_sources WHERE document_id=$1::uuid)",
            document_id,
        )
        if referenced:
            return None
        storage_key = row["storage_key"]
        await connection.execute(
            "DELETE FROM research_documents WHERE id=$1::uuid", document_id
        )
        shared = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM research_documents WHERE storage_key=$1)",
            storage_key,
        )
        return None if shared else storage_key

"""Owner-scoped persistence for knowledge bases, collections and links."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from .contracts import (
    KnowledgeBaseView,
    KnowledgeCollectionView,
    KnowledgeDocumentView,
)


class KnowledgeConflictError(RuntimeError):
    """Raised when a unique name constraint rejects the operation."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _kb_view(row: dict[str, Any]) -> KnowledgeBaseView:
    return KnowledgeBaseView(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        archived=row["archived_at"] is not None,
        collection_count=int(row["collection_count"]),
        document_count=int(row["document_count"]),
        created_at=_iso(row["created_at"]) or "",
        updated_at=_iso(row["updated_at"]) or "",
        archived_at=_iso(row["archived_at"]),
    )


def _collection_view(row: dict[str, Any]) -> KnowledgeCollectionView:
    return KnowledgeCollectionView(
        id=str(row["id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        name=row["name"],
        description=row["description"],
        document_count=int(row["document_count"]),
        created_at=_iso(row["created_at"]) or "",
        updated_at=_iso(row["updated_at"]) or "",
    )


_KB_COUNTS = """
  (SELECT count(*) FROM knowledge_collections c
    WHERE c.knowledge_base_id=kb.id) AS collection_count,
  (SELECT count(DISTINCT l.document_id) FROM knowledge_document_links l
    JOIN research_documents d ON d.id=l.document_id AND d.deleted_at IS NULL
    WHERE l.knowledge_base_id=kb.id) AS document_count
"""


async def _knowledge_base_owned(connection: asyncpg.Connection, owner_id: str, kb_id: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE id=$1::uuid AND owner_id=$2::uuid)",
            kb_id,
            owner_id,
        )
    )


async def create_knowledge_base(
    owner_id: str,
    name: str,
    description: str,
    *,
    workspace_id: str | None = None,
) -> KnowledgeBaseView:
    """Create one knowledge base inside a workspace (personal by default)."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if workspace_id is None:
            # 缺省落本人个人空间（方案 §4.1）。
            workspace_id = await connection.fetchval(
                """INSERT INTO knowledge_workspaces(kind, name, created_by)
                   VALUES ('personal', '个人空间', $1::uuid)
                   ON CONFLICT DO NOTHING RETURNING id""",
                owner_id,
            )
            if workspace_id is None:
                workspace_id = await connection.fetchval(
                    """SELECT id FROM knowledge_workspaces
                        WHERE kind='personal' AND created_by=$1::uuid""",
                    owner_id,
                )
            await connection.execute(
                """INSERT INTO knowledge_workspace_members(workspace_id, user_id, role)
                   VALUES ($1::uuid, $2::uuid, 'owner') ON CONFLICT DO NOTHING""",
                workspace_id,
                owner_id,
            )
        try:
            row = await connection.fetchrow(
                """INSERT INTO knowledge_bases
                     (owner_id, name, description, workspace_id, visibility, created_by)
                   VALUES ($1::uuid, $2, $3, $4::uuid, 'team', $1::uuid)
                   RETURNING *""",
                owner_id,
                name,
                description,
                workspace_id,
            )
        except asyncpg.UniqueViolationError as exc:
            raise KnowledgeConflictError("knowledge_base_name_conflict") from exc
    return _kb_view({**dict(row), "collection_count": 0, "document_count": 0})


async def list_knowledge_bases(
    owner_id: str,
    *,
    archived: bool | None = None,
    restrict_ids: list[str] | None = None,
) -> list[KnowledgeBaseView]:
    """List knowledge bases, optionally restricted to a readable id set."""
    owner_id = document_owner_id(owner_id)
    filter_clause = ""
    args: list[Any] = [owner_id]
    if restrict_ids is not None:
        if not restrict_ids:
            return []
        args.append(restrict_ids)
        filter_clause += f" AND kb.id=ANY(${len(args)}::uuid[])"
    if archived is not None:
        filter_clause = (
            " AND kb.archived_at IS NOT NULL" if archived else " AND kb.archived_at IS NULL"
        )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""SELECT kb.*, {_KB_COUNTS} FROM knowledge_bases kb
                WHERE kb.owner_id=$1::uuid{filter_clause}
                ORDER BY kb.archived_at IS NOT NULL, kb.updated_at DESC, kb.id""",
            *args,
        )
    return [_kb_view(dict(row)) for row in rows]


async def get_knowledge_base(owner_id: str, kb_id: str) -> KnowledgeBaseView | None:
    """Fetch one owned knowledge base, archived or not."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            f"""SELECT kb.*, {_KB_COUNTS} FROM knowledge_bases kb
                WHERE kb.id=$1::uuid AND kb.owner_id=$2::uuid""",
            kb_id,
            owner_id,
        )
    return _kb_view(dict(row)) if row else None


async def update_knowledge_base(
    owner_id: str,
    kb_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> KnowledgeBaseView | None:
    """Rename or re-describe one owned knowledge base."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        try:
            row = await connection.fetchrow(
                """UPDATE knowledge_bases kb SET
                     name=coalesce($3, kb.name),
                     description=coalesce($4, kb.description),
                     updated_at=now()
                 WHERE kb.id=$1::uuid AND kb.owner_id=$2::uuid
                 RETURNING kb.id""",
                kb_id,
                owner_id,
                name,
                description,
            )
        except asyncpg.UniqueViolationError as exc:
            raise KnowledgeConflictError("knowledge_base_name_conflict") from exc
    if not row:
        return None
    return await get_knowledge_base(owner_id, kb_id)


async def set_knowledge_base_archived(
    owner_id: str, kb_id: str, *, archived: bool
) -> KnowledgeBaseView | None:
    """Archive or restore one owned knowledge base."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """UPDATE knowledge_bases kb SET
                 archived_at=CASE WHEN $3 THEN now() ELSE NULL END, updated_at=now()
               WHERE kb.id=$1::uuid AND kb.owner_id=$2::uuid
               RETURNING kb.id""",
            kb_id,
            owner_id,
            archived,
        )
    if not row:
        return None
    return await get_knowledge_base(owner_id, kb_id)


async def create_collection(
    owner_id: str, kb_id: str, name: str, description: str
) -> KnowledgeCollectionView | None:
    """Create a collection inside one owned knowledge base."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        try:
            row = await connection.fetchrow(
                """INSERT INTO knowledge_collections(knowledge_base_id, name, description)
                   SELECT kb.id, $3, $4 FROM knowledge_bases kb
                    WHERE kb.id=$1::uuid AND kb.owner_id=$2::uuid
                   RETURNING *""",
                kb_id,
                owner_id,
                name,
                description,
            )
        except asyncpg.UniqueViolationError as exc:
            raise KnowledgeConflictError("knowledge_collection_name_conflict") from exc
    return _collection_view({**dict(row), "document_count": 0}) if row else None


async def list_collections(
    owner_id: str, kb_id: str
) -> list[KnowledgeCollectionView] | None:
    """List collections of one owned knowledge base, or None when not owned."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if not await _knowledge_base_owned(connection, owner_id, kb_id):
            return None
        rows = await connection.fetch(
            """SELECT c.*, (SELECT count(DISTINCT l.document_id)
                             FROM knowledge_document_links l
                             JOIN research_documents d ON d.id=l.document_id
                                  AND d.deleted_at IS NULL
                            WHERE l.collection_id=c.id) AS document_count
                 FROM knowledge_collections c
                WHERE c.knowledge_base_id=$1::uuid
                ORDER BY c.created_at, c.id""",
            kb_id,
        )
    return [_collection_view(dict(row)) for row in rows]


async def update_collection(
    owner_id: str,
    kb_id: str,
    collection_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> KnowledgeCollectionView | None:
    """Rename or re-describe one collection in an owned knowledge base."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        try:
            row = await connection.fetchrow(
                """UPDATE knowledge_collections c SET
                     name=coalesce($4, c.name),
                     description=coalesce($5, c.description),
                     updated_at=now()
                 FROM knowledge_bases kb
                WHERE c.id=$1::uuid AND c.knowledge_base_id=$2::uuid
                  AND kb.id=c.knowledge_base_id AND kb.owner_id=$3::uuid
                RETURNING c.id""",
                collection_id,
                kb_id,
                owner_id,
                name,
                description,
            )
        except asyncpg.UniqueViolationError as exc:
            raise KnowledgeConflictError("knowledge_collection_name_conflict") from exc
    if not row:
        return None
    collections = await list_collections(owner_id, kb_id)
    return next(
        (item for item in collections or [] if item.id == str(collection_id)), None
    )


async def delete_collection(owner_id: str, kb_id: str, collection_id: str) -> bool:
    """Remove one collection and its associations; documents are never deleted."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """DELETE FROM knowledge_collections c
                 USING knowledge_bases kb
                WHERE c.id=$1::uuid AND c.knowledge_base_id=$2::uuid
                  AND kb.id=c.knowledge_base_id AND kb.owner_id=$3::uuid
                RETURNING c.id""",
            collection_id,
            kb_id,
            owner_id,
        )
    return row is not None


async def link_documents(
    owner_id: str,
    kb_id: str,
    document_ids: list[str],
    *,
    collection_id: str | None = None,
) -> dict[str, int] | None:
    """Associate owned documents with a knowledge base (and optional collection).

    Returns ``None`` when the knowledge base is not owned. An unknown
    collection raises ``KeyError("collection_not_found")``; unknown or
    cross-owner documents raise ``KeyError`` without writing anything.
    """
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        if not await _knowledge_base_owned(connection, owner_id, kb_id):
            return None
        if collection_id is not None:
            collection_ok = await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM knowledge_collections
                    WHERE id=$1::uuid AND knowledge_base_id=$2::uuid)""",
                collection_id,
                kb_id,
            )
            if not collection_ok:
                raise KeyError("collection_not_found")
        found = await connection.fetch(
            """SELECT id FROM research_documents
                WHERE owner_id=$1::uuid AND id=ANY($2::uuid[]) AND deleted_at IS NULL""",
            owner_id,
            document_ids,
        )
        found_ids = {str(row["id"]) for row in found}
        missing = [item for item in document_ids if item not in found_ids]
        if missing:
            raise KeyError("document_not_found:" + ",".join(missing))
        inserted = await connection.fetch(
            """INSERT INTO knowledge_document_links(knowledge_base_id, collection_id, document_id)
               SELECT $1::uuid, $2::uuid, unnest($3::uuid[])
               ON CONFLICT DO NOTHING
               RETURNING id""",
            kb_id,
            collection_id,
            document_ids,
        )
    return {"requested": len(document_ids), "linked": len(inserted)}


async def unlink_document(
    owner_id: str, kb_id: str, document_id: str, *, collection_id: str | None = None
) -> int | None:
    """Remove one document's association(s); returns removed link count."""
    owner_id = document_owner_id(owner_id)
    collection_clause = (
        " AND l.collection_id=$4::uuid" if collection_id is not None else ""
    )
    args: list[Any] = [kb_id, owner_id, document_id]
    if collection_id is not None:
        args.append(collection_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        removed = await connection.fetch(
            f"""DELETE FROM knowledge_document_links l
                 USING knowledge_bases kb
                WHERE l.knowledge_base_id=$1::uuid AND l.document_id=$3::uuid
                  AND kb.id=l.knowledge_base_id AND kb.owner_id=$2::uuid
                  {collection_clause}
                RETURNING l.id""",
            *args,
        )
        if not removed:
            return 0 if await _knowledge_base_owned(connection, owner_id, kb_id) else None
    return len(removed)


async def list_knowledge_documents(
    owner_id: str,
    kb_id: str,
    *,
    collection_id: str | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[KnowledgeDocumentView], int] | None:
    """List documents in one owned knowledge base or one of its collections."""
    owner_id = document_owner_id(owner_id)
    scope_args: list[Any] = [kb_id, owner_id]
    scope_clause = ""
    if collection_id is not None:
        scope_args.append(collection_id)
        scope_clause = f" AND l.collection_id=${len(scope_args)}::uuid"
    if query:
        scope_args.append(f"%{query.strip()}%")
        scope_clause += f" AND d.filename ILIKE ${len(scope_args)}"
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        if not await _knowledge_base_owned(connection, owner_id, kb_id):
            return None
        total = await connection.fetchval(
            f"""SELECT count(DISTINCT d.id) FROM knowledge_document_links l
                 JOIN research_documents d ON d.id=l.document_id AND d.deleted_at IS NULL
                 JOIN knowledge_bases kb ON kb.id=l.knowledge_base_id
                    AND kb.owner_id=$2::uuid
                WHERE l.knowledge_base_id=$1::uuid{scope_clause}""",
            *scope_args,
        )
        rows = await connection.fetch(
            f"""SELECT DISTINCT ON (d.id) d.*, l.added_at
                  FROM knowledge_document_links l
                  JOIN research_documents d ON d.id=l.document_id AND d.deleted_at IS NULL
                  JOIN knowledge_bases kb ON kb.id=l.knowledge_base_id
                     AND kb.owner_id=$2::uuid
                 WHERE l.knowledge_base_id=$1::uuid{scope_clause}
                 ORDER BY d.id, l.added_at DESC, l.id DESC""",
            *scope_args,
        )
    ordered = sorted(
        rows, key=lambda row: (row["added_at"], str(row["id"])), reverse=True
    )
    views = [
        KnowledgeDocumentView(
            id=str(row["id"]),
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            status=row["status"],
            chunk_count=int(row["chunk_count"]),
            added_at=_iso(row["added_at"]) or "",
        )
        for row in ordered[offset : offset + limit]
    ]
    return views, int(total)

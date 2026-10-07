"""Artifact-version and parse-generation lifecycle with human publishing.

Implements plan §2.2/§2.3: ingestion state stays on the document row while
``draft → pending_review → published`` (+ ``rejected``/``withdrawn``) lives on
generations. Only the generation named by ``research_documents
.current_generation_id`` is served to retrieval; old segments stay immutable
so historical references keep resolving.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from .chunking import DocumentChunk
from .database import get_document_pool
from .embeddings import vector_literal
from .identity import document_owner_id
from .repository import DocumentConflictError
from .settings import get_document_settings
from .storage import StagedUpload

_INGESTIBLE_STATUSES = ("draft", "pending_review")


class GenerationNotFoundError(LookupError):
    """Raised when a generation or version does not exist for this owner."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _version_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "version_no": int(row["version_no"]),
        "filename": row["filename"],
        "media_type": row["media_type"],
        "size_bytes": int(row["size_bytes"]),
        "sha256": row["sha256"],
        "note": row["note"],
        "uploaded_at": _iso(row["uploaded_at"]) or "",
    }


def _generation_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "version_id": str(row["version_id"]),
        "status": row["status"],
        "revision": int(row.get("revision") or 0),
        "created_at": _iso(row["created_at"]) or "",
        "updated_at": _iso(row["updated_at"]) or "",
        "published_at": _iso(row["published_at"]),
        "review_note": row["review_note"],
        "is_current": bool(row.get("is_current")),
    }


async def _record_operation(
    connection: asyncpg.Connection,
    *,
    owner_id: str,
    operation: str,
    document_id: str | None = None,
    generation_id: str | None = None,
    changes: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    await connection.execute(
        """INSERT INTO research_document_operations
           (owner_id, document_id, generation_id, operation, actor_id, reason, changes)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $1::uuid, $5, $6::jsonb)""",
        owner_id,
        document_id,
        generation_id,
        operation,
        reason,
        _json_literal(changes or {}),
    )


def _json_literal(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def add_document_version(
    owner_id: str,
    document_id: str,
    staged: StagedUpload,
    storage_key: str,
    *,
    note: str = "",
) -> dict[str, Any] | None:
    """Upload a new artifact version for one owned logical document.

    Returns ``{"version": ..., "generation": ...}`` or ``None`` when the
    document is missing, foreign or deleted.
    """
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        document = await connection.fetchrow(
            """SELECT * FROM research_documents
                WHERE id=$1::uuid AND owner_id=$2::uuid AND deleted_at IS NULL
                FOR UPDATE""",
            document_id,
            owner_id,
        )
        if not document:
            return None
        active = await connection.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM research_document_jobs
                  WHERE document_id=$1::uuid
                    AND kind IN ('ingest','reindex')
                    AND status IN ('queued','running'))""",
            document_id,
        )
        if active:
            raise DocumentConflictError("document_version_upload_in_progress")
        version_id, version_no, generation_id = await _insert_version(
            connection,
            document_id=document_id,
            staged=staged,
            storage_key=storage_key,
            note=note,
        )
        await connection.execute(
            """UPDATE research_documents
               SET filename=$2, media_type=$3, size_bytes=$4, sha256=$5, storage_key=$6,
                   status='queued', failure_code=NULL, updated_at=now()
               WHERE id=$1::uuid""",
            document_id,
            staged.filename,
            staged.media_type,
            staged.size_bytes,
            staged.sha256,
            storage_key,
        )
        await connection.execute(
            "INSERT INTO research_document_jobs(document_id, kind, status) VALUES ($1::uuid,'ingest','queued')",
            document_id,
        )
        await _record_operation(
            connection,
            owner_id=owner_id,
            operation="upload_version",
            document_id=document_id,
            generation_id=generation_id,
            changes={"version_no": version_no, "sha256": staged.sha256},
        )
        return {
            "version": {"id": version_id, "version_no": version_no},
            "generation": {"id": generation_id, "status": "draft"},
        }


async def queue_reindex_generation(owner_id: str, document_id: str) -> str | None:
    """Queue a fresh draft generation for the current published version."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        document = await connection.fetchrow(
            """SELECT * FROM research_documents
                WHERE id=$1::uuid AND owner_id=$2::uuid AND deleted_at IS NULL
                FOR UPDATE""",
            document_id,
            owner_id,
        )
        if not document:
            return None
        active = await connection.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM research_document_jobs
                  WHERE document_id=$1::uuid
                    AND kind IN ('ingest','reindex')
                    AND status IN ('queued','running'))""",
            document_id,
        )
        if active:
            raise DocumentConflictError("document_reindex_in_progress")
        if document["current_generation_id"]:
            version_id = await connection.fetchval(
                "SELECT version_id FROM research_document_generations WHERE id=$1::uuid",
                document["current_generation_id"],
            )
        else:
            version_id = await connection.fetchval(
                """SELECT v.id FROM research_document_versions v
                    WHERE v.document_id=$1::uuid ORDER BY v.version_no DESC LIMIT 1""",
                document_id,
            )
        if not version_id:
            raise DocumentConflictError("document_has_no_artifact_version")
        generation_id = await connection.fetchval(
            """INSERT INTO research_document_generations(document_id, version_id)
               VALUES ($1::uuid, $2::uuid) RETURNING id""",
            document_id,
            version_id,
        )
        await connection.execute(
            """UPDATE research_documents SET status='queued', failure_code=NULL, updated_at=now()
               WHERE id=$1::uuid""",
            document_id,
        )
        await connection.execute(
            "INSERT INTO research_document_jobs(document_id, kind, status) VALUES ($1::uuid,'reindex','queued')",
            document_id,
        )
        await _record_operation(
            connection,
            owner_id=owner_id,
            operation="queue_reparse",
            document_id=document_id,
            generation_id=str(generation_id),
            changes={"version_id": str(version_id)},
        )
        return str(generation_id)


async def _insert_version(
    connection: asyncpg.Connection,
    *,
    document_id: str,
    staged: StagedUpload,
    storage_key: str,
    note: str,
) -> tuple[str, int, str]:
    version_no = await connection.fetchval(
        """SELECT coalesce(max(version_no), 0) + 1 FROM research_document_versions
            WHERE document_id=$1::uuid""",
        document_id,
    )
    version_id = await connection.fetchval(
        """INSERT INTO research_document_versions
           (document_id, version_no, filename, media_type, size_bytes, sha256, storage_key, note)
           VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
        document_id,
        int(version_no),
        staged.filename,
        staged.media_type,
        staged.size_bytes,
        staged.sha256,
        storage_key,
        note,
    )
    generation_id = await connection.fetchval(
        """INSERT INTO research_document_generations(document_id, version_id)
           VALUES ($1::uuid, $2::uuid) RETURNING id""",
        document_id,
        version_id,
    )
    return str(version_id), int(version_no), str(generation_id)


async def latest_draft_generation(document_id: str) -> dict[str, Any] | None:
    """Return the document's newest draft generation for the worker to fill."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT * FROM research_document_generations
                WHERE document_id=$1::uuid AND status='draft'
                ORDER BY created_at DESC, id DESC LIMIT 1""",
            document_id,
        )
    return dict(row) if row else None


async def complete_generation(
    generation_id: str,
    chunks: list[DocumentChunk],
    vectors: list[list[float]],
    *,
    embedding_model: str,
    page_count: int,
    ocr_pages: int,
) -> None:
    """Compatibility path: persist flat chunks as one paragraph unit each."""
    from .structuring import PreparedDocument, StructuredUnit

    units = [
        StructuredUnit(
            unit_type="paragraph",
            locator={"source": chunk.locator, "heading": (chunk.heading or "").strip() or None},
            raw_text=chunk.text,
            index_text=(
                f"{(chunk.heading or '').strip()}\n{chunk.text}".strip()
                if chunk.heading
                else chunk.text
            ),
            attributes={"parse_method": "legacy"},
        )
        for chunk in chunks
    ]
    prepared = PreparedDocument(
        units=units,
        page_count=page_count,
        ocr_pages=ocr_pages,
        parse_method="legacy",
    )
    prepared.segment_texts = [chunk.text for chunk in chunks]
    prepared.segment_units = list(range(len(chunks)))
    await complete_generation_rich(
        generation_id,
        prepared,
        vectors,
        embedding_model=embedding_model,
        content_hashes=[chunk.content_hash for chunk in chunks],
    )


async def complete_generation_rich(
    generation_id: str,
    prepared,
    vectors: list[list[float]],
    *,
    embedding_model: str,
    content_hashes: list[str] | None = None,
    metadata_suggestions: dict[str, Any] | None = None,
    index_profile: dict[str, Any] | None = None,
) -> None:
    """Persist structured units and row-group segments, then mark it reviewable.

    Replaces any partial write for the same generation so a retry after a
    lost lease stays idempotent; the document's legacy chunk table is never
    touched. ``prepared.segment_texts``/``segment_units`` (from
    :func:`structuring.plan_segments`) pair positionally with ``vectors``.
    """
    from .structuring import plan_segments as _plan_segments

    if not prepared.segment_texts:
        prepared = _plan_segments(prepared, get_document_settings())
    segment_texts = prepared.segment_texts
    segment_units = prepared.segment_units
    if len(segment_texts) != len(vectors):
        raise ValueError("segment_vector_count_mismatch")
    hashes = content_hashes or [
        hashlib.sha256(text.encode("utf-8")).hexdigest() for text in segment_texts
    ]
    unit_rows: list[tuple[Any, ...]] = []
    for ordinal, unit in enumerate(prepared.units):
        unit_rows.append(
            (
                str(uuid5(NAMESPACE_URL, f"insightforge:unit:{generation_id}:{ordinal}")),
                generation_id,
                ordinal,
                unit.unit_type,
                _json_literal(unit.locator),
                unit.raw_text,
                None,  # revised_text is human-authored (phase 4 corrections)
                unit.index_text,
                _json_literal(unit.attributes),
            )
        )
    unit_ids = [row[0] for row in unit_rows]
    segment_rows: list[tuple[Any, ...]] = []
    for ordinal, (text, unit_index, vector) in enumerate(
        zip(segment_texts, segment_units, vectors, strict=True)
    ):
        segment_rows.append(
            (
                str(uuid5(NAMESPACE_URL, f"insightforge:segment:{generation_id}:{ordinal}")),
                generation_id,
                unit_ids[unit_index],
                ordinal,
                text,
                _json_literal(prepared.units[unit_index].locator),
                vector_literal(vector),
                embedding_model,
                hashes[ordinal],
            )
        )
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "DELETE FROM research_document_units WHERE generation_id=$1::uuid",
            generation_id,
        )
        if unit_rows:
            await connection.executemany(
                """INSERT INTO research_document_units
                   (id, generation_id, ordinal, unit_type, locator, raw_text, revised_text,
                    index_text, attributes)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb)""",
                unit_rows,
            )
        await connection.executemany(
            """INSERT INTO research_document_segments
               (id, generation_id, unit_id, ordinal, index_text, locator,
                embedding, embedding_model, content_hash)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                       $7::vector, $8, $9)""",
            segment_rows,
        )
        document_id = await connection.fetchval(
            """UPDATE research_document_generations
               SET status='pending_review', updated_at=now(),
                   metadata_snapshot=$2::jsonb,
                   quality_report=quality_report || $3::jsonb,
                   index_profile=$4::jsonb
                WHERE id=$1::uuid RETURNING document_id""",
            generation_id,
            _json_literal(
                {
                    "suggested": metadata_suggestions or {},
                    "confirmed": {},
                }
            ),
            _json_literal(
                {
                    "flags": prepared.quality_flags,
                    "parse_method": prepared.parse_method,
                }
            ),
            _json_literal({**(index_profile or get_document_settings().index_profile), "model": embedding_model,
                           "dimensions": len(vectors[0]) if vectors else get_document_settings().embedding_dimensions}),
        )
        if document_id is None:
            raise GenerationNotFoundError(f"generation_not_found:{generation_id}")
        await connection.execute(
            """UPDATE research_documents
               SET status='ready', failure_code=NULL, page_count=$2,
                   chunk_count=$3, ocr_pages=$4, updated_at=now()
               WHERE id=$1::uuid""",
            document_id,
            prepared.page_count,
            len(segment_rows),
            prepared.ocr_pages,
        )


async def fail_generation_attempt(document_id: str, failure_code: str) -> None:
    """Mirror worker failure onto the newest draft generation for diagnostics."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            """UPDATE research_document_generations
               SET quality_report=quality_report || jsonb_build_object('last_failure', $2::text),
                   updated_at=now()
               WHERE id=(
                 SELECT id FROM research_document_generations
                  WHERE document_id=$1::uuid AND status='draft'
                  ORDER BY created_at DESC LIMIT 1)""",
            document_id,
            failure_code,
        )


async def publish_generation(
    owner_id: str, document_id: str, generation_id: str
) -> dict[str, Any] | None:
    """Publish one reviewable generation and move the document pointer.

    Publishing the already-current generation is an idempotent no-op;
    rejected or withdrawn generations are refused with a conflict. A new
    publish requires confirmed metadata (plan §2.3) — fields without a
    reliable value must be confirmed as unknown explicitly.
    """
    from .retrieval import locator_dict

    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        state = await connection.fetchrow(
            """SELECT g.id AS generation_id, g.status, g.version_id,
                      g.metadata_snapshot, d.current_generation_id, d.owner_id
                 FROM research_document_generations g
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid
                FOR UPDATE OF d""",
            generation_id,
            document_id,
            owner_id,
        )
        if not state:
            return None
        already_current = (
            state["status"] == "published"
            and state["current_generation_id"]
            and str(state["current_generation_id"]) == str(generation_id)
        )
        if not already_current:
            metadata = locator_dict(state["metadata_snapshot"])
            if not (metadata.get("confirmed") or {}):
                raise DocumentConflictError("metadata_not_confirmed")
        if state["status"] in {"rejected", "withdrawn"}:
            raise DocumentConflictError("generation_not_publishable")
        previous = str(state["current_generation_id"]) if state["current_generation_id"] else None
        if state["status"] != "published":
            await connection.execute(
                """UPDATE research_document_generations
                   SET status='published', published_at=now(), updated_at=now()
                 WHERE id=$1::uuid""",
                generation_id,
            )
        if previous != str(generation_id):
            await connection.execute(
                """UPDATE research_documents
                   SET current_generation_id=$2::uuid, updated_at=now()
                 WHERE id=$1::uuid""",
                document_id,
                generation_id,
            )
            await _record_operation(
                connection,
                owner_id=owner_id,
                operation="set_current" if state["status"] == "published" else "publish",
                document_id=document_id,
                generation_id=generation_id,
                changes={"previous_current": previous, "current": str(generation_id)},
            )
        from open_deep_research.knowledge.maintenance import enqueue
        kb_id = await connection.fetchval('SELECT home_knowledge_base_id FROM research_documents WHERE id=$1::uuid', document_id)
        if kb_id:
            await enqueue(connection, str(kb_id), owner_id, 'extract_facts',
                          {'document_id':str(document_id),'generation_id':str(generation_id)},
                          f'facts:{generation_id}:v1')
    return await generation_detail(owner_id, document_id, generation_id)


async def reject_generation(
    owner_id: str, document_id: str, generation_id: str, *, reason: str
) -> dict[str, Any] | None:
    """Reject a not-yet-published generation; already published ones conflict."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        state = await connection.fetchrow(
            """SELECT g.status FROM research_document_generations g
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid""",
            generation_id,
            document_id,
            owner_id,
        )
        if not state:
            return None
        if state["status"] not in _INGESTIBLE_STATUSES:
            raise DocumentConflictError("generation_not_rejectable")
        await connection.execute(
            """UPDATE research_document_generations
               SET status='rejected', review_note=$2, updated_at=now()
             WHERE id=$1::uuid""",
            generation_id,
            reason,
        )
        await _record_operation(
            connection,
            owner_id=owner_id,
            operation="reject",
            document_id=document_id,
            generation_id=generation_id,
            changes={"status": state["status"], "to": "rejected"},
            reason=reason,
        )
    return await generation_detail(owner_id, document_id, generation_id)


async def withdraw_version(
    owner_id: str, document_id: str, version_id: str, *, reason: str = ""
) -> dict[str, Any] | None:
    """Withdraw a version's published generation and stop new retrieval.

    Historical segment reads keep working; the current pointer is cleared
    only when it actually named the withdrawn generation.
    """
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        state = await connection.fetchrow(
            """SELECT g.id AS generation_id, d.current_generation_id
                 FROM research_document_versions v
                 JOIN research_document_generations g
                      ON g.version_id=v.id AND g.status='published'
                 JOIN research_documents d ON d.id=v.document_id AND d.owner_id=$3::uuid
                WHERE v.id=$1::uuid AND v.document_id=$2::uuid
                ORDER BY g.published_at DESC NULLS LAST, g.created_at DESC
                LIMIT 1
                FOR UPDATE OF d""",
            version_id,
            document_id,
            owner_id,
        )
        if not state:
            return None
        generation_id = str(state["generation_id"])
        await connection.execute(
            """UPDATE research_document_generations
               SET status='withdrawn', review_note=coalesce(nullif($2,''), review_note), updated_at=now()
             WHERE id=$1::uuid""",
            generation_id,
            reason,
        )
        cleared_pointer = False
        if state["current_generation_id"] and str(state["current_generation_id"]) == generation_id:
            await connection.execute(
                """UPDATE research_documents
                   SET current_generation_id=NULL, updated_at=now()
                 WHERE id=$1::uuid""",
                document_id,
            )
            cleared_pointer = True
        await _record_operation(
            connection,
            owner_id=owner_id,
            operation="withdraw",
            document_id=document_id,
            generation_id=generation_id,
            changes={"cleared_current_pointer": cleared_pointer},
            reason=reason,
        )
    return {
        "version_id": version_id,
        "generation_id": generation_id,
        "status": "withdrawn",
        "cleared_current_pointer": cleared_pointer,
    }


async def generation_detail(
    owner_id: str, document_id: str, generation_id: str
) -> dict[str, Any] | None:
    """Return one owner-scoped generation view with its currency flag."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """SELECT g.*, (d.current_generation_id=g.id) AS is_current
                 FROM research_document_generations g
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid""",
            generation_id,
            document_id,
            owner_id,
        )
    return _generation_view(dict(row)) if row else None


async def list_generations(owner_id: str, document_id: str) -> list[dict[str, Any]] | None:
    """List generations of one owned document, newest first."""
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
            """SELECT g.*, (d.current_generation_id=g.id) AS is_current,
                      (SELECT count(*) FROM research_document_units u
                        WHERE u.generation_id=g.id AND NOT u.excluded) AS unit_count,
                      (SELECT count(*) FROM research_document_segments s
                        WHERE s.generation_id=g.id) AS segment_count
                 FROM research_document_generations g
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.document_id=$1::uuid
                ORDER BY g.created_at DESC, g.id DESC""",
            document_id,
        )
    views = []
    for row in rows:
        view = _generation_view(dict(row))
        view["unit_count"] = int(row["unit_count"])
        view["segment_count"] = int(row["segment_count"])
        views.append(view)
    return views


async def generation_review_detail(
    owner_id: str,
    document_id: str,
    generation_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Return one reviewable generation with paginated units for the bench."""
    from .retrieval import locator_dict

    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        state = await connection.fetchrow(
            """SELECT g.*, (d.current_generation_id=g.id) AS is_current,
                      (SELECT count(*) FROM research_document_units u
                        WHERE u.generation_id=g.id) AS unit_total
                 FROM research_document_generations g
                 JOIN research_documents d ON d.id=g.document_id
                WHERE g.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid""",
            generation_id,
            document_id,
            owner_id,
        )
        if not state:
            return None
        unit_rows = await connection.fetch(
            """SELECT id, ordinal, unit_type, locator, raw_text, revised_text,
                      index_text, attributes, excluded, exclusion_reason
                 FROM research_document_units
                WHERE generation_id=$1::uuid
                ORDER BY ordinal LIMIT $2 OFFSET $3""",
            generation_id,
            limit,
            offset,
        )
    units = []
    for row in unit_rows:
        units.append(
            {
                "id": str(row["id"]),
                "ordinal": int(row["ordinal"]),
                "unit_type": row["unit_type"],
                "locator": locator_dict(row["locator"]),
                "raw_text": row["raw_text"],
                "revised_text": row["revised_text"],
                "index_text": row["index_text"],
                "attributes": locator_dict(row["attributes"]),
                "excluded": bool(row["excluded"]),
                "exclusion_reason": row["exclusion_reason"],
            }
        )
    view = _generation_view(
        {
            **dict(state),
            "unit_count": int(state["unit_total"]),
            "segment_count": 0,
        }
    )
    view["unit_total"] = int(state["unit_total"])
    view["units"] = units
    view["metadata"] = locator_dict(state["metadata_snapshot"])
    view["quality_report"] = locator_dict(state["quality_report"])
    return view


async def list_versions(owner_id: str, document_id: str) -> list[dict[str, Any]] | None:
    """List artifact versions with their newest generation status."""
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
            """SELECT v.*,
                      (SELECT g.status FROM research_document_generations g
                        WHERE g.version_id=v.id
                        ORDER BY g.created_at DESC LIMIT 1) AS generation_status,
                      (SELECT g.id FROM research_document_generations g
                        WHERE g.version_id=v.id AND g.status='published'
                        ORDER BY g.published_at DESC LIMIT 1) AS published_generation_id
                 FROM research_document_versions v
                WHERE v.document_id=$1::uuid
                ORDER BY v.version_no DESC""",
            document_id,
        )
    views = []
    for row in rows:
        view = _version_view(dict(row))
        view["generation_status"] = row["generation_status"]
        view["published_generation_id"] = (
            str(row["published_generation_id"]) if row["published_generation_id"] else None
        )
        views.append(view)
    return views


async def diff_generations(
    owner_id: str,
    document_id: str,
    generation_id: str,
    *,
    against_generation_id: str | None = None,
) -> dict[str, Any] | None:
    """Compare one reviewable generation against the published baseline.

    The baseline defaults to the document's current published generation.
    Units align on their locator source; text changes are summarized with
    line-level counts so the review bench can render a complete pre-publish
    difference (plan §2.3 / §KB-07).
    """
    import difflib

    from .retrieval import locator_dict

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
        target = await connection.fetchrow(
            "SELECT * FROM research_document_generations WHERE id=$1::uuid AND document_id=$2::uuid",
            generation_id,
            document_id,
        )
        if not target:
            return None
        base = None
        if against_generation_id:
            base = await connection.fetchrow(
                "SELECT * FROM research_document_generations WHERE id=$1::uuid AND document_id=$2::uuid",
                against_generation_id,
                document_id,
            )
            if not base:
                raise GenerationNotFoundError(f"generation_not_found:{against_generation_id}")
        else:
            base = await connection.fetchrow(
                """SELECT g.* FROM research_document_generations g
                    JOIN research_documents d ON d.current_generation_id=g.id
                   WHERE d.id=$1::uuid""",
                document_id,
            )

        def locator_source(row: dict[str, Any]) -> str:
            locator = locator_dict(row["locator"])
            return str(locator.get("source") or row["id"])

        async def load_units(generation_row) -> dict[str, dict[str, Any]]:
            rows = await connection.fetch(
                """SELECT * FROM research_document_units
                    WHERE generation_id=$1::uuid ORDER BY ordinal""",
                generation_row["id"],
            )
            return {locator_source(dict(row)): dict(row) for row in rows}

        def unit_view(row: dict[str, Any]) -> dict[str, Any]:
            revised = row["revised_text"]
            return {
                "unit_id": str(row["id"]),
                "source": locator_source(row),
                "unit_type": row["unit_type"],
                "text": revised if revised not in (None, "") else row["raw_text"],
                "excluded": bool(row["excluded"]),
            }

        target_units = await load_units(target)
        base_units = await load_units(base) if base else {}

        base_keys = {locator_source(row): row for row in base_units.values()}
        target_keys = {locator_source(row): row for row in target_units.values()}
        added = [unit_view(row) for key, row in target_keys.items() if key not in base_keys]
        removed = [unit_view(row) for key, row in base_keys.items() if key not in target_keys]
        changed = []
        for key, row in target_keys.items():
            before = base_keys.get(key)
            if not before:
                continue
            before_view, after_view = unit_view(before), unit_view(row)
            if before_view["text"] != after_view["text"] or before_view["excluded"] != after_view["excluded"]:
                matcher = difflib.SequenceMatcher(
                    None, before_view["text"].split("\n"), after_view["text"].split("\n")
                )
                changed.append(
                    {
                        **after_view,
                        "before": before_view["text"],
                        "lines_added": sum(
                            tag in {"insert", "replace"} and (j2 - j1) for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
                        ),
                        "lines_removed": sum(
                            tag in {"delete", "replace"} and (i2 - i1) for tag, i1, i2, _j1, _j2 in matcher.get_opcodes()
                        ),
                    }
                )

        target_metadata = locator_dict(target["metadata_snapshot"])
        base_metadata = locator_dict(base["metadata_snapshot"]) if base else {}
        fields = sorted(
            set(target_metadata.get("suggested") or {})
            | set(target_metadata.get("confirmed") or {})
            | set(base_metadata.get("confirmed") or {})
        )
        metadata_diff = {
            field: {
                "suggested": (target_metadata.get("suggested") or {}).get(field, {}).get("value")
                if isinstance((target_metadata.get("suggested") or {}).get(field), dict)
                else (target_metadata.get("suggested") or {}).get(field),
                "confirmed": (target_metadata.get("confirmed") or {}).get(field),
                "published": (base_metadata.get("confirmed") or {}).get(field),
            }
            for field in fields
        }
    return {
        "target": _generation_view(dict(target)),
        "base": _generation_view(dict(base)) if base else None,
        "units": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "excluded_now": [
                unit_view(row) for row in target_units.values() if row["excluded"]
            ],
        },
        "metadata": metadata_diff,
    }

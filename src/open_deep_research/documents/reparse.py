"""Scoped re-parse: publish a draft from a published source, replace pages.

Implements plan §KB-07 局部重解析: the draft copies every unit and vector
from the published generation, the worker re-parses only the requested
pages (with one neighbour page of context) or sheets, whole structural
units are replaced, unchanged content keeps its vectors, and a human
revision inside the replaced scope is surfaced as a conflict marker
instead of being silently overwritten.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from . import docling, structuring
from .corrections import _write_unit_segments
from .database import get_document_pool
from .embeddings import embed_texts
from .identity import document_owner_id
from .repository import DocumentConflictError
from .retrieval import locator_dict
from .settings import DocumentSettings, get_document_settings
from .storage import resolve_storage_key
from .structuring import StructuredUnit


def _json_literal(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def queue_scoped_reparse(
    owner_id: str,
    document_id: str,
    source_generation_id: str,
    *,
    pages: list[int] | None = None,
    sheets: list[str] | None = None,
    unit_ids: list[str] | None = None,
    reason: str = "",
) -> str | None:
    """Create a draft copy of a published generation plus a scoped job.

    Returns the new draft generation id, ``None`` when the source does not
    exist for this owner, and conflicts when the source is not published or
    another parse job is already in flight.
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
        source = await connection.fetchrow(
            """SELECT * FROM research_document_generations
                WHERE id=$1::uuid AND document_id=$2::uuid AND status='published'""",
            source_generation_id,
            document_id,
        )
        if not source:
            raise DocumentConflictError("reparse_source_not_published")
        if locator_dict(source["index_profile"]) != get_document_settings().index_profile:
            raise DocumentConflictError("knowledge_index_profile_mismatch: full reindex required")
        active = await connection.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM research_document_jobs
                  WHERE document_id=$1::uuid
                    AND kind IN ('ingest','reindex','reparse')
                    AND status IN ('queued','running'))""",
            document_id,
        )
        if active:
            raise DocumentConflictError("document_reparse_in_progress")

        scope: dict[str, Any] = {"source_generation": str(source_generation_id)}
        if pages:
            scope["pages"] = sorted({int(page) for page in pages if int(page) > 0})
        if sheets:
            scope["sheets"] = sorted({str(sheet) for sheet in sheets})
        if unit_ids:
            # Resolve unit scopes onto pages (PDF) or sheets before queueing.
            for row in await connection.fetch(
                """SELECT locator FROM research_document_units
                    WHERE generation_id=$1::uuid AND id=ANY($2::uuid[])""",
                source_generation_id,
                [str(item) for item in unit_ids],
            ):
                locator = locator_dict(row["locator"])
                if locator.get("page"):
                    scope.setdefault("pages", []).append(int(locator["page"]))
                if locator.get("sheet"):
                    scope.setdefault("sheets", []).append(str(locator["sheet"]))
            scope["pages"] = sorted(set(scope.get("pages", [])))
            scope["sheets"] = sorted(set(scope.get("sheets", [])))
        if not scope.get("pages") and not scope.get("sheets"):
            raise DocumentConflictError("reparse_scope_empty")

        generation_id = await connection.fetchval(
            """INSERT INTO research_document_generations
               (document_id, version_id, metadata_snapshot, reparse_scope, index_profile)
               VALUES ($1::uuid, $2::uuid,
                       jsonb_set($3::jsonb, '{confirmed}', '{}'::jsonb),
                       $4::jsonb, $5::jsonb)
               RETURNING id""",
            document_id,
            source["version_id"],
            _json_literal(locator_dict(source["metadata_snapshot"])),
            _json_literal(scope),
            _json_literal(locator_dict(source["index_profile"])),
        )

        # Copy every unit with fresh ids; segments carry their vectors over.
        unit_rows = await connection.fetch(
            """SELECT * FROM research_document_units
                WHERE generation_id=$1::uuid ORDER BY ordinal""",
            source_generation_id,
        )
        id_map: dict[str, str] = {}
        for row in unit_rows:
            new_unit_id = str(
                uuid5(NAMESPACE_URL, f"insightforge:unit:{generation_id}:{row['ordinal']}")
            )
            id_map[str(row["id"])] = new_unit_id
            await connection.execute(
                """INSERT INTO research_document_units
                   (id, generation_id, ordinal, unit_type, locator, raw_text,
                    revised_text, index_text, attributes, excluded, exclusion_reason)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $7, $8,
                           $9::jsonb, $10, $11)""",
                new_unit_id,
                generation_id,
                row["ordinal"],
                row["unit_type"],
                json.dumps(locator_dict(row["locator"]), ensure_ascii=False, default=str),
                row["raw_text"],
                row["revised_text"],
                row["index_text"],
                json.dumps(locator_dict(row["attributes"]), ensure_ascii=False, default=str),
                row["excluded"],
                row["exclusion_reason"],
            )
        segments = await connection.fetch(
            """SELECT * FROM research_document_segments
                WHERE generation_id=$1::uuid ORDER BY ordinal""",
            source_generation_id,
        )
        for ordinal, row in enumerate(segments):
            await connection.execute(
                """INSERT INTO research_document_segments
                   (id, generation_id, unit_id, ordinal, index_text, locator,
                    embedding, embedding_model, content_hash)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::jsonb,
                           $7::vector, $8, $9)""",
                str(uuid5(NAMESPACE_URL, f"insightforge:segment:{generation_id}:{ordinal}")),
                generation_id,
                id_map.get(str(row["unit_id"])),
                ordinal,
                row["index_text"],
                json.dumps(locator_dict(row["locator"]), ensure_ascii=False, default=str),
                _vector_literal(row["embedding"]),
                row["embedding_model"],
                row["content_hash"],
            )
        await connection.execute(
            """UPDATE research_documents
               SET status='queued', failure_code=NULL, updated_at=now()
             WHERE id=$1::uuid""",
            document_id,
        )
        await connection.execute(
            "INSERT INTO research_document_jobs(document_id, kind, status) "
            "VALUES ($1::uuid, 'reparse', 'queued')",
            document_id,
        )
        await connection.execute(
            """INSERT INTO research_document_operations
               (owner_id, document_id, generation_id, operation, actor_id, reason, changes)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'queue_scoped_reparse',
                       $1::uuid, $4, $5::jsonb)""",
            owner_id,
            document_id,
            str(generation_id),
            reason,
            _json_literal(scope),
        )
    return str(generation_id)


def _vector_literal(embedding) -> str | None:
    if embedding is None:
        return None
    if isinstance(embedding, str):
        return embedding  # already a vector literal from asyncpg
    return "[" + ",".join(f"{float(item):.6g}" for item in embedding) + "]"


async def execute_scoped_reparse(
    document,
    generation: dict[str, Any],
    settings: DocumentSettings,
    *,
    on_submitted=None,
) -> None:
    """Replace the scoped units of one draft generation in place.

    Called by the worker for ``reparse`` jobs. Neighbour pages join the
    upstream conversion for cross-page table context, but only units inside
    the scope are replaced; everything else keeps its copied values and
    vectors. Replacements are planned and embedded first, then applied in a
    single transaction so a worker crash never leaves a half-replaced draft.
    """
    if locator_dict(generation["index_profile"]) != settings.index_profile:
        raise DocumentConflictError("knowledge_index_profile_mismatch: full reindex required")
    scope = locator_dict(generation["reparse_scope"]) if generation.get("reparse_scope") else {}
    pages = [int(page) for page in scope.get("pages") or []]
    sheets = [str(sheet) for sheet in scope.get("sheets") or []]
    pool = await get_document_pool()
    path = resolve_storage_key(document["storage_key"], settings)
    quality_flags = ["scoped_reparse"]

    # --- plan replacements outside any database transaction ---
    if pages:
        context_pages = sorted(
            {page for center in pages for page in (center - 1, center, center + 1) if page >= 1}
        )
        task_id, result = await docling.convert(
            path,
            document["filename"],
            document["media_type"],
            settings,
            on_submitted=on_submitted,
            page_range=(context_pages[0], context_pages[-1]),
        )
        prepared = structuring.from_docling(result)
        prepared.upstream_task_id = task_id
        replacement_units = [
            unit for unit in prepared.units
            if unit.locator.get("page") and int(unit.locator["page"]) in pages
        ]
    elif sheets:
        prepared = structuring.from_xlsx(Path(path), settings)
        replacement_units = [
            unit for unit in prepared.units if unit.locator.get("sheet") in sheets
        ]
        quality_flags.append("sheet_reparse")
    else:
        raise DocumentConflictError("reparse_scope_empty")

    plans: list[tuple[StructuredUnit, list[str]]] = []
    embed_inputs: list[str] = []
    for unit in replacement_units:
        segment_plan = structuring.plan_segments(
            structuring.PreparedDocument(units=[unit]), settings
        )
        plans.append((unit, segment_plan.segment_texts))
        embed_inputs.extend(segment_plan.segment_texts)
    vectors = await embed_texts(embed_inputs, settings, operation="ingest") if embed_inputs else []
    cursor = 0
    hashed_plans = []
    for unit, texts in plans:
        take = len(texts)
        chunk = vectors[cursor : cursor + take]
        cursor += take
        vector_map = {
            hashlib.sha256(text.encode("utf-8")).hexdigest(): vector
            for text, vector in zip(texts, chunk, strict=True)
        }
        hashed_plans.append((unit, texts, vector_map))

    # --- apply atomically ---
    async with pool.acquire() as connection, connection.transaction():
        if pages:
            replaced_scope_units = await connection.fetch(
                """SELECT id, revised_text FROM research_document_units
                    WHERE generation_id=$1::uuid
                      AND (locator->>'page')::int = ANY($2::int[])""",
                str(generation["id"]),
                pages,
            )
        else:
            replaced_scope_units = await connection.fetch(
                """SELECT id, revised_text FROM research_document_units
                    WHERE generation_id=$1::uuid AND locator->>'sheet' = ANY($2::text[])""",
                str(generation["id"]),
                sheets,
            )
        conflicts = [
            {"unit_id": str(row["id"]), "revision": row["revised_text"]}
            for row in replaced_scope_units
            if row["revised_text"]
        ]
        if conflicts:
            # Human revisions inside the replaced scope are surfaced as an
            # explicit conflict the reviewer must re-apply or discard.
            quality_flags.append("reparse_revision_conflicts")

        for row in replaced_scope_units:
            await connection.execute(
                "DELETE FROM research_document_segments WHERE unit_id=$1::uuid",
                str(row["id"]),
            )
            await connection.execute(
                "DELETE FROM research_document_units WHERE id=$1::uuid",
                str(row["id"]),
            )

        next_ordinal = int(
            await connection.fetchval(
                "SELECT coalesce(max(ordinal), 0) FROM research_document_units "
                "WHERE generation_id=$1::uuid",
                str(generation["id"]),
            )
            or 0
        )
        for unit, texts, vector_map in hashed_plans:
            next_ordinal += 1
            unit_id = str(
                uuid5(NAMESPACE_URL, f"insightforge:unit:{generation['id']}:{next_ordinal}")
            )
            await connection.execute(
                """INSERT INTO research_document_units
                   (id, generation_id, ordinal, unit_type, locator, raw_text,
                    index_text, attributes)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $6, $7::jsonb)""",
                unit_id,
                str(generation["id"]),
                next_ordinal,
                unit.unit_type,
                json.dumps(unit.locator, ensure_ascii=False, default=str),
                unit.raw_text,
                json.dumps(unit.attributes, ensure_ascii=False, default=str),
            )
            await _write_unit_segments(
                connection, str(generation["id"]), unit_id, texts,
                vector_map, settings.embedding_model,
            )

        await connection.execute(
            """UPDATE research_document_generations
               SET status='pending_review', reparse_scope=NULL, updated_at=now(),
                   quality_report=quality_report || $2::jsonb
                WHERE id=$1::uuid""",
            str(generation["id"]),
            json.dumps(
                {"flags": quality_flags, "revision_conflicts": conflicts},
                ensure_ascii=False,
            ),
        )
        await connection.execute(
            """UPDATE research_documents
               SET status='ready', failure_code=NULL, updated_at=now(),
                   chunk_count=(SELECT count(*) FROM research_document_segments
                                 WHERE generation_id=$1::uuid)
             WHERE id=$2::uuid""",
            str(generation["id"]),
            str(document["id"]),
        )

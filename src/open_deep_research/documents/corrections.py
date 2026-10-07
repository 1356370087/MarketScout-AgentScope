"""Human corrections on a reviewable generation (plan §KB-07).

Corrections only ever touch draft or pending_review generations; published
generations stay immutable. A revision counter gives optimistic locking so
a stale browser tab gets a conflict instead of overwriting newer edits.
The flow is: plan all rebuild texts → embed missing ones through the
service-key ingest path → apply everything atomically under the revision
check, reusing unchanged vectors by content hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from .chunking import _windows
from .database import get_document_pool
from .embeddings import embed_texts, vector_literal
from .identity import document_owner_id
from .repository import DocumentConflictError
from .retrieval import locator_dict
from .settings import DocumentSettings, get_document_settings
from .structuring import StructuredUnit, plan_table_segments

_EDITABLE_STATUSES = ("draft", "pending_review")


class CorrectionRevisionError(RuntimeError):
    """Raised when the submitted revision is stale; carries the current one."""

    def __init__(self, current_revision: int):
        """Keep the server-side revision for the 409 payload."""
        super().__init__(f"generation_revision_conflict:{current_revision}")
        self.current_revision = current_revision


def _json_literal(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def effective_text(unit: dict[str, Any]) -> str:
    """Revised text wins over the raw extraction once a human edited it."""
    revised = unit.get("revised_text")
    return revised if revised not in (None, "") else unit.get("index_text") or ""


def _segment_texts(unit: dict[str, Any], settings: DocumentSettings) -> list[str]:
    text = effective_text(unit)
    if unit.get("unit_type") == "table":
        attributes = unit.get("attributes") or {}
        header = attributes.get("header")
        if not header and text:
            header = text.split("\n", 1)[0].split(" | ")
        placeholder = StructuredUnit(
            unit_type="table",
            locator=dict(unit.get("locator") or {}),
            raw_text=text,
            index_text=text,
            attributes={"header": header or [], "footnotes": attributes.get("footnotes") or ""},
        )
        return plan_table_segments(placeholder, settings)
    return [window for window in _windows(text) if window]


def _merged_table_rows(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Concatenate two tables, dropping the second's repeated header row."""
    left_lines = [line for line in str(left.get("raw_text") or "").split("\n") if line.strip()]
    right_lines = [line for line in str(right.get("raw_text") or "").split("\n") if line.strip()]
    if left_lines and right_lines and right_lines[0].strip() == left_lines[0].strip():
        right_lines = right_lines[1:]
    return "\n".join([*left_lines, *right_lines])


def _split_rows(unit: dict[str, Any], at_row: int) -> tuple[str, str]:
    lines = [line for line in str(unit.get("raw_text") or "").split("\n") if line.strip()]
    if at_row <= 0 or at_row >= len(lines):
        raise DocumentConflictError("split_table_row_out_of_range")
    header = lines[0]
    return (
        "\n".join([header, *lines[1:at_row]]),
        "\n".join([header, *lines[at_row:]]),
    )


async def _load_generation(
    connection: asyncpg.Connection, owner_id: str, document_id: str, generation_id: str,
    *, for_update: bool,
) -> asyncpg.Record | None:
    locking = " FOR UPDATE OF g" if for_update else ""
    return await connection.fetchrow(
        f"""SELECT g.*, d.owner_id FROM research_document_generations g
             JOIN research_documents d ON d.id=g.document_id
            WHERE g.id=$1::uuid AND g.document_id=$2::uuid AND d.owner_id=$3::uuid
            {locking}""",
        generation_id,
        document_id,
        owner_id,
    )


async def apply_corrections(
    owner_id: str,
    document_id: str,
    generation_id: str,
    *,
    revision: int,
    unit_corrections: list[dict[str, Any]] | None = None,
    metadata_confirmed: dict[str, Any] | None = None,
    merge_tables: list[list[str]] | None = None,
    split_table: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any] | None:
    """Apply one batch of human corrections under an optimistic lock.

    Returns ``{"revision": n, "applied": {...}}`` or ``None`` when the
    generation is missing for this owner.
    """
    owner_id = document_owner_id(owner_id)
    unit_corrections = unit_corrections or []
    metadata_confirmed = metadata_confirmed or {}
    merge_tables = merge_tables or []
    settings = get_document_settings()
    pool = await get_document_pool()

    async with pool.acquire() as connection:
        state = await _load_generation(connection, owner_id, document_id, generation_id, for_update=False)
        if not state:
            return None
        if state["status"] not in _EDITABLE_STATUSES:
            raise DocumentConflictError("generation_not_editable")
        unit_rows = await connection.fetch(
            "SELECT * FROM research_document_units WHERE generation_id=$1::uuid ORDER BY ordinal",
            generation_id,
        )
    units = {str(row["id"]): dict(row) for row in unit_rows}

    # --- plan: effective units after corrections, merges and the split ---
    planned_units: dict[str, dict[str, Any]] = {}
    excluded_ids: list[str] = []
    for correction in unit_corrections:
        unit_id = str(correction.get("unit_id") or "")
        if unit_id not in units:
            raise KeyError(f"unit_not_found:{unit_id}")
        if correction.get("excluded"):
            excluded_ids.append(unit_id)
            continue
        merged = {**units[unit_id]}
        if correction.get("revised_text") is not None:
            merged["revised_text"] = str(correction["revised_text"])
        planned_units[unit_id] = merged

    merge_pairs: list[tuple[str, str]] = []
    for pair in merge_tables:
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise DocumentConflictError("merge_tables_pair_invalid")
        left_id, right_id = str(pair[0]), str(pair[1])
        if left_id not in units or right_id not in units:
            raise DocumentConflictError("merge_tables_unit_not_found")
        merge_pairs.append((left_id, right_id))
    for left_id, right_id in merge_pairs:
        merged = {**units[left_id], "revised_text": None}
        merged["raw_text"] = _merged_table_rows(units[left_id], units[right_id])
        merged["index_text"] = merged["raw_text"]
        planned_units[left_id] = merged
        planned_units.pop(right_id, None)
        units[left_id] = merged  # later corrections may touch the merged table

    split_spec = split_table or {}
    split_unit_id = str(split_spec.get("unit_id") or "")
    split_parts: tuple[str, str] | None = None
    if split_unit_id:
        if split_unit_id not in units:
            raise DocumentConflictError("split_table_unit_not_found")
        try:
            at_row = int(split_spec.get("at_row"))
        except (TypeError, ValueError) as exc:
            raise DocumentConflictError("split_table_row_invalid") from exc
        split_parts = _split_rows(units[split_unit_id], at_row)

    # --- embed every rebuild text whose hash is not already indexed ---
    rebuild: dict[str, list[str]] = {
        unit_id: _segment_texts(unit, settings) for unit_id, unit in planned_units.items()
    }
    if split_parts:
        attributes = units[split_unit_id].get("attributes") or {}
        for part_index, text in enumerate(split_parts, 1):
            part_unit = {
                "unit_type": "table",
                "locator": units[split_unit_id].get("locator") or {},
                "raw_text": text,
                "revised_text": None,
                "attributes": attributes,
            }
            rebuild[f"{split_unit_id}#split{part_index}"] = _segment_texts(part_unit, settings)

    embed_inputs: list[str] = []
    async with pool.acquire() as connection:
        for texts in rebuild.values():
            hashes = [
                hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts
            ]
            known = {
                row["content_hash"]
                for row in await connection.fetch(
                    """SELECT content_hash FROM research_document_segments
                        WHERE generation_id=$1::uuid AND content_hash=ANY($2::text[])""",
                    generation_id,
                    hashes,
                )
            }
            embed_inputs.extend(
                text
                for text, digest in zip(texts, hashes, strict=True)
                if digest not in known
            )
    if embed_inputs and locator_dict(state["index_profile"]) != settings.index_profile:
        raise DocumentConflictError("knowledge_index_profile_mismatch: full reindex required")
    vectors = await embed_texts(embed_inputs, settings, operation="ingest") if embed_inputs else []
    vector_by_hash = {
        hashlib.sha256(text.encode("utf-8")).hexdigest(): vector
        for text, vector in zip(embed_inputs, vectors, strict=True)
    }

    # --- apply atomically; the revision check re-validates under lock ---
    async with pool.acquire() as connection, connection.transaction():
        state = await _load_generation(connection, owner_id, document_id, generation_id, for_update=True)
        if not state:
            return None
        if state["status"] not in _EDITABLE_STATUSES:
            raise DocumentConflictError("generation_not_editable")
        if int(state["revision"]) != int(revision):
            raise CorrectionRevisionError(int(state["revision"]))

        applied = {
            "units_corrected": 0,
            "units_excluded": 0,
            "units_merged": 0,
            "units_split": 0,
            "segments_rebuilt": 0,
            "metadata_fields_confirmed": 0,
        }

        for correction in unit_corrections:
            unit_id = str(correction.get("unit_id"))
            await connection.execute(
                """UPDATE research_document_units
                   SET revised_text=coalesce($2, revised_text),
                       excluded=coalesce($3, excluded),
                       exclusion_reason=CASE WHEN $3 THEN $4 ELSE exclusion_reason END,
                       attributes=CASE WHEN $5::jsonb IS NULL THEN attributes
                                       ELSE attributes || $5::jsonb END
                 WHERE id=$1::uuid""",
                unit_id,
                correction.get("revised_text"),
                correction.get("excluded"),
                str(correction.get("exclusion_reason") or ""),
                _json_literal(correction["attributes_patch"])
                if correction.get("attributes_patch")
                else None,
            )
        for unit_id in excluded_ids:
            await connection.execute(
                "DELETE FROM research_document_segments WHERE unit_id=$1::uuid", unit_id
            )
            applied["units_excluded"] += 1
        for unit_id, texts in rebuild.items():
            if unit_id.startswith("#split") or "#split" in unit_id:
                continue
            await _write_unit_segments(
                connection, generation_id, unit_id, texts,
                vector_by_hash, settings.embedding_model,
            )
            applied["units_corrected"] += 1
            applied["segments_rebuilt"] += len(texts)

        for left_id, right_id in merge_pairs:
            left, right = units[left_id], units[right_id]
            await connection.execute(
                """UPDATE research_document_units
                   SET raw_text=$2, index_text=$2, revised_text=NULL,
                       attributes=attributes || $3::jsonb
                 WHERE id=$1::uuid""",
                left_id,
                planned_units[left_id]["raw_text"],
                _json_literal(
                    {
                        "merged_from": [
                            (left.get("locator") or {}).get("source"),
                            (right.get("locator") or {}).get("source"),
                        ]
                    }
                ),
            )
            await connection.execute(
                "DELETE FROM research_document_segments WHERE unit_id=$1::uuid", right_id
            )
            await connection.execute(
                "DELETE FROM research_document_units WHERE id=$1::uuid", right_id
            )
            applied["units_merged"] += 1

        if split_parts:
            unit = units[split_unit_id]
            source = (unit.get("locator") or {}).get("source") or f"unit:{split_unit_id}"
            first_text, second_text = split_parts
            await connection.execute(
                """UPDATE research_document_units
                   SET raw_text=$2, index_text=$2, revised_text=NULL,
                       locator=locator || $3::jsonb,
                       attributes=attributes || $4::jsonb
                 WHERE id=$1::uuid""",
                split_unit_id,
                first_text,
                _json_literal({"source": f"{source}:split:1"}),
                _json_literal({"split_part": 1}),
            )
            next_ordinal = await connection.fetchval(
                "SELECT coalesce(max(ordinal), 0) + 1 FROM research_document_units "
                "WHERE generation_id=$1::uuid",
                generation_id,
            )
            second_unit_id = str(
                uuid5(NAMESPACE_URL, f"insightforge:unit:{generation_id}:split:{split_unit_id}")
            )
            await connection.execute(
                """INSERT INTO research_document_units
                   (id, generation_id, ordinal, unit_type, locator, raw_text, index_text, attributes)
                   VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $6, $7::jsonb)""",
                second_unit_id,
                generation_id,
                int(next_ordinal),
                unit.get("unit_type") or "table",
                _json_literal(
                    {"source": f"{source}:split:2",
                     "page": (unit.get("locator") or {}).get("page")}
                ),
                second_text,
                _json_literal({"split_part": 2}),
            )
            await connection.execute(
                "DELETE FROM research_document_segments WHERE unit_id=$1::uuid",
                split_unit_id,
            )
            for part_index, (key, unit_id) in enumerate(
                [(f"{split_unit_id}#split1", split_unit_id),
                 (f"{split_unit_id}#split2", second_unit_id)]
            ):
                await _write_unit_segments(
                    connection, generation_id, unit_id, rebuild[key],
                    vector_by_hash, settings.embedding_model,
                )
            applied["units_split"] += 1

        if metadata_confirmed:
            await connection.execute(
                """UPDATE research_document_generations
                   SET metadata_snapshot = jsonb_set(
                         metadata_snapshot, '{confirmed}',
                         (metadata_snapshot->'confirmed') || $2::jsonb, true)
                 WHERE id=$1::uuid""",
                generation_id,
                _json_literal(metadata_confirmed),
            )
            applied["metadata_fields_confirmed"] = len(metadata_confirmed)

        new_revision = await connection.fetchval(
            """UPDATE research_document_generations
               SET revision=revision+1, updated_at=now()
                WHERE id=$1::uuid RETURNING revision""",
            generation_id,
        )
        await connection.execute(
            """INSERT INTO research_document_operations
               (owner_id, document_id, generation_id, operation, actor_id, reason, changes)
               VALUES ($1::uuid, $2::uuid, $3::uuid, 'correct', $1::uuid, $4, $5::jsonb)""",
            owner_id,
            document_id,
            generation_id,
            reason,
            _json_literal(applied | {"revision": int(new_revision)}),
        )
    return {"revision": int(new_revision), "applied": applied}


async def _write_unit_segments(
    connection: asyncpg.Connection,
    generation_id: str,
    unit_id: str,
    texts: list[str],
    vector_by_hash: dict[str, list[float]],
    embedding_model: str,
) -> None:
    """Replace one unit's segments, reusing same-hash vectors from siblings.

    Segment ordinals are unique per generation, so the numbering continues
    from the generation's current maximum.
    """
    await connection.execute(
        "DELETE FROM research_document_segments WHERE unit_id=$1::uuid", unit_id
    )
    if not texts:
        return
    hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    reusable = {
        row["content_hash"]: row["embedding"]
        for row in await connection.fetch(
            """SELECT content_hash, embedding FROM research_document_segments
                WHERE generation_id=$1::uuid AND embedding IS NOT NULL
                  AND content_hash=ANY($2::text[])""",
            generation_id,
            hashes,
        )
    }
    base_ordinal = int(
        await connection.fetchval(
            "SELECT coalesce(max(ordinal), -1) FROM research_document_segments "
            "WHERE generation_id=$1::uuid",
            generation_id,
        )
        or -1
    )
    rows = []
    for offset, (text, digest) in enumerate(zip(texts, hashes, strict=True)):
        vector = vector_by_hash.get(digest)
        literal = None
        if vector is not None:
            literal = vector_literal(vector)
        elif digest in reusable:
            literal = vector_literal(list(reusable[digest]))
        rows.append(
            (
                str(uuid5(NAMESPACE_URL, f"insightforge:segment:{generation_id}:{unit_id}:{offset}")),
                generation_id,
                unit_id,
                base_ordinal + 1 + offset,
                text,
                digest,
                literal,
                embedding_model if literal else None,
            )
        )
    await connection.executemany(
        """INSERT INTO research_document_segments
           (id, generation_id, unit_id, ordinal, index_text, content_hash,
            embedding, embedding_model)
           VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7::vector, $8)""",
        rows,
    )

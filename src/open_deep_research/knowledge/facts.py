"""Fact ledger: keys, assertions, evidence, candidate extraction (KB-12).

A fact key identifies what is being compared (entity × metric × region ×
period × condition). Assertions carry the value with full context (unit,
currency, scale, data period, validity range) and link to published document
evidence. Multiple conflicting values coexist on the same key — different
periods, currencies, or package tiers are not conflicts. Published
assertions are immutable: changes create a new assertion linked to the one
it supersedes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from typing import Any

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.retrieval import locator_dict

from . import authz, editorial


class FactError(RuntimeError):
    """Raised when a fact operation is invalid."""


async def resolve_fact_key(
    workspace_id: str | None,
    entity_name: str,
    metric: str,
    *,
    region: str = "",
    period_label: str = "",
    condition_text: str = "",
) -> str:
    """Find or create one fact key; returns its id."""
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        key_id = await connection.fetchval(
            """SELECT id FROM knowledge_fact_keys
                WHERE (workspace_id IS NULL AND $1::uuid IS NULL OR workspace_id=$1::uuid)
                  AND entity_name=$2 AND metric=$3
                  AND region=$4 AND period_label=$5 AND condition_text=$6""",
            workspace_id,
            entity_name,
            metric,
            region,
            period_label,
            condition_text,
        )
        if key_id:
            return str(key_id)
        key_id = await connection.fetchval(
            """INSERT INTO knowledge_fact_keys
                 (workspace_id, entity_name, metric, region, period_label, condition_text)
               VALUES ($1::uuid, $2, $3, $4, $5, $6)
               ON CONFLICT DO NOTHING RETURNING id""",
            workspace_id,
            entity_name,
            metric,
            region,
            period_label,
            condition_text,
        )
        if key_id:
            return str(key_id)
        return str(
            await connection.fetchval(
                """SELECT id FROM knowledge_fact_keys
                WHERE (workspace_id IS NULL AND $1::uuid IS NULL OR workspace_id=$1::uuid)
                  AND entity_name=$2 AND metric=$3
                  AND region=$4 AND period_label=$5 AND condition_text=$6""",
                workspace_id,
                entity_name,
                metric,
                region,
                period_label,
                condition_text,
            )
        )


async def submit_assertion(
    actor_id: str,
    knowledge_base_id: str,
    *,
    entity_name: str,
    metric: str,
    value_text: str = "",
    value_numeric: str | None = None,
    unit: str = "",
    currency: str = "",
    scale: str = "",
    data_period: str = "",
    valid_from: str | None = None,
    valid_until: str | None = None,
    condition_text: str = "",
    raw_statement: str = "",
    value_origin: str = "source",
    region: str = "",
    period_label: str = "",
    workspace_id: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
    extraction_key: str = "",
) -> dict[str, Any]:
    """Submit one fact assertion as a draft (contributors and up).

    Evidence items must reference published generations of documents in the
    same knowledge base (plan §KB-12: 自动结果必须引用已发布的 generation).
    """
    actor_id = document_owner_id(actor_id)
    context = await authz.require_kb_capability(
        actor_id, knowledge_base_id, authz.CAP_SUBMIT
    )
    workspace_id = str(context["knowledge_base"]["workspace_id"])
    if value_numeric is not None:
        try:
            value_numeric = Decimal(str(value_numeric))
            if not value_numeric.is_finite():
                raise ValueError()
        except Exception as exc:
            raise FactError("fact_invalid_number") from exc
    try:
        valid_from = (
            date.fromisoformat(valid_from)
            if isinstance(valid_from, str)
            else valid_from
        )
        valid_until = (
            date.fromisoformat(valid_until)
            if isinstance(valid_until, str)
            else valid_until
        )
        if valid_from and valid_until and valid_from >= valid_until:
            raise ValueError()
    except ValueError as exc:
        raise FactError("fact_invalid_validity") from exc
    pool = await get_document_pool()
    fact_key_id = await resolve_fact_key(
        workspace_id,
        entity_name,
        metric,
        region=region,
        period_label=period_label,
        condition_text=condition_text,
    )
    async with pool.acquire() as connection, connection.transaction():
        # Validate evidence: each must reference a published generation in this KB.
        for item in evidence or []:
            try:
                await editorial.source(connection, knowledge_base_id, item)
            except ValueError as exc:
                raise FactError(str(exc)) from exc
            generation = await connection.fetchrow(
                """SELECT g.id, g.document_id, d.home_knowledge_base_id
                     FROM research_document_generations g
                     JOIN research_documents d ON d.id=g.document_id
                    WHERE g.id=$1::uuid AND g.status='published'
                      AND d.home_knowledge_base_id=$2::uuid""",
                item.get("generation_id"),
                knowledge_base_id,
            )
            if not generation:
                raise FactError(
                    f"evidence_generation_not_published:{item.get('generation_id')}"
                )
        assertion_id = await connection.fetchval(
            """INSERT INTO knowledge_fact_assertions
                 (knowledge_base_id, fact_key_id, value_text, value_numeric, unit,
                  currency, scale, data_period, valid_from, valid_until,
                  condition_text, raw_statement, value_origin,
                  extraction_key, created_by)
               VALUES ($1::uuid, $2::uuid, $3, $4::numeric, $5, $6, $7, $8,
                       $9::date, $10::date, $11, $12, $13, $14, $15::uuid)
               ON CONFLICT (knowledge_base_id, extraction_key)
                 WHERE extraction_key IS NOT NULL AND extraction_key<>'' DO NOTHING
             RETURNING id""",
            knowledge_base_id,
            fact_key_id,
            value_text,
            value_numeric,
            unit,
            currency,
            scale,
            data_period,
            valid_from,
            valid_until,
            condition_text or "",
            raw_statement,
            value_origin,
            extraction_key,
            actor_id,
        )
        if not assertion_id:
            existing = await connection.fetchval(
                "SELECT id FROM knowledge_fact_assertions WHERE knowledge_base_id=$1::uuid AND extraction_key=$2",
                knowledge_base_id,
                extraction_key,
            )
            return {"id": str(existing), "status": "draft", "fact_key_id": fact_key_id}
        for item in evidence or []:
            await connection.execute(
                """INSERT INTO knowledge_fact_evidence
                     (assertion_id, document_id, generation_id, unit_id, segment_id, excerpt)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6)
                   ON CONFLICT DO NOTHING""",
                assertion_id,
                item.get("document_id"),
                item.get("generation_id"),
                item.get("unit_id"),
                item.get("segment_id"),
                item.get("excerpt", ""),
            )
        await editorial.audit(
            connection, actor_id, knowledge_base_id, "fact_submit", assertion_id
        )
    return {"id": str(assertion_id), "status": "draft", "fact_key_id": fact_key_id}


async def publish_assertion(
    actor_id: str, assertion_id: str, *, supersedes_id: str | None = None
) -> dict[str, Any] | None:
    """Publish one reviewable assertion; link to the record it supersedes."""
    actor_id = document_owner_id(actor_id)
    assertion = await editorial.target(
        actor_id, "knowledge_fact_assertions", assertion_id, authz.CAP_REVIEW
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection, connection.transaction():
        evidence = await connection.fetch(
            "SELECT * FROM knowledge_fact_evidence WHERE assertion_id=$1::uuid",
            assertion_id,
        )
        if not evidence:
            raise FactError("fact_evidence_required")
        for item in evidence:
            try:
                await editorial.source(
                    connection, str(assertion["knowledge_base_id"]), dict(item)
                )
            except ValueError as exc:
                raise FactError(str(exc)) from exc
        if supersedes_id:
            valid = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM knowledge_fact_assertions WHERE id=$1::uuid AND knowledge_base_id=$2 AND fact_key_id=$3 AND status='published')",
                supersedes_id,
                assertion["knowledge_base_id"],
                assertion["fact_key_id"],
            )
            if not valid:
                raise FactError("fact_supersedes_wrong_key_or_scope")
        row = await connection.fetchrow(
            """UPDATE knowledge_fact_assertions
                  SET status='published', published_at=now(),
                      reviewed_by=$2::uuid, updated_at=now(),
                      supersedes_id=COALESCE($3::uuid, supersedes_id)
                WHERE id=$1::uuid AND status IN ('draft','pending_review')
             RETURNING id, fact_key_id""",
            assertion_id,
            actor_id,
            supersedes_id,
        )
        if not row:
            return None
        await editorial.audit(
            connection,
            actor_id,
            assertion["knowledge_base_id"],
            "fact_publish",
            assertion_id,
        )
    return {
        "id": str(row["id"]),
        "status": "published",
        "fact_key_id": str(row["fact_key_id"]),
    }


async def reject_assertion(
    actor_id: str, assertion_id: str, *, reason: str
) -> dict[str, Any] | None:
    """Reject one draft assertion with a reason."""
    actor_id = document_owner_id(actor_id)
    await editorial.target(
        actor_id, "knowledge_fact_assertions", assertion_id, authz.CAP_REVIEW
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """UPDATE knowledge_fact_assertions
                  SET status='rejected', review_note=$2, reviewed_by=$3::uuid, updated_at=now()
                WHERE id=$1::uuid AND status IN ('draft','pending_review')
             RETURNING id""",
            assertion_id,
            reason,
            actor_id,
        )
    return {"id": str(row["id"]), "status": "rejected"} if row else None


async def withdraw_assertion(actor_id: str, assertion_id: str) -> dict[str, Any] | None:
    """Withdraw one published assertion (creates no new record — just flips status)."""
    actor_id = document_owner_id(actor_id)
    await editorial.target(
        actor_id, "knowledge_fact_assertions", assertion_id, authz.CAP_REVIEW
    )
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """UPDATE knowledge_fact_assertions
                  SET status='withdrawn', updated_at=now(), reviewed_by=$2::uuid
                WHERE id=$1::uuid AND status='published'
             RETURNING id""",
            assertion_id,
            actor_id,
        )
    return {"id": str(row["id"]), "status": "withdrawn"} if row else None


async def list_assertions(
    actor_id: str,
    knowledge_base_id: str,
    *,
    entity_name: str = "",
    metric: str = "",
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List fact assertions with their keys and evidence counts."""
    actor_id = document_owner_id(actor_id)
    await authz.require_kb_capability(actor_id, knowledge_base_id, authz.CAP_VIEW)
    clauses = ["a.knowledge_base_id=$1::uuid"]
    args: list[Any] = [knowledge_base_id]
    caps = await authz.kb_capabilities(actor_id, knowledge_base_id)
    if authz.CAP_REVIEW not in caps:
        args.append(actor_id)
        clauses.append("(a.status='published' OR a.created_by=$2::uuid)")
    if entity_name:
        args.append(f"%{entity_name}%")
        clauses.append(f"k.entity_name ILIKE ${len(args)}")
    if metric:
        args.append(f"%{metric}%")
        clauses.append(f"k.metric ILIKE ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"a.status=${len(args)}")
    where = " AND ".join(clauses)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            f"""SELECT a.*, k.entity_name, k.metric, k.region, k.period_label,
                       (SELECT count(*) FROM knowledge_fact_evidence e
                          WHERE e.assertion_id=a.id) AS evidence_count
                  FROM knowledge_fact_assertions a
                  JOIN knowledge_fact_keys k ON k.id=a.fact_key_id
                 WHERE {where}
                 ORDER BY a.created_at DESC LIMIT {limit}""",
            *args,
        )
    return [
        {
            "id": str(row["id"]),
            "entity_name": row["entity_name"],
            "metric": row["metric"],
            "region": row["region"],
            "period_label": row["period_label"],
            "value_text": row["value_text"],
            "value_numeric": str(row["value_numeric"])
            if row["value_numeric"] is not None
            else None,
            "fact_key_id": str(row["fact_key_id"]),
            "condition_text": row["condition_text"],
            "adopted": row["adopted"],
            "valid_from": str(row["valid_from"]) if row["valid_from"] else None,
            "valid_until": str(row["valid_until"]) if row["valid_until"] else None,
            "unit": row["unit"],
            "currency": row["currency"],
            "scale": row["scale"],
            "data_period": row["data_period"],
            "value_origin": row["value_origin"],
            "status": row["status"],
            "verification": row["verification"],
            "supersedes_id": str(row["supersedes_id"])
            if row["supersedes_id"]
            else None,
            "evidence_count": int(row["evidence_count"]),
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Candidate extraction (deterministic regex-based; model refinement optional)
# ---------------------------------------------------------------------------

_METRIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "价格",
        re.compile(
            r"(?:价格|定价|年费|售价)[为约：:\s]*(?:每年\s*)?([\d,.]+)\s*(亿|百万|万)?\s*元",
            re.I,
        ),
    ),
    ("营收", re.compile(r"营收(?:入|额)?[为约]?([\d,.]+)\s*(亿|万|百万)?\s*元", re.I)),
    ("毛利率", re.compile(r"毛利率[为约]?([\d.]+)\s*%", re.I)),
    ("市场份额", re.compile(r"市场份额[为约]?([\d.]+)\s*%", re.I)),
    (
        "用户数",
        re.compile(
            r"(?:用户|月活|日活)(?:数|量)?[为约达]?([\d,.]+)\s*(万|亿|百万)?", re.I
        ),
    ),
    (
        "研发费用",
        re.compile(r"研发(?:费用|投入)[为约]?([\d,.]+)\s*(亿|万)?\s*元", re.I),
    ),
]

_ENTITY_HINT = re.compile(
    r"([\u4e00-\u9fff]{2,8})(?:科技|集团|公司|股份|控股|有限)", re.I
)


def _parse_number(raw: str, unit_hint: str = "") -> tuple[str, str]:
    """Parse a Chinese-formatted number with unit into value + scale."""
    cleaned = raw.replace(",", "").strip()
    if unit_hint == "亿":
        return cleaned, "亿"
    if unit_hint in {"万", "百万"}:
        return cleaned, unit_hint
    return cleaned, ""


def extract_candidates_from_text(
    text: str, entity_hint: str = ""
) -> list[dict[str, Any]]:
    """Extract fact candidates from one text block (deterministic)."""
    candidates: list[dict[str, Any]] = []
    entities = [m.group(0) for m in _ENTITY_HINT.finditer(text)] or (
        [entity_hint] if entity_hint else []
    )
    for metric_name, pattern in _METRIC_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw_value = match.group(1)
        unit_hint = match.group(2) if match.lastindex >= 2 else ""
        numeric, scale = _parse_number(raw_value, unit_hint or "")
        excerpt_start = max(0, match.start() - 30)
        excerpt = text[excerpt_start : match.end() + 50].strip()
        for entity in entities[:1]:  # take the first entity hint
            candidates.append(
                {
                    "entity_name": entity,
                    "metric": metric_name,
                    "value_text": match.group(0),
                    "value_numeric": numeric,
                    "unit": "元"
                    if "元" in match.group(0)
                    else ("%" if "%" in match.group(0) else ""),
                    "scale": scale,
                    "raw_statement": excerpt,
                    "value_origin": "source",
                    "period_label": (
                        re.search(r"(?:FY)?20\d{2}(?:年度|财年|年)?", text).group(0)
                        if re.search(r"(?:FY)?20\d{2}(?:年度|财年|年)?", text)
                        else ""
                    ),
                }
            )
    return candidates


async def extract_candidates_for_generation(
    actor_id: str,
    knowledge_base_id: str,
    document_id: str,
    generation_id: str,
    *,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Extract candidate facts from one published generation's units.

    The extraction key (generation_id + extractor version) makes retries
    idempotent: re-running the same extraction on the same generation does
    not create duplicate candidates.
    """
    actor_id = document_owner_id(actor_id)
    await authz.require_kb_capability(actor_id, knowledge_base_id, authz.CAP_SUBMIT)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        await editorial.source(
            connection,
            knowledge_base_id,
            {"document_id": document_id, "generation_id": generation_id},
        )
        metadata = locator_dict(
            await connection.fetchval(
                "SELECT metadata_snapshot FROM research_document_generations WHERE id=$1::uuid",
                generation_id,
            )
        )
        entity_hint = str((metadata.get("confirmed") or {}).get("company") or "")
        units = await connection.fetch(
            """SELECT id, index_text FROM research_document_units
                WHERE generation_id=$1::uuid AND NOT excluded
                ORDER BY ordinal""",
            generation_id,
        )
    extraction_version = "v1"
    extraction_key = f"{generation_id}:{extraction_version}"
    async with pool.acquire() as connection:
        existing = await connection.fetch(
            "SELECT extraction_key FROM knowledge_fact_assertions WHERE extraction_key LIKE $1",
            extraction_key + ":%",
        )
        existing_keys = {row["extraction_key"] for row in existing}
    candidates: list[dict[str, Any]] = []
    for unit in units:
        text = str(unit["index_text"] or "")
        for candidate in extract_candidates_from_text(text, entity_hint):
            candidate["evidence"] = [
                {
                    "document_id": document_id,
                    "generation_id": generation_id,
                    "unit_id": str(unit["id"]),
                    "segment_id": None,
                    "excerpt": candidate["raw_statement"][:200],
                }
            ]
            candidate["workspace_id"] = workspace_id
            candidate["extraction_key"] = (
                extraction_key
                + ":"
                + hashlib.sha256(
                    (
                        str(unit["id"]) + candidate["metric"] + candidate["value_text"]
                    ).encode()
                ).hexdigest()
            )
            candidates.append(candidate)
    # Persist all candidates as drafts.
    submitted = []
    for candidate in candidates:
        if candidate["extraction_key"] in existing_keys:
            continue
        result = await submit_assertion(
            actor_id,
            knowledge_base_id,
            entity_name=candidate["entity_name"],
            metric=candidate["metric"],
            value_text=candidate["value_text"],
            value_numeric=candidate["value_numeric"],
            unit=candidate["unit"],
            scale=candidate["scale"],
            period_label=candidate.get("period_label", ""),
            data_period=candidate.get("period_label", ""),
            raw_statement=candidate["raw_statement"],
            value_origin=candidate["value_origin"],
            workspace_id=candidate.get("workspace_id"),
            evidence=candidate.get("evidence"),
            extraction_key=candidate["extraction_key"],
        )
        submitted.append(result)
    return submitted


async def review_value(
    actor, assertion_id, verification="verified", adopted=False, reason=""
):
    """Record verification and choose at most one adopted assertion per comparison."""
    row = await editorial.target(
        actor, "knowledge_fact_assertions", assertion_id, authz.CAP_REVIEW
    )
    if (
        verification not in {"verified", "unverified", "disputed"}
        or row["status"] != "published"
    ):
        raise FactError("fact_not_reviewable")
    pool = await get_document_pool()
    async with pool.acquire() as c, c.transaction():
        await c.execute(
            "SELECT id FROM knowledge_fact_keys WHERE id=$1 FOR UPDATE",
            row["fact_key_id"],
        )
        if adopted:
            await c.execute(
                "UPDATE knowledge_fact_assertions SET adopted=false WHERE fact_key_id=$1 AND knowledge_base_id=$2 AND currency=$3 AND unit=$4 AND data_period=$5",
                row["fact_key_id"],
                row["knowledge_base_id"],
                row["currency"],
                row["unit"],
                row["data_period"],
            )
        await c.execute(
            "UPDATE knowledge_fact_assertions SET verification=$2,adopted=$3,review_note=$4,reviewed_by=$5::uuid WHERE id=$1::uuid",
            assertion_id,
            verification,
            adopted,
            reason,
            document_owner_id(actor),
        )
        await editorial.audit(
            c,
            actor,
            row["knowledge_base_id"],
            "fact_review",
            assertion_id,
            {"verification": verification, "adopted": adopted, "reason": reason},
        )
    return {"id": assertion_id, "verification": verification, "adopted": adopted}

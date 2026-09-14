"""Automatic metadata candidates with evidence locators (plan §KB-02).

Deterministic extraction always runs in the worker; an optional LiteLLM text
model refines the document type and company mentions. Every candidate carries
the unit indexes and excerpts that justify it, values without evidence stay
empty, and a failing model never blocks manual review or publishing.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .database import get_document_pool
from .identity import document_owner_id
from .settings import DocumentSettings
from .structuring import StructuredUnit

# Bounded input for the optional model pass: lead units plus table headers.
_MAX_MODEL_CHARS = 6000
_MAX_EVIDENCE_EXCERPT = 120

_DOC_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("财报", re.compile(r"财务报表|资产负债表|利润表|现金流量表|审计报告|annual report|financial statements?", re.I)),
    ("价格表", re.compile(r"价格表|报价单?|价目表?|定价表?|price list|quotation", re.I)),
    ("产品文档", re.compile(r"产品手册|用户手册|产品说明|使用说明|操作手册|user guide|user manual|product (?:overview|documentation)", re.I)),
    ("行业报告", re.compile(r"行业(?:研究|分析|报告)|市场(?:研究|分析|报告)|白皮书|industry (?:report|research)|market (?:report|analysis)|white ?paper", re.I)),
    ("新闻", re.compile(r"新闻稿|媒体报道|本报讯|记者.{0,6}报道|press release", re.I)),
    ("内部材料", re.compile(r"内部资料|内部文件|仅供内部|confidential|internal use only", re.I)),
]
_PERIOD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("FY", re.compile(r"(?:FY\s*)?(20\d{2})\s*财年|FY\s*(20\d{2})", re.I)),
    ("quarter", re.compile(r"(20\d{2})\s*年第\s*([一二三四1-4])\s*季度|Q\s*([1-4])\s*/?\s*(20\d{2})|(20\d{2})Q([1-4])", re.I)),
    ("half", re.compile(r"(20\d{2})\s*(?:年)?\s*[上下]半年|H\s*([12])\s*(20\d{2})", re.I)),
    ("month", re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", re.I)),
]
_DATE_PATTERNS = [
    re.compile(r"(?:发布日期|公布日期|publication date|published on|released?)[:：\s]*(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)", re.I),
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]
_VALIDITY_PATTERN = re.compile(
    r"(?:有效期|适用期间|valid from|effective)\s*[:：]?\s*"
    r"(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)\s*(?:至|到|-|~|to)\s*"
    r"(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?)"
)


def _excerpt(text: str, position: int) -> str:
    start = max(0, position - 40)
    return re.sub(r"\s+", " ", text[start : position + _MAX_EVIDENCE_EXCERPT]).strip()


def _candidate(value: Any, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"value": value, "evidence": evidence or [], "origin": "deterministic"}


def _normalize_date(parts: tuple[str, ...]) -> str:
    joined = " ".join(part for part in parts if part)
    match = re.search(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", joined)
    if not match:
        return ""
    try:
        return (
            f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        )
    except ValueError:
        return ""


def _detect_language(text: str) -> str:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk > latin:
        return "zh"
    if latin > cjk:
        return "en"
    return "unknown"


async def _owner_aliases(owner_id: str) -> dict[str, list[dict[str, Any]]]:
    """Map each confirmed alias to its owner entities (possibly ambiguous)."""
    owner_id = document_owner_id(owner_id)
    pool = await get_document_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT a.alias, e.id AS entity_id, e.entity_kind,
                      coalesce(e.name_zh, e.name_en) AS name
                 FROM knowledge_entity_aliases a
                 JOIN knowledge_entities e ON e.id=a.entity_id
                WHERE e.owner_id=$1::uuid""",
            owner_id,
        )
    aliases: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        aliases.setdefault(str(row["alias"]).lower(), []).append(
            {
                "entity_id": str(row["entity_id"]),
                "kind": row["entity_kind"],
                "name": row["name"],
            }
        )
    return aliases


def deterministic_candidates(
    units: list[StructuredUnit], filename: str
) -> dict[str, Any]:
    """Extract type, dates, period, language and validity with evidence."""
    candidates: dict[str, Any] = {}
    joined_first = "\n".join(unit.index_text for unit in units[:12])

    for doc_type, pattern in _DOC_TYPE_PATTERNS:
        evidence: list[dict[str, Any]] = []
        match = pattern.search(filename)
        if match:
            evidence.append({"unit": None, "excerpt": f"filename:{match.group(0)}"})
        for index, unit in enumerate(units[:30]):
            match = pattern.search(unit.index_text)
            if match:
                evidence.append(
                    {"unit": index, "excerpt": _excerpt(unit.index_text, match.start())}
                )
                break
        if evidence:
            candidates["doc_type"] = _candidate(doc_type, evidence)
            break

    for index, unit in enumerate(units[:30]):
        match = _VALIDITY_PATTERN.search(unit.index_text)
        if match:
            start = _normalize_date((match.group(1),))
            end = _normalize_date((match.group(2),))
            if start and end:
                candidates["validity"] = _candidate(
                    {"start": start, "end": end},
                    [{"unit": index, "excerpt": _excerpt(unit.index_text, match.start())}],
                )
                break

    for index, unit in enumerate(units[:30]):
        for label, pattern in _PERIOD_PATTERNS:
            match = pattern.search(unit.index_text)
            if not match:
                continue
            groups = [group or "" for group in match.groups()]
            digits = [group for group in groups if group.isdigit()]
            if not digits:
                continue
            candidates["period"] = _candidate(
                {"kind": label, "label": match.group(0).strip(), "year": digits[0][:4]},
                [{"unit": index, "excerpt": _excerpt(unit.index_text, match.start())}],
            )
            break
        if "period" in candidates:
            break

    for index, unit in enumerate(units[:30]):
        for pattern in _DATE_PATTERNS:
            match = pattern.search(unit.index_text)
            if match:
                normalized = _normalize_date(match.groups())
                if normalized:
                    candidates["publish_date"] = _candidate(
                        normalized,
                        [{"unit": index, "excerpt": _excerpt(unit.index_text, match.start())}],
                    )
                break
        if "publish_date" in candidates:
            break

    candidates["language"] = _candidate(_detect_language(joined_first or filename))
    return candidates


async def suggest_company_candidates(
    owner_id: str, units: list[StructuredUnit], filename: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """Match owner-confirmed entity aliases; ambiguous aliases are flagged."""
    aliases = await _owner_aliases(owner_id)
    if not aliases:
        return None, []
    text_pool = [filename, *(unit.index_text for unit in units[:20])]
    lowered = "\n".join(text_pool).lower()
    matches: dict[str, dict[str, Any]] = {}
    flags: list[str] = []
    for alias, entities in aliases.items():
        if not alias or alias not in lowered:
            continue
        ambiguous = len(entities) > 1
        if ambiguous:
            flags.append(f"company_alias_ambiguous:{alias}")
        display = next(iter(entities))["name"] if not ambiguous else alias
        matches[str(entities[0]["entity_id"])] = {
            "value": {"entity_id": entities[0]["entity_id"], "name": display},
            "evidence": [{"unit": None, "excerpt": f"alias:{alias}"}],
            "origin": "deterministic",
            "ambiguous": ambiguous,
            "entities": entities if ambiguous else None,
        }
    if not matches:
        return None, []
    return {"company": list(matches.values())}, flags


async def _model_refinement(
    units: list[StructuredUnit], settings: DocumentSettings
) -> dict[str, Any] | None:
    """Refine suggestions with an optional LiteLLM text-model route."""
    model = settings.metadata_suggestion_model
    if not model:
        return None
    lead = "\n".join(
        unit.index_text for unit in units if unit.unit_type in {"title", "section_header"}
    )[:2000]
    headers = "\n".join(
        " | ".join(str(cell) for cell in unit.attributes.get("header", []))
        for unit in units
        if unit.unit_type == "table"
    )[:1500]
    body = "\n".join(unit.index_text for unit in units[:12])[: _MAX_MODEL_CHARS - len(lead) - len(headers)]
    prompt = (
        "从以下文档片段提取元数据，仅输出 JSON：{\"doc_type\":\"财报|价格表|产品文档|行业报告|新闻|内部材料|其他\","
        "\"publish_date\":\"YYYY-MM-DD 或空\",\"period\":\"如 FY2025 或空\",\"company\":[\"名称或空\"]}\n"
        f"标题与表头：\n{lead}\n{headers}\n正文片段：\n{body}"
    )
    try:
        from openai import AsyncOpenAI

        base_url = os.getenv("LITELLM_BASE_URL", "http://litellm-proxy:4000/v1")
        service_key = os.getenv("LITELLM_SERVICE_KEY", "")
        if not service_key:
            return None
        client = AsyncOpenAI(base_url=base_url, api_key=service_key, timeout=60.0)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        finally:
            await client.close()
        payload = json.loads(response.choices[0].message.content or "{}")
        if isinstance(payload, dict):
            payload["origin"] = "model"
            return payload
    except Exception:  # noqa: BLE001 - suggestion is best-effort only
        return None
    return None


async def build_suggestions(
    owner_id: str,
    units: list[StructuredUnit],
    filename: str,
    settings: DocumentSettings,
) -> dict[str, Any]:
    """Produce the metadata candidate block stored on a generation."""
    candidates = deterministic_candidates(units, filename)
    flags: list[str] = []
    company, company_flags = await suggest_company_candidates(owner_id, units, filename)
    flags.extend(company_flags)
    if company:
        candidates.update(company)
    refinement = await _model_refinement(units, settings)
    if refinement:
        for key in ("doc_type", "publish_date", "period", "company"):
            value = refinement.get(key)
            if value in (None, "", []):
                continue
            existing = candidates.get(key)
            if existing and existing.get("origin") == "deterministic" and existing.get("evidence"):
                # Deterministic evidence wins; the model only fills gaps.
                existing.setdefault("model_value", value)
            else:
                candidates[key] = {"value": value, "evidence": [], "origin": "model"}
    else:
        flags.append("metadata_model_skipped")
    # Fields without evidence stay absent on purpose (plan §KB-02).
    return {"candidates": candidates, "flags": flags}

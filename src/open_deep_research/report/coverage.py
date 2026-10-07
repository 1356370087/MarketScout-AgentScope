"""Deterministic coverage checklist extraction for report planning and evaluation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SCOPE_EXCLUSION_PREFIX = r"(?:(?:不|不要|无需|不需要)(?:研究|讨论|涉及|比较|分析)|不扩展到)"
_SCOPE_EXCLUSION_RE = re.compile(
    rf"^(?:请)?{_SCOPE_EXCLUSION_PREFIX}|^do\s+not\s+(?:research|discuss|cover|analy[sz]e)\b",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(
    rf"[。；;\n]+|(?<=\S)[，,](?=并|以及|同时|结合|区分|说明|提出|给出|评估|比较|{_SCOPE_EXCLUSION_PREFIX})"
)
_FINAL_LIST_CONJUNCTION_RE = re.compile(
    r"\s*(?P<conjunction>以及|和|与)\s*(?=\S)"
)
_NUMBERED_ITEM_RE = re.compile(r"(?<!\w)(\d{1,2})[.)]\s+")
_NUMBERED_SENTENCE_RE = re.compile(r"[。；;\n]+")
_GLOBAL_DIRECTIVE_RE = re.compile(
    r"(?<=[.!?。！？])\s+(?=(?:for all\b|finally\b|additionally\b|also\b|最后|此外|并(?:最终|另外)))",
    re.IGNORECASE,
)
_LEADING_RE = re.compile(
    r"^(?:截至\S+?[，,]\s*)?(?:请|需要|应当|应如何|报告应)?(?:重点)?(?:覆盖|比较|评估|分析|说明|区分|提出|给出)?\s*"
)
_BRACKETED_SECTION_RE = re.compile(r"【(?P<header>[^】\r\n]{1,100})】")
_DIMENSION_SECTION_RE = re.compile(
    r"^维度\s*[一二三四五六七八九十百\d]+\s*[：:]\s*(?P<label>.{1,80})$"
)
_SPECIAL_DIMENSION_HEADERS = frozenset({"关键对比要求"})
_PROCESS_SECTION_HEADERS = frozenset({"来源要求", "引用要求", "时间范围", "时间覆盖"})
_DELIVERABLE_SECTION_HEADERS = frozenset(
    {"报告结构要求", "报告结构", "输出形式", "篇幅要求", "格式要求"}
)
_OPENING_DELIMITERS = {"(": ")", "（": "）", "[": "]", "【": "】"}
_CLOSING_DELIMITERS = frozenset(_OPENING_DELIMITERS.values())

CoverageSectionKind = Literal["dimension", "process", "deliverable"]


def is_scope_exclusion(text: str) -> bool:
    """Recognize an explicit instruction to exclude a research topic."""
    return bool(_SCOPE_EXCLUSION_RE.match(text.strip()))


def source_directive_kind(text: str) -> str | None:
    """Identify complete source/writing instructions before list splitting."""
    value = text.strip(" 。；;")
    if re.fullmatch(r"(?:请)?(?:不将|不要将|不得将).{1,100}(?:视为|当作|作为).{0,30}(?:基准|基准数据|基准结果)", value):
        return "process"
    if re.fullmatch(r"(?:请)?(?:仅|只)(?:依据|基于|使用|参考|读取)(?:所选|指定|所提供的?).{0,80}(?:页面|网页|资料|文档|来源)", value):
        return "process"
    if re.fullmatch(r"(?:请)?(?:仅|只)(?:依据|基于|使用|参考|读取)[^，,。；;\n]{0,80}官方(?:文档|资料|来源|网站)", value):
        return "process"
    if re.fullmatch(r"(?:至少|最少)(?:引用|使用|提供)[一二两三四五六七八九十\d]+个[^。；;\n]{0,24}(?:来源|文档页面)(?:并给出可核验链接)?", value):
        return "process"
    if re.fullmatch(r"不需要(?:性能跑分|市场分析)(?:或(?:性能跑分|市场分析))?", value):
        return "process"
    if re.fullmatch(r"(?:报告|回答|输出)(?:使用|采用|用)(?:中文|英文)", value):
        return "deliverable"
    if re.fullmatch(r"控制在\s*\d+\s*(?:字|词|页)(?:左右|以内|内)?", value):
        return "deliverable"
    if re.fullmatch(r"(?:请)?(?:分别)?(?:保留|提供|给出|附上)(?:(?:该|此|所选|指定|官方|可核验|可点击|相关|对应|原文|网页|页面|资料|文档|来源|的|与|和|以及)|[、，,\s])*(?:引用|链接)", value):
        return "deliverable"
    return None


@dataclass(frozen=True, slots=True)
class CoverageSection:
    """One explicit source section with exact character offsets."""

    kind: CoverageSectionKind
    label: str
    source_start: int
    source_end: int
    body_start: int
    body_end: int


def _section_descriptor(header: str) -> tuple[CoverageSectionKind, str] | None:
    normalized = _SPACE_RE.sub(" ", header).strip()
    dimension = _DIMENSION_SECTION_RE.fullmatch(normalized)
    if dimension is not None:
        return "dimension", dimension.group("label").strip()
    if normalized in _SPECIAL_DIMENSION_HEADERS:
        return "dimension", normalized
    if normalized in _PROCESS_SECTION_HEADERS:
        return "process", normalized
    if normalized in _DELIVERABLE_SECTION_HEADERS:
        return "deliverable", normalized
    return None


def derive_coverage_sections(text: str) -> tuple[CoverageSection, ...]:
    """Locate supported bracketed sections before normalizing whitespace."""
    value = text or ""
    recognized: list[tuple[re.Match[str], CoverageSectionKind, str]] = []
    for match in _BRACKETED_SECTION_RE.finditer(value):
        descriptor = _section_descriptor(match.group("header"))
        if descriptor is not None:
            recognized.append((match, *descriptor))

    sections: list[CoverageSection] = []
    for index, (match, kind, label) in enumerate(recognized):
        source_end = (
            recognized[index + 1][0].start()
            if index + 1 < len(recognized)
            else len(value)
        )
        while source_end > match.end() and value[source_end - 1].isspace():
            source_end -= 1
        body_start = match.end()
        while body_start < source_end and value[body_start].isspace():
            body_start += 1
        sections.append(
            CoverageSection(
                kind=kind,
                label=label,
                source_start=match.start(),
                source_end=source_end,
                body_start=body_start,
                body_end=source_end,
            )
        )
    return tuple(sections)


def _split_top_level(value: str, separators: frozenset[str]) -> list[str]:
    """Split only outside balanced ASCII and full-width grouping marks."""
    parts: list[str] = []
    closing_stack: list[str] = []
    start = 0
    for index, character in enumerate(value):
        expected_closing = _OPENING_DELIMITERS.get(character)
        if expected_closing is not None:
            closing_stack.append(expected_closing)
        elif character in _CLOSING_DELIMITERS:
            if closing_stack and character == closing_stack[-1]:
                closing_stack.pop()
        elif not closing_stack and character in separators:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _numbered_candidates(clean: str) -> list[str] | None:
    """Keep numbered deliverables atomic across commas, but not across sentences.

    Sentence-level splitting inside every numbered item keeps the trailing
    global constraints (来源要求/报告结构/篇幅/时间覆盖…) out of the last
    dimension item: without it the last marker absorbs everything to EOF and a
    factual dimension bundled with brevity constraints classifies wholesale as
    a non-delegable deliverable, leaving the dimension with no hard owner.
    """
    markers = list(_NUMBERED_ITEM_RE.finditer(clean))
    ordinals = [int(marker.group(1)) for marker in markers]
    if (
        len(markers) < 2
        or ordinals[0] != 1
        or any(
            current != previous + 1
            for previous, current in zip(ordinals, ordinals[1:])
        )
    ):
        return None

    candidates: list[str] = []
    preamble = clean[: markers[0].start()].strip(" ：:。. ")
    if preamble:
        candidates.append(preamble)
    for index, marker in enumerate(markers):
        end = (
            markers[index + 1].start()
            if index + 1 < len(markers)
            else len(clean)
        )
        item = clean[marker.end() : end]
        for sentence in _NUMBERED_SENTENCE_RE.split(item):
            candidates.extend(
                part.strip(" ：:。. ")
                for part in _GLOBAL_DIRECTIVE_RE.split(sentence)
                if part.strip(" ：:。. ")
            )
    return candidates


def _split_final_list_conjunction(part: str) -> list[str]:
    """Split only a plausible final list pair, never a word-internal match."""
    for match in reversed(list(_FINAL_LIST_CONJUNCTION_RE.finditer(part))):
        marker_start = match.start("conjunction")
        marker_end = match.end("conjunction")
        if (
            match.group("conjunction") == "和"
            and marker_start > 0
            and marker_end < len(part)
            and part[marker_start - 1 : marker_end + 1] == "共和国"
        ):
            continue
        left = part[: match.start()].strip()
        right = part[match.end() :].strip()
        # ``主要参与者`` contains the single character ``与`` but leaves only
        # ``者`` on the right.  Real checklist items on both sides have at
        # least two normalized characters (for example ``成本和安全性``).
        if all(len(re.sub(r"\W+", "", item)) >= 2 for item in (left, right)):
            return [
                nested
                for item in (left, right)
                for nested in _split_final_list_conjunction(item)
            ]
    return [part]


def derive_coverage_checklist(text: str, *, max_items: int = 20) -> list[str]:
    """Extract explicit deliverables from a user question or research brief.

    The checklist is deliberately deterministic: it introduces no new factual
    requirements and can be reproduced by the writer and the Judge.
    """
    clean = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()
    clean = re.sub(r"^截至[^，,。；;]+[，,]\s*", "", clean)
    if not clean:
        return []
    checklist: list[str] = []
    seen: set[str] = set()
    for group in derive_coverage_units(clean):
        for candidate in group:
            item = re.sub(
                r"^(?:以及|并且|同时|并|和|与)\s*", "", candidate
            ).strip()
            key = re.sub(r"\W+", "", item).lower()
            # Explicit list items such as "成本" and "安全性" are valid, compact
            # requirements.  Reject only one-character fragments, which are much
            # more likely to be punctuation/splitting noise.
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            checklist.append(item[:500])
            if len(checklist) >= max_items:
                return checklist
    return checklist or [clean[:500]]


def derive_coverage_units(text: str, *, max_clauses: int = 40) -> list[list[str]]:
    """Return checklist items grouped by their source clause.

    Each inner list holds the split items of one top-level clause (or one
    numbered deliverable kept atomic).  The coverage-contract compiler uses
    these groups to degrade granularity from item level to clause level when
    the factual budget cannot hold every split item.
    """
    clean = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", text or "")).strip()
    clean = re.sub(r"^截至[^，,。；;]+[，,]\s*", "", clean)
    if not clean:
        return []
    numbered = _numbered_candidates(clean)
    if numbered is not None:
        groups: list[list[str]] = []
        for candidate in numbered:
            item = candidate.strip(" ：:。. ")
            if item:
                groups.append([item[:500]])
        return groups[:max_clauses]
    groups = []
    for clause in _CLAUSE_RE.split(clean):
        if source_directive_kind(clause):
            groups.append([clause.strip(" ：:。. ")[:500]])
            continue
        clause = _LEADING_RE.sub("", clause).strip(" ：:。. ")
        if not clause:
            continue
        if is_scope_exclusion(clause):
            # Keep negation attached to the entire list. Splitting "不研究 A、B"
            # into two requirements would turn B into a positive obligation.
            groups.append([clause[:500]])
            continue
        parts = _split_top_level(clause, frozenset({"、", ",", "，"}))
        # Chinese enumerations commonly use a delimiter for the first
        # items and a conjunction for the final pair (``A、B 和 C``).
        # Once an explicit list delimiter is present, keep the final item
        # atomic as well instead of merging two independently delegable
        # coverage requirements into one impossible Subagent contract.
        if len(parts) > 1:
            expanded_parts = [
                nested
                for part in parts
                for nested in ([part] if source_directive_kind(part) else _split_final_list_conjunction(part))
            ]
        else:
            expanded_parts = parts
        items = [
            part.strip(" ：:。. ")
            for part in expanded_parts
            if part.strip(" ：:。. ")
        ]
        if items:
            groups.append([item[:500] for item in items])
    return groups[:max_clauses]


def derive_state_coverage_checklist(
    state: dict,
    *,
    max_items: int = 48,
) -> list[str]:
    """Derive requirements from original user messages before the model brief."""
    contract = _validated_state_coverage_contract(state)
    if contract is not None and contract.schema_version >= 2:
        from open_deep_research.quality.contract import (
            coverage_requirement_display_text,
        )

        return [
            coverage_requirement_display_text(contract, requirement)
            for requirement in contract.requirements[:max_items]
        ]

    source_texts: list[str] = []
    for message in state.get("messages", []):
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "")
            content = message.get("content", "")
        else:
            role = str(getattr(message, "type", ""))
            content = getattr(message, "content", "")
        if role in {"user", "human"} and content:
            source_texts.append(str(content))
    if not source_texts and state.get("research_brief"):
        source_texts.append(str(state["research_brief"]))

    requirements: list[str] = []
    seen: set[str] = set()
    for text in source_texts:
        for requirement in derive_coverage_checklist(text, max_items=max_items):
            key = re.sub(r"\W+", "", requirement).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            requirements.append(requirement)
            if len(requirements) >= max_items:
                return requirements
    return requirements


def render_coverage_checklist(items: list[str]) -> str:
    """Render checklist instructions for a report writer without exposing them."""
    if not items:
        return ""
    rows = "\n".join(f"COV-{index:02d}: {item}" for index, item in enumerate(items, 1))
    return (
        "<Coverage Checklist>\n"
        "Use this checklist internally before and after drafting. Address every item "
        "with evidence, or explicitly state that evidence is unavailable/uncertain. "
        "Do not silently omit an item and do not print checklist IDs in the report.\n"
        f"{rows}\n"
        "</Coverage Checklist>"
    )


def _validated_state_coverage_contract(state: Mapping[str, Any]) -> Any | None:
    """Load a persisted coverage contract without changing legacy payloads."""
    from open_deep_research.quality.contract import ResearchCoverageContract

    raw = state.get("coverage_contract")
    if isinstance(raw, Mapping) and raw.get("type") == "override":
        raw = raw.get("value")
    if isinstance(raw, ResearchCoverageContract):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        return ResearchCoverageContract.model_validate(dict(raw))
    except ValueError:
        return None


def render_state_coverage_checklist(
    state: Mapping[str, Any],
    *,
    max_items: int = 48,
) -> str:
    """Render v2 atomic coverage plus derived parent status for report writing."""
    contract = _validated_state_coverage_contract(state)
    if (
        contract is None
        or contract.schema_version < 2
        or not contract.dimensions
    ):
        return render_coverage_checklist(
            derive_state_coverage_checklist(dict(state), max_items=max_items)
        )

    from open_deep_research.quality.contract import (
        CoverageStatus,
        aggregate_dimension_coverage,
        coverage_requirement_display_text,
    )

    raw_ledger = state.get("coverage_ledger", {})
    if isinstance(raw_ledger, Mapping) and raw_ledger.get("type") == "override":
        raw_ledger = raw_ledger.get("value", {})
    ledger = raw_ledger if isinstance(raw_ledger, Mapping) else {}
    summaries = {
        summary.dimension_id: summary
        for summary in aggregate_dimension_coverage(contract, ledger)
    }
    requirement_by_id = {
        requirement.requirement_id: requirement
        for requirement in contract.requirements
    }
    rows: list[str] = []
    grouped_ids: set[str] = set()
    emitted = 0
    for dimension in contract.dimensions:
        summary = summaries[dimension.dimension_id]
        rows.append(
            f"[{dimension.dimension_id}] {dimension.label}: "
            f"derived_status={summary.status.value}"
        )
        unmet: list[str] = []
        for requirement_id in dimension.requirement_ids:
            if emitted >= max_items:
                break
            requirement = requirement_by_id.get(requirement_id)
            if requirement is None:
                continue
            grouped_ids.add(requirement_id)
            entry = ledger.get(requirement_id, {})
            status = (
                str(entry.get("status", CoverageStatus.UNSUPPORTED.value))
                if isinstance(entry, Mapping)
                else CoverageStatus.UNSUPPORTED.value
            )
            display_text = coverage_requirement_display_text(
                contract,
                requirement,
            )
            rows.append(
                f"  {requirement_id}: {display_text} — atomic_status={status}"
            )
            if status != CoverageStatus.SUPPORTED.value:
                unmet.append(f"{requirement_id} — {display_text}")
            emitted += 1
        rows.append(
            "  Unmet atomic requirements: "
            + ("; ".join(unmet) if unmet else "none")
        )
        if emitted >= max_items:
            break

    for requirement in contract.requirements:
        if emitted >= max_items:
            break
        if requirement.requirement_id in grouped_ids:
            continue
        rows.append(
            f"[standalone] {requirement.requirement_id}: "
            f"{coverage_requirement_display_text(contract, requirement)}"
        )
        emitted += 1
    return (
        "<Coverage Checklist>\n"
        "Parent dimension statuses below are derived from the atomic coverage "
        "ledger; parent dimensions are context only and are not gate "
        "requirements. Address every atomic item with accepted evidence, or "
        "state the limitation explicitly. Do not print checklist IDs or status "
        "metadata in the report.\n"
        + "\n".join(rows)
        + "\n</Coverage Checklist>"
    )

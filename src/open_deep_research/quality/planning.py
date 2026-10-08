"""Compile model-extracted atoms only when grounded in the original user text."""

import hashlib
import re

from open_deep_research.quality.contract import (
    CoverageRequirement,
    ResearchCoverageContract,
    _message_content,
    _message_role,
    _stable_requirement_id,
    build_research_coverage_contract,
    classify_requirement_kind,
)


def compile_planned_requirements(messages, atoms, *, brief=""):
    """Retain provenance and deterministic constraints without inventing facts."""
    original = {i: _message_content(m) for i, m in enumerate(messages)
                if _message_role(m) in {"user", "human"}}
    fallback = build_research_coverage_contract(messages, advisory_dimensions=[brief])
    selected = []
    for atom in atoms:
        row = atom.model_dump() if hasattr(atom, "model_dump") else atom
        text = row["source_text"].strip()
        index = row.get("source_message_index", 0)
        content = original.get(index, "")
        start = content.find(text)
        if not text or start < 0:
            # Model paraphrases never gain authority. Exact source clauses below
            # preserve obligations if the model failed to quote them correctly.
            continue
        kind = classify_requirement_kind(text)
        if kind == "factual" and not ("？" in text or "?" in text):
            kind = row["kind"]
        selected.append((index, start, start + len(text), text, kind))
    expanded = []
    for i, start, end, text, kind in selected:
        markers = list(re.finditer(r"[（(]\d{1,2}[）)]", text)) if kind == "factual" else []
        if len(markers) > 1:
            for n, marker in enumerate(markers):
                a = start + marker.start()
                b = start + (markers[n + 1].start() if n + 1 < len(markers) else len(text))
                while b > a and original[i][b - 1] in "；;。 \r\n":
                    b -= 1
                expanded.append((i, a, b, original[i][a:b], "factual"))
        else:
            expanded.append((i, start, end, text, kind))
    selected = expanded
    # Explicit numbered clauses are user-authored structure. Keep all of them,
    # independent of the model's grouping, quoting or omission choices.
    for i, content in original.items():
        markers = list(re.finditer(r"[（(]\d{1,2}[）)]", content))
        if len(markers) < 2:
            continue
        for n, marker in enumerate(markers):
            a = marker.start()
            b = markers[n + 1].start() if n + 1 < len(markers) else len(content)
            ending = re.search(r"[。\n]", content[a:b])
            if ending:
                b = a + ending.start()
            while b > a and content[b - 1] in "；;。 \r\n":
                b -= 1
            selected = [row for row in selected if not (row[0] == i and row[4] == "factual" and row[1] < b and row[2] > a)]
            selected.append((i, a, b, content[a:b], classify_requirement_kind(content[a:b])))
    if not any(row[4] == "factual" for row in selected):
        return fallback
    if sum(row[4] == "factual" for row in selected) > 2:
        selected = [(i, a, b, text, "deliverable" if a == 0 and re.match(r"(?:研究主题[：:]|面向.{0,40}(?:助手|场景)[，,])", text) else kind)
                    for i, a, b, text, kind in selected]
    output_atoms = []
    for i, start, end, text, kind in selected:
        if kind == "process":
            for match in re.finditer(r"(?:至少引用[^。；;]+|标明适用版本)", text):
                output_atoms.append((i, start + match.start(), start + match.end(), match.group(), "deliverable"))
    selected.extend(output_atoms)
    # A model extraction cannot silently remove an explicit question.
    for index, content in original.items():
        for match in re.finditer(r"[^。\n；;？?]+[？?]", content):
            start, end = match.span()
            fragment = match.group()
            marker = list(re.finditer(r"(?:^|\s)[（(]?\d+[.)）、]\s*", fragment))
            if marker:
                start += marker[-1].end()
            while start < end and content[start].isspace():
                start += 1
            if not any(i == index and a <= start and b >= end for i, a, b, _, _ in selected):
                selected.append((index, start, end, content[start:end], "factual"))
    for req in fallback.requirements:
        # Preserve original clauses that the extraction has not represented.
        if req.kind != "factual" and not any(i == req.source_message_index and a <= req.source_start and b >= req.source_end
                                            for i, a, b, _, _ in selected):
            selected.append((req.source_message_index, req.source_start, req.source_end, req.text, req.kind))
    unique = {}
    for row in sorted(selected):
        unique.setdefault((row[0], row[1], row[2]), row)
    requirements = tuple(CoverageRequirement(
        requirement_id=_stable_requirement_id(i, text, n), text=text, kind=kind,
        source_message_index=i, source_start=start, source_end=end,
    ) for n, (i, start, end, text, kind) in enumerate(unique.values(), 1))
    return ResearchCoverageContract(
        original_query_sha256=fallback.original_query_sha256, requirements=requirements,
        single_research_task=fallback.single_research_task, advisory_dimensions=fallback.advisory_dimensions,
    )


def unique_evidence(records):
    """Merge repeated IDs while retaining requirement bindings and full text."""
    merged = {}
    for record in records:
        key = record.get("evidence_id") or hashlib.sha256(repr(record).encode()).hexdigest()
        if key not in merged:
            merged[key] = dict(record)
        else:
            merged[key]["requirement_ids"] = list(dict.fromkeys([
                *merged[key].get("requirement_ids", []), *record.get("requirement_ids", []),
            ]))
    return list(merged.values())


def source_intent_from_user(messages):
    """Distinguish first-party preference from an exclusive source obligation."""
    text = "\n".join(_message_content(m) for m in messages if _message_role(m) in {"user", "human"})
    if re.search(r"(?:仅|只|solely|only|exclusively).{0,100}(?:官网|官方网站|官方(?:文档|资料|来源)|official)", text, re.IGNORECASE):
        return "official_only"
    if re.search(r"(?:优先|尽量|prefer).{0,40}(?:官网|官方|official)", text, re.IGNORECASE):
        return "prefer_official"
    if re.search(r"(?:来自|来源|使用|依据).{0,40}(?:官网|官方网站)", text):
        return "official_only"
    return "unrestricted"

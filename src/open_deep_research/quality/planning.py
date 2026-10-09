"""Compile model-extracted atoms only when grounded in the original user text."""

import hashlib
import re

from open_deep_research.quality.contract import (
    CoverageRequirement,
    CoverageDimension,
    ResearchCoverageContract,
    _message_content,
    _message_role,
    _stable_requirement_id,
    _stable_dimension_id,
    build_research_coverage_contract,
    classify_requirement_kind,
    research_delegation_scopes,
)


def compile_planned_requirements(messages, atoms, *, brief="", atomic=False, entities=()):
    """Retain provenance and deterministic constraints without inventing facts."""
    original = {i: _message_content(m) for i, m in enumerate(messages)
                if _message_role(m) in {"user", "human"}}
    fallback = build_research_coverage_contract(messages, advisory_dimensions=[brief])
    selected = []
    facet_parents = []
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
        facets = []
        if atomic and kind == "factual":
            for facet in row.get("facets", []):
                facet = str(facet).strip()
                offset = text.find(facet)
                if facet and offset >= 0:
                    facets.append((index, start + offset, start + offset + len(facet), facet, classify_requirement_kind(facet)))
        if facets:
            facet_parents.append((index, start, start + len(text), text))
            selected.extend(facets)
            if not _fully_quoted(text, start, facets):
                selected.append((index, start, start + len(text), text, kind))
        else:
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
    parents = []
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
            finer = [row for row in selected if row[0] == i and row[4] == "factual" and a <= row[1] < row[2] <= b
                     and (row[1], row[2]) != (a, b)] if atomic else []
            selected = [row for row in selected if not (row[0] == i and row[4] == "factual" and row[1] < b and row[2] > a)]
            if finer:
                selected.extend(finer)
            if not finer or not _fully_quoted(content[a:b], a, finer):
                selected.append((i, a, b, content[a:b], classify_requirement_kind(content[a:b])))
            if atomic:
                parents.append((i, a, b, content[a:b]))
    for parent in facet_parents:
        if not any(parent[0] == row[0] and parent[1] < row[2] and parent[2] > row[1] for row in parents):
            parents.append(parent)
    parents = sorted(dict.fromkeys(parents))
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
    comparison_subjects = _comparison_subjects(original, entities) if atomic else []
    requirements = []
    parent_rows = []
    for ordinal, (i, a, b, text) in enumerate(parents, 1):
        parent_rows.append(CoverageDimension(dimension_id=_stable_dimension_id(i, text, ordinal),
            label=text, text=text, source_message_index=i, source_start=a, source_end=b))
    for i, start, end, text, kind in unique.values():
        parent = next((row for row in parent_rows if row.source_message_index == i
                       and row.source_start <= start and end <= row.source_end), None) if kind == "factual" else None
        subjects = []
        if kind == "factual" and comparison_subjects:
            # An explicitly named subject narrows its own clause; otherwise the
            # comparison's named subjects each retain a separate obligation.
            context = parent.text if parent else text
            subjects = [name for name in comparison_subjects if name.casefold() in context.casefold()] or comparison_subjects
        for subject in subjects or [None]:
            n = len(requirements) + 1
            requirements.append(CoverageRequirement(
                requirement_id=_stable_requirement_id(i, f"{subject}: {text}" if subject else text, n),
                text=text, kind=kind, subject=subject, dimension_id=parent.dimension_id if parent else None,
                source_message_index=i, source_start=start, source_end=end))
    dimensions = tuple(row.model_copy(update={"requirement_ids": tuple(
        r.requirement_id for r in requirements if r.dimension_id == row.dimension_id)}) for row in parent_rows
        if any(r.dimension_id == row.dimension_id for r in requirements))
    compiled = ResearchCoverageContract(
        schema_version=4 if atomic else fallback.schema_version,
        original_query_sha256=fallback.original_query_sha256, requirements=tuple(requirements), dimensions=dimensions,
        single_research_task=fallback.single_research_task, advisory_dimensions=fallback.advisory_dimensions,
    )
    if atomic:
        compiled = compiled.model_copy(update={"delegation_scopes": {
            row["scope_id"]: tuple(row["requirement_ids"]) for row in research_delegation_scopes(compiled)
        }})
    return compiled


def _fully_quoted(text, start, rows):
    """A model may split a source clause, but cannot silently omit its words."""
    remaining = list(text)
    for _, a, b, _, _ in rows:
        remaining[max(0, a - start):min(len(text), b - start)] = " " * (min(len(text), b - start) - max(0, a - start))
    value = re.sub(r"[（(]\d{1,2}[）)]", "", "".join(remaining))
    value = re.sub(r"\b(?:and|or)\b|以及|及|与|和|并", "", value, flags=re.IGNORECASE)
    return not re.search(r"\w", value)


def _comparison_subjects(original, entities):
    """Bind subjects only from an explicit comparison in the original input."""
    names = [row.name if hasattr(row, "name") else row["name"] for row in entities]
    for content in original.values():
        for match in re.finditer(r"(?:比较|对比|对照|\bcompare\b|\bcomparison of\b)[^。；;\n!?]{1,240}", content, re.IGNORECASE):
            found = [(match.group().casefold().find(name.casefold()), name) for name in names if name]
            found = sorted((position, name) for position, name in found if position >= 0)
            if len(found) >= 2:
                return list(dict.fromkeys(name for _, name in found))
    return []


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

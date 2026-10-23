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
    _split_top_level_dimension_list,
    build_research_coverage_contract,
    classify_requirement_kind,
    research_delegation_scopes,
)


def compile_planned_requirements(messages, atoms, *, brief="", atomic=False, entities=(), preserve_conditions=False, preserve_scope=False, task_scopes=False):
    """Retain provenance and deterministic constraints without inventing facts."""
    original = {i: _message_content(m) for i, m in enumerate(messages)
                if _message_role(m) in {"user", "human"}}
    fallback = build_research_coverage_contract(messages, advisory_dimensions=[brief])
    marker_pattern = r"[（(]\d{1,2}[）)]|(?<![\w.])\d{1,2}[）)]" if preserve_scope else r"[（(]\d{1,2}[）)]"
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
            if not _fully_quoted(text, start, facets, marker_pattern=marker_pattern):
                selected.append((index, start, start + len(text), text, kind))
        else:
            selected.append((index, start, start + len(text), text, kind))
    expanded = []
    for i, start, end, text, kind in selected:
        markers = list(re.finditer(marker_pattern, text)) if kind == "factual" else []
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
        markers = list(re.finditer(marker_pattern, content))
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
            if not finer or not _fully_quoted(content[a:b], a, finer, marker_pattern=marker_pattern):
                selected.append((i, a, b, content[a:b], classify_requirement_kind(content[a:b])))
            if atomic:
                parents.append((i, a, b, content[a:b]))
    for parent in facet_parents:
        if not any(parent[0] == row[0] and parent[1] < row[2] and parent[2] > row[1] for row in parents):
            parents.append(parent)
    parents = sorted(dict.fromkeys(parents))
    qualified = [(i, start, end) for i, content in original.items()
                 for start, end in _qualified_question_spans(content)] if preserve_conditions else []
    if not any(row[4] == "factual" for row in selected):
        if not qualified:
            return fallback
        selected = [(r.source_message_index, r.source_start, r.source_end, r.text, r.kind) for r in fallback.requirements]
    if preserve_conditions:
        for i, start, end in qualified:
            content = original[i]
            # A scenario/claim to verify is not an asserted fact. Keep its
            # governing condition with the question, even if a model facet
            # quotes the antecedent verbatim and labels it factual.
            selected = [row for row in selected if not (row[0] == i and start <= row[1] and row[2] <= end)]
            selected.append((i, start, end, content[start:end], "factual"))
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
    if preserve_scope:
        # 提取器用省略号改写场景时，该 atom 会因不是原文而被拒绝；仍需
        # 从用户消息保留带明确标签的完整场景段落，作为计算/适用范围前提。
        # 不删除其中单独提取的事实问题，也不把给定参数变成待外部证明的事实。
        for i, content in original.items():
            for match in re.finditer(
                r"(?:^|\n)[ \t]*(?:(?:统一)?(?:业务)?场景|背景|scenario|context)\s*[：:][^\n]+",
                content, re.IGNORECASE,
            ):
                text = match.group().strip()
                if re.search(r"[?？]|是否|能否|请说明|(?<!待)(?:核实|验证)|\b(?:whether|how|verify|explain|check)\b",
                             text, re.IGNORECASE):
                    continue
                start = match.start() + len(match.group()) - len(match.group().lstrip())
                selected.append((i, start, start + len(text), text, "process"))
        # An explicitly supplied scenario is a premise, not an external fact
        # that a researcher must prove. Questions/verification within it remain
        # factual; source positions preserve the label even when the model omits it.
        scenarios = [(i, match.start(), match.end("body")) for i, text in original.items()
            for match in re.finditer(r"(?:^|[。\n])\s*(?:场景|背景|scenario|context)\s*[：:]\s*(?P<body>[^。\n]+)", text, re.I)
            if not re.search(r"[?？]|是否|能否|请说明|核实|验证|\b(?:whether|how|verify|explain|check)\b", match["body"], re.I)]
        selected = [(i, a, b, text, "process" if any(i == owner and start <= a < b <= end
                    for owner, start, end in scenarios) else kind) for i, a, b, text, kind in selected]
        # A model can quote the goal and its supplied scenario as one span.
        # Keep such a non-question preamble as scope, including across sentences.
        question = r"[?？]|是否|能否|如何|怎么|为何|什么|核实|验证|\b(?:whether|how|what|why|verify|check)\b"
        prefixes = {i: match.start() for i, text in original.items()
                    if (match := re.search(marker_pattern, text))
                    and any(row[0] == i and row[4] == "factual" and row[1] >= match.start() for row in selected)}
        # An introductory request to produce a plan/report is an output goal.
        # Its numbered factual questions already own the external research;
        # making the title another fact duplicates every sibling obligation.
        output_goal = r"^(?:(?:研究|撰写|编写|制定|设计).*(?:方案|报告|综述|计划)|(?:research|write|prepare|produce)\b.*\b(?:plan|report|strategy|roadmap))\s*[。.!！]?$"
        selected = [(i, a, b, text, "deliverable" if kind == "factual" and b <= prefixes.get(i, -1)
                    and not re.search(question, text, re.I) and re.search(output_goal, text.strip(), re.I)
                    else kind) for i, a, b, text, kind in selected]
        selected = [(i, a, b, text, "process" if kind == "factual" and b <= prefixes.get(i, -1)
                    and not re.search(question, text, re.I)
                    and any(i == owner and a < end and start < b for owner, start, end in scenarios)
                    else kind) for i, a, b, text, kind in selected]
        # If a full source clause must remain, its nested fragments are not
        # additional obligations. Fully covering facets already removed their
        # parent above; this preserves each question without counting it twice.
        selected = [row for row in selected if not any(
            row[0] == parent[0] and row[4] == parent[4]
            and parent[1] <= row[1] < row[2] <= parent[2]
            and (row[1], row[2]) != (parent[1], parent[2])
            for parent in selected)]
        # Explicit object-aspect lists must not depend on whether the model
        # happened to provide facets. One covered aspect cannot stand for the
        # whole list. Keep the shared object in the parent dimension and retain
        # each child's exact original span; do not split relational questions.
        expanded, parsed = list(selected), []
        candidates = [*parents, *((i, a, b, text) for i, a, b, text, kind in selected if kind == "factual")]
        for i, a, b, text in candidates:
            if any(owner == i and start <= a < b <= end for owner, start, end in parsed):
                continue
            facets = _enumerated_object_facets(text)
            if not facets:
                continue
            if not any(owner == i and start <= a < b <= end for owner, start, end, _ in parents):
                parents.append((i, a, b, text))
            expanded = [row for row in expanded if not (row[0] == i and row[4] == "factual" and a <= row[1] < row[2] <= b)]
            expanded.extend((i, a + offset, a + offset + len(facet), facet, "factual") for offset, facet in facets)
            parsed.append((i, a, b))
        selected = expanded
        parents = sorted(dict.fromkeys(parents))
    if task_scopes:
        selected = _task_obligations(original, selected, entities)
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


def _task_obligations(original, selected, entities):
    """Keep quoted premises and synthesis goals out of external fact checking."""
    subjects = _comparison_subjects(original, entities)
    scenarios = [(i, m.start(), m.end()) for i, content in original.items()
                 for m in re.finditer(r"(?:^|\n)[ \t]*(?:(?:统一)?(?:业务)?场景|背景|scenario|context)\s*[：:][^\n]+",
                                      content, re.I)]
    output = []
    for i, a, b, text, kind in selected:
        if kind != "factual":
            output.append((i, a, b, text, kind))
            continue
        # A roster/selection goal does not require each worker to prove the
        # entire comparison. Concrete prices, differences and questions remain.
        if (len(subjects) >= 2 and re.search(r"(?:竞品|竞争|选型).{0,8}(?:研究|分析)", text)
                and sum(name in text for name in subjects) >= 2
                and not re.search(r"[?？]|价格|成本|区别|差异|为何|如何|是否|能否|条件|限制", text)):
            output.append((i, a, b, text, "deliverable"))
            continue
        if re.match(r"(?:资料核对截止|研究|检索)日期(?:为|是)", text.strip()):
            output.append((i, a, b, text, "process"))
            continue
        goal = re.search(r"[^，,；;。]*\bp95\b[^；;。]*(?:待验证|目标|不是已测)[^；;。]*", text, re.I)
        if goal and not re.search(r"[?？]|能否|是否|核实|如何|(?<!待)验证", goal.group()):
            prefix = text[:goal.start()].rstrip(" ，,")
            if prefix:
                output.append((i, a, a + len(prefix), prefix, "factual"))
            output.append((i, a + goal.start(), a + goal.end(), goal.group(), "process"))
            suffix = text[goal.end():].strip(" ，,。")
            if suffix:
                start = text.index(suffix, goal.end())
                output.append((i, a + start, a + start + len(suffix), suffix, "factual"))
            continue
        if (any(owner == i and start <= a < b <= end for owner, start, end in scenarios)
                and re.search(r"\d", text)
                and not re.search(r"[?？]|支持|开销|费用|如何|是否|能否|核实|核查|(?<!待)验证|需.{0,12}(?:识别|说明)|请说明", text)):
            kind = "process"
        output.append((i, a, b, text, kind))
    return output


def _enumerated_object_facets(text):
    """Split explicit noun facets following a shared object's 的 qualifier."""
    prefix, marker, tail = text.rpartition("的")
    # “混合检索、权限、重排的条件、计费”中的“的”属于一个列表项，
    # 不是整段的共同对象。不能只保留其后半段，丢掉前面的研究义务。
    if not marker or "、" in prefix or "、" not in tail or re.search(r"[?？]|分别|如何|怎样|是否|能否|之间|关系|差异|区别", tail):
        return []
    parts = _split_top_level_dimension_list(tail)
    if len(parts) < 2:
        return []
    result, cursor = [], len(prefix) + len(marker)
    for part in parts:
        offset = text.find(part, cursor)
        if offset < 0:
            return []
        result.append((offset, part))
        cursor = offset + len(part)
    return result


def _qualified_question_spans(content):
    """Keep conditional and claim-verification clauses source-addressable."""
    for match in re.finditer(r"[^。；;\n!?！？]+(?:[!?！？])?", content):
        text = match.group()
        condition = re.search(r"\b(?:if|when|assuming|given that)\b|如果|假如|假设|若|当.{1,80}(?:时|情况下)", text, re.IGNORECASE)
        verification = re.search(r"核实并纠正|验证.{0,12}说法|核实.{0,12}说法|\b(?:verify|check|correct|refute)\b.{0,35}\b(?:claim|whether|statement)\b", text, re.IGNORECASE)
        question = re.search(r"[?？]|是否|能否|如何|怎么|为何|什么|\b(?:whether|how|what)\b", text, re.IGNORECASE)
        if condition and question or verification:
            start, end = match.span()
            while start < end and content[start].isspace():
                start += 1
            while end > start and content[end-1].isspace():
                end -= 1
            yield start, end


def _fully_quoted(text, start, rows, *, marker_pattern=r"[（(]\d{1,2}[）)]"):
    """A model may split a source clause, but cannot silently omit its words."""
    remaining = list(text)
    for _, a, b, _, _ in rows:
        remaining[max(0, a - start):min(len(text), b - start)] = " " * (min(len(text), b - start) - max(0, a - start))
    value = re.sub(marker_pattern, "", "".join(remaining))
    value = re.sub(r"\b(?:and|or)\b|以及|及|与|和|并", "", value, flags=re.IGNORECASE)
    return not re.search(r"\w", value)


def _comparison_subjects(original, entities):
    """Bind subjects only from an explicit comparison in the original input."""
    names = [row.name if hasattr(row, "name") else row["name"] for row in entities]
    for content in original.values():
        for match in re.finditer(r"(?:比较|对比|对照|\bcompare\b|\bcomparison of\b)[^。；;\n!?]{1,240}", content, re.IGNORECASE):
            found = []
            for name in names:
                if not name.strip():
                    continue
                # 提取器可能省略产品名与部署形态间的“的”。只容忍这一语法
                # 差异，并返回用户原文；不把模型的别名或外部实体补进比较范围。
                pattern = r"\s*(?:的\s*)?".join(re.escape(part) for part in name.split())
                subject = re.search(pattern, match.group(), re.IGNORECASE)
                if subject is not None:
                    found.append((subject.start(), subject.group()))
            found.sort()
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


def prioritize_evidence(records, requirements, *, guidance=""):
    """Order whole records by the questions being answered, before confidence.

    Lexical matches guide selection only; they never certify a
    claim. Keep different requirements represented when the input is bounded.
    """
    from collections import Counter
    import math

    stop = {"the", "and", "for", "with", "from", "that", "this", "are", "how",
            "what", "does", "explain", "whether", "container", "official"}

    def terms(text):
        result = set()
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*|[\u4e00-\u9fff]+", text):
            if word.isascii():
                word = word.casefold()
                if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
                    word = word[:-1]
                if word not in stop:
                    result.add(word)
            else:
                result.update(word[i:i+2] for i in range(len(word)-1))
        return result

    records = list(records)
    rows = [row.model_dump() if hasattr(row, "model_dump") else row for row in requirements]
    queries = [terms(f"{row.get('subject') or ''} {row.get('text', '')}") for row in rows]
    if guidance:
        queries.append(terms(guidance))
    if not records or not any(queries):
        return records
    tokens = [terms(str(r.get("supporting_excerpt") or r.get("claim") or "")) for r in records]
    frequency = Counter(t for values in tokens for t in values)

    def score(index, query):
        return sum(1 + math.log((len(records) + 1) / (frequency[t] + 1))
                   for t in query.intersection(tokens[index]))

    ranked = [sorted(range(len(records)), key=lambda i: (-score(i, query), i))
              for query in queries if query]
    ordered, seen = [], set()
    for position in range(len(records)):
        for ranking in ranked:
            index = ranking[position]
            if index not in seen:
                ordered.append(records[index])
                seen.add(index)
    return ordered


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

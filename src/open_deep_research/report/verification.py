"""Evidence witnesses and behavioral example checks for the v20 report contract."""

import hashlib
import re


def evidence_passages(text):
    """Number contiguous source passages without changing or dropping text."""
    passages, pending = [], ""
    for line in text.splitlines(keepends=True):
        if pending and len(pending) + len(line) > 1800:
            passages.append(pending)
            pending = ""
        while len(line) > 1800:
            passages.append(line[:1800])
            line = line[1800:]
        pending += line
        if not line.strip():
            passages.append(pending)
            pending = ""
    if pending:
        passages.append(pending)
    return passages


def source_passage_id(evidence_id, index):
    """Bind one selectable reference to a record and its local passage."""
    return f"{evidence_id}:{index}"


def source_passage_view(records):
    """Expose referenceable original text instead of asking the model to copy it."""
    from .writing import compact_evidence
    result = []
    for record in compact_evidence(records):
        row = dict(record)
        excerpt = str(row.pop("supporting_excerpt", None) or row.get("claim") or "")
        if row.get("claim") == excerpt:
            row.pop("claim")
        row["source_passages"] = {str(index): text for index, text in enumerate(evidence_passages(excerpt))}
        result.append(row)
    return result


def _execution_units(text):
    """Expose commands separately so a service cannot mask unfinished work."""
    import yaml  # type: ignore[import-untyped]

    lines = text.splitlines()
    language = lines[0].lstrip("`~ ").lower() if lines else ""
    body = "\n".join(lines[1:-1])
    units = []
    if language in {"yaml", "yml", "json"}:
        seen = set()

        def visit(node, path, requires_exit=False):
            if not isinstance(node, (dict, list)) or id(node) in seen:
                return
            seen.add(id(node))
            if isinstance(node, dict):
                commands = {k: node[k] for k in ("command", "args", "entrypoint", "script") if k in node}
                if commands:
                    parts = [str(item) for value in commands.values() for item in (value if isinstance(value, list) else [value])]
                    # Shell -c/ -lc arguments are scripts, not shell-escaped argv.
                    script = parts[-1] if len(parts) >= 3 and parts[-2] in {"-c", "-lc"} else " ".join(parts)
                    unit = {"path": path, "code": script}
                    if requires_exit:
                        # A batch Job cannot finish normally while a primary
                        # container's command keeps running. Helpers remain
                        # separate; this is a manifest contract, not a verdict
                        # supplied by the model or the example's title.
                        unit["required_lifecycle"] = "finite"
                    units.append(unit)
                elif requires_exit and node.get("image"):
                    # The report's static checker cannot inspect an image's
                    # default entrypoint. A batch label alone proves no exit.
                    units.append({"path": path, "code": "",
                        "image": str(node["image"]), "required_lifecycle": "finite",
                        "image_entrypoint_unavailable": True})
                for key, value in node.items():
                    visit(value, f"{path}.{key}", batch_job and
                        path == pod_path and key == "containers")
            else:
                for index, value in enumerate(node):
                    visit(value, f"{path}[{index}]", requires_exit)
        try:
            for index, node in enumerate(yaml.safe_load_all(body)):
                batch_job = isinstance(node, dict) and node.get("kind") == "Job" and str(node.get("apiVersion", "")).startswith("batch/")
                pod_path = f"document[{index}].spec.template.spec"
                visit(node, f"document[{index}]")
        except (yaml.YAMLError, RecursionError):
            units = []  # The semantic review still receives the complete example.
    elif language in {"sh", "shell", "bash", "python", "py"}:
        units.append({"path": "script", "code": body})
    for index, unit in enumerate(units, 1):
        unit["unit_id"] = f"unit-{index}"
        # Deliberately narrow: recognize only a complete unconditional shell loop
        # without an explicit escape. Other programs require semantic analysis.
        loop = re.fullmatch(r"\s*while\s+(?:true|:)\s*[;\n]\s*do\b(.*?)\bdone\s*;?\s*", unit["code"], re.S)
        unit["unconditional_loop"] = bool(loop and not re.search(r"\b(?:break|exit|return|exec|trap)\b", loop[1]))
    return units


def example_inventory(regions):
    """Identify complete fenced examples without executing their contents."""
    examples = []
    for is_code, text in regions:
        if is_code:
            examples.append({"example_id": f"example-{len(examples) + 1}-"
                             + hashlib.sha256(text.encode()).hexdigest()[:12],
                             "code": text, "execution_units": _execution_units(text)})
    return examples


def _contains(text, excerpt):
    # Format-only line wrapping must not break a verbatim witness.
    return bool(excerpt.strip()) and " ".join(excerpt.split()) in " ".join(text.split())


def check_answers(review, requirements, markdown, records, examples):
    """Produce blocking, actionable findings independently of aggregate scores."""
    from .models import ReportReviewIssue, ReportExecutionReview

    issues, errors, feedback = [], [], []
    by_id = {row["evidence_id"]: row for row in records}
    passage_refs = {source_passage_id(identity, index): (identity, index)
        for identity, record in by_id.items()
        for index, _ in enumerate(evidence_passages(str(record.get("supporting_excerpt") or record.get("claim") or "")))}
    available = review._submitted_evidence_ids
    factual = {row["requirement_id"] for row in requirements if row.get("kind", "factual") == "factual"}
    draft_passages = evidence_passages(markdown)
    for row in review.coverage:
        if row.status != "covered":
            continue
        if row.requirement_id in factual and (row.missing_aspects is None or "missing_aspects" not in row.model_fields_set):
            errors.append("report_answer_witness_invalid")
            feedback.append({"requirement_id": row.requirement_id, "field": "missing_aspects",
                "instruction": "Explicitly list remaining factual aspects, or [] only after checking that the whole scoped question is answered."})
        if row.answer_passage_index is not None:
            if 0 <= row.answer_passage_index < len(draft_passages):
                row.answer_excerpt = draft_passages[row.answer_passage_index]
            else:
                errors.append("report_answer_witness_invalid")
                feedback.append({"requirement_id": row.requirement_id, "field": "answer_passage_index",
                    "instruction": "Select an existing draft_passages index containing this answer."})
        if row.requirement_id in factual and not _contains(markdown, row.answer_excerpt):
            errors.append("report_answer_witness_invalid")
            feedback.append({"requirement_id": row.requirement_id, "field": "answer_passage_index",
                "instruction": "Select the draft_passages index containing the actual answer, not a heading or bibliography. A covered factual requirement needs an answer."})
        if row.requirement_id in factual and not row.supporting_evidence:
            errors.append("report_answer_witness_invalid")
            feedback.append({"requirement_id": row.requirement_id, "field": "supporting_evidence",
                "instruction": "A covered factual answer requires a submitted evidence_id and a contiguous quote; otherwise mark partial/missing."})
        for support in row.supporting_evidence:
            if support.source_passage_id:
                reference = passage_refs.get(support.source_passage_id)
                if reference is None or (support.evidence_id and support.evidence_id != reference[0]) or (
                    support.passage_index is not None and support.passage_index != reference[1]
                ):
                    errors.append("report_answer_witness_invalid")
                    feedback.append({"requirement_id": row.requirement_id, "field": "supporting_evidence.source_passage_id",
                        "instruction": "Select an allowed source_passage_id (evidence_id:local_index) from the schema; do not combine it with a different evidence_id or passage_index."})
                    continue
                support.evidence_id, support.passage_index = reference
            record = by_id.get(support.evidence_id)
            if record is None or available is not None and support.evidence_id not in available:
                errors.append("report_answer_witness_invalid")
                feedback.append({"requirement_id": row.requirement_id, "evidence_id": support.evidence_id,
                    "field": "supporting_evidence.evidence_id", "instruction": "Choose an evidence_id actually present in this call's report_evidence.records."})
                continue
            if support.passage_index is not None:
                passages = evidence_passages(str(record.get("supporting_excerpt") or record.get("claim") or ""))
                if 0 <= support.passage_index < len(passages):
                    support.quote = passages[support.passage_index]
                else:
                    errors.append("report_answer_witness_invalid")
                    feedback.append({"requirement_id": row.requirement_id, "evidence_id": support.evidence_id,
                        "field": "supporting_evidence.passage_index", "valid_passage_indices": list(range(len(passages))),
                        "instruction": "Choose from valid_passage_indices in this evidence record, not a draft_passages index."})
                    continue
            if not _contains(str(record.get("supporting_excerpt") or record.get("claim") or ""), support.quote):
                errors.append("report_answer_witness_invalid")
                feedback.append({"requirement_id": row.requirement_id, "evidence_id": support.evidence_id,
                    "field": "supporting_evidence", "instruction": "Select the exact evidence_id and passage_index from a submitted record's source_passages."})
        if row.missing_aspects:
            row.status = "partial"
            issues.append(ReportReviewIssue(category="coverage", severity="major",
                requirement_id=row.requirement_id, location=row.answer_excerpt[:500],
                description="Required aspects remain unresolved: " + "; ".join(row.missing_aspects),
                revision_instruction="Answer these aspects from the supplied evidence; retain partial status if support is unavailable."))
    known = {example["example_id"] for example in examples}
    supplied = [check.example_id for check in review.example_checks]
    if set(supplied) != known or len(supplied) != len(set(supplied)):
        errors.append("report_example_checks_missing_or_invalid")
    for check in review.example_checks:
        example = next((row for row in examples if row["example_id"] == check.example_id), {})
        units = {row["unit_id"]: row for row in example.get("execution_units", [])}
        if len(check.execution_checks) == len(units) and all(
            not row.unit_id or row.unit_id == identity
            for row, identity in zip(check.execution_checks, units)
        ):
            # The protocol orders rows like the supplied execution_units.
            # Bind omitted server-owned labels, never infer behavioral verdicts.
            for row, identity in zip(check.execution_checks, units):
                row.unit_id = identity
        for identity, unit in units.items():
            if unit.get("image_entrypoint_unavailable") and not any(row.unit_id == identity for row in check.execution_checks):
                # This observation comes from the manifest itself and needs no
                # model retry. Explicit commands still require model checks.
                check.execution_checks.append(ReportExecutionReview(unit_id=identity,
                    expected_lifecycle="finite", observed_lifecycle="unknown",
                    stopping_condition="The image default entrypoint is not specified or inspected."))
        checked = [row.unit_id for row in check.execution_checks]
        if set(checked) != set(units) or len(checked) != len(set(checked)):
            errors.append("report_example_checks_missing_or_invalid")
        for execution in check.execution_checks:
            if units.get(execution.unit_id, {}).get("required_lifecycle"):
                execution.expected_lifecycle = units[execution.unit_id]["required_lifecycle"]
            if units.get(execution.unit_id, {}).get("image_entrypoint_unavailable"):
                execution.observed_lifecycle = "unknown"
                execution.stopping_condition = "The image default entrypoint is not specified or inspected."
                check.status = "unverified"
                note = "A complete batch example needs an explicit primary command whose normal completion can be checked; an image name alone does not establish that behavior."
                if note not in check.explanation:
                    check.explanation += " " + note
            if units.get(execution.unit_id, {}).get("unconditional_loop"):
                execution.observed_lifecycle = "persistent"
            if {execution.expected_lifecycle, execution.observed_lifecycle} == {"finite", "persistent"}:
                check.status = "contradicts"
                check.explanation += f" Command {execution.unit_id} requires {execution.expected_lifecycle} work but is {execution.observed_lifecycle}; inspect its actual stopping condition."
            elif execution.expected_lifecycle == "finite" and execution.observed_lifecycle in {"unknown", "not_applicable"}:
                check.status = "unverified"
        if not check.observed_behavior:
            if units and set(checked) == set(units):
                check.observed_behavior = "; ".join(f"{row.unit_id}: {row.observed_lifecycle}; {row.stopping_condition}"
                                                   for row in check.execution_checks)[:1000]
            elif not units and not checked and check.explanation.strip():
                # A declarative snippet may have no executable units. Its
                # substantive configuration explanation is already the review.
                check.observed_behavior = check.explanation[:1000]
            else:
                errors.append("report_example_checks_missing_or_invalid")
        lifecycle_conflict = {check.expected_lifecycle, check.observed_lifecycle} == {"finite", "persistent"}
        if lifecycle_conflict:
            check.status = "contradicts"
        if check.status != "satisfies":
            issues.append(ReportReviewIssue(category="contradictions", severity="major",
                location=check.example_id,
                description=f"Example requires {check.expected_behavior}; actual behavior: {check.observed_behavior}. {check.explanation}",
                revision_instruction="Correct the complete example so its behavior matches its stated purpose, using the supplied evidence."))
    return issues, list(dict.fromkeys(errors)), feedback

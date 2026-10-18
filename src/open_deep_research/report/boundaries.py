"""Source-scoped boundary checks for phase-dependent sum/max formulas."""

import ast
import math
import re
from types import GenericAlias
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, create_model
from pydantic.json_schema import SkipJsonSchema

from .models import ReportReviewIssue
from .verification import evidence_passages, source_passage_view


# Hypothetical inputs, not measured data or domain-specific expected answers.
CASES = [
    {"case_id": "helper_first", "demands": {"helper": .5, "transient": 2., "steady": 1.},
     "phases": [["helper"], ["helper", "transient"], ["helper", "steady"]]},
    {"case_id": "helper_after", "demands": {"helper": .5, "transient": 2., "steady": 1.},
     "phases": [["transient"], ["helper"], ["helper", "steady"]]},
]


class PhaseCheck(BaseModel):
    case_id: Literal["helper_first", "helper_after"]
    applicable: bool = Field(description="Whether the original user scope and supplied lifecycle sources permit this ordering. A draft formula that omits the ordering is still applicable and defective; it is not a scope exclusion.")
    formula_passage_index: int | None = Field(default=None, ge=0)
    formula_expression: str = Field(default="", max_length=512)
    # Preserve earlier diagnostic receipts, but never trust their arithmetic.
    reported_value: SkipJsonSchema[float | None] = None
    evidence_ids: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=2000)


class BoundaryAudit(BaseModel):
    checks: list[PhaseCheck] = Field(min_length=2, max_length=2)
    _submitted_evidence_ids: set[str] | None = PrivateAttr(default=None)


class RequiredPhaseCheck(PhaseCheck):
    formula_passage_index: int | None = Field(..., ge=0,
        description="Required property: identify the actual draft passage for an applicable case; use null only for an inapplicable case.")
    formula_expression: str = Field(..., max_length=512)


RequiredBoundaryAudit = create_model("RequiredBoundaryAudit", __base__=BoundaryAudit,
    checks=(GenericAlias(list, RequiredPhaseCheck), Field(..., min_length=2, max_length=2)))


class SourcePhaseCheck(PhaseCheck):
    formula_passage_index: SkipJsonSchema[int | None] = None
    formula_expression: SkipJsonSchema[str] = ""


SourceBoundaryAudit = create_model("SourceBoundaryAudit", __base__=BoundaryAudit,
    checks=(GenericAlias(list, SourcePhaseCheck), Field(..., min_length=2, max_length=2)))


class FormulaByOrdering(BaseModel):
    helper_first: str | None = Field(..., min_length=1, max_length=512)
    helper_after: str | None = Field(..., min_length=1, max_length=512)


class BoundaryProtocolError(ValueError):
    pass


def evaluate_formula(expression, demands):
    """Evaluate a small arithmetic grammar, without executing model-supplied code."""
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise BoundaryProtocolError("formula_expression must be an arithmetic expression") from exc
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise BoundaryProtocolError("formula_expression is too complex")

    def value(node):
        if isinstance(node, ast.Name) and node.id in demands:
            return demands[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in (int, float) and node.value == 0:
            return 0.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left) + value(node.right)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"max", "min"} and len(node.args) >= 1 and not node.keywords):
            values = [value(arg) for arg in node.args]
            return (max if node.func.id == "max" else min)(values)
        raise BoundaryProtocolError(
            "formula_expression may use only helper, transient, steady, overhead, zero, +, max(...) and min(...); "
            "transcribe the report's operators, not a computed answer")

    return value(tree.body)


FORMULA_MARKER = re.compile(r"\b(?:max|min)\s*[（(]|最大值|最小值|最高值|最低值|较大者|较高者|较小者|较低者|峰值|求和|之和|合计|总和|\b(?:maximum|minimum|sum)\b|(?:higher|lower|larger|smaller|greater)\s+of", re.I)


def needs_boundary_audit(markdown):
    return bool(FORMULA_MARKER.search(markdown)
                and re.search(r"并发|同时|持续运行|阶段|concurr|simultaneous|persistent|phase", markdown, re.I))


def phase_calculations(case_ids):
    """Keep arithmetic feedback structured and derived from validated phases."""
    result = []
    for case in CASES:
        if case["case_id"] not in case_ids:
            continue
        totals = [sum(case["demands"][name] for name in phase) for phase in case["phases"]]
        result.append({**case, "overhead": 0., "phase_totals": totals, "peak": max(totals),
            "formula": "overhead + max(" + ", ".join(" + ".join(phase) for phase in case["phases"]) + ")"})
    return result


def _check_source_scope(audit, records, required_cases=()):
    known = {r["evidence_id"] for r in records}
    if audit._submitted_evidence_ids is not None:
        known &= audit._submitted_evidence_ids
    cases = {case["case_id"]: case for case in CASES}
    if {row.case_id for row in audit.checks} != set(cases):
        raise BoundaryProtocolError("boundary_case_rows_missing_or_duplicated")
    excluded = {row.case_id for row in audit.checks if not row.applicable} & set(required_cases)
    if excluded:
        raise BoundaryProtocolError(", ".join(sorted(excluded)) +
            ": the original scope and lifecycle sources already permit this ordering; "
            "a restricted or incomplete draft formula cannot make it inapplicable. Set applicable=true.")
    for row in audit.checks:
        if row.applicable and (not row.evidence_ids or set(row.evidence_ids) - known):
            raise BoundaryProtocolError(row.case_id + ": evidence_ids must cite supplied lifecycle records")


def check_boundaries(audit, markdown, records, *, required_cases=()):
    """Compute peaks independently; do not trust a model's overall verdict."""
    _check_source_scope(audit, records, required_cases)
    passages = evidence_passages(markdown)
    cases = {case["case_id"]: case for case in CASES}
    corrections = "; ".join(case["case_id"] + ": overhead + max(" +
        ", ".join(" + ".join(phase) for phase in case["phases"]) + ")"
        for case in CASES if any(row.case_id == case["case_id"] and row.applicable for row in audit.checks))
    issues = []
    for row in audit.checks:
        if not row.applicable:
            continue
        if row.formula_passage_index is None or row.formula_passage_index >= len(passages):
            raise BoundaryProtocolError(row.case_id + ": formula_passage_index must identify a supplied draft passage")
        case = cases[row.case_id]
        reported_value = evaluate_formula(row.formula_expression, {**case["demands"], "overhead": 0.})
        totals = [sum(case["demands"][name] for name in phase) for phase in case["phases"]]
        peak = max(totals)
        if not math.isclose(reported_value, peak, rel_tol=1e-9, abs_tol=1e-9):
            trace = "; ".join(" + ".join(phase) + f" = {total}" for phase, total in zip(case["phases"], totals))
            issues.append(ReportReviewIssue(category="contradictions", severity="major",
                location=passages[row.formula_passage_index][:500], evidence_ids=row.evidence_ids,
                description=f"Hypothetical phase boundary {row.case_id}: the report formula "
                    f"{row.formula_expression} evaluates to {reported_value}, "
                    f"but the maximum sum of simultaneously active demands is {peak}. "
                    f"Demands={case['demands']}; computed phase totals: {trace}. "
                    "Helper is persistent support, transient is finite initialization, steady is the main workload. "
                    "The formula therefore does not cover this documented ordering.",
                revision_instruction="Provide a formula that covers the requested ordering using the supplied lifecycle evidence. "
                    "Take the maximum of simultaneous totals by phase, including persistent components only "
                    "after they start; do not reserve a later component in an earlier phase. "
                    "Retain the exact counterexample inputs and order when checking the correction; "
                    "do not replace them with a steady-dominated example that hides this defect. "
                    "If a source's compact formula omits a lifecycle term, give the applicable phase formula "
                    "as a source-grounded derivation. Retain the compact quotation only with its valid "
                    "preconditions; do not leave the disproved formula as the effective-resource answer "
                    "and demote the necessary correction to optional engineering advice. "
                    "Equations derived directly from the source-supported phase sets: " + corrections + ". "
                    "Cover all these orderings in the correction, using only components already started "
                    "in each phase; a rule that adds every persistent component to every phase is incomplete. "
                    "These inputs are hypothetical arithmetic checks, not measured performance."))
    return issues


async def audit_boundaries(payload, config, cfg, invoke):
    from agentscope.exception import StructuredOutputError
    from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
    from open_deep_research.agentscope_runtime.runtime_limits import structured_attempt_budget
    from .writing import writing_messages

    flags = {"_run_schema_version": config["metadata"]["run_config_schema_version"],
        "_quality_policy_version": config["metadata"].get("quality_policy_version")}
    attempts = min(2, max(1, cfg.max_structured_output_retries))

    async def checked_call(prompt, data, records, schema, span, validate):
        token = structured_attempt_budget.set([attempts])
        try:
            for attempt in range(attempts):
                try:
                    result = await invoke("report_review", writing_messages(prompt, data, records, audit=True), cfg,
                        span_name=span, schema=schema)
                    validate(result)
                    return result
                except (StructuredOutputError, ModelOutputProtocolError, ValidationError, BoundaryProtocolError) as exc:
                    remaining = structured_attempt_budget.get()
                    if attempt + 1 >= attempts or remaining is not None and remaining[0] <= 0:
                        raise RuntimeError("report_boundary_reviewer_unavailable") from exc
                    data["protocol_feedback"] = str(exc)[:2000]
            raise RuntimeError("report_boundary_reviewer_unavailable")
        finally:
            structured_attempt_budget.reset(token)

    # Establish permitted lifecycles without showing the formula being tested.
    scope = await checked_call(
        "Check only whether the original user scope and submitted lifecycle sources permit each "
        "hypothetical ordering. Helper is persistent support, transient is finite INITIALIZATION "
        "work, and steady is the main workload that begins after initialization. Each listed phase "
        "contains its simultaneously active components. Do not compute resource formulas. "
        "Mark an ordering inapplicable only for a real user/source exclusion. Required case IDs "
        "were already established from these sources and remain applicable across revisions. "
        "Return one check per case_id, with applicable, actual supporting evidence_ids and a concise explanation.",
        {**flags, "cases": [{"case_id": row["case_id"], "phases": row["phases"]} for row in CASES],
         "scope_context": payload["coverage_contract"].get("scope_context", []),
         "required_case_ids": payload.get("required_boundary_cases", [])},
        source_passage_view(payload["evidence_registry"]), SourceBoundaryAudit, "lead.report_boundary_review",
        lambda result: _check_source_scope(result, payload["evidence_registry"], payload.get("required_boundary_cases", ())))
    applicable = {row.case_id for row in scope.checks if row.applicable}
    payload["_verified_boundary_cases"] = applicable
    if not applicable:
        return []

    passages = evidence_passages(payload["draft_markdown"])
    candidates = {f"passage_{i}": i for i, text in enumerate(passages) if FORMULA_MARKER.search(text)}
    forms = {}
    if candidates:
        # Named fields bind each result to its supplied passage. No model-written
        # index is needed. This call sees neither source formulas nor test peaks.
        schema = create_model("DraftFormulaTranscriptions", **{
            key: (FormulaByOrdering, Field(...)) for key in candidates})

        def validate_forms(result):
            values = result.model_dump()
            if set(values) != set(candidates):
                raise BoundaryProtocolError("Return every supplied formula passage field exactly once.")
            for key, expressions in values.items():
                for expression in expressions.values():
                    if expression is not None:
                        try:
                            evaluate_formula(expression, {**CASES[0]["demands"], "overhead": 0.})
                        except BoundaryProtocolError as exc:
                            raise BoundaryProtocolError(key + ": " + str(exc)) from exc

        result = await checked_call(
            "Read only the REPORT, not external facts. For every requested passage field, transcribe "
            "the complete effective-resource formula actually asserted there into symbolic arithmetic. "
            "Use the report's own definitions and explicit applicability conditions; preserve its mistakes. "
            "helper is persistent support, transient is finite initialization, steady is the main workload "
            "after initialization, and overhead is the additive overhead. steady excludes helper. "
            "helper_first means helper starts before transient; helper_after means it starts after transient. "
            "Return the same formula for both when the report does not distinguish them. Return null for "
            "an ordering only if this passage contains no complete effective-resource formula applicable "
            "to it; a component definition or one phase's subtotal is not a complete formula. "
            "Read numerical worked examples as formulas using their declared component meanings. "
            "Keep each passage's written outer operators even when another passage states a different "
            "general rule: a worked example can contradict that rule. Single-argument max/min are valid; "
            "never move sums into or out of a maximum to make the example agree with the general rule. "
            "Keep a quoted formula only in the scope for which the report still asserts it, not where "
            "the report explicitly replaces or rejects it. Only helper, transient, steady, overhead, "
            "zero, +, max(...) and min(...) are allowed. Do not evaluate, correct, add missing terms "
            "or use lifecycle knowledge to improve the report. An individual maximum remains a maximum "
            "even if you know the components may overlap. No sources, expected answers or test values "
            "are supplied to this transcription task.",
            {**flags, "draft_passages": {f"passage_{i}": text for i, text in enumerate(passages)}},
            [], schema, "lead.report_formula_transcription", validate_forms)
        forms = result.model_dump()

    sources = {row.case_id: row for row in scope.checks}
    covered, issues = set(), []
    for key, expressions in forms.items():
        checks = []
        for case in CASES:
            identity = case["case_id"]
            expression = expressions[identity]
            selected = identity in applicable and expression is not None
            if selected:
                covered.add(identity)
            checks.append(sources[identity].model_copy(update={"applicable": selected,
                "formula_passage_index": candidates[key], "formula_expression": expression or ""}))
        audit = BoundaryAudit(checks=checks)
        audit._submitted_evidence_ids = scope._submitted_evidence_ids
        issues.extend(check_boundaries(audit, payload["draft_markdown"], payload["evidence_registry"]))
    for identity in sorted(applicable - covered):
        calculation = phase_calculations({identity})[0]
        issues.append(ReportReviewIssue(category="coverage", severity="major", location="Resource formula",
            evidence_ids=sources[identity].evidence_ids,
            description=f"No complete report formula covers the source-supported ordering {identity}.",
            revision_instruction="Provide the effective-resource formula for this ordering from its "
                "documented simultaneous phases: " + calculation["formula"] + ". Preserve the original scope."))
    return issues

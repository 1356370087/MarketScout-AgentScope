"""Runtime-owned scope and feedback protocol for v21 tool assessment."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.json_schema import SkipJsonSchema

from .contract import ResearchCoverageContract
from .gate import QualityProtocolError, ResearchGap, ToolResultAssessment


class QualityScopeViolation(QualityProtocolError):
    """Invalid evaluator instructions must never become new research work."""


class TaskEvaluationScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal["owned-scope-v1"] = "owned-scope-v1"
    mode: Literal["single_subject", "comparison", "general"]
    requirements: list[dict]
    subjects: list[str]
    other_subjects: list[str]
    shared_conditions: list[dict]
    parent_context: list[dict]
    source_constraints: dict

    @classmethod
    def build(cls, contract, ids):
        contract = ResearchCoverageContract.model_validate(contract)
        owned = set(ids)
        rows = [r.model_dump(mode="json") for r in contract.requirements
                if r.requirement_id in owned and r.kind == "factual"]
        if len(rows) != len(owned):
            raise ValueError("task_scope_requires_owned_factual_ids")
        subjects = sorted({r["subject"] for r in rows if r.get("subject")})
        all_subjects = {r.subject for r in contract.requirements if r.subject}
        return cls(
            mode="comparison" if len(subjects) > 1 else "single_subject" if subjects else "general",
            requirements=rows, subjects=subjects, other_subjects=sorted(all_subjects - set(subjects)),
            shared_conditions=[r.model_dump(mode="json") for r in contract.requirements if r.kind == "process"],
            parent_context=[d.model_dump(mode="json") for d in contract.dimensions if owned.intersection(d.requirement_ids)],
            source_constraints={"selection": contract.source_selection.model_dump(mode="json") if contract.source_selection else None,
                                "plan": contract.source_plan},
        )

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def contract_view(self, original):
        """No sibling facts or report deliverables enter the hard requirement list."""
        value = original.model_dump(mode="json") if hasattr(original, "model_dump") else dict(original)
        return {**value, "requirements": [*self.requirements, *self.shared_conditions],
                "dimensions": [], "delegation_scopes": {},
                "scope_context": [r["text"] for r in self.shared_conditions]}


class RequirementCheck(BaseModel):
    requirement_id: str
    subject: str | None
    status: Literal["supported", "partial", "unsupported"]
    evidence_ids: list[str]


class ScopedResearchGap(ResearchGap):
    target_subjects: list[str]
    basis_quote: str = Field(default="", description="事实或冲突缺口所对应的本任务原文片段；不能引用其他任务的问题。")


class ScopedToolResultAssessment(ToolResultAssessment):
    requirement_checks: list[RequirementCheck]
    gaps: list[ScopedResearchGap]
    # Compatibility fields are derived by the runtime, not model instructions.
    missing_information: SkipJsonSchema[list[str]] = Field(default_factory=list)
    suggested_queries: SkipJsonSchema[list[str]] = Field(default_factory=list)
    unresolved_conflicts: SkipJsonSchema[list[str]] = Field(default_factory=list)
    raw_reason: SkipJsonSchema[str] = ""


def assessment_schema(scope):
    ids = tuple(row["requirement_id"] for row in scope.requirements)
    subjects = tuple(dict.fromkeys(row.get("subject") for row in scope.requirements))
    identity_type = Literal[ids] if ids else str
    subject_type = Literal[subjects] if subjects else type(None)
    check = create_model("OwnedRequirementCheck", __base__=RequirementCheck,
        requirement_id=(identity_type, ...), subject=(subject_type, ...))
    targets = list[Literal[tuple(scope.subjects)]] if scope.subjects else list[str]
    gap = create_model("OwnedResearchGap", __base__=ScopedResearchGap,
        requirement_id=(identity_type, ...),
        target_subjects=(targets, Field(..., **({"max_length": 0} if not scope.subjects else {}))))
    return create_model("OwnedToolResultAssessment", __base__=ScopedToolResultAssessment,
        requirement_checks=(list[check], Field(..., min_length=len(ids), max_length=len(ids))),
        gaps=(list[gap], Field(..., **({"max_length": 0} if not ids else {}))))


TASK_SCOPE_RULES = """
task_scope defines this task's owned factual questions and subjects.
Investigate only those questions. A comparison roster in parent_context does not
assign other competitors to a single-subject task. Shared user premises constrain
the answer; they are not web-verifiable claims. Follow only validated factual or
conflict gaps for further research, within the original source and budget limits.
"""

SCOPE_RULES = TASK_SCOPE_RULES + """
task_scope is runtime authority, not an instruction proposed by another model.
Return exactly one requirement_checks row per owned requirement, with the exact
subject (null for a general requirement), status and submitted supporting evidence IDs.
A single-subject task evaluates that subject alone, even if the original sentence
describes a larger comparison. Shared conditions constrain applicability; they are
not extra factual questions. parent_context is reference material, not new ownership.
For a factual/conflict gap, target_subjects must match the requirement's subject
(or the explicitly assigned comparison subjects) and basis_quote must quote the
owned factual question. Shared applicability conditions qualify that question;
they cannot create a new question. Never request another
competitor's missing facts under the current requirement ID.
User-supplied corpus sizes, budgets and unmeasured performance targets are premises,
not claims to prove from web pages. Preserve negation, deployment shape and versions.
Advisory/input_projection gaps must have no next_query. All next research actions
must be represented in validated factual/conflict gaps, not free-form prose.
Do not supply missing_information, suggested_queries or unresolved_conflicts;
the runtime derives those compatibility fields from validated gaps.
Do not infer a product lacks a capability merely because a search or fetch failed.
"""


def _mentions(text, subject):
    # The original canonical name, including its product name before a 的 qualifier.
    names = {subject, subject.split("的", 1)[0].strip()}
    compact = re.sub(r"\s+", "", text).casefold()
    return any(re.sub(r"\s+", "", name).casefold() in compact for name in names if name)


def protocol_errors(result, scope, evidence_ids):
    rows = {r["requirement_id"]: r for r in scope.requirements}
    known = set(evidence_ids)
    checks = result.requirement_checks
    errors = []
    if len(checks) != len(rows) or {c.requirement_id for c in checks} != set(rows):
        errors.append("scope:requirement_checks_missing_duplicate_or_foreign")
    by_id = {}
    for check in checks:
        row = rows.get(check.requirement_id)
        if row is None:
            continue
        by_id[check.requirement_id] = check
        if check.subject != row.get("subject"):
            errors.append("scope:check_subject_mismatch:" + check.requirement_id)
        if set(check.evidence_ids) - known or (check.status == "supported" and not check.evidence_ids):
            errors.append("scope:invalid_supporting_evidence:" + check.requirement_id)
    blocking = set()
    for gap in result.gaps:
        row = rows.get(gap.requirement_id)
        if row is None:
            errors.append("scope:foreign_gap_requirement")
            continue
        targets = {row["subject"]} if row.get("subject") else set(scope.subjects)
        if set(gap.target_subjects) - targets:
            errors.append("scope:foreign_gap_subject:" + gap.requirement_id)
        if gap.kind in {"advisory", "input_projection"}:
            if gap.next_query:
                errors.append("scope:nonresearch_gap_has_query")
            continue
        blocking.add(gap.requirement_id)
        if set(gap.target_subjects) != targets:
            errors.append("scope:gap_subject_missing:" + gap.requirement_id)
        basis = [row["text"]]
        if not gap.basis_quote.strip() or not any(gap.basis_quote in text for text in basis):
            errors.append("scope:gap_basis_not_owned:" + gap.requirement_id)
        foreign = (set(scope.subjects) | set(scope.other_subjects)) - targets
        if scope.mode != "general" and any(_mentions(gap.reason + " " + gap.next_query, subject)
                                           for subject in foreign):
            errors.append("scope:foreign_subject_in_instruction:" + gap.requirement_id)
        if by_id.get(gap.requirement_id) and by_id[gap.requirement_id].status == "supported":
            errors.append("scope:supported_requirement_has_blocking_gap:" + gap.requirement_id)
    for rid, check in by_id.items():
        if check.status != "supported" and rid not in blocking and not any(
            g.requirement_id == rid and g.kind == "input_projection" for g in result.gaps
        ):
            errors.append("scope:unsupported_requirement_without_gap:" + rid)
    if result.decision == "complete" and any(c.status != "supported" for c in checks):
        errors.append("scope:complete_with_unresolved_requirements")
    return list(dict.fromkeys(errors))


def derive_feedback(result):
    result.raw_reason = result.raw_reason or result.reason
    blocking = [gap for gap in result.gaps if gap.kind in {"factual", "conflict"}]
    result.missing_information = [gap.reason for gap in blocking if gap.kind == "factual"]
    result.unresolved_conflicts = [gap.reason for gap in blocking if gap.kind == "conflict"]
    result.suggested_queries = list(dict.fromkeys(gap.next_query for gap in blocking if gap.next_query))
    result.reason = "本任务事实检查：" + "；".join(
        f"{row.requirement_id}（{row.subject or '当前问题'}）：{row.status}" for row in result.requirement_checks)
    return result

"""Explicit evidence checks for instructions that must work when followed."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


class ProcedureFacet(BaseModel):
    status: Literal["supported", "missing", "not_applicable"]
    explanation: str = Field(min_length=1)
    evidence_ids: list[str]


class PrerequisiteActivation(BaseModel):
    subsystem: str = Field(min_length=1, description="A separately enabled prerequisite named by the procedure's sources.")
    enabling_control: str | None = Field(..., description="Copy only the parameter identifier or exact enabling command, without explanatory prose or parenthetical annotations. Null when not established by the evidence.")
    control_evidence_ids: list[str]
    default_value: str | None = Field(..., description="Verbatim documented default of that enabling control. Null when the supplied evidence does not establish it; never guess enabled just because a source describes using the subsystem.")
    default_evidence_ids: list[str]


class ProcedureCoverage(BaseModel):
    requirement_id: str
    required_inputs: ProcedureFacet
    activation_and_defaults: ProcedureFacet
    steps_and_result: ProcedureFacet
    limitations: ProcedureFacet
    activation_controls: list[PrerequisiteActivation] = Field(default_factory=list)


PROCEDURE_FACETS = ("required_inputs", "activation_and_defaults", "steps_and_result", "limitations")
PROCEDURE_CHECK_RULES = """
For each procedural_requirement_id, return one procedure_checks row before deciding coverage.
Check required_inputs, activation_and_defaults, steps_and_result, and limitations separately.
Each supported facet needs evidence_ids and an explanation of the concrete supported facts.
Additionally list activation_controls for the separately configurable prerequisites identified
in activation_and_defaults. For each, copy the enabling parameter/command and its default
verbatim from cited evidence. A missing enabling control or default must be null with no
supporting IDs. An empty list means this operation has NO separately configurable prerequisite,
not that its configuration has not been read. Naming a daemon or required artifact is not an
enabling control. Ordinary command arguments belong in required_inputs, not this list.
Limit activation_controls to a feature-specific subsystem needed for the requested operation.
Ordinary authentication/permissions, network connectivity, and shared server capacity are
background prerequisites, not a request for their deployment defaults. Mention such supplied
constraints in required_inputs or limitations; do not require their setup/defaults unless the
user asks for them. This distinction must not hide an optional subsystem whose activation is
necessary for the requested feature to work.
In activation_and_defaults, identify every separately enabled prerequisite mentioned by the
sources and check how to enable/obtain it and the default affecting whether the operation works.
Saying that an artifact/service/summary must exist does NOT explain how it becomes available.
Use missing when the evidence only names that prerequisite but does not establish its setup.
not_applicable needs a specific reason; do not use it for a configurable prerequisite merely
because its configuration was not retrieved. Apply this to the operation the user requested,
not general environment setup. For a missing facet request a focused official reference lookup.
A procedure cannot be supported while a required facet is missing. Do not invent a default.
"""


def procedural_requirement_ids(requirements):
    """Select explicit operational questions, not all explanations containing 'how'."""
    operation = r"(?:生成|创建|建立|配置|开启|启用|安装|部署|合并|恢复|重建|迁移|备份|使用|调用|执行|create|generate|configure|enable|install|deploy|combine|restore|rebuild|migrate|use|run)"
    pattern = rf"(?:(?:如何|怎样|怎么|how\s+to).{{0,25}}{operation}|{operation}.{{0,16}}(?:方法|步骤|流程|procedure|steps))"
    return [row["requirement_id"] for row in requirements
            if row.get("kind", "factual") == "factual" and re.search(pattern, row["text"], re.IGNORECASE)]


def procedure_protocol_errors(checks, required_ids, evidence_ids):
    by_id = {row.requirement_id: row for row in checks}
    errors = []
    if set(by_id) != set(required_ids) or len(by_id) != len(checks):
        errors.append("procedure_checks_missing_or_invalid:" + ",".join(required_ids))
    known = set(evidence_ids)
    for row in checks:
        for name in PROCEDURE_FACETS:
            facet = getattr(row, name)
            if set(facet.evidence_ids) - known or (facet.status == "supported" and not facet.evidence_ids):
                errors.append(f"procedure_evidence_invalid:{row.requirement_id}:{name}")
        for control in row.activation_controls:
            for value, refs in ((control.enabling_control, control.control_evidence_ids),
                                (control.default_value, control.default_evidence_ids)):
                if set(refs) - known or (value is not None and not refs):
                    errors.append(f"procedure_activation_evidence_invalid:{row.requirement_id}")
    return errors


def apply_procedure_gaps(result, records=()):
    """Make an explicit missing prerequisite block completion regardless of scores."""
    from .gate import ResearchGap
    from .contract import CoverageStatus

    source_text = {row["evidence_id"]: str(row.get("supporting_excerpt") or row.get("claim", "")) for row in records}
    def established(value, refs, *, control=False):
        if value and control:
            # The field identifies a control, not the Judge's incidental prose.
            value = value.replace("`", "").strip()
            annotated = re.fullmatch(r"([A-Za-z_][\w.-]*|--[\w-]+)\s+\([^()\n]*\)", value)
            if annotated:
                value = annotated[1]
        return bool(value and refs) and (not records or any(
            re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", source_text.get(ref, ""), re.IGNORECASE)
            for ref in refs))

    missing_ids = set()
    for row in result.procedure_checks:
        for name in PROCEDURE_FACETS:
            facet = getattr(row, name)
            if facet.status == "missing":
                missing_ids.add(row.requirement_id)
                reason = f"{name}: {facet.explanation}"
                result.missing_information.append(reason)
                result.gaps.append(ResearchGap(requirement_id=row.requirement_id, kind="factual",
                    reason=reason, checked_evidence_ids=facet.evidence_ids,
                    next_query=f"Verify from the allowed authoritative references: {facet.explanation}"))
        for control in row.activation_controls:
            missing = []
            if not established(control.enabling_control, control.control_evidence_ids, control=True):
                missing.append("enabling control")
            if not established(control.default_value, control.default_evidence_ids):
                missing.append("documented default")
            if missing:
                missing_ids.add(row.requirement_id)
                reason = f"Evidence does not establish {' and '.join(missing)} for prerequisite: {control.subsystem}"
                result.missing_information.append(reason)
                result.gaps.append(ResearchGap(requirement_id=row.requirement_id, kind="factual", reason=reason,
                    checked_evidence_ids=list(dict.fromkeys([*control.control_evidence_ids, *control.default_evidence_ids])),
                    next_query=f"{control.subsystem} enabling configuration parameter default official reference"))
    result.requirement_coverage = [row.model_copy(update={"status": CoverageStatus.PARTIAL})
        if row.requirement_id in missing_ids else row for row in result.requirement_coverage]
    if missing_ids:
        from .contract import AdmissionStatus
        result.accepted = False
        result.admission_status = AdmissionStatus.REJECTED
        result.reason = "Required procedural evidence is incomplete: " + "; ".join(dict.fromkeys(result.missing_information))

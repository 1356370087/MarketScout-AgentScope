"""Focused source checks for guarantees and prerequisites across report prose."""

import re
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, field_validator

from .models import ReportReviewIssue
from .verification import evidence_passages, source_passage_view


class GuaranteeCheck(BaseModel):
    claim_id: str
    supported: bool = Field(..., description="Required: true only if every guarantee and prerequisite in this passage follows from the submitted evidence; otherwise false.")
    evidence_ids: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=1500)
    revision_instruction: str = Field(default="", max_length=1500,
        description="Optional correction. A clear unsupported verdict and explanation remain blocking without this duplicate instruction.")

    @field_validator("explanation", "revision_instruction", mode="before")
    @classmethod
    def bound_notes(cls, value):
        return value[:1500] if isinstance(value, str) else value


class GuaranteeAudit(BaseModel):
    checks: list[GuaranteeCheck]
    _submitted_evidence_ids: set[str] | None = PrivateAttr(default=None)


class GuaranteeProtocolError(ValueError):
    pass


class GuaranteeReviewUnavailable(RuntimeError):
    pass


def guarantee_inventory(markdown):
    from .citations import body_without_references
    from .reviewer import _markdown_regions

    marker = re.compile(r"保证|确保|必然|必须|只能|一定|\b(?:guarantee\w*|ensure\w*|must|always|never)\b", re.I)
    # Describing how a report labels claims is not itself a domain guarantee.
    # Mask only the labeling clause for detection; keep the complete passage
    # whenever it also contains an actual guarantee or prerequisite.
    labeling = re.compile(
        r"官方保证[与和、/]工程建议(?:在[^。！？\n；;]{0,12})?(?:分别|明确)?(?:标注|标明|区分)"
        r"|(?:official )?guarantees and (?:engineering )?(?:advice|recommendations) "
        r"(?:are |will be )?(?:separately |clearly )?(?:labeled|labelled|marked|distinguished)", re.I)
    return [{"claim_id": f"claim-{index}", "text": text} for index, text in enumerate(
        passage for code, region in _markdown_regions(body_without_references(markdown)) if not code
        for passage in evidence_passages(region) if marker.search(labeling.sub("", passage))
        and not re.fullmatch(r"\s*#{1,6}\s+[^\n]+\s*", passage))]


def check_guarantees(audit, claims, records):
    known = {row["evidence_id"] for row in records}
    if audit._submitted_evidence_ids is not None:
        known &= audit._submitted_evidence_ids
    by_id = {claim["claim_id"]: claim for claim in claims}
    identities = [row.claim_id for row in audit.checks]
    if set(identities) != set(by_id) or len(identities) != len(set(identities)):
        raise GuaranteeProtocolError("Return exactly one check for every supplied claim_id.")
    issues = []
    for row in audit.checks:
        if set(row.evidence_ids) - known or row.supported and not row.evidence_ids:
            raise GuaranteeProtocolError(row.claim_id + ": support must cite evidence actually submitted to this call.")
        if row.supported:
            continue
        instruction = row.revision_instruction.strip() or (
            "Correct the unsupported statement identified in this finding, using the supplied evidence "
            "and verified phase calculations. Remove the unsupported assertion if the evidence does "
            "not establish it; do not replace it with another unverified guarantee.")
        issues.append(ReportReviewIssue(category="unsupported_claims",
            severity="major", location=by_id[row.claim_id]["text"][:500], evidence_ids=row.evidence_ids,
            description=row.explanation, revision_instruction=instruction))
    return issues


async def audit_guarantees(payload, config, cfg, invoke):
    from agentscope.exception import StructuredOutputError
    from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
    from open_deep_research.agentscope_runtime.runtime_limits import structured_attempt_budget
    from .writing import writing_messages
    from .boundaries import phase_calculations

    claims = guarantee_inventory(payload["draft_markdown"])
    if not claims:
        return []
    data = {"claims": claims, "_run_schema_version": config["metadata"]["run_config_schema_version"],
        "_quality_policy_version": config["metadata"].get("quality_policy_version")}
    calculations = phase_calculations(payload.get("_verified_boundary_cases", ()))
    if calculations:
        # Scope/ordering was checked against the lifecycle sources first;
        # the runtime, rather than another model, computed these phase sums.
        data["verified_phase_calculations"] = calculations
        data["phase_formula_findings"] = [issue.description for issue in payload.get("_verified_boundary_issues", [])]
    prompt = (
        "Audit each supplied passage's guarantees and prerequisites against the supplied original sources. "
        "Do not score overall coverage or accept a passage merely because most of it is correct. "
        "Check every implication, its conditions, responsible actor, exact state transition and scope, "
        "including migration guidance and checklists. Ordered process startup does not by itself prove "
        "service readiness, successful delivery or durability; a shared resource does not establish such "
        "a guarantee without source-supported enabling conditions. A semantic difference in a promised "
        "state is a factual defect, not optional wording, even if another passage correctly explains it. "
        "Use verified_phase_calculations when supplied: the lifecycle audit established those orderings "
        "from the same source records and the runtime computed their simultaneous sums and maxima. "
        "A formula derived from those established phases is source-grounded synthesis; it need not "
        "occur verbatim in one excerpt. Do not reject it solely because a source states a compact "
        "formula, or claim that an application-only sum already includes finite initialization. "
        "Check the draft against the verified phase totals and phase_formula_findings; a missing "
        "concurrent component remains a defect. Continue to verify state transitions and other "
        "guarantees against the original evidence; computed resource sums establish no readiness "
        "or delivery guarantee. "
        "A source saying the same classification criteria apply to all components does not establish "
        "that the highest or lowest component category determines the whole system's category. "
        "Such an aggregation rule needs its own explicit source support. "
        "Faithful negative answers and explicitly conditional engineering recommendations are allowed; "
        "do not demand evidence of an unconditional promise they explicitly reject. Mark supported only "
        "when every substantive guarantee in the passage follows from the submitted evidence. Otherwise "
        "set supported=false and explain the exact problem. A correction may be supplied separately; "
        "do not lose a negative finding just because the evidence cannot establish a replacement answer. Cite only submitted "
        "evidence IDs; do not infer missing source contents. Return one check per claim_id. Each check "
        "must contain claim_id, supported (a JSON boolean, including false), evidence_ids and explanation. "
        "revision_instruction is optional. Never omit the supported field."
    )
    attempts = min(2, max(1, cfg.max_structured_output_retries))
    token = structured_attempt_budget.set([attempts])
    try:
        for attempt in range(attempts):
            try:
                result = await invoke("report_review", writing_messages(prompt, data,
                    source_passage_view(payload["evidence_registry"]), audit=True), cfg,
                    span_name="lead.report_guarantee_review", schema=GuaranteeAudit)
                return check_guarantees(result, claims, payload["evidence_registry"])
            except (StructuredOutputError, ModelOutputProtocolError, ValidationError, GuaranteeProtocolError) as exc:
                remaining = structured_attempt_budget.get()
                if attempt + 1 >= attempts or remaining is not None and remaining[0] <= 0:
                    raise GuaranteeReviewUnavailable("report_guarantee_reviewer_unavailable") from exc
                data["protocol_feedback"] = str(exc)[:2000]
        raise GuaranteeReviewUnavailable("report_guarantee_reviewer_unavailable")
    finally:
        structured_attempt_budget.reset(token)

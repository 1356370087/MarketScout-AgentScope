"""Versioned task, independent trial and grader contracts for local experiments."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Verdict = Literal["pass", "fail", "not_applicable", "unknown", "error"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GraderSpec(Contract):
    id: str
    kind: Literal["code", "model", "human"] = "code"
    target: Literal["outcome", "trace", "safety", "impact", "quality"] = "outcome"
    check: str
    required: bool = True
    threshold: float = Field(default=1, ge=0, le=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FixtureTool(Contract):
    name: str
    description: str
    origin: Literal["search", "mcp", "system"] = "search"
    native_port: Literal["run_read"] | None = None
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    effect: Literal["read_only", "sensitive_read", "local_write", "external_write"] = (
        "read_only"
    )
    responses: list[dict[str, Any]] = Field(default_factory=list)


class EvalCase(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
    question: str = Field(min_length=1)
    category: str
    tags: list[str] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)
    reference_outputs: dict[str, Any] = Field(default_factory=dict)
    graders: list[GraderSpec] = Field(min_length=1)
    tools: list[FixtureTool] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    interactions: list[dict[str, str]] = Field(default_factory=list)
    reference_trial: dict[str, Any] | None = None
    negative_trials: list[dict[str, Any]] = Field(default_factory=list)
    corpus_refs: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_graders(self):
        if len({g.id for g in self.graders}) != len(self.graders):
            raise ValueError("duplicate_grader_id")
        if not any(g.required for g in self.graders):
            raise ValueError("at_least_one_required_grader")
        return self


class EvalDataset(Contract):
    schema_version: Literal["1.0"] = "1.0"
    name: str
    version: str
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_case_id")
        return self


class GraderResult(Contract):
    grader_id: str
    version: str = "1.0"
    kind: Literal["code", "model", "human"]
    target: str
    required: bool
    verdict: Verdict
    score: float | None = Field(default=None, ge=0, le=1)
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def known_scores_only(self):
        if (self.verdict in {"pass", "fail"}) != (self.score is not None):
            raise ValueError("only_decided_grades_have_scores")
        return self


class EvalTrial(Contract):
    schema_version: Literal["1.0"] = "1.0"
    case_id: str
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    trial_id: str
    repeat: int = Field(ge=0)
    mode: Literal["fixed", "live", "reference"]
    run_id: str | None = None
    runtime_status: str = "not_started"
    outputs: dict[str, Any] = Field(default_factory=dict)
    observed_state: dict[str, Any] = Field(default_factory=dict)
    observed_state_complete: bool = False
    grades: list[GraderResult] = Field(default_factory=list)
    verdict: Verdict = "unknown"
    fingerprints: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float | None = None
    judge_samples: list[list[dict[str, Any]]] = Field(default_factory=list)
    error: str | None = None


def trial_verdict(grades):
    """A confirmed violation wins over missing observations; unknown never passes."""
    required = [g for g in grades if g.required]
    if any(g.verdict == "fail" for g in required):
        return "fail"
    if not required or any(g.verdict in {"unknown", "error"} for g in required):
        return "unknown"
    if not any(g.verdict == "pass" for g in required):
        return "unknown"
    return "pass"

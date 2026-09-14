"""Pydantic schemas for structured report outputs and section planning.

These models are used for LLM structured-output calls (``.with_structured_output``)
and as the typed shape of assembled reports. They live here rather than in
``state.py`` to avoid import cycles between the ``agents/`` package and the
``report/`` package, while mirroring the structured-output convention already
established in ``state.py`` (e.g. ``Summary``, ``ClarifyWithUser``).
"""

from __future__ import annotations

import builtins
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Any, List, Literal, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class SourceRef(BaseModel):
    """A source reference with explicit provenance metadata."""

    title: str = ""
    url: str
    source_type: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    locator: str | None = None


class SectionSpec(BaseModel):
    """One entry in an LLM-generated report outline (sectioned assembly)."""

    name: str
    description: str = ""
    needs_research: bool = True
    requirement_ids: list[str] = Field(default_factory=list)


class ReportOutline(BaseModel):
    """Structured output from the outline-planning call (sectioned assembly)."""

    title: str
    sections: List[SectionSpec]


class WrittenSection(BaseModel):
    """A fully written report section."""

    name: str
    content: str


class StructuredReport(BaseModel):
    """Schema for the deterministic ``output_format=structured_json`` artifact."""

    title: str
    summary: str
    sections: List[WrittenSection]
    key_findings: List[str] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)


class InlineRun(BaseModel):
    """One styled inline fragment in the canonical report model."""

    model_config = ConfigDict(extra="forbid")

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None


class ParagraphBlock(BaseModel):
    """A paragraph made of styled inline runs."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["paragraph"] = "paragraph"
    runs: list[InlineRun] = Field(default_factory=list)


class ListBlock(BaseModel):
    """A flat ordered or unordered list."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["list"] = "list"
    ordered: bool = False
    start: int = Field(default=1, ge=1)
    items: list[list[InlineRun]] = Field(default_factory=list)


class TableBlock(BaseModel):
    """A bounded tabular block with styled cells."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["table"] = "table"
    headers: list[list[InlineRun]] = Field(default_factory=list)
    rows: list[list[list[InlineRun]]] = Field(default_factory=list)


class QuoteBlock(BaseModel):
    """A block quotation represented as inline runs."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["quote"] = "quote"
    runs: list[InlineRun] = Field(default_factory=list)


class CodeBlock(BaseModel):
    """A fenced or indented code block."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["code"] = "code"
    text: str
    language: str | None = None


ReportBlock = Annotated[
    Union[ParagraphBlock, ListBlock, TableBlock, QuoteBlock, CodeBlock],
    Field(discriminator="type"),
]


class CanonicalSection(BaseModel):
    """One ordered section in a canonical report."""

    # ``id`` is the public canonical-model name.  ``section_id`` was used by
    # the first local implementation, so accept it while reading old bundles.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(validation_alias=AliasChoices("id", "section_id"))
    title: str
    level: int = Field(default=2, ge=2, le=6)
    blocks: list[ReportBlock] = Field(default_factory=list)

    @property
    def section_id(self) -> str:
        """Return the legacy section identifier spelling."""
        return self.id


class CanonicalReport(BaseModel):
    """Versioned publisher input derived from the final sanitized Markdown."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    report_type: str = "default"
    title: str
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completion_status: Literal["success", "partial"] = "success"
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    summary_blocks: list[ReportBlock] = Field(default_factory=list)
    sections: list[CanonicalSection] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    source_markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


_HEX_COLOR_PATTERN = r"^#[0-9A-F]{6}$"


class PublisherTheme(BaseModel):
    """Bounded, data-only visual options accepted from an HTTP client."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["default", "boardroom", "academic"] = "default"
    primary_color: str = Field(default="#0F766E", pattern=_HEX_COLOR_PATTERN)
    accent_color: str = Field(default="#F6BD60", pattern=_HEX_COLOR_PATTERN)
    font_family: Literal["cjk_sans", "sans", "serif"] = "cjk_sans"
    locale: Literal["zh-CN", "en-US"] = "zh-CN"
    footer_text: str = Field(default="", max_length=120)
    pdf_page_size: Literal["a4", "letter"] = "a4"
    pptx_aspect_ratio: Literal["16:9", "4:3"] = "16:9"

    @model_validator(mode="before")
    @classmethod
    def apply_preset_defaults(cls, value: object) -> object:
        """Apply a preset only where the caller omitted an explicit value."""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        preset = str(result.get("preset") or "default")
        defaults = {
            "default": {
                "primary_color": "#0F766E",
                "accent_color": "#F6BD60",
                "font_family": "cjk_sans",
            },
            "boardroom": {
                "primary_color": "#1F4E79",
                "accent_color": "#C9A227",
                "font_family": "cjk_sans",
            },
            "academic": {
                "primary_color": "#333333",
                "accent_color": "#8B1E3F",
                "font_family": "serif",
            },
        }.get(preset, {})
        for key, default in defaults.items():
            result.setdefault(key, default)
        return result

    @field_validator("primary_color", "accent_color", mode="before")
    @classmethod
    def normalize_color(cls, value: object) -> str:
        """Normalize colors before applying the strict schema pattern."""
        return str(value).strip().upper()

    @field_validator("footer_text", mode="before")
    @classmethod
    def sanitize_footer(cls, value: object) -> str:
        """Remove control characters from the plain-text footer."""
        return " ".join(
            "".join(
                char
                for char in str(value or "")
                if char >= " "
                and not 0x7F <= ord(char) <= 0x9F
                and unicodedata.category(char) != "Cc"
            ).split()
        )


class RenderedArtifact(BaseModel):
    """Publisher output before it is committed to the run directory."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    # Qualify the builtin because the compatibility ``bytes`` property below
    # occupies that name in the class namespace during Pydantic resolution.
    content: builtins.bytes = Field(
        validation_alias=AliasChoices("content", "bytes"),
    )
    media_type: str
    extension: str
    page_count: int | None = Field(default=None, ge=1)
    slide_count: int | None = Field(default=None, ge=1)
    preview: dict | None = None

    @property
    def bytes(self) -> bytes:
        """Return the artifact bytes using the publisher-contract spelling."""
        return self.content
class ReportDraft(BaseModel):
    """Canonical Markdown draft produced before the report quality review.

    ``ReportDraft`` intentionally carries only report-product metadata and a
    bounded provenance projection.  It does not contain raw tool output or
    rejected handoff text, which makes it suitable for checkpoint persistence
    and for passing to the Reviewer.
    """

    model_config = ConfigDict(extra="allow")

    markdown: str = ""
    # ``body_markdown`` is retained as an explicit compatibility spelling for
    # callers that work directly with :class:`AssemblyResult`.
    body_markdown: str | None = None
    sources: List[SourceRef] = Field(default_factory=list)
    sections: List[WrittenSection] = Field(default_factory=list)
    profile_name: str = "default"
    report_type: str = "default"
    output_format: str = "markdown"
    reference_style: str = "numbered"
    coverage_checklist: List[Any] = Field(default_factory=list)
    evaluation_snapshot: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    # Small, JSON-native metadata needed to render non-Markdown artifacts and
    # preserve legacy completion/quality-gate fields during a staged run.
    finalization: dict[str, Any] = Field(default_factory=dict)
    sha256: str = ""
    attempt: int = 0

    @model_validator(mode="before")
    @classmethod
    def _sync_markdown_names(cls, values: Any) -> Any:
        """Accept either ``markdown`` or the legacy ``body_markdown`` key."""
        if not isinstance(values, dict):
            return values
        payload = dict(values)
        markdown = payload.get("markdown")
        body = payload.get("body_markdown")
        if body is None:
            body = payload.get("final_report") or payload.get("content") or payload.get("text")
            if body is not None:
                payload["body_markdown"] = body
        # ``body_markdown`` is the spelling used by the graph adapter when a
        # revision replaces an existing draft.  Prefer it when both aliases
        # are present but differ so a stale ``markdown`` value cannot be
        # republished after resume.
        if body is not None and body != markdown:
            payload["markdown"] = body
        if body is None and markdown is not None:
            payload["body_markdown"] = markdown
        return payload

    @model_validator(mode="after")
    def _ensure_markdown_names(self) -> ReportDraft:
        """Keep both compatibility spellings synchronized after validation."""
        if self.body_markdown is None:
            self.body_markdown = self.markdown
        elif not self.markdown:
            self.markdown = self.body_markdown
        return self

    @property
    def text(self) -> str:
        """Return the canonical Markdown body."""
        return self.markdown

    @property
    def final_report(self) -> str:
        """Compatibility accessor used by legacy graph adapters."""
        return self.markdown


class ReportReviewIssue(BaseModel):
    """One actionable defect identified in a final report draft."""

    model_config = ConfigDict(extra="allow")

    category: Literal[
        "coverage",
        "citation_correctness",
        "contradictions",
        "contradiction",
        "unsupported_claims",
        "unsupported_claim",
        "redundancy",
        "executive_readability",
        "other",
    ] = "other"
    severity: Literal[
        "info",
        "low",
        "medium",
        "minor",
        "warning",
        "major",
        "high",
        "critical",
    ] = "medium"
    location: str = ""
    requirement_id: str | None = None
    evidence_ids: List[str] = Field(default_factory=list)
    citation_target: str | None = None
    description: str = ""
    revision_instruction: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_category_aliases(cls, values: Any) -> Any:
        """Accept singular labels emitted by some structured-output providers."""
        if not isinstance(values, dict):
            return values
        payload = dict(values)
        aliases = {
            "contradiction": "contradictions",
            "unsupported_claim": "unsupported_claims",
        }
        category = payload.get("category")
        if isinstance(category, str):
            payload["category"] = aliases.get(category.strip().lower(), category)
        severity = payload.get("severity")
        if isinstance(severity, str) and severity.strip().lower() == "warning":
            payload["severity"] = "medium"
        elif isinstance(severity, str) and severity.strip().lower() == "minor":
            payload["severity"] = "low"
        for field_name, limit in (
            ("location", 500),
            ("requirement_id", 200),
            ("citation_target", 2_000),
            ("description", 2_000),
            ("revision_instruction", 2_000),
        ):
            value = payload.get(field_name)
            if value is not None:
                payload[field_name] = str(value)[:limit]
        evidence_ids = payload.get("evidence_ids")
        if isinstance(evidence_ids, list):
            payload["evidence_ids"] = [str(item)[:200] for item in evidence_ids[:30]]
        return payload


class ReportCoverageReview(BaseModel):
    """Reviewer assessment for one stable user coverage requirement."""

    model_config = ConfigDict(extra="allow")

    requirement_id: str
    status: Literal["covered", "partial", "missing"] = "missing"
    explanation: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_status(cls, values: Any) -> Any:
        """Map Research Judge vocabulary to the report-review vocabulary."""
        if isinstance(values, dict):
            payload = dict(values)
            if payload.get("status") == "unsupported":
                payload["status"] = "missing"
            if "explanation" not in payload and "notes" in payload:
                payload["explanation"] = payload["notes"]
            return payload
        return values


class ReportCitationReview(BaseModel):
    """Audit result for one factual claim and its citation target."""

    model_config = ConfigDict(extra="allow")

    claim: str = ""
    citation_target: str = ""
    supported: bool = False
    evidence_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_target_aliases(cls, values: Any) -> Any:
        """Accept common ``target``/``citation`` spellings from model output."""
        if isinstance(values, dict):
            payload = dict(values)
            if not payload.get("citation_target"):
                payload["citation_target"] = payload.get("target") or payload.get(
                    "citation", ""
                )
            return payload
        return values


def _score_value(value: Any, *, legacy_scale: bool = False) -> Any:
    """Normalize one score, preserving strict validation for malformed values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    # Some providers copy the Research Judge's 1..5 scale.  Supporting it only
    # when the complete score set clearly uses that scale keeps the public
    # contract's 0..1 bounds fail-closed for isolated values such as 1.01.
    if legacy_scale:
        number /= 5.0
    return number


class ReportDimensionScores(BaseModel):
    """Normalized scores for the six final-report review dimensions."""

    model_config = ConfigDict(extra="allow")

    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    citation_correctness: float = Field(default=0.0, ge=0.0, le=1.0)
    contradictions: float = Field(default=0.0, ge=0.0, le=1.0)
    unsupported_claims: float = Field(default=0.0, ge=0.0, le=1.0)
    redundancy: float = Field(default=0.0, ge=0.0, le=1.0)
    executive_readability: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_scores(cls, values: Any) -> Any:
        """Clamp malformed provider values without weakening deterministic gates."""
        if not isinstance(values, dict):
            return values
        payload = dict(values)
        numeric_values: list[float] = []
        for name in (
            "coverage",
            "citation_correctness",
            "contradictions",
            "unsupported_claims",
            "redundancy",
            "executive_readability",
        ):
            value = payload.get(name)
            if value is None or isinstance(value, bool):
                continue
            try:
                numeric_values.append(float(value))
            except (TypeError, ValueError):
                continue
        legacy_scale = (
            len(numeric_values) >= 2
            and sum(value > 1.0 for value in numeric_values) >= 2
            and all(0.0 <= value <= 5.0 for value in numeric_values)
        )
        for name in (
            "coverage",
            "citation_correctness",
            "contradictions",
            "unsupported_claims",
            "redundancy",
            "executive_readability",
        ):
            if name in payload:
                payload[name] = _score_value(payload[name], legacy_scale=legacy_scale)
        # A few models use ``citation_accuracy`` for this dimension.
        if "citation_correctness" not in payload and "citation_accuracy" in payload:
            payload["citation_correctness"] = _score_value(
                payload["citation_accuracy"], legacy_scale=legacy_scale
            )
        return payload

    def as_dict(self) -> dict[str, float]:
        """Return scores in a stable JSON-native mapping."""
        return {
            name: float(getattr(self, name))
            for name in (
                "coverage",
                "citation_correctness",
                "contradictions",
                "unsupported_claims",
                "redundancy",
                "executive_readability",
            )
        }


class ReportReview(BaseModel):
    """Structured result of the final-report Reviewer gate.

    The model's ``decision`` is advisory input.  The runtime recomputes the
    effective decision after deterministic identifier, source-scope, and score
    checks, so a model cannot bypass a hard failure by returning ``pass``.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1.0"
    decision: Literal["pass", "revise", "fail"] = "revise"
    dimensions: ReportDimensionScores = Field(default_factory=ReportDimensionScores)
    coverage: List[ReportCoverageReview] = Field(default_factory=list)
    citation_audit: List[ReportCitationReview] = Field(default_factory=list)
    issues: List[ReportReviewIssue] = Field(default_factory=list)
    summary: str = ""
    # ``status`` describes execution/availability and is deliberately separate
    # from the quality decision (e.g. a fail-open skipped review is degraded).
    status: Literal[
        "pending",
        "running",
        "revising",
        "completed",
        "passed",
        "revised",
        "degraded",
        "skipped",
        "failed",
        "error",
    ] = "completed"
    skipped: bool = False
    degraded: bool = False
    hard_failures: List[str] = Field(default_factory=list)
    deterministic_failures: List[str] = Field(default_factory=list)
    quality_thresholds: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0
    draft_sha256: str = ""
    model: str = ""
    policy_version: str = ""
    evaluation_epoch: str = ""
    input_truncated: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_dimension_aliases(cls, values: Any) -> Any:
        """Accept ``scores`` and ``dimension_scores`` aliases in LLM output."""
        if not isinstance(values, dict):
            return values
        payload = dict(values)
        if "dimensions" not in payload:
            for alias in ("dimension_scores", "scores"):
                if alias in payload:
                    payload["dimensions"] = payload[alias]
                    break
        # A few structured-output adapters flatten the six scores directly on
        # the review object.  Preserve that wire compatibility while keeping
        # one canonical nested representation for persistence and gating.
        if "dimensions" not in payload:
            direct = {
                name: payload[name]
                for name in (
                    "coverage",
                    "citation_correctness",
                    "contradictions",
                    "unsupported_claims",
                    "redundancy",
                    "executive_readability",
                )
                if name in payload and not isinstance(payload[name], list)
            }
            if direct:
                payload["dimensions"] = direct
        # ``coverage`` is also the name of the coverage-review list.  When a
        # flattened response uses it as a numeric score, move the score into
        # ``dimensions`` and leave the list field empty so Pydantic can parse
        # the object without silently dropping the score.
        if "coverage" in payload and not isinstance(payload.get("coverage"), list):
            dimensions = payload.setdefault("dimensions", {})
            if isinstance(dimensions, dict):
                dimensions.setdefault("coverage", payload.get("coverage"))
            payload["coverage"] = payload.get("coverage_reviews", [])
        if "citation_audit" not in payload and "citations" in payload:
            payload["citation_audit"] = payload["citations"]
        if "coverage" not in payload and "coverage_reviews" in payload:
            payload["coverage"] = payload["coverage_reviews"]
        return payload

    @property
    def scores(self) -> ReportDimensionScores:
        """Compatibility accessor for callers using the shorter name."""
        return self.dimensions

    @property
    def dimension_scores(self) -> ReportDimensionScores:
        """Compatibility accessor for explicit schema naming."""
        return self.dimensions

    @property
    def citations(self) -> List[ReportCitationReview]:
        """Compatibility accessor for callers using ``citations``."""
        return self.citation_audit

    @property
    def coverage_reviews(self) -> List[ReportCoverageReview]:
        """Compatibility accessor for callers using ``coverage_reviews``."""
        return self.coverage

    @property
    def critical_issue_count(self) -> int:
        """Return high/critical issues in the four non-compensable dimensions."""
        critical_categories = {
            "coverage",
            "citation_correctness",
            "contradictions",
            "contradiction",
            "unsupported_claims",
            "unsupported_claim",
        }
        return sum(
            issue.severity in {"high", "critical"}
            and issue.category in critical_categories
            for issue in self.issues
        )

    @property
    def issue_count(self) -> int:
        """Return the total number of normalized review issues."""
        return len(self.issues)

    @property
    def hard_failure(self) -> bool:
        """Return whether deterministic or critical failures are present."""
        blocking_codes = {
            code
            for code in self.hard_failures
            if code != "score_below_aggregate_floor"
        }
        return bool(
            blocking_codes
            or self.deterministic_failures
            or self.critical_issue_count
        )

    @property
    def gate_decision(self) -> str:
        """Compatibility accessor for the graph's gate terminology."""
        return self.decision

    @property
    def overall_score(self) -> float:
        """Return the arithmetic mean of all six dimension scores."""
        values = list(self.dimensions.as_dict().values())
        return sum(values) / len(values) if values else 0.0

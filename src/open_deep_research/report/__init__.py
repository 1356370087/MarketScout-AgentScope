"""Report product system for Open Deep Research.

A registry-based dispatch that turns collected research findings into a report
*product form* selected by configuration: report type (genre), output format,
assembly mode, and reference style. Domain skills plug in later to provide
domain-specific context orchestration.

The single entry point is :func:`build_report`, which replaces the body of the
original ``final_report_generation`` node. The ``default`` report type
retains single-call synthesis with shared writing safeguards; all other
product forms are opt-in via ``Configuration``.
"""

from open_deep_research.report.canonical import (
    CanonicalizationLimits,
    build_canonical_report,
    canonicalize_report,
    validate_canonical_report,
)
from open_deep_research.report.models import (
    CanonicalReport,
    CanonicalSection,
    CodeBlock,
    InlineRun,
    ListBlock,
    ParagraphBlock,
    PublisherTheme,
    QuoteBlock,
    RenderedArtifact,
    ReportCitationReview,
    ReportCoverageReview,
    ReportDimensionScores,
    ReportDraft,
    ReportReview,
    ReportReviewIssue,
    SourceRef,
    TableBlock,
)
from open_deep_research.report.orchestrator import (
    build_report,
    build_report_draft,
    finalize_report,
    recover_report_draft,
    review_report,
    revise_report,
)
from open_deep_research.report.publishers import (
    Publisher,
    PublisherRegistry,
    get_publisher,
    render_publication,
    resolve_publication_format,
)

__all__ = [
    "CanonicalReport",
    "CanonicalSection",
    "CanonicalizationLimits",
    "CodeBlock",
    "InlineRun",
    "ListBlock",
    "ParagraphBlock",
    "Publisher",
    "PublisherRegistry",
    "PublisherTheme",
    "QuoteBlock",
    "RenderedArtifact",
    "SourceRef",
    "TableBlock",
    "ReportCitationReview",
    "ReportCoverageReview",
    "ReportDimensionScores",
    "ReportDraft",
    "ReportReview",
    "ReportReviewIssue",
    "build_canonical_report",
    "build_report",
    "build_report_draft",
    "canonicalize_report",
    "finalize_report",
    "get_publisher",
    "recover_report_draft",
    "render_publication",
    "resolve_publication_format",
    "review_report",
    "revise_report",
    "validate_canonical_report",
]

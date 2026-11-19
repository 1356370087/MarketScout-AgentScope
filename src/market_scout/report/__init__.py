"""Report product system for Open Deep Research.

A registry-based dispatch that turns collected research findings into a report
*product form* selected by configuration: report type (genre), output format,
assembly mode, and reference style. Domain skills plug in later to provide
domain-specific context orchestration.

The single entry point is :func:`build_report`, which replaces the body of the
original ``final_report_generation`` node. The ``default`` report type
retains single-call synthesis with shared writing safeguards; all other
product forms are opt-in via ``Configuration``.

Exports load lazily so pure coverage contracts do not import legacy model callers.
"""

from importlib import import_module

_EXPORT_MODULES = {
    "CanonicalizationLimits": "market_scout.report.canonical",
    "build_canonical_report": "market_scout.report.canonical",
    "canonicalize_report": "market_scout.report.canonical",
    "validate_canonical_report": "market_scout.report.canonical",
    "CanonicalReport": "market_scout.report.models",
    "CanonicalSection": "market_scout.report.models",
    "CodeBlock": "market_scout.report.models",
    "InlineRun": "market_scout.report.models",
    "ListBlock": "market_scout.report.models",
    "ParagraphBlock": "market_scout.report.models",
    "PublisherTheme": "market_scout.report.models",
    "QuoteBlock": "market_scout.report.models",
    "RenderedArtifact": "market_scout.report.models",
    "ReportCitationReview": "market_scout.report.models",
    "ReportCoverageReview": "market_scout.report.models",
    "ReportDimensionScores": "market_scout.report.models",
    "ReportDraft": "market_scout.report.models",
    "ReportReview": "market_scout.report.models",
    "ReportReviewIssue": "market_scout.report.models",
    "SourceRef": "market_scout.report.models",
    "TableBlock": "market_scout.report.models",
    "build_report": "market_scout.report.orchestrator",
    "build_report_draft": "market_scout.report.orchestrator",
    "finalize_report": "market_scout.report.orchestrator",
    "recover_report_draft": "market_scout.report.orchestrator",
    "review_report": "market_scout.report.orchestrator",
    "revise_report": "market_scout.report.orchestrator",
    "Publisher": "market_scout.report.publishers",
    "PublisherRegistry": "market_scout.report.publishers",
    "get_publisher": "market_scout.report.publishers",
    "render_publication": "market_scout.report.publishers",
    "resolve_publication_format": "market_scout.report.publishers",
}


def __getattr__(name: str):
    """Resolve the existing public report API without eager runtime imports."""
    if name not in _EXPORT_MODULES:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORT_MODULES[name]), name)
    globals()[name] = value
    return value


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

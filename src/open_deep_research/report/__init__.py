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
    "CanonicalizationLimits": "open_deep_research.report.canonical",
    "build_canonical_report": "open_deep_research.report.canonical",
    "canonicalize_report": "open_deep_research.report.canonical",
    "validate_canonical_report": "open_deep_research.report.canonical",
    "CanonicalReport": "open_deep_research.report.models",
    "CanonicalSection": "open_deep_research.report.models",
    "CodeBlock": "open_deep_research.report.models",
    "InlineRun": "open_deep_research.report.models",
    "ListBlock": "open_deep_research.report.models",
    "ParagraphBlock": "open_deep_research.report.models",
    "PublisherTheme": "open_deep_research.report.models",
    "QuoteBlock": "open_deep_research.report.models",
    "RenderedArtifact": "open_deep_research.report.models",
    "ReportCitationReview": "open_deep_research.report.models",
    "ReportCoverageReview": "open_deep_research.report.models",
    "ReportDimensionScores": "open_deep_research.report.models",
    "ReportDraft": "open_deep_research.report.models",
    "ReportReview": "open_deep_research.report.models",
    "ReportReviewIssue": "open_deep_research.report.models",
    "SourceRef": "open_deep_research.report.models",
    "TableBlock": "open_deep_research.report.models",
    "build_report": "open_deep_research.report.orchestrator",
    "build_report_draft": "open_deep_research.report.orchestrator",
    "finalize_report": "open_deep_research.report.orchestrator",
    "recover_report_draft": "open_deep_research.report.orchestrator",
    "review_report": "open_deep_research.report.orchestrator",
    "revise_report": "open_deep_research.report.orchestrator",
    "Publisher": "open_deep_research.report.publishers",
    "PublisherRegistry": "open_deep_research.report.publishers",
    "get_publisher": "open_deep_research.report.publishers",
    "render_publication": "open_deep_research.report.publishers",
    "resolve_publication_format": "open_deep_research.report.publishers",
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

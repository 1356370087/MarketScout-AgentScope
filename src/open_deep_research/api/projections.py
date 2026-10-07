"""Stable browser response allowlists shared by native and historical runs."""

from typing import Any

_REPORT_REVIEW_SUMMARY_KEYS = (
    "schema_version",
    "status",
    "decision",
    "gate_decision",
    "attempt",
    "revision_count",
    "issue_count",
    "critical_issue_count",
    "hard_failure",
    "degraded",
    "skipped",
    "draft_sha256",
    "sha256",
    "policy_version",
    "evaluation_epoch",
)

_REPORT_REVIEW_DIMENSION_KEYS = {
    "coverage",
    "citation_correctness",
    "contradictions",
    "unsupported_claims",
    "redundancy",
    "executive_readability",
}


def _stable_report_review(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a content-free report-review summary for the public API.

    Review objects contain issue descriptions, evidence IDs, and provenance
    intended for internal recovery only.  The run API exposes a small status
    projection so clients can render progress without receiving that data.
    """
    state = result or {}
    candidates: list[Any] = [state]
    nested = state.get("result") if isinstance(state, dict) else None
    if isinstance(nested, dict):
        candidates.append(nested)
    raw: Any = None
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(
            candidate.get("report_review"), dict
        ):
            raw = candidate["report_review"]
            break
    if not isinstance(raw, dict):
        return None
    # QueryEngine/public-event producers historically used both singular and
    # plural spellings; normalize them at the API boundary.
    raw = dict(raw)
    if "issue_count" not in raw and "issues_count" in raw:
        raw["issue_count"] = raw["issues_count"]
    if "critical_issue_count" not in raw and "critical_issues_count" in raw:
        raw["critical_issue_count"] = raw["critical_issues_count"]
    summary: dict[str, Any] = {
        key: raw[key]
        for key in _REPORT_REVIEW_SUMMARY_KEYS
        if key in raw and isinstance(raw[key], str | int | float | bool)
    }
    # Dimension scores are safe numeric metadata, but never pass through
    # arbitrary nested objects from the model response.
    dimensions = (
        raw.get("dimensions") or raw.get("dimension_scores") or raw.get("scores")
    )
    if isinstance(dimensions, dict):
        numeric = {
            str(key): float(value)
            for key, value in dimensions.items()
            if key in _REPORT_REVIEW_DIMENSION_KEYS
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        }
        if numeric:
            summary["dimensions"] = numeric
    return summary or None


def _stable_output(
    result: dict[str, Any] | None,
    report: str = "",
    *,
    publications: list[dict[str, Any]] | None = None,
    preferred_output_format: str | None = None,
    publication_theme: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = result or {}
    outcome = state.get("result") if isinstance(state.get("result"), dict) else state
    outcome = outcome if isinstance(outcome, dict) else {}
    markdown = report or str(state.get("final_report") or outcome.get("result") or "")
    return {
        "markdown": markdown,
        "artifacts": state.get("artifacts") or outcome.get("artifacts") or [],
        "publications": publications or [],
        "preferred_output_format": preferred_output_format,
        "publication_theme": publication_theme,
        "quality_gate": state.get("quality_gate") or outcome.get("quality_gate"),
        "report_review": _stable_report_review(state) or _stable_report_review(outcome),
        "termination_reason": outcome.get("termination_reason"),
        "status": outcome.get("status"),
        "usage": outcome.get("usage") or {},
        "usage_accounting": outcome.get("usage_accounting"),
        "metrics": outcome.get("metrics") or {},
        "completion_status": outcome.get("completion_status"),
        "stop_reason": outcome.get("stop_reason"),
        "uncovered_requirements": outcome.get("uncovered_requirements", []),
        "research_gaps": outcome.get("research_gaps", []),
    }

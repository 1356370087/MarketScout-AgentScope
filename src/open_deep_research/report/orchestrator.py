"""Single entry point for report generation.

:func:`build_report` replaces the body of the original ``final_report_generation``
node in ``agents/deep_researcher.py``. It dispatches by report profile (a
registry lookup, not an if/elif chain) and renders the requested output format.
The ``default`` report type retains single-call synthesis with shared
evidence and citation validation.

Contract preserved for backward compatibility:

* ``final_report`` is always a markdown string.
* ``messages`` carries the writer AIMessage; terminal writer failures propagate.
* ``notes`` and ``completed_task_outputs`` are cleared via the override reducer,
  exactly as before.
* Every successful report stores a data-minimized ``evaluation_snapshot`` before
  transient evidence is released.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from open_deep_research.configuration import QUALITY_POLICY_VERSION, Configuration
from open_deep_research.evaluation import build_evaluation_snapshot
from open_deep_research.events.public import canonical_local_source
from open_deep_research.evidence import (
    source_scoped_evidence_records,
)
from open_deep_research.run_context import RunContextStore
from open_deep_research.security.content import sanitize_report_markdown

from .assembly import AssemblyResult, ReportContext, assemble
from .canonical import canonicalize_report
from .citations import check_citations
from .coverage import derive_state_coverage_checklist
from .models import ReportDraft, ReportReview, SourceRef
from .profiles import (
    AssemblyMode,
    OutputFormat,
    ReferenceStyle,
    ReportProfile,
    get_profile,
)
from .references import (
    numbered_source_urls,
    parse_sources_from_text,
    render_references,
    replace_sources_section,
)
from .renderers import render_artifacts
from .runtime import AIMessage, RunnableConfig, get_trace_recorder

_FENCE_LINE_RE = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<rest>[^\r\n]*)"
)
_URL_RE = re.compile(r"https?://[^\s)\]}>]+")
_SOURCES_SECTION_RE = re.compile(
    r"(?ims)^\s*#{1,6}\s*(?:sources|references|来源|参考资料)\s*$.*\Z"
)
_REPORT_MISSING_CITATIONS = "report_missing_verifiable_citations"
_REPORT_DISALLOWED_CODE_URL = "report_disallowed_fenced_code_url"
_REPORT_UNRESOLVED_CITATIONS = "report_unresolved_citations"
_LOCAL_DOCUMENT_LINK_RE = re.compile(
    r"/documents/[A-Za-z0-9_-]+"
    r"(?:/chunks/[A-Za-z0-9_-]+|\?chunk=[A-Za-z0-9_-]+)?"
)


def _is_internal_source(source: object) -> bool:
    """Classify report sources from explicit provenance, never URI syntax."""
    if isinstance(source, dict):
        source_type = source.get("source_type")
    else:
        source_type = getattr(source, "source_type", None)
    return source_type == "local_document"


async def build_evidence_limited_report(
    evidence_records: list[dict[str, Any]],
    **kwargs: Any,
) -> str:
    """Load the restricted writer lazily to avoid quality/report import cycles."""
    from .evidence_synthesis import (
        build_evidence_limited_report as build_restricted_report,
    )

    return await build_restricted_report(evidence_records, **kwargs)


def _cleared_state() -> dict:
    """Return the notes/task-outputs override used by the original node."""
    return {
        "notes": {"type": "override", "value": []},
        "completed_task_outputs": {"type": "override", "value": []},
    }


def _canonical_report_payload(
    markdown: str,
    state: dict,
    config: RunnableConfig,
    profile: ReportProfile,
    sources: list[Any],
) -> dict[str, Any] | None:
    """Build a publisher model without letting publication faults fail research."""
    metadata = config.get("metadata", {})
    raw_theme = metadata.get("publication_theme", {})
    locale = raw_theme.get("locale", "zh-CN") if isinstance(raw_theme, dict) else "zh-CN"
    completion = state.get("completion_decision", {})
    if (
        isinstance(completion, Mapping)
        and completion.get("type") == "override"
    ):
        completion = completion.get("value", {})
    completion_status = (
        "partial"
        if isinstance(completion, Mapping)
        and completion.get("action") == "complete_partial"
        else "success"
    )
    try:
        report = canonicalize_report(
            markdown,
            run_id=str(metadata.get("run_id", "default")),
            report_type=profile.key,
            completion_status=completion_status,
            locale=str(locale),
            sources=sources or parse_sources_from_text(markdown),
            fallback_title=str(state.get("research_brief") or "Research Report"),
        )
    except Exception:
        return None
    return report.model_dump(mode="json")


def _markdown_regions(markdown: str) -> list[tuple[bool, str]]:
    """Return ordered prose/code regions without parsing or rewriting content."""
    regions: list[tuple[bool, str]] = []
    buffer: list[str] = []
    in_fence = False
    fence_char = ""
    fence_length = 0

    def flush(is_code: bool) -> None:
        if buffer:
            regions.append((is_code, "".join(buffer)))
            buffer.clear()

    for line in markdown.splitlines(keepends=True):
        match = _FENCE_LINE_RE.match(line)
        if not in_fence:
            if match is None:
                buffer.append(line)
                continue
            flush(False)
            in_fence = True
            fence = match.group("fence")
            fence_char = fence[0]
            fence_length = len(fence)
            buffer.append(line)
            continue

        buffer.append(line)
        if match is None:
            continue
        fence = match.group("fence")
        if (
            fence[0] == fence_char
            and len(fence) >= fence_length
            and not match.group("rest").strip()
        ):
            flush(True)
            in_fence = False
            fence_char = ""
            fence_length = 0

    flush(in_fence)
    return regions


def _transform_markdown_prose(
    markdown: str,
    transform: Callable[[str], str],
) -> str:
    """Apply a text transform only outside fenced code blocks."""
    return "".join(
        region if is_code else transform(region)
        for is_code, region in _markdown_regions(markdown)
    )


def _prose_without_sources(markdown: str) -> str:
    """Return prose-only Markdown with the terminal references section removed."""
    prose = "".join(
        region
        for is_code, region in _markdown_regions(markdown)
        if not is_code
    )
    return _SOURCES_SECTION_RE.sub("", prose)


def _normalized_urls(text: str) -> set[str]:
    """Return normalized absolute URLs found in one Markdown fragment."""
    urls = {
        _canonical_url(raw_url)
        for raw_url in _URL_RE.findall(text)
        if _canonical_url(raw_url)
    }
    for raw_url in _LOCAL_DOCUMENT_LINK_RE.findall(text):
        source = canonical_local_source(raw_url)
        if source is not None:
            urls.add(source["url"])
    return urls


def _reference_identity(value: str) -> str:
    """Normalize either an external URL or an owner-controlled document route."""
    external = _canonical_url(value)
    if external:
        return external
    local = canonical_local_source(value)
    return local["url"] if local is not None else ""


def _remap_numbered_citations(markdown: str, sources: list[SourceRef]) -> str:
    """Bind writer citation numbers to approved URLs before rebuilding Sources."""
    prose = "".join(region for code, region in _markdown_regions(markdown) if not code)
    original = numbered_source_urls(prose)
    approved = {
        _reference_identity(source.url): str(index)
        for index, source in enumerate(sources, 1)
    }
    # Inline code and explicit Markdown links do not use the numeric catalogue.
    marker_re = re.compile(r"(?P<ticks>`+).*?(?P=ticks)|\[(?P<number>\d+)\](?!\()")

    def remap(match: re.Match[str]) -> str:
        if match.group("number") is None:
            return match.group(0)
        number = str(int(match.group("number")))
        identity = _reference_identity(original.get(number, ""))
        target = approved.get(identity) if identity else None
        if target is None:
            raise ValueError(_REPORT_UNRESOLVED_CITATIONS)
        return f"[{target}]"

    def remap_body(region: str) -> str:
        return "".join(
            line if numbered_source_urls(line) else marker_re.sub(remap, line)
            for line in region.splitlines(keepends=True)
        )

    return _transform_markdown_prose(markdown, remap_body)


def _canonical_url(value: str) -> str:
    """Canonicalize only security-preserving URL variations for comparison."""
    candidate = str(value or "").strip().rstrip(".,;:")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _rewrite_links_to_allowlist(markdown: str, allowed_urls: set[str]) -> str:
    """Drop unknown links and rewrite equivalent links to exact allowed URLs."""
    allowed_by_canonical = {
        canonical: url
        for url in allowed_urls
        if (canonical := _reference_identity(url))
    }

    def keep_fetched_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        allowed = allowed_by_canonical.get(_reference_identity(url))
        return f"[{label}]({allowed})" if allowed else label

    return _transform_markdown_prose(
        markdown,
        lambda prose: re.sub(
            r"\[([^\]]+)\]\((https?://[^)]+|/documents/[^)\s]+)\)",
            keep_fetched_link,
            prose,
        ),
    )


async def _repair_missing_report_citations(
    markdown: str,
    ctx: ReportContext,
) -> str:
    """Ask the writer for one bounded citation-only repair attempt."""
    messages = ctx.stage_messages(
        "Repair the complete Markdown draft. Cite each factual claim with an "
        "actually supporting source from the records. Remove or qualify unsupported "
        "claims; do not preserve an unsupported conclusion merely to add a citation. "
        "Use inline [Title](URL) links and return only the complete repaired report.",
        {"draft": markdown, "research_brief": ctx.state.get("research_brief", "")},
        ctx.evidence_records,
    )
    repaired = await ctx.invoke_writer_with_output_recovery(
        messages, span_name="lead.final_report.citation_repair",
    )
    return str(repaired.content)


def _has_verifiable_body_citation(
    markdown: str,
    *,
    allowed_urls: set[str],
    source_count: int,
) -> bool:
    """Return whether report prose cites an allowlisted source before Sources."""
    body = _prose_without_sources(markdown)
    allowed_identities = {
        identity
        for value in allowed_urls
        if (identity := _reference_identity(value))
    }
    if _normalized_urls(body).intersection(allowed_identities):
        return True
    return any(
        1 <= int(marker) <= source_count
        for marker in re.findall(r"\[(\d+)\]", body)
    )


def _assert_fenced_urls_allowlisted(
    markdown: str,
    *,
    allowed_urls: set[str],
) -> None:
    """Fail closed instead of mutating URL-shaped strings inside code."""
    allowed_identities = {
        identity
        for value in allowed_urls
        if (identity := _reference_identity(value))
    }
    disallowed = {
        url
        for is_code, region in _markdown_regions(markdown)
        if is_code
        for url in _normalized_urls(region)
        if url not in allowed_identities
    }
    if disallowed:
        raise ValueError(_REPORT_DISALLOWED_CODE_URL)


def _state_evidence_records(state: dict) -> list[dict]:
    """Return JSON-native evidence records from reducer-wrapped state."""
    records = state.get("evidence_registry", [])
    if (
        isinstance(records, dict)
        and records.get("type") == "override"
    ):
        records = records.get("value", [])
    return [
        dict(record)
        for record in records
        if isinstance(record, Mapping)
    ] if isinstance(records, list) else []


def _evidence_integrity_accepted(record: Mapping[str, Any]) -> bool:
    """Reject evidence carrying an explicit integrity/admission failure marker."""
    if record.get("hash_verified") is False or record.get("integrity_verified") is False:
        return False
    for field_name in (
        "integrity_status",
        "hash_status",
        "verification_status",
        "admission_status",
    ):
        value = str(record.get(field_name) or "").strip().lower()
        if value and any(
            token in value
            for token in (
                "quarantin",
                "reject",
                "fail",
                "mismatch",
                "tamper",
                "invalid",
            )
        ):
            return False
    return True


def _artifact_references(state: dict) -> list[dict[str, str]]:
    """Return bounded task artifact references for deterministic recovery."""
    references = state.get("research_artifact_refs", {})
    if (
        isinstance(references, dict)
        and references.get("type") == "override"
    ):
        references = references.get("value", {})
    if not isinstance(references, Mapping):
        return []
    return [
        {
            "task_id": str(task_id),
            "path": str(
                reference.get("path")
                or f"context/artifacts/research_tasks/{task_id}.json"
            ),
            "sha256": str(reference.get("sha256", "")),
        }
        for task_id, reference in list(references.items())[:50]
        if isinstance(reference, Mapping) and reference.get("sha256")
    ]


def _uncovered_requirement_ids(state: dict) -> list[str]:
    """Return contract requirement IDs not supported by the coverage ledger."""
    contract = state.get("coverage_contract", {})
    ledger = state.get("coverage_ledger", {})
    if not isinstance(contract, Mapping) or not isinstance(ledger, Mapping):
        return []
    requirements = contract.get("requirements", [])
    if not isinstance(requirements, list):
        return []
    return [
        str(requirement.get("requirement_id"))
        for requirement in requirements
        if isinstance(requirement, Mapping)
        and requirement.get("requirement_id")
        and (
            not isinstance(
                ledger.get(str(requirement.get("requirement_id"))),
                Mapping,
            )
            or ledger[str(requirement.get("requirement_id"))].get("status")
            != "supported"
        )
    ]


async def _evidence_limited_report_update(
    state: dict,
    config: RunnableConfig,
    *,
    evidence_records: list[dict],
    caveats: list[str],
    coverage_checklist: list[Any],
    evaluation_snapshot: Any,
    reason_code: str,
    completion_reason: str = "report_evidence_validation_failed",
) -> dict:
    """Return a reducer-safe partial update sourced only from eligible evidence."""
    eligible = source_scoped_evidence_records(
        evidence_records,
        state.get("coverage_contract"),
    )
    report = await build_evidence_limited_report(
        eligible,
        coverage_contract=state.get("coverage_contract"),
        coverage_ledger=(
            dict(state.get("coverage_ledger", {}))
            if isinstance(state.get("coverage_ledger"), Mapping)
            else {}
        ),
        caveats=caveats,
        uncovered_requirement_ids=_uncovered_requirement_ids(state),
        rejection_reasons=[reason_code],
        artifact_refs=_artifact_references(state),
        config=config,
    )
    prior_completion = state.get("completion_decision", {})
    prior_gaps = (
        list(prior_completion.get("gaps", []))
        if isinstance(prior_completion, Mapping)
        else []
    )
    completion = {
        "action": "complete_partial",
        "reason": completion_reason,
        "gaps": list(dict.fromkeys([*prior_gaps, reason_code])),
    }
    prior_gate = state.get("quality_gate", {})
    quality_gate = (
        dict(prior_gate)
        if isinstance(prior_gate, Mapping)
        else {}
    )
    quality_gate["status"] = "degraded"
    quality_gate["reason_codes"] = list(dict.fromkeys([
        *(
            quality_gate.get("reason_codes", [])
            if isinstance(quality_gate.get("reason_codes"), list)
            else []
        ),
        reason_code,
    ]))
    configurable = Configuration.from_runnable_config(config)
    metadata = config.get("metadata", {})
    quality_gate.setdefault(
        "evaluator_model",
        configurable.quality_evaluation_model,
    )
    quality_gate.setdefault(
        "policy_version",
        metadata.get("quality_policy_version", QUALITY_POLICY_VERSION),
    )
    quality_gate.setdefault(
        "evaluation_epoch",
        metadata.get("quality_evaluation_epoch", "legacy-unpinned"),
    )
    quality_gate.setdefault("assessment_refs", [])
    quality_gate.setdefault(
        "quality_rigor",
        metadata.get("quality_rigor_policy", {}).get(
            "rigor",
            configurable.quality_evaluation_rigor.value,
        ),
    )
    quality_gate.setdefault(
        "quality_thresholds",
        dict(metadata.get("quality_rigor_policy", {})),
    )
    sources = parse_sources_from_text(report)
    profile = get_profile(
        getattr(Configuration.from_runnable_config(config), "report_type", None)
    )
    canonical_report = _canonical_report_payload(
        report,
        {**state, "completion_decision": completion},
        config,
        profile,
        sources,
    )
    update: dict = {
        "final_report": report,
        "messages": [AIMessage(content=report)],
        "evaluation_snapshot": evaluation_snapshot.model_dump(
            mode="json",
            exclude_none=True,
        ),
        "completion_decision": {
            "type": "override",
            "value": completion,
        },
        "quality_gate": quality_gate,
        **_cleared_state(),
        "coverage_checklist": {
            "type": "override",
            "value": coverage_checklist,
        },
    }
    if canonical_report is not None:
        update["canonical_report"] = canonical_report
    if sources:
        update["sources"] = {
            "type": "override",
            "value": [source.model_dump() for source in sources],
        }
    fmt = _resolve_output_format(configurable, profile)
    if fmt in {OutputFormat.STRUCTURED_JSON, OutputFormat.SLIDES, OutputFormat.ONE_PAGER}:
        ctx = ReportContext.from_state(state, config, profile)
        artifacts = render_artifacts(AssemblyResult(body_markdown=report, sources=sources), fmt, ctx)
        update["report_artifacts"] = {"format": fmt.value, **artifacts}
    return update


def _resolve_output_format(cfg: Configuration, profile: ReportProfile) -> OutputFormat:
    """Resolve the output format from config, falling back to the profile default."""
    raw: Optional[str] = getattr(cfg, "output_format", None)
    if raw:
        try:
            return OutputFormat(raw)
        except ValueError:
            return profile.default_format
    return profile.default_format


def _resolve_reference_style(cfg: Configuration, profile: ReportProfile) -> ReferenceStyle:
    """Resolve the reference style from config, falling back to the profile default."""
    raw: Optional[str] = getattr(cfg, "reference_style", None)
    if raw:
        try:
            return ReferenceStyle(raw)
        except ValueError:
            return profile.reference_style
    return profile.reference_style


def _research_was_attempted(state: dict) -> bool:
    """Return whether the state contains observable researcher tool activity."""
    note_text = "\n".join(str(note) for note in state.get("notes", []))
    if "rejected_by_supervisor_quality_gate" in note_text:
        return True
    for message in state.get("supervisor_messages", []):
        name = message.get("name") if isinstance(message, dict) else getattr(message, "name", None)
        if name == "ConductResearch":
            return True
    return False


def _load_researcher_task_artifacts(
    state: dict,
    config: RunnableConfig,
    cfg: Configuration,
) -> list[dict]:
    """Load integrity-checked task artifacts for the minimized evaluation view."""
    refs = state.get("research_artifact_refs", {})
    if (
        isinstance(refs, dict)
        and refs.get("type") == "override"
        and isinstance(refs.get("value"), dict)
    ):
        refs = refs["value"]
    if not isinstance(refs, Mapping) or not refs:
        return []
    run_id = str(config.get("metadata", {}).get("run_id", "default"))
    store = RunContextStore(run_id, runs_dir=cfg.runs_dir)
    artifacts: list[dict] = []
    for task_id, raw_ref in list(refs.items())[:50]:
        if not isinstance(raw_ref, Mapping) or not raw_ref.get("sha256"):
            continue
        try:
            artifact = store.load_task_result(
                str(task_id),
                expected_sha256=str(raw_ref["sha256"]),
            )
        except (FileNotFoundError, ValueError):
            continue
        artifact.setdefault("task_id", str(task_id))
        artifacts.append(artifact)
    return artifacts


async def _build_report_legacy(state: dict, config: RunnableConfig) -> dict:
    """Build the final report product from collected research state.

    Args:
        state: Agent state containing ``notes`` / ``completed_task_outputs`` /
            ``research_brief`` / ``messages`` (a plain dict mutated in place by
            the orchestrator upstream).
        config: The runnable config carrying ``configurable`` (report_type,
            output_format, reference_style, model settings, ...).

    Returns:
        The state update dict (``final_report``, ``messages``, cleared notes),
        plus ``evaluation_snapshot`` for deterministic offline scoring,
        legacy ``report_artifacts`` previews for JSON/slides/one-pager, and
        ``sources`` only when non-default reference handling runs.
    """
    cfg = Configuration.from_runnable_config(config)
    profile = get_profile(getattr(cfg, "report_type", None))
    fmt = _resolve_output_format(cfg, profile)
    ref_style = _resolve_reference_style(cfg, profile)

    ctx = ReportContext.from_state(state, config, profile)
    coverage_checklist = derive_state_coverage_checklist(state)
    researcher_task_artifacts = _load_researcher_task_artifacts(
        state,
        config,
        cfg,
    )
    evaluation_snapshot = build_evaluation_snapshot(
        state,
        coverage_checklist=coverage_checklist,
        researcher_task_artifacts=researcher_task_artifacts,
    )
    caveats = list(dict.fromkeys(
        str(caveat).strip()[:500]
        for assessment in state.get("handoff_assessments", [])
        if isinstance(assessment, dict)
        and assessment.get("admission_status")
        == "accepted_with_caveats"
        for caveat in assessment.get("caveats", [])
        if str(caveat).strip()
    ))[:20]
    if ctx.strict_evidence and not ctx.evidence_records:
        return await _evidence_limited_report_update(
            state, config, evidence_records=[], caveats=caveats,
            coverage_checklist=coverage_checklist, evaluation_snapshot=evaluation_snapshot,
            reason_code="accepted_evidence_missing",
        )
    completion = _unwrap_update_value(state.get("completion_decision"), {})
    if (
        cfg.quality_evaluation_enabled
        and isinstance(completion, Mapping)
        and completion.get("action") == "complete_partial"
    ):
        return await _evidence_limited_report_update(
            state, config,
            evidence_records=_state_evidence_records(state),
            caveats=caveats,
            coverage_checklist=coverage_checklist,
            evaluation_snapshot=evaluation_snapshot,
            reason_code="research_incomplete",
            completion_reason=str(completion.get("reason") or "research_incomplete"),
        )
    result = await assemble(ctx)

    is_sectioned = profile.assembly == AssemblyMode.SECTIONED
    needs_sources = (
        fmt != OutputFormat.MARKDOWN or ref_style != ReferenceStyle.NUMBERED
        or is_sectioned or bool(ctx.sources)
    )
    evidence_allowlist_enabled = ctx.strict_evidence
    if evidence_allowlist_enabled:
        result.sources = ctx.sources
    elif needs_sources and not result.sources:
        result.sources = parse_sources_from_text(result.body_markdown + "\n" + ctx.findings)

    markdown = _transform_markdown_prose(result.body_markdown, sanitize_report_markdown)
    if caveats:
        markdown += "\n\n## 限制与不确定性\n\n" + "\n".join(f"- {caveat}" for caveat in caveats)
    if evidence_allowlist_enabled:
        allowed_urls = {source.url for source in result.sources}
        evidence_ids = {str(record["evidence_id"]) for record in ctx.evidence_records if record.get("evidence_id")}
        checked = check_citations(markdown, allowed_urls, evidence_ids)
        if checked.errors or not checked.has_body_citation:
            get_trace_recorder(config).active_span().score("report.citation_repair_count", 1)
            try:
                repaired = await _repair_missing_report_citations(markdown, ctx)
                markdown = _transform_markdown_prose(repaired, sanitize_report_markdown)
                checked = check_citations(markdown, allowed_urls, evidence_ids)
            except Exception:  # noqa: BLE001 - deterministic recovery below
                pass
        if checked.errors or not checked.has_body_citation:
            reason = (
                _REPORT_DISALLOWED_CODE_URL
                if any(location == "fenced_code" for _, _, location in checked.errors)
                else _REPORT_UNRESOLVED_CITATIONS if checked.errors
                else _REPORT_MISSING_CITATIONS
            )
            return await _evidence_limited_report_update(
                state, config, evidence_records=_state_evidence_records(state),
                caveats=caveats, coverage_checklist=coverage_checklist,
                evaluation_snapshot=evaluation_snapshot, reason_code=reason,
            )
    render_sources = bool(result.sources) and (
        is_sectioned or ref_style == ReferenceStyle.BIBTEX_LIKE or evidence_allowlist_enabled
    )
    if render_sources:
        markdown = _remap_numbered_citations(markdown, result.sources)
        # Rewrite equivalent targets, then build the catalogue from the same ordering.
        markdown = _rewrite_links_to_allowlist(markdown, {s.url for s in result.sources})
        markdown = replace_sources_section(markdown, render_references(result.sources, ref_style))
    markdown = _transform_markdown_prose(markdown, sanitize_report_markdown)

    # Render every artifact from the same canonical markdown exposed through
    # ``final_report`` so reference rewriting cannot create divergent payloads.
    result.body_markdown = markdown
    metric_sources = result.sources or parse_sources_from_text(markdown + "\n" + ctx.findings)
    citation_markers = re.findall(r"\[(\d+)\]", markdown)
    unique_citation_markers = set(citation_markers)
    citation_density = len(citation_markers) * 1000 / len(markdown) if markdown else 0.0
    cited_source_ratio = (
        min(1.0, len(unique_citation_markers) / len(metric_sources))
        if metric_sources
        else 0.0
    )
    active_span = get_trace_recorder(config).active_span()
    active_span.score("report.source_count", len(metric_sources))
    internal_source_count = sum(_is_internal_source(source) for source in metric_sources)
    active_span.score("report.internal_source_count", internal_source_count)
    active_span.score(
        "report.web_source_count", len(metric_sources) - internal_source_count
    )
    active_span.score(
        "report.internal_source_ratio",
        internal_source_count / len(metric_sources) if metric_sources else 0.0,
    )
    active_span.score("report.citation_marker_count", len(citation_markers))
    active_span.score("report.character_count", len(markdown))
    active_span.score("report.section_count", len(result.sections))
    active_span.score("report.citation_density_per_1k_chars", citation_density)
    active_span.score("report.cited_source_ratio", cited_source_ratio)
    active_span.score("report.coverage_requirement_count", len(coverage_checklist))
    artifacts = render_artifacts(result, fmt, ctx)
    canonical_report = _canonical_report_payload(
        markdown,
        state,
        config,
        profile,
        list(result.sources),
    )

    update: dict = {
        "final_report": markdown,
        "messages": (
            [result.message]
            if result.message is not None and str(result.message.content) == markdown
            else [AIMessage(content=markdown)]
        ),
        "evaluation_snapshot": evaluation_snapshot.model_dump(
            mode="json",
            exclude_none=True,
        ),
        **_cleared_state(),
        "coverage_checklist": {
            "type": "override",
            "value": coverage_checklist,
        },
    }
    if canonical_report is not None:
        update["canonical_report"] = canonical_report

    if needs_sources and result.sources:
        update["sources"] = {
            "type": "override",
            "value": [s.model_dump() for s in result.sources],
        }

    # Only surface non-default artifacts. The markdown body already lives in
    # ``final_report`` (and the SSE ``result``), so for the default markdown
    # profile we add no optional presentation artifacts.
    if fmt in {
        OutputFormat.STRUCTURED_JSON,
        OutputFormat.SLIDES,
        OutputFormat.ONE_PAGER,
    }:
        update["report_artifacts"] = {"format": fmt.value, **artifacts}

    return update


# ---------------------------------------------------------------------------
# Staged final-report Reviewer -> Revisor lifecycle
# ---------------------------------------------------------------------------


def _json_native(value: Any) -> Any:
    """Convert a small completion payload to JSON-native values.

    Draft checkpoints must never retain live LangChain message objects.  This
    helper is intentionally conservative and is used only for metadata copied
    from the legacy finalization update.
    """
    if isinstance(value, Mapping):
        return {str(key): _json_native(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_native(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_native(value.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - metadata must not block finalization
            return str(value)
    if hasattr(value, "content") and not isinstance(value, str | bytes):
        return _json_native(getattr(value, "content", ""))
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _unwrap_update_value(value: Any, default: Any = None) -> Any:
    """Unwrap reducer override envelopes used by report state updates."""
    if isinstance(value, Mapping) and value.get("type") == "override":
        return value.get("value", default)
    return value if value is not None else default


def _draft_from_legacy_update(
    state: Mapping[str, Any],
    config: RunnableConfig,
    update: Mapping[str, Any],
) -> ReportDraft:
    """Build a persisted Draft from the compatibility report update."""
    cfg = Configuration.from_runnable_config(config)
    profile = get_profile(getattr(cfg, "report_type", None))
    fmt = _resolve_output_format(cfg, profile)
    ref_style = _resolve_reference_style(cfg, profile)
    markdown = str(update.get("final_report") or "")
    raw_sources = _unwrap_update_value(update.get("sources"), [])
    sources: list[SourceRef] = []
    if isinstance(raw_sources, list):
        for raw in raw_sources:
            if isinstance(raw, SourceRef):
                sources.append(raw)
            elif isinstance(raw, Mapping) and raw.get("url"):
                try:
                    sources.append(SourceRef.model_validate(dict(raw)))
                except Exception:  # noqa: BLE001 - ignore malformed legacy metadata
                    continue
    coverage = _unwrap_update_value(update.get("coverage_checklist"), [])
    if not isinstance(coverage, list):
        coverage = []
    # Keep only the fields needed to render/recover a completed product.  Raw
    # notes, handoffs, and tool traces are deliberately absent.
    finalization: dict[str, Any] = {
        "quality_gate": _json_native(update.get("quality_gate"))
        if update.get("quality_gate") is not None
        else None,
        "completion_decision": _json_native(update.get("completion_decision"))
        if update.get("completion_decision") is not None
        else None,
        "report_artifacts": _json_native(update.get("report_artifacts"))
        if update.get("report_artifacts") is not None
        else None,
        "has_sources_update": "sources" in update,
    }
    provenance = {
        "source": "legacy_report_assembly",
        "report_type": getattr(cfg, "report_type", "default"),
        "output_format": fmt.value,
        "reference_style": ref_style.value,
    }
    digest = hashlib.sha256(markdown.encode("utf-8", errors="replace")).hexdigest()
    return ReportDraft(
        markdown=markdown,
        body_markdown=markdown,
        sources=sources,
        sections=[],
        profile_name=getattr(profile, "name", None) or str(getattr(cfg, "report_type", "default") or "default"),
        report_type=str(getattr(cfg, "report_type", "default") or "default"),
        output_format=fmt.value,
        reference_style=ref_style.value,
        coverage_checklist=coverage,
        evaluation_snapshot=_json_native(update.get("evaluation_snapshot") or {}),
        provenance=provenance,
        finalization=finalization,
        sha256=digest,
    )


async def build_report_draft(
    state: dict,
    config: RunnableConfig,
) -> ReportDraft:
    """Assemble and canonicalize a report without publishing it.

    The compatibility assembler performs all existing evidence/source safety
    normalization.  Its final update is converted into a Draft so a caller can
    persist it, run the Reviewer, and optionally revise it before publishing.
    """
    update = await _build_report_legacy(state, config)
    return _draft_from_legacy_update(state, config, update)


async def recover_report_draft(
    draft: ReportDraft | Mapping[str, Any] | str,
    state: Mapping[str, Any] | None = None,
    config: RunnableConfig | None = None,
    *,
    reason_codes: list[str] | None = None,
) -> ReportDraft:
    """Replace a blocked draft with an accepted-evidence-only recovery report.

    This is a bounded terminal recovery, not another research pass. It consumes
    only the source-scoped evidence registry and leaves the research Quality
    Judge state untouched. The returned draft must pass through Reviewer again
    before it can be finalized.
    """
    normalized = _coerce_report_draft(draft)
    normalized_state = dict(state or {})
    runnable_config = config or {}
    eligible = [
        record
        for record in source_scoped_evidence_records(
            _state_evidence_records(normalized_state),
            normalized_state.get("coverage_contract"),
        )
        if _evidence_integrity_accepted(record)
    ]
    if not eligible:
        raise RuntimeError("insufficient_evidence")

    caveats = list(
        dict.fromkeys(
            str(caveat).strip()[:500]
            for assessment in normalized_state.get("handoff_assessments", [])
            if isinstance(assessment, Mapping)
            for caveat in assessment.get("caveats", [])
            if str(caveat).strip()
        )
    )[:20]
    reasons = list(dict.fromkeys(str(code)[:200] for code in (reason_codes or []) if code))
    markdown = await build_evidence_limited_report(
        eligible,
        coverage_contract=normalized_state.get("coverage_contract"),
        coverage_ledger=(
            dict(normalized_state.get("coverage_ledger", {}))
            if isinstance(normalized_state.get("coverage_ledger"), Mapping)
            else {}
        ),
        caveats=caveats,
        uncovered_requirement_ids=_uncovered_requirement_ids(normalized_state),
        rejection_reasons=reasons or ["report_review_failed"],
        artifact_refs=_artifact_references(normalized_state),
        config=runnable_config,
    )
    markdown = _transform_markdown_prose(markdown, sanitize_report_markdown).strip()
    if not markdown:
        raise RuntimeError("insufficient_evidence")

    sources: list[SourceRef] = []
    seen_sources: set[str] = set()
    for record in eligible:
        url = str(
            record.get("source_url")
            or record.get("url")
            or record.get("source_uri")
            or ""
        ).strip()
        if not url or url in seen_sources:
            continue
        seen_sources.add(url)
        sources.append(
            SourceRef(
                title=str(record.get("source_title") or record.get("title") or ""),
                url=url,
                source_type=(str(record.get("source_type")) if record.get("source_type") else None),
                document_id=(str(record.get("document_id")) if record.get("document_id") else None),
                chunk_id=(str(record.get("chunk_id")) if record.get("chunk_id") else None),
                locator=(str(record.get("locator")) if record.get("locator") else None),
            )
        )

    completion = {
        "type": "override",
        "value": {
            "action": "complete_partial",
            "reason": "report_review_evidence_limited_recovery",
            "gaps": reasons,
        },
    }
    provenance = {
        **dict(normalized.provenance or {}),
        "source": "report_review_evidence_limited_recovery",
        "report_review_recovery": True,
        "recovery_reason_codes": reasons,
    }
    finalization = {
        **dict(normalized.finalization or {}),
        "completion_decision": completion,
    }
    return normalized.model_copy(
        update={
            "markdown": markdown,
            "body_markdown": markdown,
            "sources": sources,
            "sections": [],
            "provenance": provenance,
            "finalization": finalization,
            "sha256": hashlib.sha256(
                markdown.encode("utf-8", errors="replace")
            ).hexdigest(),
        }
    )


def _review_has_recoverable_content_failure(review: ReportReview) -> bool:
    """Distinguish report defects from Reviewer availability/protocol faults."""
    if "report_reviewer_unavailable" in review.hard_failures:
        return False
    protocol_failures = {
        "review_protocol_invalid",
        "review_dimensions_missing",
        "coverage_contract_rows_missing_or_invalid",
        "citation_audit_missing",
    }
    deterministic = set(review.deterministic_failures)
    if deterministic and deterministic <= protocol_failures:
        return False
    content_codes = {
        code
        for code in {*review.hard_failures, *review.deterministic_failures}
        if code not in protocol_failures
        and code != "report_reviewer_unavailable"
        and code != "score_below_aggregate_floor"
        and not code.startswith("review_issue:other")
    }
    return bool(
        review.critical_issue_count
        or content_codes
    )


async def review_report(
    draft: ReportDraft | Mapping[str, Any] | str,
    state: Mapping[str, Any] | None = None,
    config: RunnableConfig | None = None,
    *,
    attempt: int | None = None,
) -> ReportReview:
    """Run the final-report Reviewer stage.

    The implementation lives in ``report.reviewer`` so it can be tested in
    isolation; this wrapper keeps the public report orchestrator API stable and
    makes the stage easy to monkeypatch in graph/persistence tests.
    """
    from .reviewer import review_report as _review_report

    return await _review_report(draft, state, config, attempt=attempt)


async def revise_report(
    draft: ReportDraft | Mapping[str, Any] | str,
    review: ReportReview | Mapping[str, Any],
    state: Mapping[str, Any] | None = None,
    config: RunnableConfig | None = None,
) -> str:
    """Run the constrained final-report Revisor stage."""
    from .reviewer import revise_report as _revise_report

    return await _revise_report(draft, review, state, config)


def _coerce_report_draft(value: ReportDraft | Mapping[str, Any] | str) -> ReportDraft:
    """Normalize a draft returned by a custom graph adapter or test double."""
    if isinstance(value, ReportDraft):
        return value
    if isinstance(value, str):
        return ReportDraft(markdown=value, body_markdown=value)
    if isinstance(value, Mapping):
        return ReportDraft.model_validate(dict(value))
    raise TypeError("draft must be ReportDraft, mapping, or Markdown string")


async def finalize_report(
    draft: ReportDraft | Mapping[str, Any] | str,
    state: Mapping[str, Any] | None = None,
    config: RunnableConfig | None = None,
) -> dict:
    """Publish a reviewed Draft and render all configured output artifacts.

    ``finalize_report`` is deliberately deterministic: it never invokes a
    language model.  Non-Markdown artifacts are rendered from the exact Markdown
    string that is exposed as ``final_report``.
    """
    normalized = _coerce_report_draft(draft)
    normalized_state = dict(state or {})
    runnable_config = config or {}
    cfg = Configuration.from_runnable_config(runnable_config)
    profile = get_profile(normalized.report_type or getattr(cfg, "report_type", None))
    try:
        fmt = OutputFormat(normalized.output_format)
    except ValueError:
        fmt = _resolve_output_format(cfg, profile)
    try:
        ref_style = ReferenceStyle(normalized.reference_style)
    except ValueError:
        ref_style = _resolve_reference_style(cfg, profile)

    markdown = str(normalized.markdown or normalized.body_markdown or "")
    update: dict[str, Any] = {
        "final_report": markdown,
        "messages": [AIMessage(content=markdown)],
        "evaluation_snapshot": dict(normalized.evaluation_snapshot or {}),
        **_cleared_state(),
        "coverage_checklist": {
            "type": "override",
            "value": list(normalized.coverage_checklist or []),
        },
    }
    if normalized.sources and (
        fmt != OutputFormat.MARKDOWN
        or ref_style != ReferenceStyle.NUMBERED
        or profile.assembly == AssemblyMode.SECTIONED
        or normalized.provenance.get("source") == "legacy_report_assembly"
    ):
        update["sources"] = {
            "type": "override",
            "value": [source.model_dump(mode="json") for source in normalized.sources],
        }

    # Reconstruct an AssemblyResult so every presentation artifact uses the
    # reviewed Markdown. Rendering failures are terminal: reusing a pre-review
    # artifact would let a non-Markdown deliverable diverge from the final text.
    if fmt != OutputFormat.MARKDOWN:
        ctx = ReportContext.from_state(
            normalized_state,
            runnable_config,
            profile,
            sources=list(normalized.sources),
        )
        result = AssemblyResult(
            body_markdown=markdown,
            message=AIMessage(content=markdown),
            sections=list(normalized.sections),
            sources=list(normalized.sources),
        )
        artifacts = render_artifacts(result, fmt, ctx)
        update["report_artifacts"] = {"format": fmt.value, **artifacts}

    # Preserve compatibility metadata generated by evidence-limited recovery;
    # Reviewer state remains separate and is attached by the caller.
    for key in ("quality_gate", "completion_decision"):
        value = normalized.finalization.get(key)
        if value is not None:
            update[key] = _json_native(value)
    return update


async def build_report(state: dict, config: RunnableConfig) -> dict:
    """Build a final report, optionally running the Reviewer -> Revisor loop.

    With ``report_review_enabled`` absent or false this delegates directly to
    the pre-existing assembler, preserving its byte-level behavior.  Enabling
    the option activates the staged lifecycle and records a bounded review
    history in the returned state update.
    """
    cfg = Configuration.from_runnable_config(config)
    if not bool(getattr(cfg, "report_review_enabled", False)):
        return await _build_report_legacy(state, config)

    draft = await build_report_draft(state, config)
    review = await review_report(draft, state, config, attempt=1)
    history: list[dict[str, Any]] = [review.model_dump(mode="json")]
    revision_count = 0
    try:
        max_revisions = int(getattr(cfg, "report_review_max_revisions", 1) or 0)
    except (TypeError, ValueError):
        max_revisions = 1
    max_revisions = max(0, min(3, max_revisions))

    # A fail-open transport/protocol outage is explicitly marked ``skipped``;
    # it must not turn into a meaningless Revisor call with no review issues.
    while (
        review.decision == "revise"
        and review.status != "skipped"
        and not getattr(review, "skipped", False)
        and revision_count < max_revisions
    ):
        revised_markdown = await revise_report(draft, review, state, config)
        revision_count += 1
        draft = draft.model_copy(
            update={
                "markdown": revised_markdown,
                "body_markdown": revised_markdown,
                "attempt": revision_count,
                "sha256": hashlib.sha256(
                    revised_markdown.encode("utf-8", errors="replace")
                ).hexdigest(),
            }
        )
        review = await review_report(
            draft,
            state,
            config,
            attempt=revision_count + 1,
        )
        history.append(review.model_dump(mode="json"))

    # A fail-open unavailable Reviewer may be explicitly published as degraded;
    # ordinary unresolved critical/protocol issues remain blocked.
    recovered = False
    if (
        review.decision in {"fail", "revise"}
        and _review_has_recoverable_content_failure(review)
    ):
        reason_codes = list(
            dict.fromkeys(
                [
                    *review.hard_failures,
                    *review.deterministic_failures,
                    *[
                        f"review_issue:{issue.category}"
                        for issue in review.issues
                        if issue.severity in {"high", "critical"}
                    ],
                ]
            )
        )
        draft = await recover_report_draft(
            draft,
            state,
            config,
            reason_codes=reason_codes,
        )
        recovered = True
        review = await review_report(
            draft,
            state,
            config,
            attempt=len(history) + 1,
        )
        history.append(review.model_dump(mode="json"))

    if review.decision == "fail":
        if recovered:
            raise RuntimeError("insufficient_evidence")
        raise RuntimeError("report_review_failed")
    if review.status in {"failed", "error"} and not getattr(review, "skipped", False):
        raise RuntimeError("report_review_failed")
    if review.status == "skipped" or getattr(review, "skipped", False):
        # A skipped review is publishable only when the draft itself passed the
        # deterministic safety checks.  ``fail_open`` cannot waive an
        # unallowlisted URL, missing evidence, or malformed review protocol.
        if review.hard_failure:
            raise RuntimeError("report_review_failed")
        review.decision = "pass"
    elif review.decision == "revise":
        if review.hard_failure:
            if recovered:
                raise RuntimeError("insufficient_evidence")
            raise RuntimeError("report_review_revision_limit_exceeded")
        review.status = "degraded"
        review.degraded = True
    if recovered:
        review.status = "degraded"
        review.degraded = True
    history[-1] = review.model_dump(mode="json")

    update = await finalize_report(draft, state, config)
    update.update(
        {
            "final_report_draft": draft.markdown,
            "report_review": review.model_dump(mode="json"),
            "report_review_history": history,
            "report_revision_count": revision_count,
        }
    )
    return update


__all__ = [
    "build_report",
    "build_report_draft",
    "finalize_report",
    "recover_report_draft",
    "review_report",
    "revise_report",
]

"""Shared deterministic report citation checks (not semantic grounding)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .references import numbered_source_urls


@dataclass
class CitationCheck:
    """Invalid targets and whether the body contains a verifiable citation."""

    errors: list[tuple[str, str, str]] = field(default_factory=list)
    has_body_citation: bool = False


def check_citations(markdown: str, allowed_urls: set[str], evidence_ids: set[str]) -> CitationCheck:
    """Check rendered targets, IDs and body presence using shared normalization."""
    # Lazy import keeps existing helper compatibility without a module cycle.
    from .orchestrator import (
        _LOCAL_DOCUMENT_LINK_RE,
        _URL_RE,
        _markdown_regions,
        _prose_without_sources,
        _reference_identity,
    )

    result = CitationCheck()
    allowed = {_reference_identity(url) for url in allowed_urls} - {""}
    for code, region in _markdown_regions(markdown):
        for target in [*_URL_RE.findall(region), *_LOCAL_DOCUMENT_LINK_RE.findall(region)]:
            if _reference_identity(target) not in allowed:
                result.errors.append(("source_not_allowlisted", target, "fenced_code" if code else "prose"))
    body = _prose_without_sources(markdown)
    # Literal inline code cannot supply a citation or masquerade as a marker.
    body = re.sub(r"(`+).*?\1", "", body)
    prose = "".join(region for code, region in _markdown_regions(markdown) if not code)
    try:
        catalogue = numbered_source_urls(prose)
    except ValueError:
        result.errors.append(("ambiguous_citation_number", "", "prose"))
        catalogue = {}
    for target in [*_URL_RE.findall(body), *_LOCAL_DOCUMENT_LINK_RE.findall(body)]:
        if _reference_identity(target) in allowed:
            result.has_body_citation = True
    for marker in re.findall(r"\[(\d+)\](?!\()", body):
        target = catalogue.get(str(int(marker)), "")
        if target and _reference_identity(target) in allowed:
            result.has_body_citation = True
        else:
            result.errors.append(("unresolved_citation", f"[{marker}]", "prose"))
    for marker in re.findall(r"\[([A-Za-z][A-Za-z0-9_.:-]+)\](?!\()", body):
        if marker in evidence_ids:
            result.has_body_citation = True
        elif marker.startswith(("EV-", "evidence-", "ev_")):
            result.errors.append(("unknown_evidence_id", marker, "prose"))
    return result

r"""Reference rendering in selectable styles, plus best-effort source extraction.

Today the search tools flatten ``{url, title}`` into text before it reaches state
(see ``tools/utils.py``), so structured sources are recovered by regex-parsing
the patterns the compression/final prompts already instruct the model to emit:
``[Title](URL)``, ``[n] Title: URL`` list lines, ``--- SOURCE n: Title ---\\nURL: ...``
blocks, and bare URLs. This is intentionally non-invasive (no tool-layer change)
and degrades gracefully when no sources are found.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Sequence
from urllib.parse import parse_qsl, urlparse

from .models import SourceRef
from .profiles import ReferenceStyle

# [Title](URL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+|/documents/[^\s\)]+)\)")
# "[1] Title: URL" or "Title: URL" list lines (the ### Sources format prompts emit)
_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:\[\d+\]\s*)?(?P<title>[^:\[\]\n]+?)\s*:\s*(?P<url>https?://\S+)\s*$",
    re.MULTILINE,
)
# "--- SOURCE n: Title ---\nURL: ..." blocks baked into findings by the search tools
_SOURCE_BLOCK_RE = re.compile(
    r"---\s*SOURCE\s*\d+\s*:\s*(?P<title>.+?)\s*---\s*\n\s*URL:\s*(?P<url>\S+)",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(r"https?://[^\s\)\]\.]+(?:\.[^\s\)\]\.]+)*")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_NUMBERED_SOURCE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(?P<number>\d+)\]:?[^\n]*?"
    r"(?P<url>https?://[^\s)\]>]+|/documents/[^\s)\]>]+)",
    re.MULTILINE,
)


def numbered_source_urls(markdown: str) -> dict[str, str]:
    """Recover explicit citation numbers without relying on source-list order."""
    sources: dict[str, str] = {}
    for match in _NUMBERED_SOURCE_RE.finditer(markdown):
        number = str(int(match.group("number")))
        url = _safe_reference_url(match.group("url"))
        if number in sources and sources[number] != url:
            raise ValueError("report_ambiguous_citation_number")
        sources[number] = url
    return sources


def _safe_reference_url(value: str) -> str:
    candidate = (value or "").strip().rstrip(".,;)")
    if (
        not candidate
        or len(candidate) > 4096
        or any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or unicodedata.category(char) == "Cc"
            for char in candidate
        )
    ):
        return ""
    if candidate.startswith("/documents/"):
        parsed = urlparse(candidate)
        segments = [part for part in parsed.path.split("/") if part]
        if parsed.netloc or parsed.scheme:
            return ""
        if len(segments) == 2:
            if not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in segments):
                return ""
        elif len(segments) == 4 and segments[2] == "chunks":
            if not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in (segments[1], segments[3])):
                return ""
            if parsed.query:
                return ""
        else:
            return ""
        query = parse_qsl(parsed.query, keep_blank_values=False)
        if len(segments) == 2 and query and (
            len(query) != 1
            or query[0][0] != "chunk"
            or not re.fullmatch(r"[A-Za-z0-9_-]+", query[0][1])
        ):
            return ""
        return candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return candidate


def _safe_reference_title(value: str) -> str:
    return _HTML_TAG_RE.sub("", value or "").replace("\n", " ").strip()[:300]


def parse_sources_from_text(text: str) -> List[SourceRef]:
    """Best-effort extraction of :class:`SourceRef` from findings/report markdown.

    Deduplicates by URL, preserving first-seen order. Source-block titles
    (richest) win over markdown-link titles.
    """
    if not text:
        return []
    seen: dict[str, SourceRef] = {}

    def _add(title: str, url: str) -> None:
        url = _safe_reference_url(url)
        if url and url not in seen:
            seen[url] = SourceRef(title=_safe_reference_title(title), url=url)

    for m in _SOURCE_BLOCK_RE.finditer(text):
        _add(m.group("title"), m.group("url"))
    for m in _MD_LINK_RE.finditer(text):
        _add(m.group(1), m.group(2))
    for m in _SOURCE_LINE_RE.finditer(text):
        _add(m.group("title"), m.group("url"))
    for url in _BARE_URL_RE.findall(text):
        _add("", url)
    return list(seen.values())


def dedupe_sources(sources: Sequence) -> List[SourceRef]:
    """Normalize a mixed sequence (SourceRef / dict / str) into deduped SourceRefs."""
    seen: dict[str, SourceRef] = {}
    for s in sources or []:
        if isinstance(s, SourceRef):
            ref = SourceRef(
                title=_safe_reference_title(s.title),
                url=_safe_reference_url(s.url),
                source_type=s.source_type,
                document_id=s.document_id,
                chunk_id=s.chunk_id,
                locator=s.locator,
            )
        elif isinstance(s, dict):
            ref = SourceRef(
                title=_safe_reference_title(s.get("title", "") or ""),
                url=_safe_reference_url(
                    s.get("url") or s.get("source_uri") or s.get("source_url") or ""
                ),
                source_type=s.get("source_type"),
                document_id=s.get("document_id"),
                chunk_id=s.get("chunk_id"),
                locator=s.get("locator"),
            )
        else:
            ref = SourceRef(url=_safe_reference_url(str(s)))
        if ref.url and ref.url not in seen:
            seen[ref.url] = ref
    return list(seen.values())


def render_references(sources: Sequence, style: ReferenceStyle) -> str:
    """Render a Sources block in the given style. Empty string when no sources."""
    refs = dedupe_sources(sources or [])
    if not refs:
        return ""
    if style == ReferenceStyle.BIBTEX_LIKE:
        return "\n\n".join(_to_bibtex(r, i) for i, r in enumerate(refs, 1))
    # default: numbered
    return "\n".join(
        f"[{i}] {r.title or 'Source'}: {r.url}" for i, r in enumerate(refs, 1)
    )


def _to_bibtex(ref: SourceRef, idx: int) -> str:
    title = ref.title or "Untitled"
    return (
        f"@misc{{ref{idx},\n"
        f"  title = {{{title}}},\n"
        f"  howpublished = {{{ref.url}}},\n"
        f"}}"
    )


# Matches an existing "### Sources" section up to the next heading or end of text.
_SOURCES_SECTION_RE = re.compile(
    r"\n*^#{1,6}\s*(?:Sources|References|来源|参考资料)\s*\n.*?(?=^#{1,6}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def replace_sources_section(markdown: str, references_block: str) -> str:
    """Replace any existing ``### Sources`` section with ``references_block``, or append it."""
    if not references_block:
        return markdown
    block = f"\n\n### Sources\n{references_block}"
    if _SOURCES_SECTION_RE.search(markdown):
        return _SOURCES_SECTION_RE.sub(block, markdown).rstrip() + "\n"
    return markdown.rstrip() + "\n" + block

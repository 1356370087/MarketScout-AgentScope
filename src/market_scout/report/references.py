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
from itertools import chain
from typing import List, Sequence
from urllib.parse import parse_qsl, urlparse

from .models import SourceRef
from .profiles import ReferenceStyle

# CommonMark source labels may contain escaped punctuation. Angle-delimited
# destinations also preserve parentheses in URLs.
_LABEL_ESCAPE_RE = re.compile(r"([\\`*{}\[\]()<>#+\-.!_|>])")
_LINK = (r"\[(?P<title>(?:\\.|[^\\\]\n])+)\]\("
         r"(?:<(?P<delimited>(?:https?://|/documents/)[^<>\n]+)>|(?P<plain>(?:https?://|/documents/)[^\s)]+))\)")
_MD_LINK_RE = re.compile(_LINK)
_NUMBERED_LINK_RE = re.compile(r"^[ \t]*\[(?P<number>\d+)\][ \t]+" + _LINK + r"[ \t]*$", re.MULTILINE)
# "[1] Title: URL" or "Title: URL" list lines (the ### Sources format prompts emit)
_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:\[\d+\]\s*)?(?P<title>[^:\[\]\n]+?)\s*:\s*<?(?P<url>https?://[^<>\s]+)>?\s*$",
    re.MULTILINE,
)
# "--- SOURCE n: Title ---\nURL: ..." blocks baked into findings by the search tools
_SOURCE_BLOCK_RE = re.compile(
    r"---\s*SOURCE\s*\d+\s*:\s*(?P<title>.+?)\s*---\s*\n\s*URL:\s*(?P<url>\S+)",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(r"https?://[^\s\)\]<>\.]+(?:\.[^\s\)\]<>\.]+)*")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_NUMBERED_SOURCE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\[(?P<number>\d+)\]:?[^\n]*?"
    r"(?P<url>https?://[^\s)\]>]+|/documents/[^\s)\]>]+)",
    re.MULTILINE,
)
# Older numbered catalogues used the fetch URL as its own missing title.
# Match only the repeated identical URL, preserving real path punctuation.
_URL_AS_TITLE_RE = re.compile(
    r"^([ \t]*(?:[-*][ \t]+)?\[\d+\][ \t]+)(https?://\S+):[ \t]+\2[ \t]*$", re.MULTILINE
)


def _ordered_source_entries(markdown: str):
    """Use CommonMark's displayed list numbers only inside an actual source catalogue."""
    if not re.search(r"(?m)^ {0,3}\d{1,9}[.)][ \t]+", markdown):
        return
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark")
    for start, end in source_sections(markdown):
        number = None
        for token in parser.parse(markdown[start:end]):
            if token.type == "ordered_list_open" and token.level == 0:
                number = int(token.attrGet("start") or 1)
            elif token.type == "ordered_list_close" and token.level == 0:
                number = None
            elif number is not None and token.type == "list_item_close" and token.level == 1:
                number += 1
            elif number is not None and token.type == "inline" and token.level == 3:
                links = [child.attrGet("href") for child in (token.children or []) if child.type == "link_open"]
                targets = links or [ref.url for ref in parse_sources_from_text(token.content)]
                for target in targets:
                    if target:
                        yield str(number), target


def numbered_source_urls(markdown: str) -> dict[str, str]:
    """Recover explicit markers and numbered catalogue entries before reordering sources."""
    sources: dict[str, str] = {}
    linked = ((str(int(m["number"])), m["delimited"] or m["plain"]) for m in _NUMBERED_LINK_RE.finditer(markdown))
    remaining = _NUMBERED_LINK_RE.sub("", markdown)
    numbered = _NUMBERED_SOURCE_RE.finditer(_URL_AS_TITLE_RE.sub(r"\1Source: \2", remaining))
    explicit = ((str(int(match["number"])), match["url"]) for match in numbered)
    for number, target in chain(linked, explicit, _ordered_source_entries(markdown)):
        url = _safe_reference_url(target)
        if number in sources and sources[number] != url:
            raise ValueError("report_ambiguous_citation_number")
        sources[number] = url
    return sources


def _safe_reference_url(value: str) -> str:
    candidate = (value or "").strip().rstrip(".,;")
    if candidate.endswith(")"):
        unmatched = max(0, candidate.count(")") - candidate.count("("))
        candidate = candidate[:max(len(candidate.rstrip(")")), len(candidate) - unmatched)]
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
    text = _URL_AS_TITLE_RE.sub(r"\1Source: \2", text)
    seen: dict[str, SourceRef] = {}

    def _add(title: str, url: str) -> None:
        url = _safe_reference_url(url)
        if url and url not in seen:
            seen[url] = SourceRef(title=_safe_reference_title(title), url=url)

    for m in _SOURCE_BLOCK_RE.finditer(text):
        _add(m.group("title"), m.group("url"))
    for m in _MD_LINK_RE.finditer(text):
        _add(re.sub(r"\\([^\w\s])", r"\1", m["title"]), m["delimited"] or m["plain"])
    for m in _SOURCE_LINE_RE.finditer(text):
        _add(m.group("title"), m.group("url"))
    for url in _BARE_URL_RE.findall(_MD_LINK_RE.sub("", text)):
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
    lines = []
    for i, ref in enumerate(refs, 1):
        title = ref.title if ref.title and ref.title != ref.url else "Source"
        label = _LABEL_ESCAPE_RE.sub(r"\\\1", title)
        destination = ref.url.replace("\\", "%5C").replace("<", "%3C").replace(">", "%3E")
        lines.append(f"[{i}] [{label}](<{destination}>)")
    return "\n".join(lines)


def normalize_source_counts(markdown: str, sources: Sequence, source_plan: dict) -> str:
    """Replace entity-attributed page counts using the actual reference catalogue.

    Only explicit source-count phrases are changed; product metrics, versions
    and source URLs are not interpreted as counts. The caller preserves code.
    """
    from market_scout.evidence import source_plan_entry_urls
    refs = dedupe_sources(sources)
    for entry in source_plan.get("entries", []):
        name = entry.get("entity")
        if not name:
            continue
        roots = [urlparse(url) for url in source_plan_entry_urls(entry)]
        count = sum(any(urlparse(ref.url).hostname == root.hostname and (
            not root.path.rstrip("/") or urlparse(ref.url).path.rstrip("/") == root.path.rstrip("/")
            or urlparse(ref.url).path.startswith(root.path.rstrip("/") + "/")) for root in roots) for ref in refs)
        pattern = re.compile(r"(" + re.escape(name) + r"\s*)(\d+)(\s*(?:个|份|篇)\s*(?:不同的?|官方)?(?:页面|文档|来源))", re.I)
        markdown = pattern.sub(lambda match: match[1] + str(count) + match[3], markdown)
    return markdown


def _to_bibtex(ref: SourceRef, idx: int) -> str:
    title = ref.title or "Untitled"
    return (
        f"@misc{{ref{idx},\n"
        f"  title = {{{title}}},\n"
        f"  howpublished = {{{ref.url}}},\n"
        f"}}"
    )


_REFERENCE_TITLE = (r"(?:sources?|references?(?:\s+(?:and|&)\s+further\s+reading)?|"
                    r"further\s+reading|来源|出处|参考(?:资料|文献|来源)(?:与延伸阅读)?)")


def is_sources_heading(value: str) -> bool:
    title = re.sub(r"^(?:\d+(?:\.\d+)*|[一二三四五六七八九十百]+)\s*[.)、．:：-]?\s*", "", value.strip(), count=1)
    return re.fullmatch(rf"{_REFERENCE_TITLE}(?:\s*(?:[（(]\s*{_REFERENCE_TITLE}\s*[)）]|[/／]\s*{_REFERENCE_TITLE}))?",
                        title, re.I) is not None


def source_sections(markdown: str) -> list[tuple[int, int]]:
    """Find actual top-level Markdown headings, preserving fenced examples and other sections."""
    from markdown_it import MarkdownIt
    offsets = [0]
    for line in markdown.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").parse(markdown)
    headings = [(offsets[t.map[0]], is_sources_heading("".join(
        child.content for child in (tokens[i+1].children or []) if child.type in {"text", "code_inline"})))
        for i, t in enumerate(tokens) if t.type == "heading_open" and t.level == 0 and t.map]
    return [(start, headings[i+1][0] if i+1 < len(headings) else len(markdown))
            for i, (start, reference) in enumerate(headings) if reference]


def without_source_sections(markdown: str) -> str:
    parts, pos = [], 0
    for start, end in source_sections(markdown):
        parts.append(markdown[pos:start])
        pos = end
    return "".join([*parts, markdown[pos:]])


def generated_catalogue_conflict(draft: str, writer_text: str) -> bool:
    """Recognize the old assembler's extra catalogue, never resolve an ambiguous writer citation by guessing."""
    sections = source_sections(draft)
    if (len(sections) != 2 or len(source_sections(writer_text)) != 1
            or sections[-1][1] != len(draft) or not draft[sections[-1][0]:].startswith("### Sources\n")):
        return False
    split = sections[-1][0]
    try:
        authored = numbered_source_urls(writer_text)
        before = numbered_source_urls(draft[:split])
        after = numbered_source_urls(draft[split:])
    except ValueError:
        return False
    return bool(authored and authored == before and any(n in after and after[n] != url for n, url in before.items()))


def replace_sources_section(markdown: str, references_block: str) -> str:
    """Replace any existing ``### Sources`` section with ``references_block``, or append it."""
    if not references_block:
        return markdown
    block = f"\n\n### Sources\n{references_block}"
    if source_sections(markdown):
        return without_source_sections(markdown).rstrip() + block + "\n"
    return markdown.rstrip() + "\n" + block

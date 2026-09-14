"""Build the canonical report model from final, sanitized Markdown."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from markdown_it import MarkdownIt

from .models import (
    CanonicalReport,
    CanonicalSection,
    CodeBlock,
    InlineRun,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    ReportBlock,
    SourceRef,
    TableBlock,
)
from .references import _safe_reference_url, dedupe_sources


@dataclass(frozen=True, slots=True)
class CanonicalizationLimits:
    """Deterministic resource bounds for Markdown canonicalization."""

    max_chars: int = 500_000
    max_sections: int = 100
    max_blocks: int = 2_000
    max_table_cells: int = 10_000
    max_sources: int = 200


_ACTIVE_HTML_RE = re.compile(
    r"<\s*(?:script|style|iframe|object|embed|form|svg)\b[^>]*>.*?"
    r"<\s*/\s*(?:script|style|iframe|object|embed|form|svg)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>|<!--.*?-->", re.DOTALL)
_SOURCE_HEADING_PREFIX_RE = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*)|[一二三四五六七八九十百]+)\s*[.)、．:：-]?\s*",
    re.IGNORECASE,
)
_SOURCE_HEADING_RE = re.compile(
    r"(?:sources?|references?(?:\s+(?:and|&)\s+further\s+reading)?|"
    r"further\s+reading|来源|参考(?:资料|文献)(?:与延伸阅读)?)",
    re.IGNORECASE,
)


def _strip_controls(value: str, *, keep_newlines: bool = True) -> str:
    """Remove control characters that office/PDF writers cannot safely store."""
    allowed = {"\n", "\r", "\t"} if keep_newlines else set()
    return "".join(
        char
        for char in str(value)
        if char in allowed
        or (
            char >= " "
            and not 0x7F <= ord(char) <= 0x9F
            and unicodedata.category(char) != "Cc"
        )
    )


def _safe_href(value: str, allowed_urls: set[str]) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 4096 or any(
        ord(char) < 32 or ord(char) == 127 for char in candidate
    ):
        return None
    if candidate.startswith("#"):
        return candidate[:300]
    if candidate.startswith("/documents/"):
        candidate_base = candidate.split("#", 1)[0]
        if _safe_reference_url(candidate_base) != candidate_base:
            return None
        allowed_bases = {url.split("#", 1)[0] for url in allowed_urls}
        return candidate if candidate_base in allowed_bases else None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    # A citation may point at a fragment within an allowlisted source page;
    # the fragment never changes the network destination.
    candidate_base = parsed._replace(fragment="").geturl()
    for allowed in allowed_urls:
        try:
            allowed_parsed = urlsplit(allowed)
        except ValueError:
            continue
        if allowed_parsed._replace(fragment="").geturl() == candidate_base:
            return candidate
    return None


def _is_sources_heading(value: str) -> bool:
    """Recognize common numbered and extended source-section headings."""
    normalized = _SOURCE_HEADING_PREFIX_RE.sub("", value.strip(), count=1)
    return _SOURCE_HEADING_RE.fullmatch(normalized) is not None


def _inline_runs(node: Tag, allowed_urls: set[str]) -> list[InlineRun]:
    runs: list[InlineRun] = []

    def append_text(
        text: str,
        *,
        bold: bool,
        italic: bool,
        code: bool,
        href: str | None,
    ) -> None:
        text = _strip_controls(text)
        if not text:
            return
        if runs and (
            runs[-1].bold,
            runs[-1].italic,
            runs[-1].code,
            runs[-1].href,
        ) == (bold, italic, code, href):
            runs[-1].text += text
            return
        runs.append(
            InlineRun(
                text=text,
                bold=bold,
                italic=italic,
                code=code,
                href=href,
            )
        )

    def visit(
        current: Tag | NavigableString,
        *,
        bold: bool = False,
        italic: bool = False,
        code: bool = False,
        href: str | None = None,
    ) -> None:
        if isinstance(current, NavigableString):
            text = str(current)
            if not code:
                text = _ACTIVE_HTML_RE.sub("", text)
                text = _HTML_TAG_RE.sub("", text)
            append_text(
                text,
                bold=bold,
                italic=italic,
                code=code,
                href=href,
            )
            return
        name = (current.name or "").lower()
        if name == "br":
            append_text("\n", bold=bold, italic=italic, code=code, href=href)
            return
        if name == "img":
            append_text(
                str(current.get("alt") or ""),
                bold=bold,
                italic=italic,
                code=code,
                href=None,
            )
            return
        child_href = (
            _safe_href(str(current.get("href") or ""), allowed_urls)
            if name == "a"
            else href
        )
        for child in current.children:
            if not isinstance(child, Tag | NavigableString):
                continue
            visit(
                child,
                bold=bold or name in {"strong", "b"},
                italic=italic or name in {"em", "i"},
                code=code or name == "code",
                href=child_href,
            )

    visit(node)
    filtered = [run for run in runs if run.text]
    if filtered:
        # Markdown-it wraps blockquote and multi-line list content in layout
        # newlines.  Keep intentional internal breaks but remove formatting
        # whitespace at block boundaries.
        filtered[0].text = filtered[0].text.lstrip()
        filtered[-1].text = filtered[-1].text.rstrip()
    return [run for run in filtered if run.text]


def _top_level_li_items(node: Tag, allowed_urls: set[str]) -> list[list[InlineRun]]:
    """Return list items in document order without duplicating nested text.

    ``ListBlock`` is intentionally flat, so nested Markdown lists are flattened
    in preorder.  Removing each item's child lists before extracting its own
    runs preserves every item exactly once instead of dropping nested items.
    """
    items: list[list[InlineRun]] = []
    for item in node.find_all("li"):
        clone = BeautifulSoup(str(item), "html.parser").find("li")
        if clone is None:
            continue
        for nested in clone.find_all(["ul", "ol"]):
            nested.decompose()
        runs = _inline_runs(clone, allowed_urls)
        if runs:
            items.append(runs)
    return items


def _table_block(node: Tag, allowed_urls: set[str]) -> TableBlock:
    headers: list[list[InlineRun]] = []
    rows: list[list[list[InlineRun]]] = []
    header_row = node.find("thead")
    if header_row:
        first = header_row.find("tr")
        if first:
            headers = [
                _inline_runs(cell, allowed_urls)
                for cell in first.find_all(["th", "td"], recursive=False)
            ]
    body = node.find("tbody")
    row_nodes = body.find_all("tr", recursive=False) if body else node.find_all("tr")
    for row in row_nodes:
        cells = [
            _inline_runs(cell, allowed_urls)
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        # When Markdown-it emits a separate ``tbody`` every row belongs to the
        # body, even if its values happen to equal the header row.  The
        # equality guard is only needed for parsers that expose one combined
        # ``tr`` sequence without a tbody.
        if cells and (body is not None or cells != headers):
            rows.append(cells)
    return TableBlock(headers=headers, rows=rows)


def _block_from_tag(node: Tag, allowed_urls: set[str]) -> ReportBlock | None:
    name = (node.name or "").lower()
    if name == "p":
        runs = _inline_runs(node, allowed_urls)
        return ParagraphBlock(runs=runs) if runs else None
    if name in {"ul", "ol"}:
        items = _top_level_li_items(node, allowed_urls)
        if not items:
            return None
        try:
            start = int(str(node.get("start") or 1))
        except (TypeError, ValueError):
            start = 1
        return ListBlock(ordered=name == "ol", start=max(1, start), items=items)
    if name == "table":
        return _table_block(node, allowed_urls)
    if name == "blockquote":
        runs = _inline_runs(node, allowed_urls)
        return QuoteBlock(runs=runs) if runs else None
    if name == "pre":
        code = node.find("code")
        classes: list[str] = [str(value) for value in (code.get("class") or [])] if code else []
        language = next(
            (
                str(value).removeprefix("language-")
                for value in classes
                if str(value).startswith("language-")
            ),
            None,
        )
        text = _strip_controls((code or node).get_text())
        return CodeBlock(text=text, language=language) if text else None
    return None


def _plain_text(runs: Iterable[InlineRun]) -> str:
    return "".join(run.text for run in runs).strip()


def canonicalize_report(
    markdown: str,
    *,
    run_id: str,
    report_type: str = "default",
    completion_status: str = "success",
    locale: str = "zh-CN",
    sources: Iterable[SourceRef | dict | str] = (),
    fallback_title: str = "Research Report",
    limits: CanonicalizationLimits | None = None,
) -> CanonicalReport:
    """Parse final Markdown into a bounded, publisher-safe document model."""
    resolved_limits = limits or CanonicalizationLimits()
    if len(markdown) > resolved_limits.max_chars:
        raise ValueError("canonical_report_input_too_large")
    resolved_sources = dedupe_sources(list(sources))
    if len(resolved_sources) > resolved_limits.max_sources:
        raise ValueError("canonical_report_source_limit_exceeded")
    allowed_urls = {source.url for source in resolved_sources}
    parser = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(
        "table"
    )
    # Markdown-it is configured with ``html=False``.  It escapes raw HTML, so
    # sanitization is performed on non-code text nodes in ``_inline_runs``;
    # parsing the source untouched is what preserves fenced code verbatim.
    parse_source = markdown
    html = parser.render(parse_source)
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    summary_blocks: list[ReportBlock] = []
    sections: list[CanonicalSection] = []
    current: CanonicalSection | None = None
    in_sources_section = False
    block_count = 0
    table_cell_count = 0

    for node in soup.children:
        if not isinstance(node, Tag):
            continue
        heading = re.fullmatch(r"h([1-6])", node.name or "")
        if heading:
            heading_title = _strip_controls(
                _HTML_TAG_RE.sub(
                    "",
                    _ACTIVE_HTML_RE.sub("", node.get_text(" ", strip=True)),
                ),
                keep_newlines=False,
            )
            raw_level = int(heading.group(1))
            if raw_level == 1 and not title:
                title = heading_title
                continue
            if _is_sources_heading(heading_title):
                current = None
                in_sources_section = True
                continue
            in_sources_section = False
            if len(sections) >= resolved_limits.max_sections:
                raise ValueError("canonical_report_section_limit_exceeded")
            level = min(6, max(2, raw_level if raw_level > 1 else 2))
            current = CanonicalSection(
                id=f"section-{len(sections) + 1}",
                title=heading_title or f"Section {len(sections) + 1}",
                level=level,
            )
            sections.append(current)
            continue

        block = _block_from_tag(node, allowed_urls)
        if block is None:
            continue
        if in_sources_section:
            continue
        block_count += 1
        if block_count > resolved_limits.max_blocks:
            raise ValueError("canonical_report_block_limit_exceeded")
        if isinstance(block, TableBlock):
            table_cell_count += len(block.headers) + sum(len(row) for row in block.rows)
            if table_cell_count > resolved_limits.max_table_cells:
                raise ValueError("canonical_report_table_limit_exceeded")
        if current is None:
            summary_blocks.append(block)
        else:
            current.blocks.append(block)

    if not title:
        title = fallback_title.strip() or "Research Report"
    title = _HTML_TAG_RE.sub("", _ACTIVE_HTML_RE.sub("", title))
    title = re.sub(r"\s+", " ", _strip_controls(title, keep_newlines=False)).strip()[:500]
    normalized_completion: Literal["success", "partial"] = (
        "partial" if completion_status == "partial" else "success"
    )
    normalized_locale: Literal["zh-CN", "en-US"] = (
        "en-US" if locale == "en-US" else "zh-CN"
    )
    return CanonicalReport(
        run_id=run_id,
        report_type=report_type or "default",
        title=title,
        completion_status=normalized_completion,
        locale=normalized_locale,
        summary_blocks=summary_blocks,
        sections=sections,
        sources=resolved_sources,
        source_markdown_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def runs_to_plain_text(runs: Iterable[InlineRun]) -> str:
    """Return human-readable plain text for one sequence of inline runs."""
    return _plain_text(runs)


def validate_canonical_report(
    report: CanonicalReport,
    *,
    limits: CanonicalizationLimits | None = None,
) -> CanonicalReport:
    """Validate bounds and link safety on a loaded canonical report bundle.

    A persisted JSON bundle is an input boundary for the isolated publisher
    worker.  Rechecking it keeps hand-edited or partially corrupted files from
    bypassing the limits enforced by :func:`canonicalize_report`.
    """
    resolved_limits = limits or CanonicalizationLimits()
    def check_text(
        value: str,
        field_name: str,
        *,
        max_length: int | None = None,
        keep_layout: bool = True,
    ) -> None:
        if max_length is not None and len(value) > max_length:
            raise ValueError(f"canonical_report_{field_name}_too_large")
        allowed_controls = {"\n", "\r", "\t"} if keep_layout else set()
        if any(
            char not in allowed_controls
            and (
                ord(char) < 32
                or 0x7F <= ord(char) <= 0x9F
                or unicodedata.category(char) == "Cc"
            )
            for char in value
        ):
            raise ValueError("canonical_report_control_character")

    check_text(report.run_id, "run_id", max_length=128, keep_layout=False)
    check_text(report.report_type, "report_type", max_length=120, keep_layout=False)
    check_text(report.title, "title", max_length=500, keep_layout=False)
    if len(report.sections) > resolved_limits.max_sections:
        raise ValueError("canonical_report_section_limit_exceeded")
    if len(report.sources) > resolved_limits.max_sources:
        raise ValueError("canonical_report_source_limit_exceeded")
    allowed_urls = {source.url for source in report.sources}
    for source in report.sources:
        check_text(source.title, "source_title", max_length=300, keep_layout=False)
        check_text(source.url, "source_url", max_length=4096, keep_layout=False)
        for metadata in (
            source.source_type,
            source.document_id,
            source.chunk_id,
            source.locator,
        ):
            if metadata is not None:
                check_text(
                    str(metadata),
                    "source_metadata",
                    max_length=500,
                    keep_layout=False,
                )
        if (
            not source.url
            or source.url.startswith("#")
            or _safe_href(source.url, {source.url}) is None
        ):
            raise ValueError("canonical_report_source_invalid")
    block_count = 0
    table_cell_count = 0
    # ``max_chars`` is the source Markdown/body budget.  Run metadata, the
    # generated section IDs, source metadata and URL targets have independent
    # bounds and must not make a report that was accepted at the Markdown
    # boundary fail validation after parsing.
    body_char_count = 0

    def visit_runs(runs: Iterable[InlineRun]) -> None:
        nonlocal body_char_count
        for run in runs:
            check_text(run.text, "inline_text")
            body_char_count += len(run.text)
            if run.href is not None:
                check_text(run.href, "href", max_length=4096, keep_layout=False)
                if _safe_href(run.href, allowed_urls) is None:
                    raise ValueError("canonical_report_link_not_allowed")

    def visit_block(block: ReportBlock) -> None:
        nonlocal block_count, table_cell_count, body_char_count
        block_count += 1
        if isinstance(block, ParagraphBlock | QuoteBlock):
            visit_runs(block.runs)
        elif isinstance(block, ListBlock):
            for item in block.items:
                visit_runs(item)
        elif isinstance(block, TableBlock):
            table_cell_count += len(block.headers)
            table_cell_count += sum(len(row) for row in block.rows)
            for cell in block.headers:
                visit_runs(cell)
            for row in block.rows:
                for cell in row:
                    visit_runs(cell)
        elif isinstance(block, CodeBlock):
            check_text(block.text, "code_text")
            if block.language is not None:
                check_text(
                    block.language,
                    "code_language",
                    max_length=80,
                    keep_layout=False,
                )
                body_char_count += len(block.language)
            body_char_count += len(block.text)

    for block in report.summary_blocks:
        visit_block(block)
    for section in report.sections:
        check_text(section.id, "section_id", max_length=128, keep_layout=False)
        check_text(section.title, "section_title", max_length=500, keep_layout=False)
        body_char_count += len(section.title)
        for block in section.blocks:
            visit_block(block)
    if block_count > resolved_limits.max_blocks:
        raise ValueError("canonical_report_block_limit_exceeded")
    if table_cell_count > resolved_limits.max_table_cells:
        raise ValueError("canonical_report_table_limit_exceeded")
    if body_char_count > resolved_limits.max_chars:
        raise ValueError("canonical_report_input_too_large")
    return report


build_canonical_report = canonicalize_report

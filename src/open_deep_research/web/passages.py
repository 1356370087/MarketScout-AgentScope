"""Source passages retain parameter definitions, tables and code as evidence.

All returned text comes from the fetched document. These helpers never infer
facts or treat a translated model paraphrase as the authoritative source.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from open_deep_research.security.content import inspect_untrusted_content

MAX_PASSAGE_CHARS = 2400
_END = re.compile(r"[。！？.!?](?:[\"'”’)\]}`*_]+)?$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_CALLOUT_HEADING = re.compile(r"^#{1,6}\s+(?:note|warning|caution|tip|important|注意|警告|提示|说明|重要)\s*[:：]?\s*$", re.IGNORECASE)


def blocks(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield source offsets without splitting fenced code at blank lines."""
    start, offset = 0, 0
    fence = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
        if not line.strip() and fence is None:
            if text[start:offset].strip():
                yield start, offset, text[start:offset].strip()
            start = offset + len(line)
        offset += len(line)
    if text[start:].strip():
        yield start, len(text), text[start:].strip()


def is_table(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or not _TABLE_SEPARATOR.match(lines[1]):
        return False
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    # Navigation tables have empty headings and only Prev/Up/Next links.
    return any(re.search(r"\w", cell) for cell in header) and any(
        len(line.strip("|").split("|")) == len(header) for line in lines[2:]
    )


def is_code(text: str) -> bool:
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return False
    first, last = _FENCE.match(lines[0]), _FENCE.match(lines[-1])
    return bool(first and last and first[1][0] == last[1][0] and any(line.strip() for line in lines[1:-1]))


def valid_passage(text: str) -> bool:
    """Validate a whole factual passage, not its lexical similarity to a claim."""
    value = text.strip()
    if not 20 <= len(value) <= MAX_PASSAGE_CHARS or inspect_untrusted_content(value):
        return False
    # A governing heading is context, not a fact by itself. It may accompany
    # a complete sourced paragraph, table or code example.
    value = re.sub(r"^#{1,6}\s+[^\n]+\n?", "", value, flags=re.MULTILINE).strip()
    if is_table(value) or is_code(value):
        return True
    if value.startswith("#") or not _END.search(value):
        return False
    # A standalone link, breadcrumb or version menu is not a factual passage.
    visible = re.sub(r"\[[^\]]*\]\([^)]*\)", "", value)
    visible = re.sub(r"[`*_#|→\s]", "", visible)
    return len(visible) >= 20 and not re.match(r"^(?:Supported|Development|Unsupported) Versions:", value)


def passages(text: str) -> list[str]:
    """Keep complete blocks; split long prose at sentence boundaries only."""
    result = []
    for _, _, block in blocks(text):
        if valid_passage(block):
            result.append(block)
            continue
        if len(block) <= MAX_PASSAGE_CHARS or is_table(block) or is_code(block):
            continue
        # Mask Markdown links and inline code for boundary detection, retaining
        # original offsets so periods in URLs/options cannot truncate evidence.
        masked = re.sub(r"\[[^\]]*\]\([^)]*\)|`[^`]*`", lambda m: "x" * len(m[0]), block)
        start = 0
        for boundary in re.finditer(r"[。！？.!?](?:[\"'”’)\]]*)\s+|$", masked):
            end = boundary.end()
            candidate = block[start:end].strip()
            if valid_passage(candidate):
                result.append(candidate)
            start = end
    blocked: set[int] = set()
    for index in range(len(result) - 1):
        if inspect_untrusted_content(result[index] + " " + result[index + 1]):
            blocked.update((index, index + 1))
    return list(dict.fromkeys(value for index, value in enumerate(result) if index not in blocked))


def source_excerpt(text: str, quote: str, *, span: tuple[int, int] | None = None) -> str | None:
    """Restore whole source paragraphs and nearby governing section context.

    Search only the selected chunk when a span is supplied; context may extend
    outside it, but always remains a bounded, contiguous slice of this source.
    """
    if not quote.strip() or len(quote) > MAX_PASSAGE_CHARS:
        return None
    source_blocks = list(blocks(text))
    headings = list(re.finditer(r"^#{1,6}\s+[^\n]+", text, re.MULTILINE))
    values = set()
    for match in re.finditer(r"\s+".join(re.escape(word) for word in quote.split()), text):
        if span and not (span[0] <= match.start() and match.end() <= span[1]):
            continue
        start, end = match.span()
        for a, b, block in source_blocks:
            if a <= start and end <= b and valid_passage(block):
                start, end = a, b
                break
        value = text[start:end].strip()
        # A note such as "This guarantees ..." depends on the section's
        # opening condition. Do not detach it from a strategy/version heading.
        preceding = [heading for heading in headings if heading.start() < start
                     and not _CALLOUT_HEADING.fullmatch(heading[0])]
        if preceding:
            context = text[preceding[-1].start():end].strip()
            if valid_passage(context):
                value = context
        values.add(value)
    # A generic definition shared by different parameters is ambiguous; ask
    # the extractor for a quote that includes the parameter instead of guessing.
    return values.pop() if len(values) == 1 else None

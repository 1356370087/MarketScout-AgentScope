"""HTML, text and PDF extraction plus source-located document chunks."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin

import pymupdf
from bs4 import BeautifulSoup
from markdownify import markdownify

from open_deep_research.web.fetching import RawFetch, _decode_body
from open_deep_research.web.models import (
    CandidateSource,
    DocumentChunk,
    ExtractedDocument,
)
from open_deep_research.web.settings import WebPipelineSettings
from open_deep_research.web.sources import canonicalize_url, stable_id

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
JS_SHELL_MARKERS = (
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "__next_data__",
    'id="root"',
    'id="app"',
)


def _metadata_from_html(soup: BeautifulSoup) -> dict[str, str | None]:
    def meta(*names: str) -> str | None:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            if tag and tag.get("content"):
                return str(tag.get("content")).strip()
        return None

    author = meta("author", "article:author")
    published = meta("article:published_time", "datePublished", "date")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.string or "null")
        except TypeError, json.JSONDecodeError:
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            author_obj = node.get("author")
            if not author and isinstance(author_obj, dict):
                author = str(author_obj.get("name") or "") or None
            published = published or node.get("datePublished")
    html = soup.find("html")
    return {
        "title": meta("og:title", "twitter:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "author": author,
        "published_at": str(published) if published else None,
        "language": str(html.get("lang")) if html and html.get("lang") else None,
    }


def extract_html(candidate: CandidateSource, raw: RawFetch) -> ExtractedDocument:
    """Extract main HTML content, metadata, and Markdown."""
    content_type = raw.result.content_type or "text/html"
    text = _decode_body(raw.body, None)
    soup = BeautifulSoup(text, "html.parser")
    metadata = _metadata_from_html(soup)
    for selector in (
        "script",
        "style",
        "noscript",
        "nav",
        "footer",
        "aside",
        "form",
        "iframe",
        "object",
        "embed",
    ):
        for node in soup.select(selector):
            node.decompose()
    for node in soup.select(
        "[class*='advert'],[class*='recommend'],[id*='advert'],[id*='recommend']"
    ):
        node.decompose()
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    markdown = markdownify(str(main), heading_style="ATX").strip()
    visible_chars = len(re.sub(r"\s+", "", markdown))
    flags: list[str] = []
    lowered = text.lower()
    if (
        visible_chars == 0
        or any(marker in lowered for marker in JS_SHELL_MARKERS)
        and visible_chars < 1500
    ):
        flags.append("dynamic_render_recommended")
    elif visible_chars < 600:
        flags.append("thin_content")
    canonical_tag = soup.find(
        "link", attrs={"rel": lambda value: value and "canonical" in value}
    )
    canonical = candidate.canonical_url
    if canonical_tag and canonical_tag.get("href"):
        try:
            canonical = canonicalize_url(
                urljoin(
                    raw.result.final_url or canonical, str(canonical_tag.get("href"))
                )
            )
        except ValueError:
            pass
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return ExtractedDocument(
        document_id=stable_id("doc", f"{canonical}:{digest}"),
        candidate_id=candidate.candidate_id,
        requested_url=raw.result.requested_url,
        final_url=raw.result.final_url or candidate.canonical_url,
        canonical_url=canonical,
        title=str(metadata["title"] or candidate.title),
        author=metadata["author"],
        published_at=metadata["published_at"],
        language=metadata["language"],
        content_type=content_type,
        markdown=markdown,
        extractor="beautifulsoup+markdownify",
        content_hash=digest,
        quality_flags=flags,
    )


def extract_pdf(
    candidate: CandidateSource, raw: RawFetch, max_pages: int
) -> ExtractedDocument:
    """Extract text and page locators from a non-scanned PDF."""
    flags: list[str] = []
    try:
        pdf = pymupdf.open(stream=raw.body, filetype="pdf")
        if pdf.needs_pass:
            raise ValueError("encrypted_pdf")
        page_count = min(len(pdf), max_pages)
        parts: list[str] = []
        for index in range(page_count):
            page_text = pdf[index].get_text("text").strip()
            parts.append(f"<!-- page:{index + 1} -->\n\n{page_text}")
        markdown = "\n\n".join(parts).strip()
        metadata = pdf.metadata or {}
    except Exception as exc:
        raise ValueError(f"pdf_extract_failed:{exc}") from exc
    if page_count and len(re.sub(r"\s+", "", markdown)) / page_count < 100:
        flags.append("needs_external_extraction")
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return ExtractedDocument(
        document_id=stable_id("doc", f"{candidate.canonical_url}:{digest}"),
        candidate_id=candidate.candidate_id,
        requested_url=raw.result.requested_url,
        final_url=raw.result.final_url or candidate.canonical_url,
        canonical_url=candidate.canonical_url,
        title=str(metadata.get("title") or candidate.title),
        author=str(metadata.get("author") or "") or None,
        published_at=str(metadata.get("creationDate") or "") or None,
        content_type="application/pdf",
        markdown=markdown,
        page_count=page_count,
        extractor="pymupdf",
        content_hash=digest,
        quality_flags=flags,
    )


def extract_document(
    candidate: CandidateSource, raw: RawFetch, settings: WebPipelineSettings
) -> ExtractedDocument:
    """Route a successful response to the appropriate local extractor."""
    content_type = raw.result.content_type or ""
    if content_type == "application/pdf" or raw.body.startswith(b"%PDF"):
        return extract_pdf(candidate, raw, settings.pdf_max_pages)
    if content_type in {"text/plain", "text/markdown", "application/json"}:
        return text_document(
            candidate, _decode_body(raw.body, None), raw.result.final_url, content_type
        )
    if content_type.startswith("text/") or content_type in {
        "application/xhtml+xml",
        "",
    }:
        return extract_html(candidate, raw)
    raise ValueError(f"unsupported_content_type:{content_type}")


def text_document(
    candidate: CandidateSource,
    text: str,
    final_url: str | None = None,
    content_type: str = "text/plain",
    extractor: str = "text",
) -> ExtractedDocument:
    """Keep Markdown, code samples and browser snapshot text verbatim."""
    canonical = canonicalize_url(final_url or candidate.canonical_url)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ExtractedDocument(
        document_id=stable_id("doc", f"{canonical}:{digest}"),
        candidate_id=candidate.candidate_id,
        requested_url=candidate.canonical_url,
        final_url=final_url or candidate.canonical_url,
        canonical_url=canonical,
        title=candidate.title,
        content_type=content_type,
        markdown=text,
        extractor=extractor,
        content_hash=digest,
    )


def needs_extraction_fallback(document: ExtractedDocument, *, evidence: bool) -> bool:
    """Render shells in either mode while allowing short documents to be read raw."""
    flags = document.quality_flags
    return (
        "dynamic_render_recommended" in flags
        or "needs_external_extraction" in flags
        or (evidence and "thin_content" in flags)
    )


def chunk_document(
    document: ExtractedDocument, settings: WebPipelineSettings
) -> list[DocumentChunk]:
    """Split Markdown into overlapping, stable chunks with page/heading locators."""
    text = document.markdown
    chunks: list[DocumentChunk] = []
    start = 0
    heading: str | None = None
    while start < len(text):
        end = min(len(text), start + settings.chunk_chars)
        if end < len(text):
            boundary = max(
                text.rfind("\n\n", start, end),
                text.rfind("。", start, end),
                text.rfind(". ", start, end),
            )
            if boundary > start + settings.chunk_chars // 2:
                end = boundary + 1
        segment = text[start:end].strip()
        heading_matches = list(HEADING_RE.finditer(text, 0, start + 1))
        if heading_matches:
            heading = heading_matches[-1].group(2).strip()
        page_matches = list(re.finditer(r"<!-- page:(\d+) -->", text[: start + 1]))
        page = int(page_matches[-1].group(1)) if page_matches else None
        if segment:
            digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()
            chunks.append(
                DocumentChunk(
                    chunk_id=stable_id(
                        "chk", f"{document.document_id}:{start}:{digest}"
                    ),
                    document_id=document.document_id,
                    heading=heading,
                    page=page,
                    start_offset=start,
                    end_offset=end,
                    text=segment,
                    content_hash=digest,
                )
            )
        if end >= len(text):
            break
        start = max(start + 1, end - settings.chunk_overlap_chars)
    return chunks

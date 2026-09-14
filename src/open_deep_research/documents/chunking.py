"""Structure-aware, stable chunking for parsed local documents."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from .parsers import ParsedDocument


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """One stable chunk ready for lexical and vector indexing."""

    id: str
    ordinal: int
    locator: str
    heading: str | None
    text: str
    content_hash: str


# Character windows approximate the P0 target of 800/120 English tokens. The
# character contract is deliberate: it remains deterministic for CJK text and
# avoids loading a model-specific tokenizer in the worker.
CHUNK_TARGET_CHARS = 3200
CHUNK_OVERLAP_CHARS = 480


def _windows(
    text: str,
    size: int = CHUNK_TARGET_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split one structural unit into bounded character windows."""
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("chunk_window_bounds_invalid")
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if len(normalized) <= size:
        return [normalized] if normalized else []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        if end < len(normalized):
            boundary = max(
                normalized.rfind("\n", start + size // 2, end),
                normalized.rfind("。", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        value = normalized[start:end].strip()
        if value:
            chunks.append(value)
        if end >= len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks


def build_chunks(document_id: str, parsed: ParsedDocument) -> list[DocumentChunk]:
    """Build deterministic chunks without crossing structural-unit boundaries."""
    chunks: list[DocumentChunk] = []
    ordinal = 0
    for unit in parsed.units:
        for part_index, text in enumerate(_windows(unit.text), 1):
            ordinal += 1
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            identity = f"{document_id}:{unit.locator}:{part_index}:{digest}"
            chunks.append(
                DocumentChunk(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                    ordinal=ordinal,
                    locator=unit.locator,
                    heading=unit.heading,
                    text=text,
                    content_hash=digest,
                )
            )
    return chunks

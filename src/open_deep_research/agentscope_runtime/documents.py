"""Native RAG interfaces over the versioned document domain (AgentScope 2.0.8)."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from agentscope.message import DataBlock, TextBlock
from agentscope.rag import Chunk, ChunkerBase, ParserBase, Section

from open_deep_research.documents.chunking import _windows
from open_deep_research.documents.parse_pipeline import parse_document_structured
from open_deep_research.documents.parsers import DocumentParseError
from open_deep_research.documents.structuring import StructuredUnit, plan_table_segments


def sections_for(prepared, filename):
    """Preserve domain unit identity and provenance through native sections."""
    return [
        Section(
            content=TextBlock(text=unit.index_text),
            source=filename,
            metadata={"unit_index": index, "domain_unit": asdict(unit)},
        )
        for index, unit in enumerate(prepared.units)
    ]


class VersionedDocumentParser(ParserBase):
    """Adapt one MIME family; job recovery context is supplied per call."""

    def __init__(self, media_type, settings, *, parse_impl=parse_document_structured):
        self.supported_media_types = [media_type]
        self.media_type, self.settings, self.parse_impl = (
            media_type,
            settings,
            parse_impl,
        )

    async def prepare(self, file, filename, **job_context):
        """Return the domain envelope without losing OCR/task/quality metadata."""
        if isinstance(file, bytes):
            with TemporaryDirectory(prefix="agentscope-document-") as folder:
                path = Path(folder) / ("upload" + Path(filename).suffix)
                path.write_bytes(file)
                return await self.parse_impl(
                    path, filename, self.media_type, self.settings, **job_context
                )
        return await self.parse_impl(
            Path(file), filename, self.media_type, self.settings, **job_context
        )

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Implement native parsing; the Worker uses prepare for its job envelope."""
        return sections_for(await self.prepare(file, filename), filename)


class VersionedDocumentChunker(ChunkerBase):
    """Keep table row groups and prose windows identical to the domain index."""

    chunker_type = "insightforge_versioned_document"

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    async def chunk(self, sections: list[Section]) -> list[Chunk]:
        chunks = []
        for section in sections:
            if isinstance(section.content, DataBlock):
                content = [section.content]
            else:
                payload = section.metadata.get("domain_unit")
                unit = StructuredUnit(**payload) if payload else None
                texts = (
                    plan_table_segments(unit, self.settings.table_row_group_size)
                    if unit and unit.unit_type == "table"
                    else _windows(section.content.text)
                )
                content = [TextBlock(text=text) for text in texts if text]
            for block in content:
                chunks.append(
                    Chunk(
                        content=block,
                        source=section.source,
                        metadata=deepcopy(section.metadata),
                        chunk_index=len(chunks),
                        total_chunks=0,
                    )
                )
        for chunk in chunks:
            chunk.total_chunks = len(chunks)
        return chunks


async def prepare_document(
    file,
    filename,
    media_type,
    settings,
    *,
    parse_impl=parse_document_structured,
    **job_context,
):
    """Worker composition: native boundaries, existing publication envelope."""
    parser = VersionedDocumentParser(media_type, settings, parse_impl=parse_impl)
    prepared = await parser.prepare(file, filename, **job_context)
    chunks = await VersionedDocumentChunker(settings).chunk(
        sections_for(prepared, filename)
    )
    if not chunks:
        raise DocumentParseError("document_no_extractable_text")
    prepared.segment_texts = [chunk.content.text for chunk in chunks]
    prepared.segment_units = [chunk.metadata["unit_index"] for chunk in chunks]
    return prepared

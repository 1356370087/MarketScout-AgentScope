"""Native document contracts preserve versioned indexing behavior."""

from copy import deepcopy

import pytest
from agentscope.message import DataBlock
from agentscope.rag import Section

from open_deep_research.agentscope_runtime.documents import (
    VersionedDocumentChunker,
    VersionedDocumentParser,
    prepare_document,
)
from open_deep_research.documents import structuring
from open_deep_research.documents.parsers import DocumentParseError
from open_deep_research.documents.settings import DocumentSettings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,media,body",
    [
        (
            "notes.md",
            "text/markdown",
            "# 一\n\n" + "研究内容。" * 1800 + "\n# 二\n\n资料来源",
        ),
        ("table.csv", "text/csv", "项目,年度,金额\n收入,2025,100\n利润,2025,20\n"),
        ("notes.txt", "text/plain", "证据内容\n" * 1000),
    ],
    ids=["markdown", "csv", "text"],
)
async def test_native_segments_match_existing_domain(filename, media, body):
    settings = DocumentSettings()
    parser = VersionedDocumentParser(media, settings)
    prepared = await parser.prepare(body.encode(), filename)
    expected = structuring.plan_segments(deepcopy(prepared), settings)
    actual = await prepare_document(body.encode(), filename, media, settings)
    assert actual == expected
    sections = await parser.parse(body.encode(), filename)
    chunks = await VersionedDocumentChunker(settings).chunk(sections)
    assert [chunk.content.text for chunk in chunks] == expected.segment_texts
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.total_chunks == len(chunks) for chunk in chunks)
    for chunk in chunks:
        assert chunk.source == filename
        assert chunk.metadata == sections[chunk.metadata["unit_index"]].metadata


@pytest.mark.asyncio
async def test_resume_context_and_envelope_survive_and_tempfile_is_cleaned():
    paths = []
    submitted = []

    async def remember(task_id):
        submitted.append(task_id)

    async def parse(path, filename, media, settings, **context):
        paths.append(path)
        assert path.read_bytes() == b"payload"
        assert context["version_id"] == "version-1"
        assert context["resume_task_id"] == "upstream-1"
        await context["on_submitted"]("upstream-1")
        return structuring.PreparedDocument(
            units=[
                structuring.StructuredUnit("paragraph", {"page": 7}, "raw", "indexed")
            ],
            page_count=7,
            ocr_pages=2,
            quality_flags=["office_preview_used"],
            parse_method="docling",
            upstream_task_id="upstream-1",
        )

    prepared = await prepare_document(
        b"payload",
        "original.pdf",
        "application/pdf",
        DocumentSettings(),
        parse_impl=parse,
        version_id="version-1",
        resume_task_id="upstream-1",
        on_submitted=remember,
    )
    assert prepared.upstream_task_id == "upstream-1" and prepared.ocr_pages == 2
    assert prepared.quality_flags == ["office_preview_used"]
    assert prepared.units[0].locator == {"page": 7}
    assert submitted == ["upstream-1"] and not paths[0].exists()


@pytest.mark.asyncio
async def test_empty_document_rejected_and_parse_failure_cleans_bytes():
    async def empty(*args, **kwargs):
        return structuring.PreparedDocument()

    with pytest.raises(DocumentParseError, match="no_extractable"):
        await prepare_document(
            b"", "empty.txt", "text/plain", DocumentSettings(), parse_impl=empty
        )
    paths = []

    async def fail(path, *args, **kwargs):
        paths.append(path)
        raise DocumentParseError("upstream_failed")

    with pytest.raises(DocumentParseError, match="upstream_failed"):
        await VersionedDocumentParser(
            "text/plain", DocumentSettings(), parse_impl=fail
        ).parse(b"x", "a.txt")
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_native_multimodal_chunk_is_not_split():
    block = DataBlock(
        source={"type": "base64", "media_type": "image/png", "data": "YWJj"}
    )
    section = Section(content=block, source="image.png", metadata={"page": 1})
    chunks = await VersionedDocumentChunker(DocumentSettings()).chunk([section])
    assert len(chunks) == 1 and chunks[0].content == block
    assert chunks[0].metadata == section.metadata


@pytest.mark.asyncio
async def test_worker_ingests_through_native_adapter(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from open_deep_research.documents import worker

    path = tmp_path / "notes.txt"
    path.write_text("可引用研究材料", encoding="utf-8")
    monkeypatch.setattr(worker, "get_document_settings", lambda: DocumentSettings())
    monkeypatch.setattr(
        worker,
        "claim_job",
        AsyncMock(return_value={"id": "job", "kind": "ingest", "document_id": "doc"}),
    )
    monkeypatch.setattr(
        worker,
        "load_job_document",
        AsyncMock(
            return_value={
                "id": "doc",
                "owner_id": "owner",
                "storage_key": "key",
                "filename": "notes.txt",
                "media_type": "text/plain",
            }
        ),
    )
    monkeypatch.setattr(
        worker.versioning,
        "latest_draft_generation",
        AsyncMock(return_value={"id": "gen", "version_id": "version"}),
    )
    monkeypatch.setattr(worker, "resolve_storage_key", lambda *args: path)
    embedding = AsyncMock(return_value=[[0.1]])
    saved = AsyncMock()
    done = AsyncMock(return_value=True)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "embed_texts", embedding)
    monkeypatch.setattr(worker, "build_suggestions", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker.versioning, "complete_generation_rich", saved)
    monkeypatch.setattr(worker, "complete_job", done)
    monkeypatch.setattr(worker, "fail_job", failed)
    assert await worker._process("worker-one")
    failed.assert_not_awaited()
    done.assert_awaited_once_with("job", "worker-one")
    prepared = saved.await_args.args[1]
    assert prepared.segment_texts == ["可引用研究材料"]
    assert prepared.segment_units == [0]
    assert embedding.await_args.args[0] == prepared.segment_texts


@pytest.mark.asyncio
async def test_credentials_context_is_task_local_and_restored(monkeypatch):
    import asyncio

    from open_deep_research.models.credentials_context import (
        bind_run_key,
        current_gateway_key,
        current_run_key,
        reset_run_key,
    )

    monkeypatch.setenv("LITELLM_SERVICE_KEY", "service-fixture")

    async def run(key):
        token = bind_run_key(key)
        try:
            await asyncio.sleep(0)
            assert current_run_key() == key and current_gateway_key() == key
        finally:
            reset_run_key(token)

    await asyncio.gather(run("one"), run("two"))
    assert current_gateway_key() == "service-fixture"
    with pytest.raises(RuntimeError, match="run_key_unavailable"):
        current_run_key()

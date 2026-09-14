"""Local-document source-selection and deterministic parsing tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from open_deep_research.documents.chunking import (
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    build_chunks,
)
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.documents.parsers import parse_document
from open_deep_research.documents.settings import DocumentSettings


def test_source_selection_defaults_to_web() -> None:
    selection = SourceSelection()
    assert selection.mode.value == "web"
    assert selection.sources == []
    assert selection.web_enabled is True
    assert selection.documents_enabled is False


def test_chunk_window_contract_matches_documented_token_approximation() -> None:
    assert CHUNK_TARGET_CHARS == 3200
    assert CHUNK_OVERLAP_CHARS == 480
    assert 0 <= CHUNK_OVERLAP_CHARS < CHUNK_TARGET_CHARS


@pytest.mark.parametrize("mode", ["documents", "hybrid"])
def test_document_modes_require_a_document(mode: str) -> None:
    with pytest.raises(ValidationError, match="document_source_required"):
        SourceSelection.model_validate({"mode": mode, "sources": []})


def test_specific_normalizes_and_deduplicates_hard_boundaries() -> None:
    selection = SourceSelection.model_validate(
        {
            "mode": "specific",
            "sources": [
                {"type": "url", "url": "HTTPS://Example.COM/report#part"},
                {"type": "domain", "domain": "Docs.Example.COM."},
                {"type": "document", "id": "38d7d636-bc49-42d5-bbc8-0e6378b388d5"},
            ],
        }
    )
    assert selection.urls == ["https://example.com/report"]
    assert selection.domains == ["docs.example.com"]
    assert selection.web_enabled and selection.documents_enabled


def test_documents_reject_web_sources() -> None:
    with pytest.raises(ValidationError, match="documents_and_hybrid_accept_document_sources_only"):
        SourceSelection.model_validate(
            {
                "mode": "documents",
                "sources": [
                    {"type": "document", "id": "doc-1"},
                    {"type": "url", "url": "https://example.com/"},
                ],
            }
        )


def test_text_parser_and_chunk_locations_are_stable(tmp_path: Path) -> None:
    path = tmp_path / "internal.md"
    path.write_text("# 市场结论\n\n" + "内部证据。" * 900, encoding="utf-8")
    settings = DocumentSettings(storage_dir=tmp_path)
    parsed = parse_document(path, "text/markdown", settings)
    first = build_chunks("3d42eb32-d63a-4102-9ff2-c84e94757808", parsed)
    second = build_chunks("3d42eb32-d63a-4102-9ff2-c84e94757808", parsed)
    assert len(first) >= 2
    assert [item.id for item in first] == [item.id for item in second]
    assert all(item.locator for item in first)

"""Researcher tool assembly tests for source-mode evidence boundaries."""

from __future__ import annotations

import pytest

from open_deep_research.tools.base import ToolOrigin
from open_deep_research.tools.governance import AgentRole
from open_deep_research.tools.registry import assemble_toolset


def _config(mode: str, sources: list[dict]) -> dict:
    return {"metadata": {"source_selection": {"mode": mode, "sources": sources}}}


@pytest.mark.asyncio
async def test_web_mode_preserves_web_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "false")
    tools = await assemble_toolset(AgentRole.RESEARCHER, _config("web", []))
    names = {tool.name for tool in tools}
    assert "web_research" in names
    assert "fetch_url" in names
    assert "search_documents" not in names


@pytest.mark.asyncio
async def test_documents_mode_exposes_no_external_evidence_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://unused/test")
    tools = await assemble_toolset(
        AgentRole.RESEARCHER,
        _config("documents", [{"type": "document", "id": "doc-1"}]),
    )
    assert {tool.name for tool in tools} == {
        "ResearchComplete",
        "think_tool",
        "search_documents",
    }
    assert not any(
        tool.origin in {ToolOrigin.SEARCH, ToolOrigin.MCP, ToolOrigin.BROWSER}
        for tool in tools
    )


@pytest.mark.asyncio
async def test_specific_mode_keeps_only_bounded_web_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "false")
    tools = await assemble_toolset(
        AgentRole.RESEARCHER,
        _config("specific", [{"type": "domain", "domain": "example.com"}]),
    )
    assert {tool.name for tool in tools} == {
        "ResearchComplete",
        "think_tool",
        "web_research",
        "fetch_url",
    }


@pytest.mark.asyncio
async def test_specific_document_only_mode_does_not_expose_web_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://unused/test")
    tools = await assemble_toolset(
        AgentRole.RESEARCHER,
        _config("specific", [{"type": "document", "id": "doc-1"}]),
    )
    assert {tool.name for tool in tools} == {
        "ResearchComplete",
        "think_tool",
        "search_documents",
    }

"""Source modes filter the actual native researcher's model-visible toolkit."""

import pytest
from agentscope.message import TextBlock
from pydantic import BaseModel

from open_deep_research.agentscope_runtime.research_agents import Researcher, ResearchAssignment
from open_deep_research.tools.base import ToolOrigin, ToolResult, build_tool
from open_deep_research.tools.search_documents import search_documents
from tests.as_runtime.test_research_migration import Models, cfg, contract


class Empty(BaseModel):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,sources,documents,expected", [
    ("web", [], False, {"web_research", "fetch_url", "mcp_read", "browser_read"}),
    ("documents", [{"type": "document", "id": "doc-1"}], True, {"search_documents"}),
    ("specific", [{"type": "domain", "domain": "example.com"}], False, {"web_research", "fetch_url"}),
    ("specific", [{"type": "document", "id": "doc-1"}], True, {"search_documents"}),
])
async def test_source_modes_filter_native_research_toolkit(monkeypatch, mode, sources, documents, expected):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", str(documents).lower())
    monkeypatch.setenv("DOCUMENT_DATABASE_URL", "postgresql://unused/test")
    config = cfg()
    config["metadata"]["source_selection"] = {"mode": mode, "sources": sources}

    async def call(*args):
        return ToolResult(output="fixture")

    async def tools_for(assignment):
        return [search_documents, *[
            build_tool(name=name, input_schema=Empty, description=name, origin=origin, call=call)
            for name, origin in (("web_research", ToolOrigin.SEARCH), ("fetch_url", ToolOrigin.SEARCH),
                                 ("mcp_read", ToolOrigin.MCP), ("browser_read", ToolOrigin.BROWSER))
        ]]

    models = Models({"researcher": [[TextBlock(text="completed")]]})
    await Researcher(models, lambda: config, tools_for, run_id="run").run(ResearchAssignment(research_topic="q"), contract())
    names = {item["function"]["name"] for item in models.created[0][2].calls[0]["tools"]}
    assert names - {"ResearchComplete", "think_tool", "CompressContext"} == expected

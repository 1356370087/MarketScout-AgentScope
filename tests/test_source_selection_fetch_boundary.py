"""A network grant never expands the user's exact source selection."""

import pytest
from open_deep_research.agentscope_runtime import web_tools
from open_deep_research.tools.base import ToolContext


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.postgresql.org/docs/17/btree.html",
    "https://www.postgresql.org/docs/17/release-17.html?chapter=other",
    "https://postgr.es/c/example",
])
async def test_granted_network_target_cannot_fetch_an_unselected_url(monkeypatch, url):
    def no_network(*args, **kwargs):
        pytest.fail("source boundary must reject before creating the fetch pipeline")

    monkeypatch.setattr(web_tools, "WebResearchPipeline", no_network)
    config = {"configurable": {}, "metadata": {
        "source_selection": {"mode": "specific", "sources": [
            {"type": "url", "url": "https://www.postgresql.org/docs/17/release-17.html"},
        ]},
        "sandbox_gateway_authorized_hosts": ["www.postgresql.org", "postgr.es"],
    }}
    tool = web_tools.fetch_url_tool(lambda: config, None, web_tools.WebFetchLedger())
    with pytest.raises(ValueError, match="outside this run's specific-source boundary"):
        await tool.call(tool.input_schema(url=url, objective="research"),
                        ToolContext(config=config, role="researcher", tool_call_id="source-boundary"))

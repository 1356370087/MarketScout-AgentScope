"""A network grant never expands the user's exact source selection."""

import pytest
from langchain_core.tools import ToolException

from open_deep_research.tools.fetch_url import definition


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.postgresql.org/docs/17/btree.html",
    "https://www.postgresql.org/docs/17/release-17.html?chapter=other",
    "https://postgr.es/c/example",
])
async def test_granted_network_target_cannot_fetch_an_unselected_url(monkeypatch, url):
    def no_network(*args, **kwargs):
        pytest.fail("source boundary must reject before creating the fetch pipeline")

    monkeypatch.setattr(definition, "WebResearchPipeline", no_network)
    config = {"configurable": {}, "metadata": {
        "source_selection": {"mode": "specific", "sources": [
            {"type": "url", "url": "https://www.postgresql.org/docs/17/release-17.html"},
        ]},
        "sandbox_gateway_authorized_hosts": ["www.postgresql.org", "postgr.es"],
    }}
    with pytest.raises(ToolException, match="outside this run's specific-source boundary"):
        await definition._fetch_url_call.coroutine(url=url, objective="research", config=config)

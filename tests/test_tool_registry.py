"""Tests for declarative tool availability and unified assembly."""

from __future__ import annotations

import pytest

from open_deep_research.tools.governance import AgentRole
from open_deep_research.tools.registry import prepare_existing_toolset
from open_deep_research.agentscope_runtime.web_tools import native_web_tools, WebFetchLedger
from open_deep_research.agentscope_runtime.search import search_provider_tools


async def assemble_native(config):
    tools = [*native_web_tools(lambda: config, None, WebFetchLedger()),
             *search_provider_tools(lambda: config, None)]
    return [tool for tool in tools if tool.is_enabled(config)]


async def prepare_native(config):
    return await prepare_existing_toolset(await assemble_native(config), AgentRole.RESEARCHER, config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "search_api", "present", "absent"),
    [
        ("enforced", "tavily", {"web_research", "fetch_url"}, {"tavily_search", "fetch_webpage"}),
        ("legacy", "tavily", {"tavily_search", "fetch_url"}, {"web_research", "fetch_webpage"}),
        ("shadow", "openai", {"openai_web_search", "fetch_url"}, {"tavily_search", "web_research"}),
        ("legacy", "anthropic", {"anthropic_web_search", "fetch_url"}, {"web_research"}),
    ],
)
async def test_researcher_is_enabled_matrix(
    mode,
    search_api,
    present,
    absent,
):
    tools = await assemble_native(
        {
            "configurable": {
                "web_pipeline_mode": mode,
                "search_api": search_api,
            }
        },
    )
    names = {tool.name for tool in tools}

    assert present <= names
    assert not (absent & names)


@pytest.mark.asyncio
async def test_prepare_toolset_builds_guidance_from_enabled_tools_only():
    assembly = await prepare_native(
        {"configurable": {"web_pipeline_mode": "enforced", "search_api": "tavily"}},
    )

    assert "web_research" in assembly.guidance
    assert "fetch_url" in assembly.guidance
    assert "tavily_search" not in assembly.guidance
    assert {item["name"] for item in assembly.definitions} == {
        tool.name for tool in assembly.tools
    }


@pytest.mark.asyncio
async def test_tool_description_budget_is_not_mcp_description_budget():
    assembly = await prepare_native(
        {
            "configurable": {
                "web_pipeline_mode": "enforced",
                "max_tool_description_chars": 64,
                "max_mcp_description_chars": 2000,
            }
        },
    )

    assert all(len(item["description"]) <= 64 for item in assembly.definitions)


@pytest.mark.asyncio
async def test_offline_network_mode_hides_all_web_tools(monkeypatch):
    monkeypatch.setattr(
        "open_deep_research.agentscope_runtime.web_tools.network_policy_mode",
        lambda _configurable: "offline",
    )

    monkeypatch.setattr("open_deep_research.agentscope_runtime.search.network_policy_mode", lambda _configurable: "offline")
    for mode in ("legacy", "shadow", "enforced"):
        tools = await assemble_native(
            {
                "configurable": {
                    "web_pipeline_mode": mode,
                    "search_api": "tavily",
                }
            },
        )
        names = {tool.name for tool in tools}

        assert not {
            "tavily_search",
            "openai_web_search",
            "anthropic_web_search",
            "fetch_webpage",
            "web_research",
            "fetch_url",
        } & names

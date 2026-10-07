"""Tests for the progressive broad-to-narrow research strategy."""

import pytest

from open_deep_research.prompts import research_system_prompt
from open_deep_research.tools.governance import AgentRole
from open_deep_research.tools.registry import prepare_existing_toolset
from open_deep_research.agentscope_runtime.research_agents import _Topic, _Thought, _control_tool
from open_deep_research.agentscope_runtime.teams_tools import TaskCreateInput
from open_deep_research.agentscope_runtime.search import search_provider_tools, tavily_search_tool


def test_research_prompt_requires_progressive_search_within_task_contract() -> None:
    rendered = research_system_prompt.format(
        tool_guidance="Configured search tool\nthink_tool",
        mcp_prompt="",
        date="July 7, 2026",
    )
    normalized = " ".join(rendered.split())

    assert "Treat the delegated task contract as binding" in normalized
    assert "If the task says to use only `fetch_url`, never call `web_research`" in normalized
    assert "Start broad only when unconstrained" in normalized
    assert "1-3 short, broad queries" in normalized
    assert "Map the information landscape" in normalized
    assert "Add only the necessary dimension" in normalized
    assert (
        "rephrase it more broadly without crossing any explicit tool, URL, source"
        in normalized
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search_api", "selected", "not_selected"),
    [
        ("tavily", "`tavily_search`", "`openai_web_search`"),
        ("openai", "`openai_web_search`", "`anthropic_web_search`"),
        ("anthropic", "`anthropic_web_search`", "`tavily_search`"),
    ],
)
async def test_research_prompt_uses_only_the_selected_search_backend(
    search_api: str,
    selected: str,
    not_selected: str,
) -> None:
    config = {"configurable": {"web_pipeline_mode": "legacy", "search_api": search_api}}
    async def unused(*_args):
        pytest.fail("prompt rendering must not execute a tool")
    tools = [*search_provider_tools(lambda: config, None), _control_tool("think_tool", _Thought, unused)]
    assembly = await prepare_existing_toolset(tools,
                                              AgentRole.RESEARCHER, config)
    rendered = research_system_prompt.format(
        tool_guidance=assembly.guidance,
        mcp_prompt="",
        date="July 7, 2026",
    )

    assert "web_search" in rendered
    assert not_selected.strip("`") not in rendered
    assert "Do not call it in parallel with a search or another tool" in rendered

    base_prompt = research_system_prompt.format(
        tool_guidance="SEARCH_TOOL_GUIDANCE_SENTINEL",
        mcp_prompt="",
        date="July 7, 2026",
    )
    assert "SEARCH_TOOL_GUIDANCE_SENTINEL" in base_prompt
    assert "tavily_search" not in base_prompt
    assert "openai_web_search" not in base_prompt
    assert "anthropic_web_search" not in base_prompt


def test_research_task_schemas_distinguish_objectives_from_queries() -> None:
    for task_model in (_Topic, TaskCreateInput):
        description = task_model.model_fields["research_topic"].description or ""

        assert "complete, self-contained research objective" in description
        assert "not a search-engine query to copy verbatim" in description
        assert "at least a paragraph" not in description


@pytest.mark.asyncio
async def test_tavily_schema_exposes_progressive_query_guidance() -> None:
    tavily_search = tavily_search_tool(lambda: {}, None)
    schema_description = tavily_search.input_schema.model_json_schema()["description"]
    tool_guidance = tavily_search.prompt({}) or ""

    assert "Short, search-engine-ready queries" in schema_description
    assert "Begin broad on the first call" in schema_description
    assert "evidence-backed gaps" in schema_description
    assert "Start broad" in tool_guidance
    assert "refine only when the evidence leaves a concrete gap" in tool_guidance

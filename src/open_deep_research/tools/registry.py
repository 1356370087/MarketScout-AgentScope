"""Single assembly and model-binding entry point for project tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, cast

from open_deep_research.config_types import RuntimeConfig

from open_deep_research.configuration import Configuration
from open_deep_research.documents.contracts import selection_from_config
from open_deep_research.tools.base import (
    Tool,
    build_tool_registry,
    tools_to_model_definitions,
)
from open_deep_research.tools.governance import AgentRole, filter_tools_by_permission


@dataclass(frozen=True, slots=True)
class ToolAssembly:
    """Enabled, permitted tools and their model-facing projections."""

    tools: list[Tool]
    definitions: list[dict]
    guidance: str


def render_tool_guidance(tools: Iterable[Tool], config: RuntimeConfig) -> str:
    """Render detailed guidance for exactly the tools exposed to the model."""
    sections = []
    for tool in tools:
        section = tool.prompt(config)
        if section and section.strip():
            sections.append(section.strip())
    return "\n\n".join(sections)


async def prepare_existing_toolset(
    tools: Iterable[Tool],
    role: AgentRole,
    config: RuntimeConfig,
) -> ToolAssembly:
    """Permission-filter and project an already assembled toolset."""
    candidate_tools = list(tools)
    build_tool_registry(candidate_tools)
    candidate_tools = [tool for tool in candidate_tools if tool.is_enabled(config)]
    permitted_tools = cast(
        list[Tool],
        filter_tools_by_permission(candidate_tools, role, config),
    )
    selection = selection_from_config(config)
    if (
        role is AgentRole.RESEARCHER
        and selection.documents_enabled
        and not any(
            tool.name == "search_documents" for tool in permitted_tools
        )
    ):
        raise ValueError(
            "document_mode_requires_search_documents: grant research.tool.document "
            "and keep the local document search tool enabled."
        )
    if not permitted_tools:
        raise ValueError(
            f"No tools found for {role.value}: configure an allowed tool source and "
            "ensure role tool policies do not exclude every tool."
        )
    definitions = await tools_to_model_definitions(
        permitted_tools,
        max_description_chars=Configuration.from_runnable_config(
            config
        ).max_tool_description_chars,
    )
    return ToolAssembly(
        tools=permitted_tools,
        definitions=definitions,
        guidance=render_tool_guidance(permitted_tools, config),
    )


__all__ = ["ToolAssembly", "prepare_existing_toolset", "render_tool_guidance"]

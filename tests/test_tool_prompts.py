"""Prompt contracts for native tools and the actual Supervisor model input."""

import ast
from pathlib import Path

import pytest
from agentscope.message import TextBlock
from open_deep_research.agentscope_runtime.research_agents import Supervisor
from tests.as_runtime.test_research_migration import Models, cfg, contract
from tests.test_tool_registry import assemble_native

TOOLS_ROOT = Path(__file__).parents[1] / "src" / "open_deep_research" / "tools"


def test_tool_prompt_modules_are_pure_string_renderers() -> None:
    prompt_paths = sorted(TOOLS_ROOT.rglob("prompt.py"))

    assert prompt_paths
    for prompt_path in prompt_paths:
        tree = ast.parse(prompt_path.read_text(encoding="utf-8"))
        imports = [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert not imports, f"{prompt_path} must not import tool implementation code"



def test_native_tool_definitions_own_their_call_implementations():
    files = [TOOLS_ROOT / name / "definition.py" for name in
             ("read_file", "write_file", "shell_exec", "search_documents", "research_complete")]
    files += [TOOLS_ROOT.parent / "agentscope_runtime" / name for name in ("search.py", "web_tools.py")]
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                   (node.name.endswith("_call") or node.name == "call") for node in ast.walk(tree)), path
        assert "clone_builtin_tool" not in source
        assert "tools.implementations" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "shadow", "enforced"])
async def test_every_enabled_builtin_has_nonempty_description_and_prompt(mode):
    tools = await assemble_native({"configurable": {"web_pipeline_mode": mode, "search_api": "tavily"}})
    assert tools
    for tool in tools:
        assert (await tool.description()).strip()
        assert (tool.prompt({}) or "").strip()


async def supervisor_prompt(**config):
    models = Models({"supervisor": [[TextBlock(text="done")]]})
    with pytest.raises(ValueError, match="supervisor produced no research handoff"):
        await Supervisor(models, lambda: cfg(async_research_mode="collaborator", **config),
                         None, run_id="prompt-test").run("Investigate the topic", contract())
    messages = models.created[0][2].calls[0]["messages"]
    return "\n".join(message.get_text_content() or "" for message in messages if message.role == "system")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_enabled,expected,absent", [
    (False, "`ConductResearch`", "`TaskCreate`"),
    (True, "`TaskCreate`", "`ConductResearch`"),
])
async def test_supervisor_available_tools_are_rendered_from_actual_toolset(async_enabled, expected, absent):
    prompt = await supervisor_prompt(enable_async_research=async_enabled)
    assert expected in prompt
    assert absent not in prompt


@pytest.mark.asyncio
async def test_initial_supervisor_prompt_uses_permission_filtered_guidance():
    prompt = await supervisor_prompt(supervisor_tool_whitelist=["think_tool"])
    available_tools = prompt.split("<Available Tools>", 1)[1].split("</Available Tools>", 1)[0]
    assert "`think_tool`" in available_tools
    assert "`ConductResearch`" not in available_tools

"""Public AgentScope truncation and governed offload-read contracts."""

import json
import re
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent, ContextConfig
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState, UserMsg
from pydantic import BaseModel
from test_recovery import create, store  # noqa: F401
from test_research_migration import ScriptedModel
from test_tools import config, invoke, toolkit

from open_deep_research.agentscope_runtime.context import RunContextOffloader
from open_deep_research.agentscope_runtime.context_tools import context_read_tools
from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.tools.base import ToolExecutionZone, ToolOrigin, ToolResult, build_tool

pytestmark = pytest.mark.asyncio


async def make_offloader(store, tmp_path):
    state, lease = await create(store)
    return RunContextOffloader(tmp_path, SimpleNamespace(store=store, lease=lease)), lease


async def test_context_pages_roundtrip_without_exposing_other_sessions(store, tmp_path):
    offloader, lease = await make_offloader(store, tmp_path)
    original = UserMsg("user", "来源约束与需求 COV-01。" * 120)
    ref = await offloader.offload_context("session", [original])
    parts, offset = [], 0
    while offset is not None:
        page = await offloader.read(ref, session_id="session", offset=offset, limit=37)
        assert len(page["content"]) <= 37
        parts.append(page["content"])
        offset = page["next_offset"]
    assert json.loads("".join(parts))["messages"] == [original.model_dump(mode="json")]
    with pytest.raises(PermissionError, match="session"):
        await offloader.read(ref, session_id="other")
    await store.release(lease)
    with pytest.raises(FenceLost):
        await offloader.read(ref, session_id="session")
    with pytest.raises(FenceLost):
        await offloader.offload_tool_result("session", ToolResultBlock(id="c", name="search", output="x"))


@pytest.mark.parametrize("suffix", ["../manifest.json", "../../other.json", "sub/record.json", "record.json?key=x", "record.json#x"])
async def test_offload_read_rejects_paths_outside_the_payload_directory(store, tmp_path, suffix):
    offloader, lease = await make_offloader(store, tmp_path)
    with pytest.raises(ValueError):
        await offloader.read(f"run-context://{lease.run_id}/{suffix}", session_id="session")
    with pytest.raises(ValueError):
        await offloader.read("run-context://other/record.json", session_id="session")


async def test_governed_reader_binds_session_and_rechecks_tool_permission(store, tmp_path):
    offloader, _ = await make_offloader(store, tmp_path)
    ref = await offloader.offload_tool_result(
        "session", ToolResultBlock(id="c", name="search", output="原始结果"),
    )
    cfg = config()
    tk = await toolkit(context_read_tools(offloader, lambda: "session"), cfg=cfg)
    response = await invoke(tk, name="ReadContextArtifact", reference=ref, limit=8192)
    assert response.state is ToolResultState.SUCCESS
    page = json.loads(response.content[0].text)
    assert "原始结果" in page["content"]
    assert page["next_offset"] is None
    cfg["configurable"]["researcher_tool_whitelist"] = []
    assert (await invoke(tk, name="ReadContextArtifact", reference=ref)).state is ToolResultState.DENIED


async def test_reader_schema_cannot_override_session_identity(store, tmp_path):
    offloader, _ = await make_offloader(store, tmp_path)
    ref = await offloader.offload_context("other", [UserMsg("user", "private")])
    tk = await toolkit(context_read_tools(offloader, lambda: "session"))
    response = await invoke(tk, name="ReadContextArtifact", reference=ref, session_id="other")
    assert response.state is not ToolResultState.SUCCESS
    assert "private" not in str(response.content)


async def test_framework_truncates_tool_result_and_exposes_readable_reference(store, tmp_path):
    offloader, _ = await make_offloader(store, tmp_path)

    class Empty(BaseModel):
        pass

    async def large(input, context, progress):
        return ToolResult(output="word " * 4000 + "TAIL_EVIDENCE")

    source = build_tool(
        name="large", input_schema=Empty, origin=ToolOrigin.SYSTEM,
        description="Read a long source", prompt=lambda cfg: "Read large.",
        execution_zone=ToolExecutionZone.HOST_CONTROL, call=large,
    )
    tk = await toolkit([source, *context_read_tools(offloader, lambda: agent.state.session_id)])
    model = ScriptedModel([
        [ToolCallBlock(id="large-call", name="large", input="{}")],
        [TextBlock(text="finished")],
    ])
    agent = Agent(
        name="reader", model=model, toolkit=tk, system_prompt="Read the source.",
        offloader=offloader, context_config=ContextConfig(tool_result_limit=128),
    )
    await agent.reply(UserMsg("user", "Read large."))
    result = next(
        b for m in agent.state.context for b in m.content
        if isinstance(b, ToolResultBlock) and b.name == "large"
    )
    text = result.model_dump_json()
    assert "TRUNCATED" in text
    ref = re.search(r"run-context://[^'\s\"\\]+", text).group()
    chunks, offset = [], 0
    while offset is not None:
        page = await offloader.read(ref, session_id=agent.state.session_id, offset=offset)
        chunks.append(page["content"])
        offset = page["next_offset"]
    persisted = json.loads("".join(chunks))
    assert persisted["tool_result"]["id"] == "large-call"
    assert "TAIL_EVIDENCE" in json.dumps(persisted)

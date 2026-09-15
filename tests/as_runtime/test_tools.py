"""AS-A024/025: native Toolkit calls and shared execution policy contracts."""

import asyncio
import base64
import json
import os
import sys
from dataclasses import replace

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.state import AgentState
from agentscope.tool import ToolResponse
from pydantic import BaseModel, ConfigDict

from open_deep_research.as_runtime.tools import (
    ToolGovernanceMiddleware,
    prepare_toolkit,
)
from open_deep_research.tools.base import (
    ToolEffect,
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)
from open_deep_research.tools.governance import AgentRole

pytestmark = pytest.mark.asyncio


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def config(**kwargs):
    return {"configurable": {"event_log_enabled": False, **kwargs}, "metadata": {}}


def make_tool(name="read", **kwargs):
    async def call(value, context, progress):
        return ToolResult(
            output={"value": value.value, "operation_id": context.operation_id}
        )

    defaults = {
        "name": name,
        "input_schema": Input,
        "origin": ToolOrigin.SYSTEM,
        "description": "description" * 30,
        "prompt": lambda cfg: f"Use {name}.",
        "call": call,
        "concurrency_safe": True,
    }
    defaults.update(kwargs)
    return build_tool(**defaults)


async def toolkit(tools=None, cfg=None, **kwargs):
    cfg = cfg if cfg is not None else config()
    defaults = {
        "role": AgentRole.RESEARCHER,
        "config_provider": lambda: cfg,
        "run_id": "run",
        "task_id": "task",
        "local_zones": frozenset(ToolExecutionZone),
        "retry_delay": 0,
    }
    defaults.update(kwargs)
    return await prepare_toolkit(tools or [make_tool()], **defaults)


async def invoke(tk, name="read", call_id="call-1", **args):
    call = ToolCallBlock(id=call_id, name=name, input=json.dumps(args or {"value": 1}))
    events = [event async for event in tk.call_tool(call, AgentState())]
    assert isinstance(events[-1], ToolResponse)
    return events[-1]


async def test_no_langchain_required_for_native_tool_import():
    env = {**os.environ, "PYTHONPATH": "src"}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; from open_deep_research.as_runtime.tools import prepare_toolkit; assert 'langchain_core' not in sys.modules",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")


async def test_canonical_catalog_schema_guidance_and_ids():
    tk = await toolkit(cfg=config(max_tool_description_chars=64))
    schema = (await tk.get_tool_schemas())[0]["function"]
    assert schema["name"] == "read"
    assert len(schema["description"]) == 64
    assert "operation_id" not in schema["parameters"]["properties"]
    assert tk.guidance == "Use read."
    response = await invoke(tk)
    assert response.id == "call-1"
    assert response.state is ToolResultState.SUCCESS
    assert json.loads(response.content[0].text)["operation_id"] == "run:task:call-1"


async def test_duplicate_names_fail_before_filtering():
    with pytest.raises(ValueError, match="Duplicate"):
        await toolkit(
            [make_tool(), make_tool()], cfg=config(researcher_tool_whitelist=[])
        )


@pytest.mark.parametrize("name", ["GenerateStructuredOutput", "CompressContext"])
async def test_framework_owned_names_cannot_be_overwritten(name):
    with pytest.raises(ValueError, match="Reserved"):
        await toolkit([make_tool(name)])


@pytest.mark.parametrize("role", list(AgentRole))
async def test_role_allowlist_and_enabled_tools_drive_guidance(role):
    cfg = config(**{f"{role.value}_tool_whitelist": ["read"]})
    tk = await toolkit([make_tool(), make_tool("hidden")], cfg=cfg, role=role)
    assert [s["function"]["name"] for s in await tk.get_tool_schemas()] == ["read"]
    assert "hidden" not in tk.guidance
    disabled = make_tool("disabled", is_enabled=lambda cfg: False)
    tk = await toolkit([make_tool(), disabled])
    assert "disabled" not in tk.guidance


@pytest.mark.parametrize("mode", ["whitelist", "origin", "user", "disabled"])
async def test_policy_revocation_rechecked_at_execution(mode):
    cfg = config()
    tool = make_tool(is_enabled=lambda c: not c["metadata"].get("disabled"))
    tk = await toolkit([tool], cfg=cfg)
    if mode == "whitelist":
        cfg["configurable"]["researcher_tool_whitelist"] = []
    elif mode == "origin":
        cfg["configurable"]["researcher_blocked_origins"] = ["system"]
    elif mode == "user":
        cfg["configurable"].update(
            user_permissions=["viewer"], role_tool_blacklist={"viewer": ["read"]}
        )
    else:
        cfg["metadata"]["disabled"] = True
    result = await invoke(tk)
    assert result.state is ToolResultState.DENIED
    assert result.metadata["error_type"] == "permission_denied"
    assert await tk.get_tool_schemas() == []
    assert await tk.get_guidance() == ""


async def test_mcp_auth_and_authenticated_origin_permissions():
    tool = make_tool(origin=ToolOrigin.MCP)
    cfg = config(
        mcp_config={
            "url": "https://mcp.example/mcp",
            "auth_required": True,
            "tools": [],
        }
    )
    with pytest.raises(ValueError, match="No tools"):
        await toolkit([tool], cfg=cfg)
    cfg["configurable"]["mcp_tokens"] = {"token": "fixture"}
    tk = await toolkit([tool], cfg=cfg)
    cfg["configurable"]["langgraph_auth_user"] = {"effective_permissions": []}
    assert (await invoke(tk)).state is ToolResultState.DENIED


@pytest.mark.parametrize("method", ["call", "__call__"])
async def test_direct_tool_call_requires_trusted_framework_identity(method):
    tk = await toolkit()
    tool = await tk.get_tool("read")
    result = await getattr(tool, method)(value=1)
    assert result.state is ToolResultState.DENIED
    assert result.metadata["error_type"] == "missing_call_context"


async def test_invalid_input_does_not_execute():
    calls = []

    async def call(value, context, progress):
        calls.append(value)
        return ToolResult(output="unexpected")

    tk = await toolkit([make_tool(call=call)])
    response = await invoke(tk, value=1, operation_id="forged")
    assert response.state is ToolResultState.ERROR
    assert not calls


async def test_sensitive_approval_is_for_exact_call_only():
    cfg = config()
    tk = await toolkit([make_tool(effect=ToolEffect.EXTERNAL_WRITE)], cfg=cfg)
    assert (await invoke(tk)).state is ToolResultState.DENIED
    cfg["metadata"]["approved_sensitive_tool_call_ids"] = ["call-1"]
    assert (await invoke(tk)).state is ToolResultState.SUCCESS
    assert (await invoke(tk, call_id="call-2")).state is ToolResultState.DENIED


async def test_egress_denied_then_trusted_approval():
    cfg = config(
        sandbox_enabled=True,
        enable_async_research=True,
        sandbox_root_signing_key=base64.b64encode(b"k" * 32).decode(),
        sandbox_policy_path="config/sandbox-policy.toml",
    )
    tk = await toolkit(
        [make_tool(egress_urls=lambda args: ["https://untrusted.example/x"])], cfg=cfg
    )
    response = await invoke(tk)
    assert response.metadata["error_type"] == "egress_domain_denied"
    cfg["metadata"]["sandbox_gateway_authorized_hosts"] = ["untrusted.example"]
    assert (await invoke(tk)).state is ToolResultState.SUCCESS


async def test_external_execution_cannot_fall_back_to_local_or_retry_dispatch():
    calls = []

    async def local(value, context, progress):
        pytest.fail("Gateway-only tool executed on host")

    async def dispatch(tool, value, context):
        calls.append(context.operation_id)
        raise ConnectionError("gateway unavailable")

    tool = make_tool(
        call=local, execution_zone=ToolExecutionZone.GATEWAY, retryable=True
    )
    zones = frozenset({ToolExecutionZone.HOST_CONTROL})
    tk = await toolkit([tool], local_zones=zones)
    assert (await invoke(tk)).metadata["error_type"] == "execution_zone_denied"
    tk = await toolkit([tool], local_zones=zones, dispatcher=dispatch)
    assert (await invoke(tk)).state is ToolResultState.ERROR
    assert calls == ["run:task:call-1"]
    tk.config_provider()["configurable"]["researcher_tool_whitelist"] = []
    assert (await invoke(tk)).state is ToolResultState.DENIED
    assert len(calls) == 1


@pytest.mark.parametrize(
    "effect,idempotent,expected",
    [
        (ToolEffect.READ_ONLY, False, 3),
        (ToolEffect.EXTERNAL_WRITE, False, 1),
        (ToolEffect.EXTERNAL_WRITE, True, 3),
    ],
)
async def test_retry_policy_and_stable_operation_key(effect, idempotent, expected):
    calls = []

    async def call(value, context, progress):
        calls.append(context.operation_id)
        raise ConnectionError("transient")

    tool = make_tool(
        call=call, effect=effect, supports_idempotency=idempotent, retryable=True
    )
    tk = await toolkit([tool], role=AgentRole.SUPERVISOR, max_retries=2)
    response = await invoke(tk)
    assert response.state is ToolResultState.ERROR
    assert calls == ["run:task:call-1"] * expected
    assert (
        len([e for e in response.metadata["governance"] if e["type"] == "retry"])
        == expected - 1
    )


async def test_output_budget_preserved():
    async def call(value, context, progress):
        return ToolResult(output="x" * 1000)

    tk = await toolkit(
        [make_tool(call=call, max_output_chars=100)],
        cfg=config(max_mcp_output_chars=256),
    )
    response = await invoke(tk)
    assert response.content[0].text == "x" * 100 + "\n[truncated 900 chars]"


async def test_safe_calls_parallel_and_unsafe_call_exclusive():
    entered = asyncio.Event()
    release = asyncio.Event()
    active = []
    seen = []

    async def call(value, context, progress):
        active.append(context.tool_call_id)
        seen.append(tuple(active))
        if len(active) == 2:
            entered.set()
        await release.wait()
        active.remove(context.tool_call_id)
        return ToolResult(output="ok")

    safe = make_tool(call=call)
    unsafe = replace(safe, name="write", concurrency_safe=False)
    tk = await toolkit([safe, unsafe])
    a = asyncio.create_task(invoke(tk, call_id="a"))
    b = asyncio.create_task(invoke(tk, call_id="b"))
    await asyncio.wait_for(entered.wait(), 2)
    c = asyncio.create_task(invoke(tk, name="write", call_id="c"))
    await asyncio.sleep(0)
    assert "c" not in active
    release.set()
    await asyncio.gather(a, b, c)
    assert seen == [("a",), ("a", "b"), ("c",)]


async def test_cancellation_releases_serial_gate_and_call_identity():
    entered = asyncio.Event()

    async def call(value, context, progress):
        entered.set()
        await asyncio.Event().wait()

    tk = await toolkit(
        [make_tool(call=call, concurrency_safe=False), make_tool("next")]
    )
    task = asyncio.create_task(invoke(tk))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert tk.identity.get() is None
    assert (
        await asyncio.wait_for(invoke(tk, name="next"), 2)
    ).state is ToolResultState.SUCCESS


@pytest.mark.parametrize("structured", [False, True])
async def test_native_agent_uses_toolkit_and_permission_middleware(structured):
    from agentscope.agent import Agent, ContextConfig, ReActConfig
    from agentscope.credential import CredentialBase
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.model import ChatModelBase, ChatResponse

    from open_deep_research.as_runtime.gateway import SandboxChatModel

    class Fake(ChatModelBase):
        def __init__(self):
            super().__init__(
                CredentialBase(),
                "fixture",
                SandboxChatModel.Parameters(),
                stream=False,
                max_retries=0,
            )
            self.formatter = OpenAIChatFormatter()
            self.count = 0

        async def _call_api(self, *args, **kwargs):
            self.count += 1
            content = [ToolCallBlock(id="agent-call", name="read", input='{"value":2}')]
            if self.count > 1:
                content = [TextBlock(text="done")]
                if structured:
                    content = [
                        ToolCallBlock(
                            id="structured",
                            name="GenerateStructuredOutput",
                            input='{"value":2}',
                        )
                    ]
            return ChatResponse(content=content, is_last=True)

    tk = await toolkit()
    agent = Agent(
        name="researcher",
        system_prompt="Use tools. {tool_guidance}",
        model=Fake(),
        toolkit=tk,
        middlewares=[ToolGovernanceMiddleware()],
        context_config=ContextConfig(compression_tool_enabled=True),
        react_config=ReActConfig(max_iters=3),
    )
    result = await agent.reply(
        UserMsg("user", "Read the value"),
        structured_schema=Input if structured else None,
    )
    if not structured:
        assert result.get_text_content() == "done"
    else:
        assert agent.state.reply_context.structured_output == {"value": 2}
    assert "run:task:agent-call" in agent.state.model_dump_json()

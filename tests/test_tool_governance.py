"""Tests for the tool governance layer.

Covers: tool_governance.py (origin labeling, whitelist/permission, parameter
validation including configured constraints, error classification, retry with
exponential backoff scoped by origin, the governed execution entry point, the
supervisor gate, pre-bind filtering, and user-role blacklists), self-hosted IAM
Principal role propagation, plus integration tests that exercise
the shared execution protocol used by the native GovernedToolkit.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from open_deep_research.config_types import RuntimeConfig as RunnableConfig
from inspect import signature, isawaitable, isclass
from typing import get_type_hints
from pydantic import BaseModel, create_model

from open_deep_research.configuration import Configuration
from open_deep_research.tools.base import Tool, ToolContext, ToolEffect, ToolOrigin, ToolResult, build_tool
from open_deep_research.tools.governance import (
    AgentRole,
    ToolErrorType,
    ToolExecutionFailure,
    classify_retryable_error,
    filter_tools_by_permission,
    get_tool_origin,
    get_tool_retryable,
    get_user_permissions,
    resolve_allowed_tools,
    validate_tool_args,
)
from open_deep_research.tools.governance import (
    check_permission as _check_permission,
)
from open_deep_research.tools.governance import (
    execute_governed_tool_call as _execute_governed_tool_call,
)
from open_deep_research.tools.governance import (
    invoke_tool_with_retry as _invoke_tool_with_retry,
)
from open_deep_research.agentscope_runtime.research_agents import _Topic as ConductResearch
from open_deep_research.agentscope_runtime.search import tavily_search_tool
from open_deep_research.agentscope_runtime.web_tools import fetch_url_tool, WebFetchLedger
from security.rbac.principal import Principal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**configurable: Any) -> RunnableConfig:
    """Build a RunnableConfig with the given configurable overrides."""
    return dict(configurable=configurable, metadata={"run_id": "test"})


def _runtime_user(
    *roles: str,
    permissions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the runtime identity shape emitted by self-hosted IAM."""
    return Principal(
        user_id="u1",
        email="u1@example.test",
        status="active",
        session_id="session-1",
        roles=frozenset(roles),
        permissions=frozenset(permissions),
        authz_version=1,
    ).to_runtime_dict()


def _client_response_error(status: int, message: str = "err") -> aiohttp.ClientResponseError:
    """Construct an aiohttp.ClientResponseError with a minimal but str-safe payload."""
    return aiohttp.ClientResponseError(
        request_info=None, history=(), status=status, message=message,
    )


def _make_tool(fn, *, origin, retryable=False, name=None, **kwargs):
    """Build the project's typed Tool directly from small test callables."""
    if isclass(fn) and issubclass(fn, BaseModel):
        schema = fn
    else:
        hints = get_type_hints(fn)
        schema = create_model(fn.__name__ + "Input", **{
            key: (hints.get(key, Any), parameter.default if parameter.default is not parameter.empty else ...)
            for key, parameter in signature(fn).parameters.items()
        })

    async def call(input, context, on_progress=None):
        result = fn(**input.model_dump())
        if isawaitable(result):
            result = await result
        return ToolResult(output=result.model_dump_json() if isinstance(result, BaseModel) else result)

    return build_tool(name=name or fn.__name__, input_schema=schema, call=call,
                      description=fn.__doc__ or "Fixture tool", origin=origin,
                      retryable=retryable, **kwargs)


def _research_complete_tool(*, origin=ToolOrigin.SYSTEM, retryable=False, auth_satisfied=False):
    if origin is ToolOrigin.MCP:
        from types import SimpleNamespace
        from agentscope.message import TextBlock, ToolResultState
        from agentscope.tool import ToolResponse
        from mcp.types import Tool as Descriptor
        from open_deep_research.agentscope_runtime.mcp import NativeMcpTool
        handle = AsyncMock(return_value=ToolResponse(content=[TextBlock(text="ResearchComplete")],
                                                     state=ToolResultState.SUCCESS))
        server = SimpleNamespace(ensure=AsyncMock(), client=SimpleNamespace(get_tool=AsyncMock(return_value=handle)))
        return NativeMcpTool(server, Descriptor(name="ResearchComplete", inputSchema={"type": "object", "properties": {}}),
                             origin=origin, effect=ToolEffect.READ_ONLY, retryable=retryable,
                             egress_urls_url=None, auth_satisfied=auth_satisfied)

    async def complete():
        return "ResearchComplete"
    return _make_tool(complete, name="ResearchComplete", origin=origin, retryable=retryable)


async def ok_tool() -> str:
    """A tool that always succeeds."""
    return "ok-result"


async def _flaky_503_fn() -> str:
    """A tool that always raises an HTTP 503."""
    raise _client_response_error(503, "busy")


async def _bad_request_404_fn() -> str:
    """A tool that always raises an HTTP 404 (non-retryable)."""
    raise _client_response_error(404, "nope")


# Module-level @tool wrappers (kept for the retry-unit tests that call
# invoke_tool_with_retry directly with a StructuredTool).
ok_tool = _make_tool(ok_tool, origin=ToolOrigin.SYSTEM)
flaky_503 = _make_tool(
    _flaky_503_fn, origin=ToolOrigin.SEARCH, retryable=True
)
bad_request_404 = _make_tool(
    _bad_request_404_fn, origin=ToolOrigin.SEARCH, retryable=True
)


tavily_search = tavily_search_tool(lambda: {}, None)
fetch_url = fetch_url_tool(lambda: {}, None, WebFetchLedger())

async def _think(reflection: str):
    return f"Reflection recorded: {reflection}"

think_tool = _make_tool(_think, name="think_tool", origin=ToolOrigin.SYSTEM)


async def _probe_search_fn(query: str) -> str:
    """A probe search tool."""
    return query


def _is_denied(msg) -> bool:
    """True if a ToolMessage carries a permission_denied structured error."""
    try:
        return json.loads(msg.content).get("error_type") == "permission_denied"
    except Exception:
        return False  # non-JSON content means the tool executed (success)
    """True if a ToolMessage carries a permission_denied structured error."""
    try:
        return json.loads(msg.content).get("error_type") == "permission_denied"
    except Exception:
        return False  # non-JSON content means the tool executed (success)


def check_permission(
    tool_name,
    tool,
    role,
    allowed,
    origin_index=None,
    config=None,
):
    """Bridge the former test call shape to the Tool-owned origin contract."""
    del origin_index
    return _check_permission(tool_name, tool, role, allowed, config)


async def execute_governed_tool_call(*args, **kwargs):
    """Return the typed transport message for shared scenario assertions."""
    kwargs.pop("origin_index", None)
    outcome = await _execute_governed_tool_call(*args, **kwargs)
    return outcome.message


async def invoke_tool_with_retry(
    tool,
    args,
    config,
    **kwargs,
):
    """Invoke the new typed retry seam while preserving scenario assertions."""
    config = config or _config()
    if not isinstance(tool, Tool):
        tool = _make_tool(
            tool,
            origin=ToolOrigin.SEARCH,
            retryable=True,
        )
    input = tool.input_schema.model_validate(args)
    result = await _invoke_tool_with_retry(
        tool,
        input,
        ToolContext(config=config, role="researcher", tool_call_id="retry-test"),
        **kwargs,
    )
    return result.output


# ---------------------------------------------------------------------------
# Tool origin tagging (4-category model)
# ---------------------------------------------------------------------------


class TestToolOriginFields:
    def test_system_origin_is_declared_on_tool(self):
        tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SYSTEM,
            retryable=False,
        )
        assert get_tool_origin(tool) is ToolOrigin.SYSTEM

    def test_search_origin_and_retry_policy_are_direct_fields(self):
        tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SEARCH,
            retryable=True,
        )
        assert tool.origin is ToolOrigin.SEARCH
        assert get_tool_retryable(tool) is True

    def test_retryable_defaults_are_conservative(self):
        assert get_tool_retryable(ok_tool) is False

    def test_effectful_tool_is_serial_unless_explicitly_opted_in(self):
        effectful_tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.MCP,
            effect=ToolEffect.EXTERNAL_WRITE,
        )

        assert effectful_tool.concurrency_safe is False


# ---------------------------------------------------------------------------
# Whitelist filtering + pre-bind filtering
# ---------------------------------------------------------------------------



class TestWhitelistFiltering:
    def test_resolve_allowed_none_when_whitelist_unset(self):
        # Arrange -- default config has no whitelist
        config = _config()
        # Act
        allowed = resolve_allowed_tools(AgentRole.RESEARCHER, config, {"tavily_search", "think_tool"})
        # Assert
        assert allowed is None  # backward compatible: all assembled tools allowed

    def test_resolve_allowed_intersects_with_assembled(self):
        # Arrange -- whitelist references a tool that is not assembled (stale)
        config = _config(researcher_tool_whitelist=["tavily_search", "ghost_tool"])
        # Act
        allowed = resolve_allowed_tools(AgentRole.RESEARCHER, config, {"tavily_search", "think_tool"})
        # Assert
        assert allowed == {"tavily_search"}  # stale name dropped

    def test_tool_whitelist_env_accepts_comma_separated_values(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "RESEARCHER_TOOL_WHITELIST",
            "fetch_url, think_tool, ResearchComplete",
        )

        configurable = Configuration.from_runnable_config({"configurable": {}})

        assert configurable.researcher_tool_whitelist == [
            "fetch_url",
            "think_tool",
            "ResearchComplete",
        ]

    def test_resolve_allowed_supervisor_uses_supervisor_whitelist(self):
        # Arrange
        config = _config(supervisor_tool_whitelist=["think_tool"])
        # Act
        allowed = resolve_allowed_tools(AgentRole.SUPERVISOR, config, {"think_tool", "ConductResearch"})
        # Assert
        assert allowed == {"think_tool"}

    def test_permission_denied_when_tool_not_in_whitelist(self):
        # Arrange
        tool = _research_complete_tool()
        config = _config(researcher_tool_whitelist=["think_tool"])
        # Act
        err = check_permission(
            "ResearchComplete", tool, AgentRole.RESEARCHER,
            allowed={"think_tool"}, origin_index=None, config=config,
        )
        # Assert
        assert err is not None
        assert err.error_type == ToolErrorType.permission_denied
        assert err.detail["role"] == "researcher"

    def test_permission_passes_when_whitelist_is_none(self):
        # Arrange
        tool = _research_complete_tool()
        config = _config()
        # Act
        err = check_permission(
            "ResearchComplete", tool, AgentRole.RESEARCHER,
            allowed=None, origin_index=None, config=config,
        )
        # Assert
        assert err is None

    def test_permission_denied_by_origin_blocklist(self):
        # Arrange -- a tool tagged MCP, blocked for researchers
        mcp_tool = _research_complete_tool(origin=ToolOrigin.MCP)
        config = _config(researcher_blocked_origins=["mcp"])
        # Act
        err = check_permission(
            "ResearchComplete", mcp_tool, AgentRole.RESEARCHER,
            allowed=None, origin_index={"ResearchComplete": ToolOrigin.MCP}, config=config,
        )
        # Assert
        assert err is not None
        assert err.error_type == ToolErrorType.permission_denied
        assert err.detail["origin"] == "mcp"


class TestPreBindFiltering:
    def test_no_filter_config_returns_all(self):
        # Arrange
        tools = [_research_complete_tool(), think_tool, tavily_search]
        # Act
        out = filter_tools_by_permission(tools, AgentRole.RESEARCHER, _config())
        # Assert -- backward compatible: everything passes
        assert len(out) == len(tools)

    def test_whitelist_filters_before_bind(self):
        # Arrange -- whitelist allows only tavily_search
        tools = [_research_complete_tool(), think_tool, tavily_search]
        config = _config(researcher_tool_whitelist=["tavily_search"])
        # Act
        out = filter_tools_by_permission(tools, AgentRole.RESEARCHER, config)
        names = {t.name if hasattr(t, "name") else t.get("name") for t in out}
        # Assert -- only the whitelisted tool remains; others never reach bind_tools
        assert names == {"tavily_search"}

    def test_origin_blocklist_filters_system_tools(self):
        # Arrange -- block system origin -> only search/MCP remain
        tools = [_research_complete_tool(), think_tool, tavily_search]
        config = _config(researcher_blocked_origins=["system"])
        # Act
        out = filter_tools_by_permission(tools, AgentRole.RESEARCHER, config)
        names = {t.name if hasattr(t, "name") else t.get("name") for t in out}
        # Assert
        assert "ResearchComplete" not in names
        assert "think_tool" not in names
        assert "tavily_search" in names

    def test_skill_tool_filtered_by_name(self):
        # Arrange
        skill_tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SKILL,
            retryable=True,
            name="skill_search",
        )
        tools = [_research_complete_tool(), skill_tool]
        config = _config(researcher_tool_whitelist=["ResearchComplete"])
        # Act
        out = filter_tools_by_permission(tools, AgentRole.RESEARCHER, config)
        names = {t.name for t in out}
        # Assert -- the non-whitelisted Tool is filtered out by name
        assert names == {"ResearchComplete"}

    def test_native_fetch_requires_search_permission(self):
        config = _config(
            langgraph_auth_user=_runtime_user(
                "researcher",
                permissions=("research.run.create",),
            )
        )

        out = filter_tools_by_permission(
            [fetch_url], AgentRole.RESEARCHER, config
        )

        assert out == []

    def test_auth_required_mcp_is_filtered_without_loaded_token(self):
        mcp_tool = _research_complete_tool(origin=ToolOrigin.MCP)
        config = _config(
            mcp_config={
                "url": "https://mcp.example",
                "tools": ["ResearchComplete"],
                "tool_effects": {"ResearchComplete": "read_only"},
                "auth_required": True,
            }
        )

        out = filter_tools_by_permission(
            [mcp_tool], AgentRole.RESEARCHER, config
        )

        assert out == []


# ---------------------------------------------------------------------------
# Parameter validation (incl. configured constraints)
# ---------------------------------------------------------------------------


class TestParamValidation:
    def test_missing_required_arg(self):
        # Arrange / Act
        err = validate_tool_args(tavily_search, {})
        # Assert
        assert err is not None
        assert err.error_type == ToolErrorType.validation_error
        assert err.detail["missing"] == ["queries"]

    def test_wrong_type_arg(self):
        # Arrange / Act
        err = validate_tool_args(tavily_search, {"queries": "not-a-list"})
        # Assert
        assert err is not None
        assert err.error_type == ToolErrorType.validation_error
        assert err.detail["argument"] == "queries"
        assert err.detail["expected_type"] == "array"

    def test_array_element_wrong_type(self):
        # Arrange / Act
        err = validate_tool_args(tavily_search, {"queries": ["ok", 123]})
        # Assert
        assert err is not None
        assert err.error_type == ToolErrorType.validation_error
        assert err.detail["index"] == 1
        assert err.detail["expected_type"] == "string"

    def test_valid_args_pass(self):
        # Arrange / Act / Assert
        assert validate_tool_args(tavily_search, {"queries": ["a", "b"]}) is None

    def test_empty_schema_passes(self):
        # Arrange -- ResearchComplete has no fields
        tool = _research_complete_tool()
        # Act / Assert
        assert validate_tool_args(tool, {}) is None

    def test_injected_args_not_required(self):
        """InjectedToolArg params (max_results/topic/config) must NOT be required."""
        # Arrange / Act / Assert
        assert validate_tool_args(tavily_search, {"queries": ["x"]}) is None

    def test_unknown_arg_lenient_by_default(self):
        # Arrange / Act / Assert
        assert validate_tool_args(tavily_search, {"queries": ["x"], "bogus": 1}) is None

    def test_conduct_research_missing_topic(self):
        # Arrange
        tool = _make_tool(
            ConductResearch,
            origin=ToolOrigin.SYSTEM,
        )
        # Act
        err = validate_tool_args(tool, {})
        # Assert
        assert err is not None
        assert err.detail["missing"] == ["research_topic"]

    def test_config_maxItems_on_queries(self):
        # Arrange -- configured constraint: tavily queries maxItems=3
        config = _config(tool_param_constraints={"tavily_search": {"queries": {"maxItems": 3}}})
        # Act / Assert -- too many queries rejected
        err = validate_tool_args(tavily_search, {"queries": ["a", "b", "c", "d"]}, config)
        assert err is not None and err.detail["constraint"] == "maxItems"
        # within limit passes
        assert validate_tool_args(tavily_search, {"queries": ["a", "b"]}, config) is None

    def test_config_minItems_on_queries(self):
        # Arrange
        config = _config(tool_param_constraints={"tavily_search": {"queries": {"minItems": 1}}})
        # Act / Assert
        err = validate_tool_args(tavily_search, {"queries": []}, config)
        assert err is not None and err.detail["constraint"] == "minItems"

    def test_config_per_element_maxLength_deep_merged(self):
        # Arrange -- per-query maxLength=5, deep-merged with schema items type=string
        config = _config(
            tool_param_constraints={"tavily_search": {"queries": {"items": {"maxLength": 5}}}},
        )
        # Act / Assert -- a too-long query is rejected
        err = validate_tool_args(tavily_search, {"queries": ["short", "toolongstring"]}, config)
        assert err is not None and "maxLength" in str(err.detail)
        # short queries pass
        assert validate_tool_args(tavily_search, {"queries": ["ok", "ok2"]}, config) is None
        # element type still validated (deep merge preserved type=string)
        err2 = validate_tool_args(tavily_search, {"queries": ["ok", 123]}, config)
        assert err2 is not None and err2.detail["expected_type"] == "string"

    def test_no_config_means_no_extra_constraints(self):
        # Arrange / Act / Assert -- without config, 4 queries pass (schema has no maxItems)
        assert validate_tool_args(tavily_search, {"queries": ["a", "b", "c", "d"]}) is None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestClassifyRetryableError:
    def test_timeout_retryable(self):
        assert classify_retryable_error(asyncio.TimeoutError()) == (ToolErrorType.timeout, True)

    def test_429_rate_limited(self):
        assert classify_retryable_error(_client_response_error(429)) == (ToolErrorType.rate_limited, True)

    def test_503_service_unavailable(self):
        assert classify_retryable_error(_client_response_error(503)) == (ToolErrorType.service_unavailable, True)

    def test_500_service_unavailable(self):
        assert classify_retryable_error(_client_response_error(500)) == (ToolErrorType.service_unavailable, True)

    def test_404_not_retryable(self):
        assert classify_retryable_error(_client_response_error(404)) == (ToolErrorType.unknown, False)

    def test_408_rate_limited(self):
        assert classify_retryable_error(_client_response_error(408)) == (ToolErrorType.rate_limited, True)

    def test_clienterror_network_retryable(self):
        assert classify_retryable_error(aiohttp.ClientError("boom")) == (ToolErrorType.network_error, True)

    def test_oserror_network_retryable(self):
        assert classify_retryable_error(OSError("net down")) == (ToolErrorType.network_error, True)

    def test_generic_runtime_not_retryable(self):
        assert classify_retryable_error(RuntimeError("whatever")) == (ToolErrorType.unknown, False)

    def test_cause_chain_recursion(self):
        # Arrange -- wrap a 503 inside a RuntimeError via raise...from
        try:
            try:
                raise _client_response_error(503, "up")
            except Exception as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            # Act / Assert
            assert classify_retryable_error(outer) == (ToolErrorType.service_unavailable, True)




# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------


class TestInvokeToolWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        # Arrange
        sleeper = AsyncMock()
        # Act
        result = await invoke_tool_with_retry(ok_tool, {}, None, max_retries=3, sleeper=sleeper)
        # Assert
        assert result == "ok-result"
        sleeper.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_with_growing_delays(self):
        # Arrange -- a tool that 503s twice then succeeds
        state = {"n": 0}

        async def transient() -> str:
            """Fails twice then succeeds."""
            state["n"] += 1
            if state["n"] < 3:
                raise _client_response_error(503, "busy")
            return "recovered"

        delays: list[float] = []

        async def recorder(d: float) -> None:
            delays.append(d)

        # Act
        result = await invoke_tool_with_retry(transient, {}, None, max_retries=3, base_delay=1.0, max_delay=30.0, sleeper=recorder)
        # Assert
        assert result == "recovered"
        assert len(delays) == 2  # two retries before success
        assert 1.0 <= delays[0] < 2.0   # attempt 0: 1 + [0,1)
        assert 2.0 <= delays[1] < 3.0   # attempt 1: 2 + [0,1)
        assert delays[1] > delays[0]    # backoff grows (base dominates jitter)

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        # Arrange
        sleeper = AsyncMock()
        # Act
        with pytest.raises(ToolExecutionFailure) as exc_info:
            await invoke_tool_with_retry(flaky_503, {}, None, max_retries=2, base_delay=1.0, sleeper=sleeper)
        # Assert
        failure = exc_info.value
        assert failure.error_type == ToolErrorType.max_retries_exceeded
        assert failure.attempts == 3  # initial attempt + 2 retries
        assert sleeper.await_count == 2

    @pytest.mark.asyncio
    async def test_non_retryable_no_retry(self):
        # Arrange
        sleeper = AsyncMock()
        # Act
        with pytest.raises(ToolExecutionFailure) as exc_info:
            await invoke_tool_with_retry(bad_request_404, {}, None, max_retries=3, sleeper=sleeper)
        # Assert
        assert exc_info.value.error_type == ToolErrorType.unknown
        assert exc_info.value.attempts == 1
        sleeper.assert_not_called()


# ---------------------------------------------------------------------------
# Governed execution entry point (retry scoped by origin)
# ---------------------------------------------------------------------------


class TestExecuteGovernedToolCall:
    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        # Arrange
        tc = {"name": "nope", "args": {}, "id": "tc1"}
        # Act
        msg = await execute_governed_tool_call(tc, {}, AgentRole.RESEARCHER, _config())
        # Assert
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "tool_not_found"
        assert msg.name == "nope"

    @pytest.mark.asyncio
    async def test_provider_native_dict_must_be_adapted_before_execution(self):
        from open_deep_research.tools.base import build_tool_registry

        provider_dict = {"type": "web_search_preview", "name": "web_search"}
        with pytest.raises(TypeError):
            build_tool_registry([provider_dict])

    @pytest.mark.asyncio
    async def test_validation_error_short_circuits_before_invoke(self):
        """Validation must run before the tool is invoked."""
        # Arrange -- a spy tool with tavily's schema; missing required `queries`.
        spy = AsyncMock(return_value="should-not-happen")
        spy.name = "tavily_search"
        spy.metadata = {"tool_retryable": True}
        spy.input_schema = tavily_search.input_schema
        tc = {"name": "tavily_search", "args": {}, "id": "tc3"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"tavily_search": spy}, AgentRole.RESEARCHER, _config(),
            apply_retry=True, max_retries=2,
        )
        # Assert
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "validation_error"
        assert parsed["detail"]["missing"] == ["queries"]
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_returns_toolmessage_with_content(self):
        # Arrange -- ok_tool is untagged (system, non-retryable); apply_retry=True
        # but get_tool_retryable(ok_tool) is False -> single execution, succeeds.
        tc = {"name": "ok_tool", "args": {}, "id": "tc4"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"ok_tool": ok_tool}, AgentRole.RESEARCHER, _config(),
            apply_retry=True, max_retries=2,
        )
        # Assert
        assert msg.content == "ok-result"
        assert msg.name == "ok_tool"

    @pytest.mark.asyncio
    async def test_per_tool_output_budget_is_applied_during_serialization(self):
        async def long_output() -> str:
            """Return content longer than the declared output budget."""
            return "abcdefghij"

        declared = _make_tool(
            long_output,
            origin=ToolOrigin.SYSTEM,
            max_output_chars=5,
        )
        result = await execute_governed_tool_call(
            {"name": declared.name, "args": {}, "id": "tc-budget"},
            {declared.name: declared},
            AgentRole.RESEARCHER,
            _config(),
        )

        assert result.content == "abcde\n[truncated 5 chars]"

    @pytest.mark.asyncio
    async def test_search_tool_retryable_failure_returns_structured_error(self):
        # Arrange -- flaky_503 tagged as SEARCH + retryable
        search_503 = _make_tool(_flaky_503_fn, origin=ToolOrigin.SEARCH, retryable=True)
        sleeper = AsyncMock()
        tc = {"name": "flaky_503", "args": {}, "id": "tc5"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"flaky_503": search_503}, AgentRole.RESEARCHER, _config(),
            apply_retry=True, max_retries=1, base_delay=1.0, sleeper=sleeper,
        )
        # Assert -- retried (max_retries_exceeded) because SEARCH tools are retryable
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "max_retries_exceeded"
        assert parsed["attempts"] == 2
        assert parsed["tool_name"] == "flaky_503"
        assert sleeper.await_count == 1

    @pytest.mark.asyncio
    async def test_system_tool_not_retried_even_when_apply_retry_true(self):
        # Arrange -- flaky_503 tagged as SYSTEM + non-retryable
        system_503 = _make_tool(_flaky_503_fn, origin=ToolOrigin.SYSTEM, retryable=False)
        sleeper = AsyncMock()
        tc = {"name": "flaky_503", "args": {}, "id": "tc5b"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"flaky_503": system_503}, AgentRole.RESEARCHER, _config(),
            apply_retry=True, max_retries=3, sleeper=sleeper,
        )
        # Assert -- NOT retried: single attempt, service_unavailable (not max_retries_exceeded)
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "service_unavailable"
        sleeper.assert_not_called()

    @pytest.mark.asyncio
    async def test_effectful_tool_is_not_retried_without_idempotency(self):
        attempts = 0

        async def external_write() -> str:
            """Perform an effectful operation that fails transiently."""
            nonlocal attempts
            attempts += 1
            raise _client_response_error(503, "busy")

        effectful = _make_tool(
            external_write,
            origin=ToolOrigin.MCP,
            effect=ToolEffect.EXTERNAL_WRITE,
            retryable=True,
        )
        sleeper = AsyncMock()

        msg = await execute_governed_tool_call(
            {"name": effectful.name, "args": {}, "id": "write-once"},
            {effectful.name: effectful},
            AgentRole.SUPERVISOR,
            _config(),
            max_retries=3,
            sleeper=sleeper,
        )

        assert json.loads(msg.content)["error_type"] == "service_unavailable"
        assert attempts == 1
        sleeper.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation_id, expected", [("run:write-idempotent", 2), ("", 1)])
    async def test_effectful_tool_can_retry_with_idempotency_support(self, operation_id, expected):
        attempts = 0

        async def idempotent_external_write() -> str:
            """Perform an idempotent operation that fails transiently."""
            nonlocal attempts
            attempts += 1
            raise _client_response_error(503, "busy")

        effectful = _make_tool(
            idempotent_external_write,
            origin=ToolOrigin.MCP,
            effect=ToolEffect.EXTERNAL_WRITE,
            retryable=True,
            supports_idempotency=True,
        )
        sleeper = AsyncMock()

        msg = await execute_governed_tool_call(
            {"name": effectful.name, "args": {}, "id": "write-idempotent"},
            {effectful.name: effectful},
            AgentRole.SUPERVISOR,
            _config(),
            operation_id=operation_id,
            max_retries=1,
            sleeper=sleeper,
        )

        error_type = "max_retries_exceeded" if operation_id else "service_unavailable"
        assert json.loads(msg.content)["error_type"] == error_type
        assert attempts == expected
        assert sleeper.await_count == expected - 1

    @pytest.mark.asyncio
    async def test_selected_local_document_read_uses_frozen_source_approval(self):
        async def read_selected_document(query: str) -> str:
            """Read from the already owner-validated document selection."""
            return f"local:{query}"

        local_read = _make_tool(
            read_selected_document, name="search_documents",
            origin=ToolOrigin.LOCAL_DOCUMENT,
            effect=ToolEffect.SENSITIVE_READ,
        )
        config = _config()
        config["metadata"].update(
            {
                "owner": "owner-1",
                "source_selection": {
                    "mode": "documents",
                    "sources": [{"type": "document", "id": "doc-1"}],
                },
            }
        )

        msg = await execute_governed_tool_call(
            {
                "name": local_read.name,
                "args": {"query": "needle"},
                "id": "local-read",
            },
            {local_read.name: local_read},
            AgentRole.RESEARCHER,
            config,
            apply_retry=False,
        )

        assert msg.content == "local:needle"

    @pytest.mark.asyncio
    async def test_unselected_local_document_read_still_requires_approval(self):
        async def read_local_document(query: str) -> str:
            """Read a local document without a frozen selection."""
            return query

        local_read = _make_tool(
            read_local_document, name="search_documents",
            origin=ToolOrigin.LOCAL_DOCUMENT,
            effect=ToolEffect.SENSITIVE_READ,
        )

        msg = await execute_governed_tool_call(
            {
                "name": local_read.name,
                "args": {"query": "needle"},
                "id": "unselected-local-read",
            },
            {local_read.name: local_read},
            AgentRole.RESEARCHER,
            _config(),
            apply_retry=False,
        )

        assert json.loads(msg.content)["error_type"] == (
            "sensitive_tool_approval_required"
        )

    @pytest.mark.asyncio
    async def test_mcp_auth_satisfied_tool_is_permitted(self):
        """An auth-required MCP tool loaded with a token (mcp_auth_satisfied=True)
        is permitted to execute -- not wrongly denied (P1#3)."""
        # Arrange
        mcp_tool = _research_complete_tool(
            origin=ToolOrigin.MCP,
            auth_satisfied=True,
        )
        config = _config()
        config["configurable"]["mcp_config"] = {"url": "http://x", "tools": ["ResearchComplete"], "auth_required": True}
        tc = {"name": "ResearchComplete", "args": {}, "id": "tc6"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"ResearchComplete": mcp_tool}, AgentRole.RESEARCHER, config,
            origin_index={"ResearchComplete": ToolOrigin.MCP}, apply_retry=False,
        )
        # Assert -- permitted (executes; ResearchComplete returns empty content, not a denial)
        assert not _is_denied(msg)

    @pytest.mark.asyncio
    async def test_mcp_auth_required_without_marker_denied(self):
        # Arrange -- MCP tool without the auth_satisfied marker + auth_required
        mcp_tool = _research_complete_tool(origin=ToolOrigin.MCP)
        config = _config()
        config["configurable"]["mcp_config"] = {"url": "http://x", "tools": ["ResearchComplete"], "auth_required": True}
        tc = {"name": "ResearchComplete", "args": {}, "id": "tc7"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"ResearchComplete": mcp_tool}, AgentRole.RESEARCHER, config,
            origin_index={"ResearchComplete": ToolOrigin.MCP}, apply_retry=False,
        )
        # Assert
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "permission_denied"
        assert parsed["detail"]["auth_required"] is True

    @pytest.mark.asyncio
    async def test_user_role_tool_blacklist_denied(self):
        # Arrange -- user has role 'admin', which is blacklisted from tavily_search
        config = _config(
            langgraph_auth_user=_runtime_user(
                "admin",
                permissions=("research.tool.search",),
            ),
            role_tool_blacklist={"admin": ["tavily_search"]},
        )
        tc = {"name": "tavily_search", "args": {"queries": ["x"]}, "id": "tc8"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"tavily_search": tavily_search}, AgentRole.RESEARCHER, config, apply_retry=False,
        )
        # Assert
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "permission_denied"
        assert parsed["detail"]["user_role"] == "admin"

    @pytest.mark.asyncio
    async def test_user_role_origin_blacklist_denied(self):
        # Arrange -- admin blocked from 'search' origin
        config = _config(
            langgraph_auth_user=_runtime_user(
                "admin",
                permissions=("research.tool.search",),
            ),
            role_blocked_origins={"admin": ["search"]},
        )
        tc = {"name": "tavily_search", "args": {"queries": ["x"]}, "id": "tc9"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"tavily_search": tavily_search}, AgentRole.RESEARCHER, config, apply_retry=False,
        )
        # Assert
        parsed = json.loads(msg.content)
        assert parsed["error_type"] == "permission_denied"
        assert parsed["detail"]["scope"] == "origin"

    @pytest.mark.asyncio
    async def test_anonymous_user_skips_role_layer(self):
        # Arrange -- no user permissions; role blacklist configured but does not apply
        config = _config(role_tool_blacklist={"admin": ["think_tool"]})
        assert get_user_permissions(config) == []
        tc = {"name": "think_tool", "args": {"reflection": "p"}, "id": "tc10"}
        # Act
        msg = await execute_governed_tool_call(
            tc, {"think_tool": think_tool}, AgentRole.RESEARCHER, config, apply_retry=False,
        )
        # Assert -- allowed (anonymous skips role layer)
        assert "Reflection recorded" in msg.content


# ---------------------------------------------------------------------------
# Supervisor gate
# ---------------------------------------------------------------------------


class TestSupervisorGovernedExecution:
    @pytest.mark.asyncio
    async def test_executor_blocks_unknown_tool(self):
        message = await execute_governed_tool_call(
            {"name": "ghost", "args": {}, "id": "g1"},
            {"think_tool": think_tool},
            AgentRole.SUPERVISOR,
            _config(),
        )
        assert json.loads(message.content)["error_type"] == "tool_not_found"

    def test_registry_rejects_unwrapped_callable(self):
        from open_deep_research.tools.base import build_tool_registry

        with pytest.raises(TypeError):
            build_tool_registry([ConductResearch])

    @pytest.mark.asyncio
    async def test_executor_blocks_whitelist_violation(self):
        tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SYSTEM,
            retryable=False,
            name="ConductResearch",
        )
        message = await execute_governed_tool_call(
            {
                "name": "ConductResearch",
                "args": {"query": "x"},
                "id": "g3",
            },
            {"ConductResearch": tool},
            AgentRole.SUPERVISOR,
            _config(supervisor_tool_whitelist=["think_tool"]),
            allowed_tools={"think_tool"},
        )
        assert json.loads(message.content)["error_type"] == "permission_denied"

    @pytest.mark.asyncio
    async def test_executor_blocks_invalid_args(self):
        tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SYSTEM,
            retryable=False,
            name="ConductResearch",
        )
        message = await execute_governed_tool_call(
            {"name": "ConductResearch", "args": {}, "id": "g4"},
            {"ConductResearch": tool},
            AgentRole.SUPERVISOR,
            _config(),
        )
        payload = json.loads(message.content)
        assert payload["error_type"] == "validation_error"
        assert payload["detail"]["missing"] == ["query"]

    @pytest.mark.asyncio
    async def test_executor_passes_valid_call(self):
        tool = _make_tool(
            _probe_search_fn,
            origin=ToolOrigin.SYSTEM,
            retryable=False,
            name="ConductResearch",
        )
        message = await execute_governed_tool_call(
            {
                "name": "ConductResearch",
                "args": {"query": "ai safety"},
                "id": "g5",
            },
            {"ConductResearch": tool},
            AgentRole.SUPERVISOR,
            _config(),
        )
        assert message.content == "ai safety"


# ---------------------------------------------------------------------------
# Self-hosted IAM Principal role propagation
# ---------------------------------------------------------------------------


class TestPrincipalRolePropagation:
    def test_extract_single_role(self):
        # Arrange / Act / Assert
        config = _config(langgraph_auth_user=_runtime_user("admin"))
        assert get_user_permissions(config) == ["admin"]

    def test_extract_roles_list(self):
        config = _config(
            langgraph_auth_user=_runtime_user("researcher", "viewer")
        )
        assert get_user_permissions(config) == ["researcher", "viewer"]

    def test_extract_dedupes(self):
        config = _config(
            langgraph_auth_user=_runtime_user(
                "admin",
                "admin",
                "researcher",
            )
        )
        assert get_user_permissions(config) == ["admin", "researcher"]

    def test_extract_none_metadata(self):
        assert get_user_permissions(
            _config(langgraph_auth_user=None)
        ) == []

    def test_extract_empty_metadata(self):
        assert get_user_permissions(
            _config(langgraph_auth_user={})
        ) == []

    def test_get_user_permissions_reads_langgraph_auth_user(self):
        # Arrange -- server-injected user dict
        config = _config(langgraph_auth_user={"identity": "u1", "permissions": ["admin", "researcher"]})
        # Act / Assert
        assert get_user_permissions(config) == ["admin", "researcher"]

    def test_get_user_permissions_fallback_user_permissions(self):
        # Arrange -- plain fallback key (tests / non-server)
        config = _config(user_permissions=["viewer"])
        # Act / Assert
        assert get_user_permissions(config) == ["viewer"]

    def test_get_user_permissions_empty_when_absent(self):
        # Act / Assert
        assert get_user_permissions(_config()) == []


# ---------------------------------------------------------------------------
# Integration: researcher_tools with a retryable failure (retry scoped to SEARCH)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration: supervisor_tools -- ResearchComplete whitelist bypass + id filtering
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Egress domain allowlist (check_egress_domain inside execute_governed_tool_call)
# ---------------------------------------------------------------------------


async def _ok_fetch_fn(url: str) -> str:
    """A fetch_webpage stand-in that returns fixed content (no real network)."""
    return f"fetched:{url}"


def _fetch_tool() -> Any:
    """Build a retryable fetch_webpage Tool used by egress scenarios."""
    return _make_tool(
        _ok_fetch_fn, name="fetch_webpage",
        origin=ToolOrigin.SYSTEM,
        retryable=True,
        egress_urls=lambda args: [args["url"]],
    )


def _egress_config(**configurable: Any) -> RunnableConfig:
    base = {
        "sandbox_enabled": True,
        "enable_async_research": True,
        "sandbox_root_signing_key": base64.b64encode(b"k" * 32).decode(),
        "sandbox_policy_path": "config/sandbox-policy.toml",
    }
    base.update(configurable)
    return dict(
        configurable=base, metadata={"run_id": "egress-run"}
    )
class TestEgressAllowlist:
    def test_post_approval_authorized_domain_passes(self):
        tool = _fetch_tool()
        cfg = _egress_config()
        cfg["metadata"]["sandbox_gateway_authorized_hosts"] = ["example.com"]
        msg = asyncio.run(
            execute_governed_tool_call(
                {"name": "fetch_webpage", "id": "tc1", "args": {"url": "https://example.com/x"}},
                {"fetch_webpage": tool},
                AgentRole.RESEARCHER,
                cfg,
                apply_retry=False,
            )
        )
        assert msg.content == "fetched:https://example.com/x"

    def test_unknown_domain_is_fail_closed_before_durable_approval(self):
        tool = _fetch_tool()
        cfg = _egress_config()
        msg = asyncio.run(
            execute_governed_tool_call(
                {"name": "fetch_webpage", "id": "tc1", "args": {"url": "https://untrusted.example/x"}},
                {"fetch_webpage": tool},
                AgentRole.RESEARCHER,
                cfg,
                apply_retry=False,
            )
        )
        payload = json.loads(msg.content)
        assert payload["error_type"] == "egress_domain_denied"
        assert payload["detail"]["network_mode"] == "gateway-only"
        assert payload["detail"]["profile_id"] == "research-gateway-only"

    def test_every_declared_egress_domain_must_be_allowed(self):
        calls = 0

        async def multi_fetch(url: str) -> str:
            """Fetch through two declared outbound hosts."""
            nonlocal calls
            calls += 1
            return url

        tool = _make_tool(
            multi_fetch,
            origin=ToolOrigin.SEARCH,
            egress_urls=lambda args: [
                args["url"],
                "https://blocked.example/secondary",
            ],
        )
        cfg = _egress_config()
        cfg["metadata"]["sandbox_gateway_authorized_hosts"] = ["example.com"]

        msg = asyncio.run(
            execute_governed_tool_call(
                {
                    "name": tool.name,
                    "id": "multi-egress",
                    "args": {"url": "https://example.com/primary"},
                },
                {tool.name: tool},
                AgentRole.RESEARCHER,
                cfg,
                apply_retry=False,
            )
        )

        payload = json.loads(msg.content)
        assert payload["error_type"] == "egress_domain_denied"
        assert payload["detail"]["domains"] == ["blocked.example"]
        assert calls == 0

    def test_disabled_sandbox_skips_egress_gate(self):
        tool = _fetch_tool()
        cfg = dict(configurable={}, metadata={"run_id": "plain"})
        msg = asyncio.run(
            execute_governed_tool_call(
                {"name": "fetch_webpage", "id": "tc1", "args": {"url": "https://untrusted.example/x"}},
                {"fetch_webpage": tool},
                AgentRole.RESEARCHER,
                cfg,
                apply_retry=False,
            )
        )
        assert msg.content == "fetched:https://untrusted.example/x"

    def test_search_tool_without_url_is_delegated_to_gateway(self):
        # tavily_search is SEARCH origin with no url arg -> _egress_host_for_tool
        # returns None -> no egress check, tool would proceed (we only assert no
        # egress error is produced for a SEARCH tool targeting an unknown host).
        cfg = _egress_config()
        # Use check_egress_domain directly with a SEARCH-origin tool.
        from open_deep_research.tools.governance import check_egress_domain

        result = asyncio.run(
            check_egress_domain(
                {"id": "tc"},
                tavily_search,
                {"queries": ["x"]},
                cfg,
            )
        )
        assert result is None  # SEARCH tool -> no egress interception




@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["budget", "deadline", "fence", "unknown", "cancel"])
@pytest.mark.parametrize("retryable", [False, True])
async def test_runtime_controls_propagate_without_retry(control, retryable):
    from open_deep_research.agentscope_runtime.recovery_store import (
        FenceLost,
        UnknownOperation,
    )
    from open_deep_research.budgets import (
        BudgetDimension,
        BudgetExhausted,
        DeadlineExceeded,
    )

    error = {
        "budget": BudgetExhausted(BudgetDimension.TOOL_CALLS),
        "deadline": DeadlineExceeded("fixture deadline"),
        "fence": FenceLost("fixture fence"),
        "unknown": UnknownOperation("fixture unknown outcome"),
        "cancel": asyncio.CancelledError("fixture cancellation"),
    }[control]
    calls = []

    async def interrupted():
        calls.append(1)
        raise error

    tool = _make_tool(
        interrupted, origin=ToolOrigin.SYSTEM, effect=ToolEffect.READ_ONLY,
        retryable=retryable,
    )
    sleeper = AsyncMock()
    with pytest.raises(type(error)) as raised:
        await _execute_governed_tool_call(
            {"name": tool.name, "id": "control-fixture-call", "args": {}},
            {tool.name: tool},
            AgentRole.SUPERVISOR,
            _config(event_log_enabled=False, sqlite_observability_enabled=False),
            max_retries=3,
            sleeper=sleeper,
        )
    assert raised.value is error
    assert calls == [1]
    sleeper.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["budget", "deadline", "fence", "unknown"])
async def test_retry_helper_preserves_runtime_control_exception(control):
    from open_deep_research.agentscope_runtime.recovery_store import (
        FenceLost,
        UnknownOperation,
    )
    from open_deep_research.budgets import (
        BudgetDimension,
        BudgetExhausted,
        DeadlineExceeded,
    )

    error = {
        "budget": BudgetExhausted(BudgetDimension.TOOL_CALLS),
        "deadline": DeadlineExceeded("fixture deadline"),
        "fence": FenceLost("fixture fence"),
        "unknown": UnknownOperation("fixture unknown outcome"),
    }[control]
    calls = []

    async def interrupted():
        calls.append(1)
        raise error

    tool = _make_tool(interrupted, origin=ToolOrigin.SYSTEM, retryable=True)
    sleeper = AsyncMock()
    with pytest.raises(type(error)) as raised:
        await _invoke_tool_with_retry(
            tool,
            tool.input_schema.model_validate({}),
            ToolContext(config=_config(), role="supervisor", tool_call_id="fixture-call"),
            max_retries=3,
            sleeper=sleeper,
        )
    assert raised.value is error
    assert calls == [1]
    sleeper.assert_not_awaited()

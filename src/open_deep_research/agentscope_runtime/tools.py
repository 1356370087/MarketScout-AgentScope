"""AgentScope 2.0.8 tool transport over the shared project governance core.

Only Toolkit owns the executable catalog. Business policies stay in tools.governance;
the adapter binds trusted call identity, execution location and concurrency scope.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from open_deep_research.configuration import Configuration
from open_deep_research.tools.base import (
    Tool,
    ToolContext,
    ToolEffect,
    ToolExecutionZone,
    ToolResult,
    tool_to_model_definition,
)
from open_deep_research.tools.governance import (
    AgentRole,
    check_permission,
    execute_governed_tool_call_native,
    resolve_allowed_tools,
)
from open_deep_research.tools.registry import prepare_existing_toolset

ConfigProvider = Callable[[], dict[str, Any]]
Dispatcher = Callable[[Tool, Any, ToolContext], Awaitable[ToolResult[Any]]]


@dataclass(frozen=True)
class _CallIdentity:
    name: str
    call_id: str
    operation_id: str


class _ExecutionGate:
    """Safe calls share a slot; unsafe calls exclude every other call."""

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @asynccontextmanager
    async def enter(self, safe: bool):
        async with self.condition:
            if safe:
                await self.condition.wait_for(
                    lambda: not self.writer and not self.waiting_writers,
                )
                self.readers += 1
            else:
                self.waiting_writers += 1
                try:
                    await self.condition.wait_for(
                        lambda: not self.writer and not self.readers
                    )
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
                    self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                if safe:
                    self.readers -= 1
                else:
                    self.writer = False
                self.condition.notify_all()


class _CallTrace:
    """Collect governance observations in native tool metadata for tracing sinks."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def active_span(self):
        return self

    def record_outcome(self, **fields):
        self.events.append({"type": "outcome", **fields})

    def record_retry(self, **fields):
        self.events.append({"type": "retry", **fields})

    def score(self, name, value, reason):
        self.events.append(
            {"type": "score", "name": name, "value": value, "reason": reason}
        )


class _DispatchedTool:
    """Keep domain metadata while routing execution to the trusted zone dispatcher."""

    remote_execution = True

    def __init__(self, tool: Tool, dispatcher: Dispatcher) -> None:
        self.tool = tool
        self.dispatcher = dispatcher

    def __getattr__(self, name):
        return getattr(self.tool, name)

    async def call(self, input, context, on_progress=None):
        return await self.dispatcher(self.tool, input, context)


class GovernedTool(ToolBase):
    """Native tool with mandatory governance even when called directly."""

    def __init__(self, tool: Tool, definition: dict, owner: GovernedToolkit) -> None:
        super().__init__()
        self.domain_tool = tool
        self.owner = owner
        self.name = tool.name
        self.description = definition["description"]
        self.input_schema = definition["parameters"]
        self.is_concurrency_safe = tool.concurrency_safe
        self.is_read_only = tool.effect is ToolEffect.READ_ONLY
        # Gateway tools remain callable adapters. AgentScope's external-tool event
        # path would skip this boundary and therefore must not be enabled here.
        self.is_external_tool = False

    def _permission_error(self, config: dict):
        if not self.domain_tool.is_enabled(config):
            return "Tool is disabled in the current run configuration."
        allowed = resolve_allowed_tools(self.owner.role, config, {self.name})
        error = check_permission(
            self.name, self.domain_tool, self.owner.role, allowed, config
        )
        return error.message if error else None

    async def check_permissions(self, tool_input, context):
        error = self._permission_error(self.owner.config_provider())
        return PermissionDecision(
            behavior=PermissionBehavior.DENY if error else PermissionBehavior.ALLOW,
            message=error
            or "Project tool policy permits this tool; execution checks still apply.",
        )

    @staticmethod
    def _denied(code: str, message: str) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(text=message)],
            state=ToolResultState.DENIED,
            metadata={"error_type": code},
        )

    async def call(self, **kwargs) -> ToolChunk:
        import time

        from open_deep_research.events.task_activity import publish_task_activity


        queued_at = time.monotonic()
        identity = self.owner.identity.get()
        if identity is None or identity.name != self.name:
            return self._denied(
                "missing_call_context", "Use the governed Toolkit call boundary."
            )
        recorder = self.owner.config_provider().get("_evaluation_recorder")
        if recorder is not None:
            await recorder.request(identity.operation_id, self.owner.task_id, self.owner.role.value,
                                   self.domain_tool, identity.call_id, kwargs)
        async with self.owner.gate.enter(self.is_concurrency_safe):
            # Resolve after waiting for the execution slot so revocation cannot be
            # bypassed by calls queued under an older authorization snapshot.
            config = self.owner.config_provider()
            error = self._permission_error(config)
            if error:
                return self._denied("permission_denied", error)
            tool = self.domain_tool
            remote = tool.execution_zone not in self.owner.local_zones
            if remote:
                if self.owner.dispatcher is None:
                    return self._denied(
                        "execution_zone_denied",
                        "No dispatcher for this execution zone.",
                    )
                tool = _DispatchedTool(tool, self.owner.dispatcher)
            trace = _CallTrace()
            started_at = time.monotonic()
            await publish_task_activity(config, "tool.started", task_id=self.owner.task_id,
                kind="tool", phase="tool_execution", status="running", title=self.name,
                summary="开始执行工具", iteration=None, duration_ms=None,
                payload={"tool_call_id": identity.call_id, "tool_name": self.name,
                         "tool_category": self.domain_tool.origin.value, "args_keys": sorted(kwargs)},
                dedupe_key=f"native-tool:{identity.operation_id}:started", update_run_summary=self.owner.task_id not in {"supervisor", "pipeline"})
            if self.owner.journal:
                config = self.owner.journal.tool_config(config, self.name, identity.call_id, kwargs)
            async def execute():
                return await execute_governed_tool_call_native(
                    {"name": self.name, "id": identity.call_id, "args": kwargs},
                    {self.name: tool},
                    self.owner.role,
                    config,
                    allowed_tools=resolve_allowed_tools(
                        self.owner.role, config, {self.name}
                    ),
                    operation_id=identity.operation_id,
                    apply_retry=not remote,
                    max_retries=self.owner.max_retries,
                    base_delay=self.owner.retry_delay,
                    recorder=trace,
                )
            result = (
                await self.owner.journal.tool(
                    tool, identity.call_id, kwargs, execute, bill=not remote
                )
                if self.owner.journal
                else await execute()
            )
            duration = max(0, int((time.monotonic() - started_at) * 1000))
            tool_metadata = result.result.metadata or {} if result.result else {}
            await publish_task_activity(config, "tool.failed" if result.error else "tool.completed",
                task_id=self.owner.task_id, kind="tool", phase="tool_execution",
                status="error" if result.error else "success", title=self.name, duration_ms=duration,
                summary="工具调用未成功" if result.error else "工具执行完成", iteration=None,
                payload={"tool_call_id": identity.call_id, "tool_name": self.name,
                         "tool_category": self.domain_tool.origin.value,
                         "error_code": result.error.error_type.value if result.error else "",
                         "queue_ms": max(0, int((started_at - queued_at) * 1000)),
                         "execution_ms": tool_metadata.get("execution_ms", duration),
                         "approval_wait_ms": tool_metadata.get("approval_wait_ms", 0)},
                dedupe_key=f"native-tool:{identity.operation_id}:completed", update_run_summary=self.owner.task_id not in {"supervisor", "pipeline"})
            if self.owner.result_observer is not None:
                await self.owner.result_observer(self.name, identity.call_id, result)
            error_type = result.error.error_type.value if result.error else None
            if recorder is not None:
                await recorder.outcome(identity.operation_id, self.owner.task_id, identity.call_id,
                                       error_type=error_type, output=result.result.output if result.result else None)
            denied = error_type in {
                "permission_denied",
                "egress_domain_denied",
                "sensitive_tool_approval_required",
            }
            state = (
                ToolResultState.DENIED
                if denied
                else ToolResultState.ERROR
                if result.error
                else ToolResultState.SUCCESS
            )
            return ToolChunk(
                content=[TextBlock(text=result.message.content)],
                state=state,
                metadata={
                    "tool_call_id": identity.call_id,
                    "operation_id": identity.operation_id,
                    "execution_zone": tool.execution_zone.value,
                    "effect": tool.effect.value,
                    "error_type": error_type,
                    "governance": trace.events,
                },
            )


class GovernedToolkit(Toolkit):
    """Bind framework call IDs without exposing trusted metadata as model input."""

    def __init__(
        self,
        *,
        role: AgentRole,
        config_provider: ConfigProvider,
        run_id: str,
        task_id: str,
        local_zones: frozenset[ToolExecutionZone],
        dispatcher: Dispatcher | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        super().__init__()
        self.role = role
        self.config_provider = config_provider
        self.run_id = run_id
        self.task_id = task_id
        self.local_zones = local_zones
        self.dispatcher = dispatcher
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        # Domain evidence consumers see typed results before the model-facing
        # output budget truncates them. This hook cannot bypass execution policy.
        self.result_observer = None
        self.journal = None
        self.identity: ContextVar[_CallIdentity | None] = ContextVar(
            "tool_identity", default=None
        )
        self.gate = _ExecutionGate()
        self.guidance = ""

    async def get_tool_schemas(self, groups=None) -> list[dict]:
        """Hide revoked tools and refresh descriptions before each reasoning step."""
        config = self.config_provider()
        budget = Configuration.from_runnable_config(config).max_tool_description_chars
        schemas = []
        for schema in await super().get_tool_schemas(groups):
            tool = await self.get_tool(schema["function"]["name"])
            if not isinstance(tool, GovernedTool):
                # Framework-owned helpers (structured output/context compression)
                # retain their native schemas and permission checks.
                schemas.append(schema)
            elif tool._permission_error(config) is None:
                definition = await tool_to_model_definition(
                    tool.domain_tool, max_description_chars=budget
                )
                schemas.append({"type": "function", "function": definition})
        if Configuration.from_runnable_config(config).research_efficiency_mode == "bounded":
            schemas.sort(key=lambda item: item["function"]["name"])
        return schemas

    async def get_guidance(self) -> str:
        """Render guidance from the current native catalog and authorization."""
        config = self.config_provider()
        sections = []
        for schema in await self.get_tool_schemas():
            tool = await self.get_tool(schema["function"]["name"])
            if not isinstance(tool, GovernedTool):
                continue
            prompt = tool.domain_tool.prompt(config)
            if prompt and prompt.strip():
                sections.append(prompt.strip())
        return "\n\n".join(sections)

    async def call_tool(self, tool_call, state):
        identity = _CallIdentity(
            name=tool_call.name,
            call_id=tool_call.id,
            operation_id=f"{self.run_id}:{self.task_id}:{tool_call.id}",
        )
        token = self.identity.set(identity)
        try:
            async with aclosing(super().call_tool(tool_call, state)) as stream:
                async for event in stream:
                    yield event
        finally:
            self.identity.reset(token)


class ToolGovernanceMiddleware(MiddlewareBase):
    """Apply project permission rules before native confirmation/rule resolution."""

    async def on_check_permission(self, agent, input_kwargs, next_handler):
        tool = input_kwargs["tool"]
        if isinstance(tool, GovernedTool):
            error = tool._permission_error(tool.owner.config_provider())
            if error:
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY, message=error
                )
        return await next_handler(**input_kwargs)

    async def on_system_prompt(self, agent, current_prompt):
        """Fill the explicit guidance slot from the currently allowed toolset."""
        if isinstance(agent.toolkit, GovernedToolkit):
            return current_prompt.replace(
                "{tool_guidance}", await agent.toolkit.get_guidance()
            )
        return current_prompt


async def prepare_toolkit(
    tools: Iterable[Tool],
    *,
    role: AgentRole,
    config_provider: ConfigProvider,
    run_id: str,
    task_id: str,
    local_zones: frozenset[ToolExecutionZone],
    dispatcher: Dispatcher | None = None,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> GovernedToolkit:
    """Project the canonical role assembly into one native executable catalog.

    ``config_provider`` is injected by the authenticated application, never from
    tool input. Rebuild at role/config changes to refresh schemas and guidance;
    execution always rechecks the current policy, including revocations.
    """
    assembly = await prepare_existing_toolset(tools, role, config_provider())
    toolkit = GovernedToolkit(
        role=role,
        config_provider=config_provider,
        run_id=run_id,
        task_id=task_id,
        local_zones=local_zones,
        dispatcher=dispatcher,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
    for tool, definition in zip(assembly.tools, assembly.definitions, strict=True):
        # AgentScope 2.0.8 installs/replaces these tools during reply(). Reject
        # business catalog collisions before the framework can overwrite them.
        if tool.name in {"GenerateStructuredOutput", "CompressContext"}:
            raise ValueError(f"Reserved AgentScope tool name: {tool.name}")
        await toolkit.add_tool(GovernedTool(tool, definition, toolkit))
    toolkit.guidance = assembly.guidance
    return toolkit

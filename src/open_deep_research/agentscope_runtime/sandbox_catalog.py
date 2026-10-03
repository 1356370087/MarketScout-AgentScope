"""Request-scoped native Gateway tools and model resources."""

from contextvars import ContextVar
from functools import wraps

import httpx

from open_deep_research.agentscope_runtime.gateway import SandboxServiceBinding
from open_deep_research.agentscope_runtime.models import ModelFactory
from open_deep_research.agentscope_runtime.run_config import RunConfig

_active = ContextVar("native_gateway_tools")


async def assembled_tools(role, config):
    return [tool for tool in _active.get() if tool.is_enabled(config)]


def native_tools_scope(method):
    """Own MCP connections and nested native models for one authorized RPC."""

    @wraps(method)
    async def scoped(runtime, request, context, *args, **kwargs):
        from open_deep_research.agentscope_runtime.mcp import (
            close_native_mcp_tools,
            load_native_browser_mcp_tools,
            load_native_mcp_tools,
        )
        from open_deep_research.agentscope_runtime.search import search_provider_tools
        from open_deep_research.agentscope_runtime.web_tools import (
            WebFetchLedger,
            native_web_tools,
        )
        from open_deep_research.configuration import Configuration
        from open_deep_research.sandbox.egress_context import egress_authorizer
        from open_deep_research.sandbox.gateway import (
            approval_deadline,
            create_gateway_app,
        )
        from open_deep_research.sandbox.internal_api import EgressTargetCheckRequest
        from open_deep_research.sandbox.policy import egress_target_from_url
        from open_deep_research.sandbox.schema import resolve_profile
        from open_deep_research.sandbox.wire import (
            GatewayToolCatalogOutcomeV1,
            GatewayToolOutcomeV1,
            GatewayToolRequestV1,
        )
        from open_deep_research.security.network import validate_http_url_syntax
        from open_deep_research.tools.governance import AgentRole
        from open_deep_research.tools.read_file import read_file
        from open_deep_research.tools.search_documents import search_documents
        from open_deep_research.tools.shell_exec import shell_exec
        from open_deep_research.tools.write_file import write_file

        config = {
            **context.config,
            "metadata": {
                **context.config.get("metadata", {}),
                "run_id": request.run_id,
                "task_id": request.task_id,
            },
        }
        _, _, profile = resolve_profile(Configuration.from_runnable_config(config))
        discovery_grants = {}
        pending_approval = None

        async def authorize_discovery(url, capability, consume=False):
            nonlocal pending_approval
            validate_http_url_syntax(url)
            target = egress_target_from_url(url)
            if target is None:
                return "deny"
            host, port = target
            discovery = GatewayToolRequestV1(
                run_id=request.run_id, task_id=request.task_id, role=request.role,
                stage=request.stage, execution_zone="gateway", tool_name="mcp.discovery",
                arguments={"url": url}, tool_call_id="mcp-discovery",
                logical_operation_id=f"mcp-discovery:{request.task_id}:{host}:{port}",
            )

            async def check():
                return await runtime._egress_precheck(
                    run_id=request.run_id, task_id=request.task_id, fence_token=context.fence_token,
                    stage=request.stage, host=host, port=port, tool_name=discovery.tool_name,
                    capability=capability, operation_id=discovery.logical_operation_id, profile=profile,
                    operation_key=runtime._network_approval_operation_key(discovery, host=host, port=port),
                )

            async def authority():
                query = runtime.internal.signed(EgressTargetCheckRequest, run_id=request.run_id,
                    fence_token=context.fence_token, capability=capability, target={"domain": host, "port": port})
                return await runtime.internal.post("/internal/sandbox/egress/target/check", query)

            precheck = await check()
            if precheck.decision != "ask":
                return precheck.decision
            state = await authority()
            key = (host, port, capability)
            if key in discovery_grants and discovery_grants[key] == state.get("version", 0):
                return "allow"
            result, approval = await runtime._request_network_approval(
                discovery, context, host=host, port=port, capability=capability, consume=False,
                trigger_reason=precheck.source,
                expires_at=approval_deadline(context, timeout_seconds=profile.resources.approval_timeout_seconds),
            )
            if result != "allowed":
                if result == "pending":
                    pending_approval = approval.approval_id
                return "ask" if result == "pending" else "deny"
            fresh = await authority()
            if fresh.get("version", 0) != state.get("version", 0) or (await check()).decision == "deny":
                return "deny"
            if consume:
                await runtime._consume_network_approval(discovery, context, approval)
                discovery_grants[key] = fresh.get("version", 0)
            return "allow"
        # 物理网关关闭本地用量写入，覆盖的开关不属于原始冻结契约。
        # 模型工厂必须验证控制面签名的契约，而非这些进程内覆盖值。
        run = RunConfig.compile(context.frozen_config or config)
        factory = ModelFactory(run, scope="run", owner=request.run_id, bindings={})
        tools = []
        if runtime._native_model_app is None:
            runtime._native_model_app = create_gateway_app(runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(runtime._native_model_app),
            base_url="http://gateway-internal",
        ) as client:

            class ToolModels:
                def policy_middleware(self, role):
                    binding = SandboxServiceBinding(
                        "http://gateway-internal",
                        request.run_id,
                        request.task_id,
                        role,
                        request.stage,
                        context.fence_token,
                        runtime.keys.service_auth,
                    )
                    model = factory.build_sandbox(role, binding, client=client)
                    return factory.policy_middleware(role, candidates=[model])

            try:
                models = ToolModels()
                tools = [
                    *native_web_tools(
                        lambda: config,
                        models,
                        runtime.native_fetch_ledgers.setdefault(
                            request.run_id, WebFetchLedger()
                        ),
                    ),
                    *search_provider_tools(lambda: config, models),
                    search_documents,
                    read_file,
                    write_file,
                    shell_exec,
                ]
                discovery_token = egress_authorizer.set(authorize_discovery)
                try:
                    tools.extend(await load_native_mcp_tools(config, {t.name for t in tools}, role=AgentRole(request.role)))
                    tools.extend(await load_native_browser_mcp_tools(config, {t.name for t in tools}, role=AgentRole(request.role)))
                finally:
                    egress_authorizer.reset(discovery_token)
                if pending_approval:
                    if isinstance(request, GatewayToolRequestV1):
                        return GatewayToolOutcomeV1(
                            logical_operation_id=request.logical_operation_id,
                            tool_call_id=request.tool_call_id, status="approval_required",
                            approval_id=pending_approval,
                            error={"error_type": "mcp_discovery_approval_required",
                                   "message": "MCP discovery awaits network approval."},
                        )
                    return GatewayToolCatalogOutcomeV1(status="approval_required", approval_id=pending_approval)
                token = _active.set(tools)
                try:
                    return await method(runtime, request, context, *args, **kwargs)
                finally:
                    _active.reset(token)
            finally:
                await close_native_mcp_tools(tools)
                await factory.aclose()

    return scoped

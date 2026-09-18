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
        from open_deep_research.sandbox.gateway import create_gateway_app
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
                tools.extend(
                    await load_native_mcp_tools(config, {t.name for t in tools})
                )
                tools.extend(
                    await load_native_browser_mcp_tools(config, {t.name for t in tools})
                )
                token = _active.set(tools)
                try:
                    return await method(runtime, request, context, *args, **kwargs)
                finally:
                    _active.reset(token)
            finally:
                await close_native_mcp_tools(tools)
                await factory.aclose()

    return scoped

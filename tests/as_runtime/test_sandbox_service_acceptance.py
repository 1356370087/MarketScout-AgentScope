"""V2 客户端接真实 FastAPI 路由及鉴权；模型执行使用隔离结果夹具。"""

import base64
import time
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr
from agentscope.message import UserMsg

from open_deep_research.configuration import Configuration
from open_deep_research.agentscope_runtime.gateway import (
    SandboxChatModel,
    SandboxBinding,
    SandboxServiceBinding,
    GatewayCallError,
)
from open_deep_research.sandbox.gateway import (
    GatewayRuntime,
    GatewayRunContext,
    create_gateway_app,
)
from open_deep_research.sandbox.crypto import encode_task_token
from open_deep_research.sandbox.wire import TaskTokenClaimsV1, GatewayModelOutcomeV2

pytestmark = pytest.mark.asyncio


async def test_registered_catalog_preserves_signed_fingerprint_with_physical_overrides():
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.sandbox.crypto import sign_payload
    from open_deep_research.sandbox.gateway import GatewayRunRegistrationRequest
    from open_deep_research.sandbox.wire import GatewayToolCatalogRequestV1
    from security.rbac.dependencies import apply_principal_to_config
    from security.rbac.principal import synthetic_dev_principal

    run = RunConfig.compile({"configurable": {
        "token_usage_accounting_enabled": True, "search_api": "tavily",
        "web_pipeline_mode": "enforced", "enable_memory": False,
    }})
    frozen = apply_principal_to_config(run.compatibility_projection(), synthetic_dev_principal())
    runtime = GatewayRuntime(Configuration(
        sandbox_root_signing_key=base64.b64encode(b"fixture-root-key-32-bytes-long!!!x").decode(),
    ))
    request = GatewayRunRegistrationRequest(
        run_id="run", fence_token=7, frozen_config=frozen, api_keys={},
        expires_at=time.time() + 60, service_timestamp=time.time(),
        service_nonce="registered-catalog-native", service_signature="pending",
    )
    request.service_signature = sign_payload(request.signed_payload(), runtime.keys.service_auth)
    runtime.register(request)
    context = runtime.runs["run"]
    assert context.config["configurable"]["token_usage_accounting_enabled"] is False
    outcome = await runtime.tool_catalog(GatewayToolCatalogRequestV1(
        run_id="run", task_id="task", role="researcher",
    ), context)
    assert {"web_research", "fetch_url"}.issubset({tool.name for tool in outcome.tools})
    assert context.frozen_config["metadata"]["run_config_fingerprint"] == run.compatibility_projection()["metadata"]["run_config_fingerprint"]


@pytest.mark.parametrize("route,method", [
    ("catalog", "tool_catalog"),
    ("call", "invoke_tool"),
    ("authorize-local", "authorize_local_tool"),
])
async def test_tool_http_routes_authenticate_before_runtime_scope(route, method):
    from open_deep_research.sandbox.wire import (
        GatewayToolCatalogOutcomeV1, GatewayToolOutcomeV1,
    )
    runtime = GatewayRuntime(Configuration(
        sandbox_root_signing_key=base64.b64encode(b"fixture-root-key-32-bytes-long!!!x").decode(),
    ))
    runtime.runs["run"] = GatewayRunContext({}, 7, time.time() + 300)
    dispatched = []

    async def invoke(request, context):
        dispatched.append(request.task_id)
        if route == "catalog":
            return GatewayToolCatalogOutcomeV1(tools=[])
        return GatewayToolOutcomeV1(logical_operation_id="op", tool_call_id="call", status="completed")

    setattr(runtime, method, invoke)
    request = {"run_id": "run", "task_id": "task", "role": "researcher", "stage": "researching"}
    if route != "catalog":
        request.update(execution_zone="gateway", logical_operation_id="op", tool_call_id="call", tool_name="fetch_url", arguments={})
    token = encode_task_token(TaskTokenClaimsV1(
        run_id="run", task_id="task", fence_token=7, profile_id="test", policy_digest="fixture",
        issued_at=time.time() - 1, expires_at=time.time() + 60, jti="fixture-tool",
    ), runtime.keys.task_token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_gateway_app(runtime)), base_url="http://fixture") as client:
        headers = {"X-Sandbox-Timestamp": str(time.time()), "X-Sandbox-Nonce": "unauthenticated-tool-route"}
        denied = await client.post(f"/v1/tools/{route}", json=request, headers=headers)
        assert denied.status_code == 401 and not dispatched
        headers.update(Authorization=f"Bearer {token}", **{"X-Sandbox-Nonce": "authenticated-tool-route"})
        response = await client.post(f"/v1/tools/{route}", json=request, headers=headers)
        assert response.status_code == 200, response.text
        assert dispatched == ["task"]


@pytest.mark.parametrize(
    "case",
    ["service", "task", "stale-fence", "bad-signature", "expired-task", "wrong-task"],
)
async def test_v2_real_route_authorization(case, monkeypatch):
    # SDK 客户端仅用于路由构造，本测试不连接真实内部 API。
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        monkeypatch.delenv(key, raising=False)
    runtime = GatewayRuntime(
        Configuration(
            sandbox_root_signing_key=base64.b64encode(
                b"fixture-root-key-32-bytes-long!!!x"
            ).decode()
        )
    )
    runtime.runs["run"] = GatewayRunContext({}, 7, time.time() + 300)
    dispatched = []

    async def invoke(request, context):
        dispatched.append(request.logical_operation_id)
        return GatewayModelOutcomeV2(
            logical_operation_id=request.logical_operation_id,
            requested_model=request.model,
            status="completed",
            message={"role": "assistant", "content": "ok"},
            finish_reason="stop",
        )

    runtime.invoke_model_operation_v2 = invoke
    binding = SandboxServiceBinding(
        "https://fixture.invalid",
        "run",
        "task",
        "researcher",
        "researching",
        7,
        runtime.keys.service_auth,
    )
    if case == "stale-fence":
        binding = replace(binding, fence_token=6)
    elif case == "bad-signature":
        binding = replace(binding, service_key=b"wrong")
    elif case in {"task", "expired-task", "wrong-task"}:
        claims = TaskTokenClaimsV1(
            run_id="run",
            task_id="wrong" if case == "wrong-task" else "task",
            fence_token=7,
            profile_id="test",
            policy_digest="fixture",
            issued_at=time.time() - 60,
            expires_at=time.time() + (-1 if case == "expired-task" else 60),
            jti="fixture-jti",
        )
        binding = SandboxBinding(
            binding.url,
            "run",
            "task",
            "researcher",
            "researching",
            SecretStr(encode_task_token(claims, runtime.keys.task_token)),
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_gateway_app(runtime)),
            base_url=binding.url,
        ) as client:
            model = SandboxChatModel(
                binding=binding, model="test", stream=False, client=client
            )
            if case in {"service", "task"}:
                result = await model([UserMsg("u", "q")])
                assert result.content[0].text == "ok" and len(dispatched) == 1
            else:
                with pytest.raises(GatewayCallError) as caught:
                    await model([UserMsg("u", "q")])
                assert caught.value.status_code == 401
                assert dispatched == []
    finally:
        runtime.runs.clear()

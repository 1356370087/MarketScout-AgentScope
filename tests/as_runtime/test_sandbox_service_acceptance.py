"""V2 客户端接真实 FastAPI 路由及鉴权；模型执行使用隔离结果夹具。"""

import base64
import time
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr
from agentscope.message import UserMsg

pytest.importorskip(
    "langchain_core",
    reason="既有 Gateway 服务尚依赖旧环境；使用 .venv-legacy 依赖补充执行本协议测试",
)
from open_deep_research.configuration import Configuration
from open_deep_research.as_runtime.gateway import (
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

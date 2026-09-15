"""显式 Docker 联调：真实沙箱路由、业务预算/操作账本及 LiteLLM。

新解释器运行；追加旧依赖仅用于尚未清退的 Sandbox 服务测试。
创建独立临时容器、短期模型密钥和 .runs 验收目录，finally 关闭并撤销。
"""

import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
import uuid

sys.path.append(str(Path(".venv-legacy/Lib/site-packages").resolve()))

import docker
import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr
from agentscope.message import UserMsg
from open_deep_research.agentscope_runtime.gateway import (
    SandboxChatModel,
    SandboxServiceBinding,
    SandboxBinding,
    GatewayCallError,
)
from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.crypto import SandboxDerivedKeys, encode_task_token
from open_deep_research.sandbox.gateway import GatewayRunRegistrationRequest
from open_deep_research.sandbox.internal_api import (
    InternalRunContext,
    SandboxInternalClient,
    build_internal_sandbox_router,
)
from open_deep_research.sandbox.wire import TaskTokenClaimsV1
from open_deep_research.sandbox.operations import ModelOperationStore


class Answer(BaseModel):
    value: int


async def main():
    config = dotenv_values(".env")
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        os.environ.pop(name, None)
    root = base64.b64encode(secrets.token_bytes(32)).decode()
    keys = SandboxDerivedKeys.from_root(root)
    run_id = "as-acceptance-" + uuid.uuid4().hex
    runs_dir = str(Path(".runs/as-docker-acceptance").resolve())
    route = config["LITELLM_SUMMARIZATION_MODEL"]
    values = dict(
        sandbox_root_signing_key=root,
        runs_dir=runs_dir,
        model_backend="litellm",
        max_run_model_calls=3,
        token_usage_accounting_enabled=False,
        sqlite_observability_enabled=False,
        event_log_enabled=False,
    )
    settings = Configuration(**values)
    frozen = {"configurable": settings.model_dump(mode="json"), "metadata": {}}
    context = InternalRunContext(frozen, settings, 1, time.time())
    app = FastAPI()
    app.include_router(
        build_internal_sandbox_router(lambda rid: context if rid == run_id else None)
    )
    sock = socket.socket()
    sock.bind(("0.0.0.0", 0))
    api_port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.05)
    engine = docker.from_env()
    container = None
    run_key = None
    output = {"run_id": run_id, "checks": [], "cleanup": {}}
    proxy = config["LITELLM_BASE_URL"].rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
        try:
            created = await client.post(
                proxy + "/key/generate",
                headers={"Authorization": "Bearer " + config["LITELLM_MASTER_KEY"]},
                json={
                    "models": [route],
                    "duration": "10m",
                    "max_budget": 0.05,
                    "key_alias": run_id,
                },
            )
            created.raise_for_status()
            run_key = created.json()["key"]
            container = await asyncio.to_thread(
                engine.containers.run,
                "insight_forge-sandbox-gateway",
                detach=True,
                name=run_id,
                network="insight_forge_default",
                ports={"8081/tcp": ("127.0.0.1", None)},
                environment={
                    "SANDBOX_ROOT_SIGNING_KEY": root,
                    "SANDBOX_ENABLED": "true",
                    "ENABLE_ASYNC_RESEARCH": "true",
                    "SANDBOX_GATEWAY_PORT": "8081",
                    "SANDBOX_GATEWAY_PROXY_PORT": "8080",
                    "SANDBOX_API_INTERNAL_URL": f"http://host.docker.internal:{api_port}",
                    "LITELLM_BASE_URL": "http://litellm-proxy:4000/v1",
                    "SANDBOX_GATEWAY_PHYSICAL_PROCESS": "true",
                    "PYTHONPATH": "/app/src",
                },
                volumes={
                    str(Path("src").resolve()): {"bind": "/app/src", "mode": "ro"}
                },
                labels={"insightforge.acceptance": "T020-T022"},
                mem_limit="1g",
            )
            await asyncio.to_thread(container.reload)
            port = container.attrs["NetworkSettings"]["Ports"]["8081/tcp"][0][
                "HostPort"
            ]
            url = "http://127.0.0.1:" + port
            for _ in range(60):
                await asyncio.to_thread(container.reload)
                if container.status == "exited":
                    raise RuntimeError("sandbox_container_exited")
                try:
                    if (await client.get(url + "/healthz", timeout=2)).is_success:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("sandbox_container_not_ready")
            internal = SandboxInternalClient(url, root)
            registration = internal.signed(
                GatewayRunRegistrationRequest,
                run_id=run_id,
                fence_token=1,
                frozen_config=frozen,
                api_keys={"LITELLM_RUN_KEY": run_key},
                expires_at=time.time() + 300,
            )
            registered = await client.post(
                url + "/internal/v1/runs/register",
                json=registration.model_dump(mode="json"),
            )
            registered.raise_for_status()
            binding = SandboxServiceBinding(
                url,
                run_id,
                "acceptance-task",
                "summarization",
                "researching",
                1,
                keys.service_auth,
            )
            async with httpx.AsyncClient(
                base_url=url, trust_env=False, timeout=90
            ) as gateway_client:
                model = SandboxChatModel(
                    binding=binding,
                    model=route,
                    stream=False,
                    client=gateway_client,
                    parameters=SandboxChatModel.Parameters(max_tokens=256),
                )
                result = await model(
                    [UserMsg("user", "Reply OK only.")], logical_operation_id="plain"
                )
                replay = await model(
                    [UserMsg("user", "Reply OK only.")], logical_operation_id="plain"
                )
                record = ModelOperationStore(run_id, runs_dir=runs_dir).get("plain")
                output["checks"].append(
                    {
                        "name": "service_plain_and_replay",
                        "passed": result.metadata["request_id"]
                        == replay.metadata["request_id"]
                        and len(record.physical_attempts) == 1,
                        "finish_reason": result.metadata["provider_finish_reason"],
                        "usage_present": result.usage is not None,
                    }
                )
                claims = TaskTokenClaimsV1(
                    run_id=run_id,
                    task_id="acceptance-task",
                    fence_token=1,
                    profile_id="acceptance",
                    policy_digest="acceptance",
                    issued_at=time.time() - 1,
                    expires_at=time.time() + 120,
                    jti=uuid.uuid4().hex,
                )
                task_binding = SandboxBinding(
                    url,
                    run_id,
                    "acceptance-task",
                    "summarization",
                    "researching",
                    SecretStr(encode_task_token(claims, keys.task_token)),
                )
                task_model = SandboxChatModel(
                    binding=task_binding,
                    model=route,
                    stream=True,
                    client=gateway_client,
                    parameters=SandboxChatModel.Parameters(max_tokens=256),
                )
                stream = await task_model(
                    [UserMsg("user", "Reply OK only.")], logical_operation_id="stream"
                )
                chunks = [chunk async for chunk in stream]
                output["checks"].append(
                    {
                        "name": "task_complete_result_stream",
                        "passed": len(chunks) == 1 and chunks[-1].is_last,
                        "finish_reason": chunks[-1].metadata["provider_finish_reason"],
                    }
                )
                structured = await model.generate_structured_output(
                    [UserMsg("user", "Return value equal to 1.")],
                    Answer,
                    logical_operation_id="structured",
                )
                output["checks"].append(
                    {"name": "structured", "passed": structured.content == {"value": 1}}
                )
                # 三次请求预算已用完；第四次应在业务预算边界被拒绝。
                try:
                    await model(
                        [UserMsg("user", "Reply OK only.")],
                        logical_operation_id="budget-denied",
                    )
                    denied = False
                except GatewayCallError:
                    denied = True
                output["checks"].append(
                    {"name": "business_budget_rejection", "passed": denied}
                )
                ledger_file = next(
                    (Path(runs_dir) / run_id).rglob("budget_ledger.json")
                )
                ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
                output["checks"][-1].update(
                    passed=denied
                    and ledger["exhausted"] == "model_calls"
                    and len(ledger["reservations"]) == 3
                    and ModelOperationStore(run_id, runs_dir=runs_dir).get(
                        "budget-denied"
                    )
                    is None,
                    exhausted=ledger["exhausted"],
                    reservations=len(ledger["reservations"]),
                )
                expired = claims.model_copy(
                    update={
                        "issued_at": time.time() - 120,
                        "expires_at": time.time() - 1,
                    }
                )
                bad_model = SandboxChatModel(
                    binding=SandboxBinding(
                        url,
                        run_id,
                        "acceptance-task",
                        "summarization",
                        "researching",
                        SecretStr(encode_task_token(expired, keys.task_token)),
                    ),
                    model=route,
                    client=gateway_client,
                )
                try:
                    await bad_model([UserMsg("user", "q")])
                    rejected = False
                except GatewayCallError as error:
                    rejected = error.status_code == 401
                output["checks"].append(
                    {"name": "expired_task_token", "passed": rejected}
                )
        except Exception as error:
            output["error_type"] = type(error).__name__
            output["error_code"] = str(error) if type(error) is RuntimeError else None
        finally:
            if run_key:
                deleted = await client.post(
                    proxy + "/key/delete",
                    headers={"Authorization": "Bearer " + config["LITELLM_MASTER_KEY"]},
                    json={"keys": [run_key]},
                )
                output["cleanup"]["run_key_revoked"] = deleted.is_success
            if container is not None:
                await asyncio.to_thread(container.remove, force=True)
                output["cleanup"]["sandbox_container_removed"] = True
            server.should_exit = True
            await server_task
            sock.close()
            engine.close()
            output["cleanup"]["authority_stopped"] = True
    return output


if __name__ == "__main__":
    result = asyncio.run(main())
    Path(
        "docs/agentscope-migration/implementation/m3-sandbox-docker-live.json"
    ).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(
        int("error_type" in result or any(not c["passed"] for c in result["checks"]))
    )

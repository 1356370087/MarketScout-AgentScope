"""显式运行的 LiteLLM 联调探针；读取 .env，输出仅包含脱敏验收结果。

运行：PYTHONPATH=src .venv/Scripts/python tests/as_runtime/probe_litellm_live.py
只调用 /models 及三个小输出请求，不创建/修改网关密钥或预算。
"""

import asyncio
import json
import os
from pathlib import Path

import httpx2
from dotenv import dotenv_values
from pydantic import BaseModel
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from open_deep_research.agentscope_runtime.gateway import LiteLLMChatModel
from open_deep_research.agentscope_runtime.model_policy import recover_output, ModelCallPolicy


class Answer(BaseModel):
    value: int


async def main():
    selected = set(
        os.environ.get(
            "AS_LIVE_CHECKS", "plain,stream,structured,recovery,disconnect"
        ).split(",")
    )
    config = dotenv_values(".env")
    base = config.get("LITELLM_BASE_URL", "").rstrip("/")
    key = config.get("LITELLM_SERVICE_KEY")
    route = config.get("LITELLM_SUMMARIZATION_MODEL", "if-summarization-v1")
    result = {"source": ".env", "route": route, "checks": [], "real_model_calls": 0}

    # asyncio.run 在 main 返回后关闭遗留生成器；保留退出阶段错误，避免只看
    # HTTP 结果就把偶发清理错误漏记为通过。回调仅用于本独立探针进程。
    def capture_loop_error(loop, context):
        result.setdefault("async_generator_errors", []).append(
            type(context.get("exception")).__name__
        )
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(capture_loop_error)
    if not base or not key:
        result["blocker"] = "missing_gateway_configuration"
        return result
    async with httpx2.AsyncClient(trust_env=False, timeout=45) as client:
        try:
            response = await client.get(
                base + "/models", headers={"Authorization": "Bearer " + key}
            )
            result["checks"].append(
                {"name": "authorized_models", "http_status": response.status_code}
            )
            response.raise_for_status()
            if route not in {m["id"] for m in response.json().get("data", [])}:
                result["blocker"] = "configured_route_not_authorized"
                return result
        except Exception as error:
            result["blocker"] = type(error).__name__
            return result
        denied = await client.get(
            base + "/models", headers={"Authorization": "Bearer acceptance-invalid"}
        )
        result["checks"].append(
            {
                "name": "invalid_key",
                "http_status": denied.status_code,
                "passed": denied.status_code in {401, 403},
            }
        )
        for mode in [m for m in ["plain", "stream", "structured"] if m in selected]:
            model = LiteLLMChatModel(
                OpenAICredential(api_key=key, base_url=base),
                route,
                parameters=LiteLLMChatModel.Parameters(max_tokens=256),
                stream=mode == "stream",
                max_retries=0,
                client_kwargs={"max_retries": 0, "http_client": client},
            )
            try:
                result["real_model_calls"] += 1
                if mode == "structured":
                    reply = await model.generate_structured_output(
                        [UserMsg("user", "Return value equal to 1.")], Answer
                    )
                    passed = reply.content == {"value": 1}
                else:
                    reply = await model([UserMsg("user", "Reply with OK only.")])
                    if mode == "stream":
                        chunks = [chunk async for chunk in reply]
                        reply = chunks[-1]
                    passed = (
                        reply.is_last
                        and bool(reply.content)
                        and reply.metadata.get("provider_finish_reason") == "stop"
                    )
                result["checks"].append(
                    {
                        "name": mode,
                        "passed": passed,
                        "finish_reason": reply.metadata.get("provider_finish_reason"),
                        "usage_present": reply.usage is not None,
                    }
                )
            except Exception as error:
                result["checks"].append(
                    {"name": mode, "passed": False, "error_type": type(error).__name__}
                )
        model = LiteLLMChatModel(
            OpenAICredential(api_key=key, base_url=base),
            route,
            parameters=LiteLLMChatModel.Parameters(max_tokens=128),
            stream=False,
            max_retries=0,
            client_kwargs={"max_retries": 0, "http_client": client},
        )
        state = {}

        async def complete(messages, limit):
            result["real_model_calls"] += 1
            return await model(messages, max_tokens=limit)

        if "recovery" in selected:
            try:
                reply = await recover_output(
                    complete,
                    [
                        UserMsg(
                            "user",
                            "Write exactly the integers 1 through 20, separated by spaces.",
                        )
                    ],
                    requested_tokens=1,
                    maximum_tokens=512,
                    continuations=1,
                    state=state,
                )
                result["checks"].append(
                    {
                        "name": "bounded_output_recovery",
                        "passed": state["completed"]
                        and state["finish_reasons"][0] == "length"
                        and state["finish_reasons"][-1] == "stop",
                        "finish_reasons": state["finish_reasons"],
                        "attempts": len(state["attempt_usage"]),
                    }
                )
            except Exception as error:
                result["checks"].append(
                    {
                        "name": "bounded_output_recovery",
                        "passed": False,
                        "error_type": type(error).__name__,
                        "reason": str(error)
                        if type(error).__name__ == "RecoveryExhausted"
                        else None,
                    }
                )
        # 首块交给调用方后主动关闭；中间件不得开启第二次模型请求。
        model.stream = True
        attempts = []

        async def handler(current_model, messages):
            attempts.append(1)
            result["real_model_calls"] += 1
            return await current_model(messages)

        if "disconnect" in selected:
            try:
                stream = await ModelCallPolicy([model], attempts=3).invoke(
                    handler,
                    {"messages": [UserMsg("user", "Write the numbers 1 through 100.")]},
                    {},
                )
                await anext(stream)
                await stream.aclose()
                result["checks"].append(
                    {
                        "name": "stream_consumer_disconnect",
                        "passed": len(attempts) == 1,
                        "attempts": len(attempts),
                    }
                )
            except Exception as error:
                result["checks"].append(
                    {
                        "name": "stream_consumer_disconnect",
                        "passed": False,
                        "error_type": type(error).__name__,
                        "reason": str(error)
                        if type(error).__name__ == "RecoveryExhausted"
                        else None,
                    }
                )
    return result


if __name__ == "__main__":
    output = asyncio.run(main())
    path = Path("docs/agentscope-migration/implementation/m3-litellm-live.json")
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(
        1
        if output.get("blocker")
        or output.get("async_generator_errors")
        or any(not c.get("passed", True) for c in output["checks"])
        else 0
    )

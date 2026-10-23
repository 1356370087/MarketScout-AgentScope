"""Native provider executes the real AgentScope/OpenAI stack over HTTP fixtures."""

import json
import asyncio

import httpx
import pytest

from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
from open_deep_research.models.protocol_errors import ModelGatewayError
from open_deep_research.sandbox.wire import GatewayModelRequestV2


def request():
    return GatewayModelRequestV2(
        run_id="r",
        task_id="t",
        role="researcher",
        stage="researching",
        logical_operation_id="op",
        model="research",
        messages=[{"role": "user", "content": "Research"}],
        max_output_tokens=100,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_native_provider_preserves_sdk_interruption(interruption):
    started = asyncio.Event()
    calls = []

    async def handler(req):
        calls.append(req)
        started.set()
        await asyncio.Event().wait()

    provider = NativeGatewayProvider(api_key="fixture", base_url="https://fixture.invalid/v1",
                                     transport=httpx.MockTransport(handler))
    try:
        if interruption == "cancel":
            task = asyncio.create_task(provider.complete(request()))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(provider.complete(request()), 0.05)
        assert len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_native_provider_headers_tools_usage_and_close():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        assert req.headers["x-litellm-request-id"] == "op"
        assert req.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            headers={
                "x-litellm-response-cost": "0.002",
                "x-litellm-model-provider": "fixture",
                "x-litellm-model-id": "deployment",
            },
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 1,
                "model": "served",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query":"hello"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "total_tokens": 17,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
        )

    provider = NativeGatewayProvider(
        api_key="test-key",
        base_url="https://fixture.invalid/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await provider.complete(
            request().model_copy(
                update={
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "description": "Lookup",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                },
                            },
                        }
                    ],
                    "tool_choice": {"type": "function", "function": {"name": "lookup"}},
                }
            )
        )
        assert result.message["tool_calls"][0]["function"]["name"] == "lookup"
        assert result.usage == {
            "input_tokens": 12,
            "output_tokens": 5,
            "total_tokens": 17,
            "cached_input_tokens": 3,
            "reasoning_tokens": 2,
        }
        assert result.response_cost_usd == 0.002
        assert result.served_model == "served" and result.provider == "fixture"
        assert calls[0]["max_tokens"] == 100
        assert "max_completion_tokens" not in calls[0]
        assert calls[0]["metadata"]["run_id"] == "r"
    finally:
        await provider.aclose()
    assert provider.client.is_closed


@pytest.mark.asyncio
async def test_native_provider_does_not_retry_budget_rejection():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            429, json={"error": {"message": "Budget exceeded", "type": "budget"}}
        )

    provider = NativeGatewayProvider(
        api_key="test-key",
        base_url="https://fixture.invalid/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelGatewayError, match="gateway_budget_exceeded"):
            await provider.complete(request())
        assert len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_passes_logical_call_allowance_to_http_client():
    calls = []

    def handler(req):
        calls.append(req)
        assert req.extensions["timeout"]["read"] == 365
        return httpx.Response(200, json={
            "id":"response", "object":"chat.completion", "created":1, "model":"served",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"complete"}}],
            "usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3},
        })

    provider = NativeGatewayProvider(api_key="fixture",base_url="https://fixture.invalid/v1",
        transport=httpx.MockTransport(handler),request_timeout=365)
    try:
        result = await provider.complete(request())
        assert result.status == "completed" and len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_timeout", [("bounded",365),("baseline",180)])
async def test_gateway_constructs_provider_with_frozen_call_timeout(monkeypatch,mode,expected_timeout):
    import base64
    import time
    from open_deep_research.sandbox import gateway as gateway_module
    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
    from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

    for key in ("RESEARCH_EFFICIENCY_MODE","MODEL_CALL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key,raising=False)
    runtime = GatewayRuntime(Configuration(sandbox_root_signing_key=base64.b64encode(b"x"*32).decode()))
    journal, calls = {}, []

    async def post(path,body):
        if path.endswith("/get"):
            return {"found":bool(journal),"operation":journal.copy()}
        if path.endswith("/transition"):
            journal.update(status=body.status,outcome=body.outcome)
        return {}

    class Provider:
        def __init__(self,*,api_key,request_timeout):
            assert request_timeout == expected_timeout

        async def complete(self,value):
            calls.append(value.logical_operation_id)
            return GatewayModelOutcomeV2(logical_operation_id=value.logical_operation_id,status="completed",
                requested_model=value.model,message={"role":"assistant","content":"answer"},usage={"input_tokens":2,"output_tokens":1})

    runtime.internal.post = post
    monkeypatch.setattr(gateway_module,"NativeGatewayProvider",Provider)
    context = GatewayRunContext({"configurable":{"research_efficiency_mode":mode,"model_call_timeout_seconds":360}},
        1,time.time()+600,api_keys={"LITELLM_RUN_KEY":"fixture"})
    first = await runtime.invoke_model_operation_v2(request(),context)
    assert await runtime.invoke_model_operation_v2(request(),context) == first
    assert calls == ["op"]


@pytest.mark.asyncio
async def test_gateway_catalog_uses_native_tools_without_langchain():
    import base64
    import sys
    import time

    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
    from open_deep_research.sandbox.wire import GatewayToolCatalogRequestV1

    cfg = Configuration(sandbox_root_signing_key=base64.b64encode(b"x" * 32).decode())
    runtime = GatewayRuntime(cfg)
    context = GatewayRunContext(
        {
            "configurable": {
                "web_pipeline_mode": "enforced",
                "enable_async_research": False,
                "search_api": "tavily",
            },
            "metadata": {"run_id": "r"},
        },
        1,
        time.time() + 300,
    )
    result = await runtime.tool_catalog(
        GatewayToolCatalogRequestV1(
            run_id="r", task_id="t", role="researcher", stage="researching"
        ),
        context,
    )
    assert {t.name for t in result.tools} >= {"web_research", "fetch_url"}
    assert not any(k.startswith("langchain") for k in sys.modules)


@pytest.mark.asyncio
async def test_gateway_replays_native_outcome_without_second_charge():
    import base64
    import time

    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.gateway import GatewayRunContext, GatewayRuntime
    from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

    runtime = GatewayRuntime(
        Configuration(sandbox_root_signing_key=base64.b64encode(b"x" * 32).decode())
    )
    journal, posts, attempts, activities = {}, [], [], []

    async def post(path, body):
        posts.append(path)
        if path.endswith("/task-activity"):
            activities.append(body)
        if path.endswith("/get"):
            return {"found": bool(journal), "operation": journal.copy()}
        if path.endswith("/transition"):
            journal.update(status=body.status, outcome=body.outcome)
        return {}

    runtime.internal.post = post

    class Provider:
        async def complete(self, value):
            attempts.append(value.logical_operation_id)
            assert journal["status"] == "dispatched"
            return GatewayModelOutcomeV2(
                logical_operation_id=value.logical_operation_id,
                status="completed",
                requested_model=value.model,
                message={"role": "assistant", "content": "answer"},
                usage={"input_tokens": 3, "output_tokens": 4},
            )

    runtime.model_gateways["r"] = Provider()
    context = GatewayRunContext(
        {}, 1, time.time() + 300, api_keys={"LITELLM_RUN_KEY": "test"}
    )
    first = await runtime.invoke_model_operation_v2(request(), context)
    second = await runtime.invoke_model_operation_v2(request(), context)
    assert second == first and attempts == ["op"]
    assert posts.count("/internal/sandbox/budgets/reserve") == 1
    assert posts.count("/internal/sandbox/budgets/settle") == 1
    assert [event.event_type for event in activities] == ["model.started", "model.completed"]
    assert all(event.task_id == "t" and event.fence_token == 1 for event in activities)
    assert activities[-1].payload["input_tokens"] == 3
    assert "messages" not in activities[-1].payload

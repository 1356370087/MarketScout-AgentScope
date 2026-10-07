"""Native server-search streaming, credentials, wire contracts and accounting."""

import json

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from open_deep_research.agentscope_runtime.models import CredentialBinding, ModelFactory
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
from open_deep_research.agentscope_runtime.search_models import ServerSearchModel
from open_deep_research.sandbox.wire import GatewayModelRequestV2, ServerSearchRequest


def sse(events):
    return "".join(
        f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events
    )


def openai_events(model):
    response = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "A supported search summary.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://docs.example/api",
                                "title": "API",
                                "start_index": 0,
                                "end_index": 3,
                            }
                        ],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.web_search_call.searching",
            "sequence_number": 1,
            "item_id": "search-0",
            "output_index": 0,
        },
        {"type": "response.completed", "sequence_number": 2, "response": response},
    ]


def anthropic_events(model):
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "A supported search summary."},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "server_tool_use",
                "id": "search-0",
                "name": "web_search",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"query":"actual query"}',
            },
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "web_search_tool_result",
                "tool_use_id": "search-0",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://docs.example/api",
                        "title": "API",
                        "encrypted_content": "fixture",
                    }
                ],
            },
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_server_search_port_preserves_citations_usage_and_stream_progress(
    provider,
):
    calls, progress = [], []

    async def transport(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        assert "fixture-key" in request.headers.get(
            "authorization", ""
        ) + request.headers.get("x-api-key", "")
        events = (
            openai_events(data["model"])
            if provider == "openai"
            else anthropic_events(data["model"])
        )
        return (httpx if provider == "openai" else httpx2).Response(
            200, headers={"content-type": "text/event-stream"}, text=sse(events)
        )

    async def emit(phase, **payload):
        progress.append((phase, payload))

    http = httpx if provider == "openai" else httpx2
    async with http.AsyncClient(transport=http.MockTransport(transport)) as client:
        model = ServerSearchModel(
            provider=provider,
            model=f"{provider}:fixture",
            api_key="fixture-key",
            base_url="https://proxy.example/v1/",
            http_client=client,
        )
        result = await model.search_web(
            "query", allowed_domains=["docs.example"], progress=emit
        )
    assert result.content["sources"] == [
        {"url": "https://docs.example/api", "title": "API"}
    ]
    assert result.usage.input_tokens == 7 and result.usage.output_tokens == 3
    assert progress and progress[0][1]["provider"] == provider
    assert calls[0][0] == ("/v1/responses" if provider == "openai" else "/v1/messages")
    assert calls[0][1]["model"] == "fixture"
    assert calls[0][1]["tools"][0].get(
        "allowed_domains",
        calls[0][1]["tools"][0].get("filters", {}).get("allowed_domains"),
    ) == ["docs.example"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_gateway_server_search_uses_run_key_alias_and_cost_headers(provider):
    calls = []

    async def transport(request):
        data = json.loads(request.content)
        calls.append(data)
        assert request.headers["x-litellm-request-id"] == "operation"
        events = (
            openai_events(data["model"])
            if provider == "openai"
            else anthropic_events(data["model"])
        )
        return (httpx if provider == "openai" else httpx2).Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-litellm-response-cost": "0.003",
            },
            text=sse(events),
        )

    gateway = NativeGatewayProvider(
        api_key="fixture-run-key",
        base_url="https://proxy.example/v1",
        transport=httpx.MockTransport(transport),
        anthropic_transport=httpx2.MockTransport(transport),
    )
    request = GatewayModelRequestV2(
        run_id="r",
        task_id="t",
        stage="researching",
        role=f"{provider}_search",
        logical_operation_id="operation",
        model=f"if-{provider}-search-v1",
        messages=[{"role": "user", "content": "query"}],
        server_search=ServerSearchRequest(provider=provider, query="query"),
        max_output_tokens=1024,
    )
    try:
        outcome = await gateway.complete(request)
        assert outcome.search_result["sources"][0]["url"] == "https://docs.example/api"
        assert outcome.usage["input_tokens"] == 7
        assert outcome.response_cost_usd == 0.003
        assert calls[0]["model"] == f"if-{provider}-search-v1"
    finally:
        await gateway.aclose()


def test_ordinary_wire_digest_does_not_gain_an_empty_search_field():
    request = GatewayModelRequestV2(
        run_id="r",
        task_id="t",
        stage="researching",
        role="researcher",
        logical_operation_id="op",
        model="fixture",
        messages=[],
    )
    assert "server_search" not in request.model_dump()
    with pytest.raises(ValueError, match="own model role"):
        GatewayModelRequestV2(
            **request.model_dump(), server_search={"provider": "openai", "query": "q"}
        )


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_model_factory_owns_search_clients_and_secret_free_descriptors(
    provider, monkeypatch
):
    for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(key, raising=False)
    role, spec = f"{provider}_search", f"{provider}:fixture"
    run = RunConfig.compile({"configurable": {f"{provider}_search_model": spec}})
    binding = CredentialBinding(
        "search-ref",
        "run",
        "r",
        (spec,),
        SecretStr("fixture-secret"),
        "https://proxy.example/v1",
    )
    factory = ModelFactory(run, scope="run", owner="r", bindings={role: binding})
    model = factory.build(role)
    assert isinstance(model, ServerSearchModel)
    assert model is factory.build(role)
    assert model.client.max_retries == 0
    assert "fixture-secret" not in json.dumps(factory.descriptor(role)) + repr(
        binding
    ) + json.dumps(run.snapshot())
    await factory.aclose()
    assert model.client.is_closed()


@pytest.mark.asyncio
async def test_shadow_usage_counts_each_structured_output_repair():
    from agentscope.message import UserMsg
    from pydantic import BaseModel

    from open_deep_research.agentscope_runtime.gateway import (
        SandboxChatModel,
        SandboxServiceBinding,
    )
    from open_deep_research.agentscope_runtime.web_progress import shadow_model_usage
    from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

    class Result(BaseModel):
        value: int

    calls = []

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body["logical_operation_id"])
        outcome = GatewayModelOutcomeV2(
            logical_operation_id=body["logical_operation_id"],
            requested_model=body["model"],
            status="completed",
            structured={"wrong": True} if len(calls) == 1 else {"value": 7},
            usage={"input_tokens": 5, "output_tokens": 3},
            response_cost_usd=0.001,
        )
        return httpx.Response(200, json=outcome.model_dump(mode="json"))

    totals = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    token = shadow_model_usage.set(totals)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://gateway"
        ) as client:
            model = SandboxChatModel(
                binding=SandboxServiceBinding(
                    "http://gateway",
                    "r",
                    "t",
                    "web_evidence",
                    "researching",
                    1,
                    b"x" * 32,
                ),
                model="fixture",
                client=client,
                structured_attempts=2,
            )
            result = await model.generate_structured_output(
                [UserMsg("user", "q")], Result
            )
            assert result.content == {"value": 7}
        assert totals == {
            "model_calls": 2,
            "input_tokens": 10,
            "output_tokens": 6,
            "cost_usd": 0.002,
        }
        assert len(set(calls)) == 2
    finally:
        shadow_model_usage.reset(token)


@pytest.mark.asyncio
async def test_known_search_rejection_is_not_an_unknown_outcome():
    from open_deep_research.agentscope_runtime.gateway import (
        GatewayCallError,
        SandboxChatModel,
        SandboxServiceBinding,
    )
    from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

    async def respond(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=GatewayModelOutcomeV2(
                logical_operation_id=body["logical_operation_id"],
                requested_model=body["model"],
                status="failed",
                error_code="authentication",
            ).model_dump(mode="json"),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gateway"
    ) as client:
        model = SandboxChatModel(
            binding=SandboxServiceBinding(
                "http://gateway", "r", "t", "openai_search", "researching", 1, b"x" * 32
            ),
            model="fixture",
            client=client,
        )
        with pytest.raises(GatewayCallError) as error:
            await model.search_web("q")
        assert error.value.status_code == 401 and error.value.uncertain is False

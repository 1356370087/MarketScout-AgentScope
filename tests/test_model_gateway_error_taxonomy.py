"""Provider errors retain safe context-limit semantics across the native gateway."""

import asyncio
import json

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from open_deep_research.agentscope_runtime.sandbox_provider import NativeGatewayProvider
from open_deep_research.models.errors import GATEWAY_TOKEN_LIMIT_MARKER, is_token_limit_exceeded
from open_deep_research.models.protocol_errors import ModelGatewayError
from tests.as_runtime.test_sandbox_provider import request


def test_gateway_error_with_token_limit_marker_detected():
    error = ModelGatewayError("invalid_request", status_code=400,
                              provider_error_code=GATEWAY_TOKEN_LIMIT_MARKER)
    assert is_token_limit_exceeded(error)
    assert is_token_limit_exceeded(error, "litellm:if-research-v1")


def test_gateway_error_without_marker_is_not_token_limit():
    assert not is_token_limit_exceeded(ModelGatewayError("invalid_request", status_code=400))
    assert not is_token_limit_exceeded(ModelGatewayError("invalid_request", status_code=400,
                                                       provider_error_code="invalid_json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error,marker", [
    ({"code": "400", "type": "invalid_request_error",
      "message": "This model's maximum context length is 4097 tokens. PRIVATE-UPSTREAM-TEXT"}, GATEWAY_TOKEN_LIMIT_MARKER),
    ({"code": "context_length_exceeded", "message": "PRIVATE-UPSTREAM-TEXT"}, GATEWAY_TOKEN_LIMIT_MARKER),
    ({"code": "invalid_json", "message": "PRIVATE-UPSTREAM-TEXT"}, None),
])
async def test_native_provider_classifies_context_rejections_without_exposing_text(error, marker):
    calls = []
    def serve(req):
        calls.append(req)
        return httpx.Response(400, json={"error": error})
    provider = NativeGatewayProvider(api_key="fixture", base_url="https://fixture.invalid/v1",
                                     transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(ModelGatewayError) as raised:
            await provider.complete(request())
        assert raised.value.provider_error_code == marker
        assert is_token_limit_exceeded(raised.value) is (marker is not None)
        assert str(raised.value) == "invalid_request"
        assert len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_connection_failure_maps_to_uncertain_code():
    calls = []
    def serve(req):
        calls.append(req)
        raise httpx.ReadError("connection reset", request=req)
    provider = NativeGatewayProvider(api_key="fixture", base_url="https://fixture.invalid/v1",
                                     transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(ModelGatewayError) as raised:
            await provider.complete(request())
        assert raised.value.code == "gateway_connection_failed"
        assert isinstance(raised.value.__cause__, APIConnectionError)
        assert len(calls) == 1
    finally:
        await provider.aclose()


def test_apitimeout_is_connection_level():
    assert isinstance(APITimeoutError(request=httpx.Request("POST", "https://fixture.invalid")), APIConnectionError)


@pytest.mark.asyncio
async def test_native_provider_pools_models_per_run_without_sharing_credentials():
    seen = []
    async def serve(req):
        seen.append((req.headers["authorization"], json.loads(req.content)["metadata"]["run_id"]))
        await asyncio.sleep(0)
        return httpx.Response(200, json={"id": "response", "object": "chat.completion", "created": 1,
            "model": "research", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "answer"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    providers = [NativeGatewayProvider(api_key=key, base_url="https://fixture.invalid/v1",
                                       transport=httpx.MockTransport(serve)) for key in ("run-one", "run-two")]
    try:
        await asyncio.gather(*(provider.complete(request().model_copy(update={"run_id": str(index)}))
                               for index, provider in enumerate(providers)))
        cached_model = providers[0].models["research"]
        await providers[0].complete(request().model_copy(update={"run_id": "0"}))
        assert providers[0].models["research"] is cached_model
        assert sorted(seen) == [("Bearer run-one", "0"), ("Bearer run-one", "0"), ("Bearer run-two", "1")]
    finally:
        await asyncio.gather(*(provider.aclose() for provider in providers))
    assert all(provider.client.is_closed for provider in providers)

"""Gateway error taxonomy: token-limit passthrough and connection uncertainty."""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from open_deep_research.models.errors import (
    GATEWAY_TOKEN_LIMIT_MARKER,
    is_token_limit_exceeded,
)
from open_deep_research.models.gateway import (
    LiteLLMModelGateway,
    ModelGatewayError,
    _gateway_provider_error_code,
    bind_run_key,
    reset_run_key,
)


class _FakeStatusError(Exception):
    """Mimic openai APIStatusError's status_code/body surface."""

    def __init__(self, status_code: int, body: dict) -> None:
        super().__init__(f"Error code: {status_code} - {body}")
        self.status_code = status_code
        self.body = body


def test_gateway_error_with_token_limit_marker_detected() -> None:
    error = ModelGatewayError(
        "invalid_request",
        status_code=400,
        provider_error_code=GATEWAY_TOKEN_LIMIT_MARKER,
    )
    assert is_token_limit_exceeded(error) is True
    assert is_token_limit_exceeded(error, "litellm:if-research-v1") is True


def test_gateway_error_without_marker_is_not_token_limit() -> None:
    error = ModelGatewayError("invalid_request", status_code=400)
    assert is_token_limit_exceeded(error) is False
    error = ModelGatewayError(
        "invalid_request", status_code=400, provider_error_code="invalid_json"
    )
    assert is_token_limit_exceeded(error) is False


def test_provider_error_code_extracts_marker_from_sanitized_fragments() -> None:
    exc = _FakeStatusError(
        400,
        {
            "error": {
                "code": "400",
                "type": "invalid_request_error",
                "message": (
                    "This model's maximum context length is 4097 tokens. "
                    "However you requested 5000 tokens."
                ),
            }
        },
    )
    assert _gateway_provider_error_code(exc) == GATEWAY_TOKEN_LIMIT_MARKER


def test_provider_error_code_keeps_identifier_shaped_codes_only() -> None:
    exc = _FakeStatusError(
        400,
        {
            "error": {
                "code": "context_length_exceeded",
                "type": "invalid_request_error",
                "message": "some free-form provider text",
            }
        },
    )
    assert _gateway_provider_error_code(exc) == "context_length_exceeded"

    exc = _FakeStatusError(401, {"error": {"message": "bad key; contact support"}})
    # No identifier-shaped code/type and no token-limit marker: nothing leaks.
    assert _gateway_provider_error_code(exc) is None


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "http://gateway/v1"))


@pytest.mark.asyncio
async def test_connection_failure_maps_to_uncertain_code() -> None:
    """Timeouts/connection resets must classify as uncertain, not definite."""

    class RaisingClient:
        def with_options(self, **_kwargs):
            return self

        class chat:  # noqa: N801
            class completions:  # noqa: N801
                class with_raw_response:  # noqa: N801
                    @staticmethod
                    async def create(**_kwargs):
                        raise _connection_error()

    gateway = LiteLLMModelGateway(base_url="http://gateway/v1", client=RaisingClient())
    from open_deep_research.models.gateway import ModelRequest

    key_token = bind_run_key("sk-run-test")
    try:
        with pytest.raises(ModelGatewayError) as excinfo:
            await gateway.complete(
                ModelRequest(
                    run_id="run-1",
                    task_id="task-1",
                    logical_operation_id="op-1",
                    role="researcher",
                    stage="research",
                    model="if-research-v1",
                    messages=[],
                )
            )
    finally:
        reset_run_key(key_token)
    assert excinfo.value.code == "gateway_connection_failed"
    assert isinstance(excinfo.value.__cause__, APIConnectionError)


def test_apitimeout_is_connection_level() -> None:
    """APITimeoutError is a subclass of APIConnectionError (taxonomy guard)."""
    timeout = APITimeoutError(request=httpx.Request("POST", "http://gateway/v1"))
    assert isinstance(timeout, APIConnectionError)


@pytest.mark.asyncio
async def test_request_client_resolves_run_key_per_call() -> None:
    """A pooled client must never pin the first Run Key it observed."""
    gateway = LiteLLMModelGateway(base_url="http://gateway/v1")
    token_one = bind_run_key("sk-run-one")
    client_one = gateway._request_client()
    assert client_one.api_key == "sk-run-one"
    reset_run_key(token_one)

    token_two = bind_run_key("sk-run-two")
    client_two = gateway._request_client()
    assert client_two.api_key == "sk-run-two"
    reset_run_key(token_two)

    # One pooled httpx transport shared across requests, not one per key.
    assert client_two._client is client_one._client
    await gateway.aclose()

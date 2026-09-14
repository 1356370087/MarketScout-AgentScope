"""LiteLLM OpenAI-compatible client normalization tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from openai import AsyncOpenAI
from pydantic import BaseModel

from open_deep_research.models.codec import MessageCodecError, structured_output_tool
from open_deep_research.models.gateway import (
    LiteLLMModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelResult,
    ModelRoute,
    ModelStreamCompleted,
    ModelStreamDelta,
    ModelUsage,
    bind_run_key,
    current_traceparent,
    reset_run_key,
)
from open_deep_research.models.invocation import (
    _logical_operation_id,
    complete_model,
)
from open_deep_research.quality.gate import HandoffAssessment, ToolResultAssessment


class FakeRawResponse:
    headers = {
        "x-litellm-response-cost": "0.012345",
        "x-litellm-model-provider": "openai",
        "x-litellm-deployment-id": "deployment-1",
        "x-request-id": "request-1",
    }

    def parse(self) -> Any:
        message = SimpleNamespace(
            model_dump=lambda **_kwargs: {"role": "assistant", "content": "answer"}
        )
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=usage,
            model="gpt-4.1",
        )


class FakeCreate:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> FakeRawResponse:
        self.kwargs = kwargs
        return FakeRawResponse()


class FakeClient:
    def __init__(self) -> None:
        self.create = FakeCreate()
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(with_raw_response=self.create)
        )

    def with_options(self, **_kwargs: Any) -> "FakeClient":
        # Mirror the OpenAI SDK copy-on-write option override.
        return self

    async def close(self) -> None:
        return None


def request() -> ModelRequest:
    return ModelRequest(
        run_id="run-1",
        task_id="task-1",
        logical_operation_id="operation-1",
        role="researcher",
        stage="researching",
        model="if-research-v1",
        messages=[HumanMessage(content="question")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("output_limit", [None, 256, 10000])
async def test_gateway_output_budget_uses_portable_litellm_parameter(
    streaming: bool, output_limit: int | None,
) -> None:
    """Opaque aliases must work with zai and retain output recovery budgets."""
    bodies: list[dict[str, Any]] = []

    def respond(incoming: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(incoming.content))
        common = {"id": "test", "created": 0, "model": "glm-5.3-flash"}
        if streaming:
            chunk = {
                **common, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "delta": {"content": "answer"}}],
            }
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json={
            **common, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "answer"}}],
        })

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://proxy.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        gateway = LiteLLMModelGateway(
            base_url="http://proxy.test/v1", api_key="test-key", client=client,
        )
        model_request = replace(request(), max_output_tokens=output_limit)
        if streaming:
            events = [event async for event in gateway.complete_stream(model_request)]
            assert isinstance(events[-1], ModelStreamCompleted)
            assert events[-1].result.message.content == "answer"
        else:
            assert (await gateway.complete(model_request)).message.content == "answer"

    assert len(bodies) == 1
    assert "max_completion_tokens" not in bodies[0]
    if output_limit is None:
        assert "max_tokens" not in bodies[0]
    else:
        assert bodies[0]["max_tokens"] == output_limit


@pytest.mark.asyncio
async def test_gateway_normalizes_usage_route_cost_and_traceparent() -> None:
    client = FakeClient()
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=client,  # type: ignore[arg-type]
    )
    result = await gateway.complete(request())

    assert result.usage.total_tokens == 14
    assert result.usage.cached_input_tokens == 3
    assert result.response_cost_usd == pytest.approx(0.012345)
    assert result.route.served_model == "gpt-4.1"
    assert result.route.deployment_id == "deployment-1"
    traceparent = client.create.kwargs["extra_headers"]["traceparent"]
    assert traceparent.startswith("00-")
    assert len(traceparent) == 55


@pytest.mark.asyncio
async def test_gateway_sends_spend_tags_for_filterable_analytics() -> None:
    client = FakeClient()
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=client,  # type: ignore[arg-type]
    )
    await gateway.complete(request())

    metadata = client.create.kwargs["metadata"]
    assert metadata["tags"] == ["run:run-1", "role:researcher", "stage:researching"]


@pytest.mark.asyncio
async def test_gateway_forwards_static_provider_options() -> None:
    client = FakeClient()
    gateway = LiteLLMModelGateway(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="run-key",
        extra_body={"enable_thinking": False},
        client=client,  # type: ignore[arg-type]
    )

    await gateway.complete(request())

    assert client.create.kwargs["extra_body"] == {"enable_thinking": False}


def test_request_client_applies_injected_run_key() -> None:
    """The sandbox Gateway vault key must reach every request (E2E P0)."""
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="sk-injected-run-key",
    )
    assert gateway._request_client().api_key == "sk-injected-run-key"  # noqa: SLF001


def test_request_client_applies_context_run_key(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_SERVICE_KEY", "")
    gateway = LiteLLMModelGateway(base_url="http://litellm-proxy:4000/v1")
    token = bind_run_key("sk-context-run-key")
    try:
        assert gateway._request_client().api_key == "sk-context-run-key"  # noqa: SLF001
    finally:
        reset_run_key(token)


def test_openai_sdk_retries_are_disabled(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("open_deep_research.models.gateway.AsyncOpenAI", Client)
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
    )
    gateway._openai_client()  # noqa: SLF001
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 180


@pytest.mark.asyncio
async def test_gateway_error_does_not_copy_upstream_body() -> None:
    class FailingCreate:
        async def create(self, **_kwargs: Any) -> None:
            error = RuntimeError("api_key=secret upstream body")
            error.status_code = 503  # type: ignore[attr-defined]
            raise error

    client = FakeClient()
    client.chat.completions.with_raw_response = FailingCreate()
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=client,  # type: ignore[arg-type]
    )
    with pytest.raises(ModelGatewayError) as caught:
        await gateway.complete(request())
    assert str(caught.value) == "gateway_unavailable"
    assert "secret" not in str(caught.value)


def test_traceparent_is_stable_per_run_and_valid() -> None:
    first = current_traceparent("run-1")
    second = current_traceparent("run-1")
    assert first.split("-")[1] == second.split("-")[1]
    assert all(len(part) == size for part, size in zip(first.split("-"), (2, 32, 16, 2)))


def test_logical_operation_id_includes_output_recovery_parameters() -> None:
    common = {
        "run_id": "run-1",
        "task_id": "task-1",
        "operation": "lead.final_report",
        "model": "if-final-report-v1",
        "messages": [HumanMessage(content="write")],
        "tools": (),
        "tool_choice": None,
        "output_schema": None,
        "temperature": 0.1,
    }
    first = _logical_operation_id(max_output_tokens=1024, **common)
    duplicate = _logical_operation_id(max_output_tokens=1024, **common)
    upgraded = _logical_operation_id(max_output_tokens=2048, **common)

    assert first == duplicate
    assert first != upgraded


@pytest.mark.parametrize("schema", [ToolResultAssessment, HandoffAssessment])
def test_quality_schema_has_no_propertyless_objects(schema: type[BaseModel]) -> None:
    parameters = structured_output_tool(schema)["function"]["parameters"]

    def propertyless_objects(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for child in value for item in propertyless_objects(child)]
        if not isinstance(value, dict):
            return []
        found = (
            [value]
            if value.get("type") == "object" and not value.get("properties")
            else []
        )
        return [
            *found,
            *(item for child in value.values() for item in propertyless_objects(child)),
        ]

    assert propertyless_objects(parameters) == []


@pytest.mark.parametrize("schema", [ToolResultAssessment, HandoffAssessment])
def test_quality_schema_is_strict_at_every_object(schema: type[BaseModel]) -> None:
    parameters = structured_output_tool(schema)["function"]["parameters"]

    def object_schema_errors(value: Any) -> list[str]:
        if isinstance(value, list):
            return [error for child in value for error in object_schema_errors(child)]
        if not isinstance(value, dict):
            return []
        errors: list[str] = []
        properties = value.get("properties")
        if isinstance(properties, dict):
            if set(value.get("required", [])) != set(properties):
                errors.append("required_properties_mismatch")
            if value.get("additionalProperties") is not False:
                errors.append("additional_properties_not_disabled")
        return [
            *errors,
            *(error for child in value.values() for error in object_schema_errors(child)),
        ]

    assert object_schema_errors(parameters) == []


def test_litellm_structured_tool_can_disable_provider_strict_mode() -> None:
    tool = structured_output_tool(ToolResultAssessment, strict=False)

    assert tool["function"]["strict"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", [False, True])
async def test_structured_schema_failure_uses_bounded_application_repair(
    monkeypatch, missing_field,
) -> None:
    class Answer(BaseModel):
        value: int

    class RepairGateway:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, model_request: ModelRequest) -> ModelResult:
            self.requests.append(model_request)
            if len(self.requests) == 1:
                if missing_field:
                    Answer.model_validate({"private_payload": "secret-not-for-retry"})
                raise MessageCodecError("invalid structured arguments")
            if missing_field:
                feedback = str(model_request.messages[-1].content)
                assert '"field": ["value"]' in feedback
                assert '"type": "missing"' in feedback
                assert "secret-not-for-retry" not in feedback
            return ModelResult(
                message=AIMessage(content=""),
                structured=Answer(value=7),
                usage=ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7),
                response_cost_usd=0.001,
                request_id="request-2",
                route=ModelRoute(
                    requested_model="if-quality-v1",
                    served_model="gpt-4.1-mini",
                ),
                finish_reason="tool_calls",
                latency_ms=10,
            )

    gateway = RepairGateway()
    monkeypatch.setattr(
        "open_deep_research.models.invocation.get_model_gateway",
        lambda _config: gateway,
    )
    result = await complete_model(
        [HumanMessage(content="return a value")],
        {
            "configurable": {
                "model_backend": "litellm",
                "max_structured_output_retries": 2,
            },
            "metadata": {"run_id": "service"},
        },
        role="quality_evaluation",
        stage="finalizing",
        model="if-quality-v1",
        max_output_tokens=128,
        span_name="test.structured",
        output_schema=Answer,
    )

    assert result == Answer(value=7)
    assert len(gateway.requests) == 2
    assert gateway.requests[0].logical_operation_id != gateway.requests[1].logical_operation_id
    assert len(gateway.requests[1].messages) == 2


class _AsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._it = iter(items)

    def __aiter__(self) -> "_AsyncIter":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def _chunk(content: str | None = None, finish: str | None = None, usage: Any = None, model: str | None = "gpt-4.1") -> Any:
    return SimpleNamespace(
        model=model,
        usage=usage,
        choices=[
            SimpleNamespace(finish_reason=finish, delta=SimpleNamespace(content=content))
        ],
    )


@pytest.mark.asyncio
async def test_complete_stream_merges_deltas_and_usage() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    chunks = [
        _chunk(content="Hello"),
        _chunk(content=" world"),
        _chunk(content=None, finish="stop"),
        SimpleNamespace(model="gpt-4.1", usage=usage, choices=None),
    ]

    class StreamRaw:
        headers = dict(FakeRawResponse.headers)

        def parse(self) -> Any:
            return _AsyncIter(chunks)

    class StreamClient:
        def __init__(self) -> None:
            self.create = None
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=self._create)
                )
            )

        async def _create(self, **kwargs: Any) -> StreamRaw:
            self.create_kwargs = kwargs
            return StreamRaw()

        def with_options(self, **_kwargs: Any) -> "StreamClient":
            return self

        async def close(self) -> None:
            return None

    client = StreamClient()
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=client,  # type: ignore[arg-type]
    )

    events = [event async for event in gateway.complete_stream(request())]

    assert client.create_kwargs["stream"] is True
    assert client.create_kwargs["stream_options"] == {"include_usage": True}
    assert [event.text for event in events[:-1]] == ["Hello", " world"]
    completed = events[-1]
    assert isinstance(completed, ModelStreamCompleted)
    assert completed.result.message.content == "Hello world"
    assert completed.result.usage.total_tokens == 14
    assert completed.result.usage.cached_input_tokens == 3
    assert completed.result.finish_reason == "stop"
    assert completed.result.route.served_model == "gpt-4.1"
    assert completed.result.response_cost_usd == pytest.approx(0.012345)
    assert completed.first_packet_ms > 0


@pytest.mark.asyncio
async def test_complete_stream_rejects_tools_and_schema() -> None:
    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=FakeClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="plain_text_request"):
        async for _event in gateway.complete_stream(
            ModelRequest(
                run_id="run-1",
                task_id="task-1",
                logical_operation_id="operation-1",
                role="researcher",
                stage="researching",
                model="if-research-v1",
                messages=[HumanMessage(content="q")],
                output_schema=HandoffAssessment,
            )
        ):
            pass


@pytest.mark.asyncio
async def test_complete_stream_marks_unsupported_deployments() -> None:
    class UnsupportedClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    with_raw_response=SimpleNamespace(create=self._create)
                )
            )

        async def _create(self, **_kwargs: Any) -> Any:
            raise RuntimeError("this deployment does not support streaming")

        def with_options(self, **_kwargs: Any) -> "UnsupportedClient":
            return self

        async def close(self) -> None:
            return None

    gateway = LiteLLMModelGateway(
        base_url="http://litellm-proxy:4000/v1",
        api_key="run-key",
        client=UnsupportedClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(ModelGatewayError) as excinfo:
        async for _event in gateway.complete_stream(request()):
            pass
    assert excinfo.value.code == "gateway_stream_unsupported"


def _plain_result(content: str) -> ModelResult:
    return ModelResult(
        message=AIMessage(content=content),
        structured=None,
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        response_cost_usd=None,
        request_id=None,
        route=ModelRoute(requested_model="if-final-report-v1"),
        finish_reason="stop",
        latency_ms=5.0,
    )


class StubStreamGateway:
    """Gateway double exercising the streaming downgrade contract."""

    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.complete_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.complete_calls += 1
        return _plain_result("fallback answer")

    async def complete_stream(self, request: ModelRequest):  # type: ignore[no-untyped-def]
        if self.behavior == "hang":
            await asyncio.sleep(5)
            yield  # pragma: no cover - first packet never arrives
        if self.behavior == "unsupported":
            raise ModelGatewayError("gateway_stream_unsupported")
        yield ModelStreamDelta(text="partial ")
        yield ModelStreamCompleted(
            result=_plain_result("streamed answer"), first_packet_ms=1.5
        )


def _stream_config() -> dict[str, Any]:
    # run_id omitted on purpose: "service" identity skips public-event writes.
    return {"configurable": {}, "metadata": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["hang", "unsupported"])
async def test_complete_model_stream_downgrades_once(monkeypatch, behavior) -> None:
    import open_deep_research.models.invocation as invocation_module

    monkeypatch.setenv("MODEL_FIRST_PACKET_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("MODEL_SLOW_FIRST_PACKET_THRESHOLD_SECONDS", "0.01")
    stub = StubStreamGateway(behavior)
    monkeypatch.setattr(invocation_module, "get_model_gateway", lambda _cfg: stub)

    message = await invocation_module.complete_model_stream(
        [HumanMessage(content="write a report")],
        _stream_config(),
        role="final_report",
        stage="writing",
        model="if-final-report-v1",
        span_name="lead.section.test",
    )

    assert message.content == "fallback answer"
    assert stub.complete_calls == 1


@pytest.mark.asyncio
async def test_complete_model_stream_returns_streamed_result(monkeypatch) -> None:
    import open_deep_research.models.invocation as invocation_module

    stub = StubStreamGateway("ok")
    monkeypatch.setattr(invocation_module, "get_model_gateway", lambda _cfg: stub)

    message = await invocation_module.complete_model_stream(
        [HumanMessage(content="write a report")],
        _stream_config(),
        role="final_report",
        stage="writing",
        model="if-final-report-v1",
        span_name="lead.section.test",
    )

    assert message.content == "streamed answer"
    assert stub.complete_calls == 0
    routed = message.response_metadata["model_routed"]
    assert routed["streamed"] is True
    assert routed["first_packet_latency_ms"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_complete_model_stream_passes_through_without_stream_support(monkeypatch) -> None:
    import open_deep_research.models.invocation as invocation_module

    class PlainGateway:
        async def complete(self, request: ModelRequest) -> ModelResult:
            return _plain_result("plain answer")

    monkeypatch.setattr(
        invocation_module, "get_model_gateway", lambda _cfg: PlainGateway()
    )

    message = await invocation_module.complete_model_stream(
        [HumanMessage(content="write a report")],
        _stream_config(),
        role="final_report",
        stage="writing",
        model="if-final-report-v1",
        span_name="lead.section.test",
    )

    assert message.content == "plain answer"

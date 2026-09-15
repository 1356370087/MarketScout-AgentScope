"""真实 SDK 的确定性传输验收；不使用真实模型凭据。"""

import json
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from pydantic import BaseModel
from agentscope.credential import (
    AnthropicCredential,
    GeminiCredential,
    DeepSeekCredential,
)
from agentscope.message import UserMsg
from open_deep_research.agentscope_runtime.provider_models import (
    GovernedAnthropicChatModel,
    GovernedGeminiChatModel,
    GovernedDeepSeekChatModel,
)
from open_deep_research.agentscope_runtime.gateway import (
    SandboxChatModel,
    SandboxServiceBinding,
)
from open_deep_research.agentscope_runtime.model_policy import (
    ModelCallPolicy,
    retryable,
    recover_output,
    RecoveryExhausted,
)

pytestmark = pytest.mark.asyncio


class Answer(BaseModel):
    value: int


async def test_thinking_truncation_can_escalate_without_replaying_thinking():
    from agentscope.message import ThinkingBlock, TextBlock
    from agentscope.model import ChatResponse

    calls = []

    async def call(messages, limit):
        calls.append(limit)
        assert len(messages) == 1
        if len(calls) == 1:
            return ChatResponse(
                content=[ThinkingBlock(thinking="fixture")],
                is_last=True,
                metadata={"provider_finish_reason": "length"},
            )
        return ChatResponse(
            content=[TextBlock(text="complete")],
            is_last=True,
            metadata={"provider_finish_reason": "stop"},
        )

    result = await recover_output(
        call, [UserMsg("u", "q")], requested_tokens=1, maximum_tokens=128
    )
    assert calls == [1, 128] and result.content[0].text == "complete"


async def test_native_stream_close_releases_http_response_immediately():
    from agentscope.credential import OpenAICredential
    from open_deep_research.agentscope_runtime.gateway import LiteLLMChatModel

    closed = []

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"r","object":"chat.completion.chunk","created":1,"model":"test","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            raise AssertionError("consumer must not drain further content")

        async def aclose(self):
            closed.append(True)

    async def serve(request):
        return httpx2.Response(
            200, stream=Body(), headers={"content-type": "text/event-stream"}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(serve)) as client:
        model = LiteLLMChatModel(
            OpenAICredential(api_key="fixture"),
            "test",
            stream=True,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )

        async def handler(current_model, messages):
            return await current_model(messages)

        stream = await ModelCallPolicy([model]).invoke(
            handler, {"messages": [UserMsg("u", "q")]}, {}
        )
        await anext(stream)
        await stream.aclose()
        assert closed == [True]


@pytest.mark.parametrize("gateway", [False, True])
@pytest.mark.parametrize("override", [None, 512])
async def test_openai_and_litellm_emit_one_output_limit(gateway, override):
    from agentscope.credential import OpenAICredential
    from open_deep_research.agentscope_runtime.gateway import (
        GovernedOpenAIChatModel,
        LiteLLMChatModel,
    )

    async def serve(request):
        body = json.loads(request.content)
        expected = "max_tokens" if gateway else "max_completion_tokens"
        assert body[expected] == (override or 128)
        assert ("max_completion_tokens" if gateway else "max_tokens") not in body
        return httpx2.Response(
            200,
            json={
                "id": "fixture",
                "model": "test",
                "object": "chat.completion",
                "created": 1,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(serve)) as client:
        cls = LiteLLMChatModel if gateway else GovernedOpenAIChatModel
        model = cls(
            OpenAICredential(api_key="fixture"),
            "test",
            parameters=cls.Parameters(max_tokens=128),
            stream=False,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        await model(
            [UserMsg("u", "q")], **({"max_tokens": override} if override else {})
        )
        assert model.parameters.max_tokens == 128


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "deepseek"])
@pytest.mark.parametrize("status", [400, 401, 429, 503])
@pytest.mark.parametrize("structured", [False, True])
async def test_sdk_errors_do_not_retry_inside_model(provider, status, structured):
    calls = []
    lib = httpx if provider == "gemini" else httpx2

    async def serve(request):
        calls.append(1)
        return lib.Response(
            status,
            json={
                "error": {
                    "code": status,
                    "message": "fixture",
                    "type": "invalid_request_error",
                }
            },
        )

    if provider == "gemini":
        model = GovernedGeminiChatModel(
            GeminiCredential(api_key="fixture"),
            "gemini-test",
            stream=False,
            max_retries=0,
            client_kwargs={
                "http_options": {
                    "retry_options": {"attempts": 1},
                    "client_args": {"trust_env": False},
                    "async_client_args": {
                        "transport": httpx.MockTransport(serve),
                        "trust_env": False,
                    },
                }
            },
        )
    else:
        cls, credential = (
            (GovernedAnthropicChatModel, AnthropicCredential)
            if provider == "anthropic"
            else (GovernedDeepSeekChatModel, DeepSeekCredential)
        )
        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(serve), trust_env=False
        )
        model = cls(
            credential(api_key="fixture"),
            "test",
            stream=False,
            max_retries=0,
            client_kwargs={"max_retries": 0, "http_client": client},
        )
    try:
        with pytest.raises(Exception) as caught:
            if structured:
                await model.generate_structured_output([UserMsg("u", "q")], Answer)
            else:
                await model([UserMsg("u", "q")])
        assert retryable(caught.value) is (status in {429, 503})
        assert len(calls) == 1
    finally:
        if provider == "gemini":
            await model.client.aio.aclose()
            model.client.close()
        else:
            await model.client.close()


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "deepseek"])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_sdk_completion(provider, structured, stream):
    seen = []

    async def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        if provider == "anthropic":
            content = [{"type": "text", "text": "ok"}]
            if structured:
                content = [
                    {
                        "type": "tool_use",
                        "id": "call1",
                        "name": body["tools"][0]["name"],
                        "input": {"value": 1},
                    }
                ]
            data = {
                "id": "msg1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": content,
                "stop_reason": "tool_use" if structured else "max_tokens",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        elif provider == "gemini":
            assert body["generationConfig"]["maxOutputTokens"] == 123
            part = {"text": "ok"}
            if structured:
                name = body["tools"][0]["functionDeclarations"][0]["name"]
                part = {"functionCall": {"name": name, "args": {"value": 1}}}
            data = {
                "responseId": "msg1",
                "modelVersion": "gemini-test",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [part]},
                        "finishReason": "STOP" if structured else "MAX_TOKENS",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
            }
        else:
            message = {"role": "assistant", "content": "ok"}
            if structured:
                message["tool_calls"] = [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {
                            "name": body["tools"][0]["function"]["name"],
                            "arguments": '{"value":1}',
                        },
                    }
                ]
            data = {
                "id": "msg1",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-test",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if structured else "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        response_cls = httpx.Response if provider == "gemini" else httpx2.Response
        if stream:
            if provider == "anthropic":
                block = data["content"][0]
                delta = (
                    {"type": "input_json_delta", "partial_json": '{"value":1}'}
                    if structured
                    else {"type": "text_delta", "text": "ok"}
                )
                start = (
                    {**block, "input": {}}
                    if structured
                    else {"type": "text", "text": ""}
                )
                events = [
                    {
                        "type": "message_start",
                        "message": {**data, "content": [], "stop_reason": None},
                    },
                    {"type": "content_block_start", "index": 0, "content_block": start},
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                    {"type": "content_block_stop", "index": 0},
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": data["stop_reason"],
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": 1},
                    },
                    {"type": "message_stop"},
                ]
                content = "".join(
                    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                )
            elif provider == "deepseek":
                message = data["choices"][0].pop("message")
                if structured:
                    message["tool_calls"][0]["index"] = 0
                data["choices"][0]["delta"] = message
                data["object"] = "chat.completion.chunk"
                content = f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n"
            else:
                content = f"data: {json.dumps(data)}\n\n"
            return response_cls(
                200, content=content, headers={"content-type": "text/event-stream"}
            )
        return response_cls(200, json=data)

    if provider == "anthropic":
        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(serve), trust_env=False
        )
        model = GovernedAnthropicChatModel(
            AnthropicCredential(api_key="fixture"),
            "claude-test",
            stream=stream,
            max_retries=0,
            client_kwargs={"max_retries": 0, "http_client": client},
        )
    elif provider == "gemini":
        model = GovernedGeminiChatModel(
            GeminiCredential(api_key="fixture"),
            "gemini-test",
            stream=stream,
            max_retries=0,
            client_kwargs={
                "http_options": {
                    "retry_options": {"attempts": 1},
                    "client_args": {"trust_env": False},
                    "async_client_args": {
                        "transport": httpx.MockTransport(serve),
                        "trust_env": False,
                    },
                }
            },
        )
    else:
        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(serve), trust_env=False
        )
        model = GovernedDeepSeekChatModel(
            DeepSeekCredential(api_key="fixture"),
            "deepseek-test",
            stream=stream,
            max_retries=0,
            client_kwargs={"max_retries": 0, "http_client": client},
        )
    try:
        if structured:
            result = await model.generate_structured_output(
                [UserMsg("u", "q")], Answer, max_tokens=123
            )
            assert result.content == {"value": 1}
        else:
            result = await model([UserMsg("u", "q")], max_tokens=123)
            if stream:
                chunks = [chunk async for chunk in result]
                result = chunks[-1]
                assert result.is_last
            assert result.metadata["provider_finish_reason"].lower() in {
                "max_tokens",
                "length",
            }
        assert result.metadata["request_id"] == "msg1"
        assert result.usage.input_tokens == 2
        assert len(seen) == 1
    finally:
        if provider == "gemini":
            await model.client.aio.aclose()
            model.client.close()
        else:
            await model.client.close()


@pytest.mark.parametrize(
    "status,expected",
    [(400, False), (401, False), (403, False), (429, True), (503, True)],
)
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
async def test_real_sdk_error_types(provider, status, expected):
    if provider == "gemini":
        from google.genai.errors import APIError

        error = APIError(status, {"error": {"code": status, "message": "fixture"}})
    else:
        module = __import__(provider)
        lib = httpx2
        response = lib.Response(
            status, request=lib.Request("POST", "https://fixture.invalid")
        )
        error = module.APIStatusError(
            "fixture", response=response, body={"error": {"message": "fixture"}}
        )
    assert retryable(error) is expected


async def test_proxy_owns_retries_and_fallback():
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs["current_model"])
        raise ConnectionError("fixture")

    proxy = SimpleNamespace(retry_owner="gateway")
    with pytest.raises(ConnectionError):
        await ModelCallPolicy([proxy, object()], attempts=3).invoke(
            handler, {"messages": []}, {}
        )
    assert calls == [proxy]


async def test_service_signed_wire_matches_gateway_authorization():
    from open_deep_research.sandbox.crypto import verify_payload

    binding = SandboxServiceBinding(
        "https://fixture.invalid",
        "run",
        "task",
        "researcher",
        "researching",
        7,
        b"fixture-service-key",
    )

    async def serve(request):
        body = json.loads(request.content)
        assert "authorization" not in request.headers
        assert verify_payload(
            {
                "request": body,
                "timestamp": float(request.headers["x-sandbox-timestamp"]),
                "nonce": request.headers["x-sandbox-nonce"],
                "fence_token": int(request.headers["x-sandbox-fence-token"]),
            },
            request.headers["x-sandbox-service-signature"],
            binding.service_key,
        )
        return httpx.Response(
            200,
            json={
                "protocol_version": 2,
                "logical_operation_id": body["logical_operation_id"],
                "requested_model": "test",
                "status": "completed",
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve), base_url=binding.url
    ) as client:
        model = SandboxChatModel(
            binding=binding, model="test", stream=False, client=client
        )
        result = await model([UserMsg("u", "q")])
        assert result.content[0].text == "ok"
        assert binding.service_key.decode() not in repr(binding)


@pytest.mark.parametrize(
    "reason", ["SAFETY", "content_filter", "pause_turn", "unknown"]
)
async def test_refusal_is_not_successful_recovery(reason):
    from agentscope.model import ChatResponse

    async def call(messages, limit):
        return ChatResponse(
            content=[], is_last=True, metadata={"provider_finish_reason": reason}
        )

    with pytest.raises(RecoveryExhausted):
        await recover_output(call, [], requested_tokens=10, maximum_tokens=10)


async def test_compact_context_survives_escalation_and_restore():
    from agentscope.model import ChatResponse
    from agentscope.message import TextBlock

    seen = []
    state = {}

    async def compact(messages):
        return [messages[0]]

    async def call(messages, limit):
        seen.append([m.get_text_content() for m in messages])
        if len(seen) == 1:
            raise ValueError("maximum context length exceeded")
        if len(seen) == 3:
            raise ConnectionError("fixture transport failed after compaction")
        return ChatResponse(
            content=[TextBlock(text="partial")],
            is_last=True,
            metadata={"provider_finish_reason": "length"},
        )

    messages = [UserMsg("u", "protected"), UserMsg("u", "old context")]
    with pytest.raises(ConnectionError):
        await recover_output(
            call,
            messages,
            requested_tokens=10,
            maximum_tokens=20,
            compact=compact,
            state=state,
        )
    assert seen == [["protected", "old context"], ["protected"], ["protected"]]

    async def resumed(messages, limit):
        assert [m.get_text_content() for m in messages] == ["protected"]
        return ChatResponse(
            content=[TextBlock(text="done")],
            is_last=True,
            metadata={"provider_finish_reason": "STOP"},
        )

    await recover_output(
        resumed,
        messages,
        requested_tokens=10,
        maximum_tokens=20,
        state=json.loads(json.dumps(state)),
    )


async def test_native_compactor_preserves_authority_and_tool_groups():
    from agentscope.message import Msg, ToolCallBlock, ToolResultBlock
    from open_deep_research.agentscope_runtime.context import NativeContextCompactor
    from open_deep_research.agentscope_runtime.messages import validate_tool_pairs

    question = UserMsg("u", "question")
    evidence = UserMsg("domain", "coverage and evidence references")
    call = Msg(
        name="a",
        role="assistant",
        content=[ToolCallBlock(id="call", name="search", input="{}")],
    )
    result = Msg(
        name="a",
        role="assistant",
        content=[ToolResultBlock(id="call", name="search", output="result")],
    )
    last = UserMsg("u", "continue")
    protected = [question, evidence, call, result, last]
    limit = sum(len(m.model_dump_json()) for m in protected)
    compact = NativeContextCompactor(
        max_chars=limit, protected_ids={evidence.id, result.id}
    )
    messages = [question, UserMsg("u", "old" * 1000), evidence, call, result, last]
    projected = await compact(messages)
    assert [m.id for m in projected] == [m.id for m in protected]
    validate_tool_pairs(projected)
    with pytest.raises(ValueError, match="protected context"):
        await NativeContextCompactor(max_chars=1, protected_ids={evidence.id})(messages)

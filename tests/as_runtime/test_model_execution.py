"""T020/T021/T022 离线 wire、流式故障与恢复契约。"""

import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from pydantic import BaseModel, SecretStr
from agentscope.message import UserMsg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from open_deep_research.agentscope_runtime.gateway import (
    SandboxBinding,
    SandboxChatModel,
    GatewayCallError,
    GovernedOpenAIChatModel,
)
from open_deep_research.agentscope_runtime.model_policy import (
    ModelCallPolicy,
    ModelPolicyMiddleware,
    recover_output,
    RecoveryExhausted,
)

pytestmark = pytest.mark.asyncio


def response(text="ok", reason="stop"):
    return ChatResponse(
        content=[TextBlock(text=text)],
        is_last=True,
        metadata={"provider_finish_reason": reason},
    )


@pytest.mark.parametrize("mode", ["plain", "stream", "structured"])
async def test_sandbox_wire_v2_auth_and_result(mode):
    seen = []

    async def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/v2/models/complete"
        assert request.headers["authorization"] == "Bearer fixture-capability"
        assert request.headers["x-sandbox-nonce"]
        assert "fixture-capability" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "protocol_version": 2,
                "logical_operation_id": body["logical_operation_id"],
                "requested_model": "openai:test",
                "status": "completed",
                "message": {"role": "assistant", "content": "ok"},
                "structured": {"value": 1} if mode == "structured" else None,
                "finish_reason": "stop",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve), base_url="https://gateway.invalid"
    ) as client:
        model = SandboxChatModel(
            binding=SandboxBinding(
                "https://gateway.invalid",
                "run",
                "task",
                "researcher",
                "researching",
                SecretStr("fixture-capability"),
            ),
            model="openai:test",
            stream=mode == "stream",
            client=client,
        )

        class Answer(BaseModel):
            value: int

        if mode == "structured":
            result = await model.generate_structured_output(
                [UserMsg("user", "hello")], Answer
            )
            assert result.content == {"value": 1}
            assert seen[0]["structured_schema"]["type"] == "object"
        else:
            result = await model([UserMsg("user", "hello")])
            if mode == "stream":
                chunks = [c async for c in result]
                assert len(chunks) == 1
                result = chunks[0]
            assert result.content[0].text == "ok"
        assert result.usage.input_tokens == 2
        assert result.metadata["retry_owner"] == "gateway"
        await model.aclose()
        assert not client.is_closed


async def test_sandbox_uncertain_is_not_retried():
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        raise GatewayCallError("uncertain", uncertain=True)

    policy = ModelCallPolicy([object(), object()])
    with pytest.raises(GatewayCallError):
        await policy.invoke(handler, {"messages": []}, {})
    assert len(calls) == 1


async def test_fallback_state_survives_serialization():
    a, b = object(), object()
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs["current_model"])
        if kwargs["current_model"] is a:
            raise ConnectionError("offline")
        return response()

    policy = ModelCallPolicy([a, b], attempts=1)
    middleware = ModelPolicyMiddleware(policy)
    agent = SimpleNamespace(state=AgentState())
    await middleware.on_model_call(agent, {"messages": []}, handler)
    restored = SimpleNamespace(
        state=AgentState.model_validate_json(agent.state.model_dump_json())
    )
    await middleware.on_model_call(restored, {"messages": []}, handler)
    assert calls == [a, b, b]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_terminal_http_errors_do_not_fallback(status):
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        raise GatewayCallError("http", status_code=status)

    with pytest.raises(GatewayCallError):
        await ModelCallPolicy([object(), object()], attempts=3).invoke(
            handler, {"messages": []}, {}
        )
    assert len(calls) == 1


async def test_stream_after_first_chunk_never_replays():
    calls = []

    async def stream():
        yield ChatResponse(content=[TextBlock(text="visible")], is_last=False)
        raise ConnectionError("broken")

    async def handler(**kwargs):
        calls.append(1)
        return stream()

    result = await ModelCallPolicy([object(), object()]).invoke(
        handler, {"messages": []}, {}
    )
    assert (await anext(result)).content[0].text == "visible"
    with pytest.raises(ConnectionError):
        await anext(result)
    assert len(calls) == 1


async def test_first_packet_timeout_falls_back_and_closes():
    a, b = object(), object()
    closed = []

    async def stalled():
        try:
            await asyncio.Event().wait()
            yield response()
        finally:
            closed.append(True)

    async def handler(**kwargs):
        return stalled() if kwargs["current_model"] is a else response()

    result = await ModelCallPolicy(
        [a, b], attempts=1, first_packet_timeout=0.01
    ).invoke(handler, {"messages": []}, {})
    assert result.is_last and closed == [True]


async def test_bounded_escalation_continuation_overlap():
    calls = []
    results = iter(
        [
            response("discarded", "length"),
            response("hello world", "length"),
            response("world done"),
        ]
    )

    async def call(messages, limit):
        calls.append(limit)
        return next(results)

    state = {}
    result = await recover_output(
        call,
        [UserMsg("user", "q")],
        requested_tokens=10,
        maximum_tokens=20,
        state=state,
    )
    assert calls == [10, 20, 20]
    assert result.content[0].text == "hello world done"
    assert result.metadata["recovery_finish_reasons"] == ["length", "length", "stop"]
    assert state["completed"]


async def test_truncated_never_returns_success_and_state_retained():
    calls = []

    async def call(messages, limit):
        calls.append(1)
        return response("partial", "length")

    state = {}
    with pytest.raises(RecoveryExhausted):
        await recover_output(
            call,
            [],
            requested_tokens=10,
            maximum_tokens=10,
            continuations=1,
            state=state,
        )
    assert len(calls) == 2 and state["text"] == "partial"


async def test_context_compaction_is_bounded():
    calls = []

    async def call(messages, limit):
        calls.append(1)
        raise ValueError("maximum context length exceeded")

    async def compact(messages):
        return messages

    with pytest.raises(ValueError):
        await recover_output(
            call,
            [],
            requested_tokens=10,
            maximum_tokens=10,
            compact=compact,
            context_attempts=1,
        )
    assert len(calls) == 2


async def test_truncated_tools_are_not_executed_or_concatenated():
    async def call(messages, limit):
        return ChatResponse(
            content=[ToolCallBlock(id="c", name="t", input="{")],
            is_last=True,
            metadata={"provider_finish_reason": "length"},
        )

    with pytest.raises(RecoveryExhausted):
        await recover_output(call, [], requested_tokens=10, maximum_tokens=20)


async def test_litellm_native_mock_http_stream_preserves_length(monkeypatch):
    from agentscope.credential import OpenAICredential

    # SDK 在当前环境使用 httpx2；采用其原生 transport，零模型网络请求。
    import httpx2 as sdk_httpx

    async def serve(request):
        assert request.headers["authorization"] == "Bearer fixture-key"
        data = {
            "id": "req",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "alias",
            "choices": [
                {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
            ],
        }
        final = {
            "id": "req",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "alias",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
        }
        return sdk_httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: "
            + json.dumps(data)
            + "\n\ndata: "
            + json.dumps(final)
            + "\n\ndata: [DONE]\n\n",
        )

    async with sdk_httpx.AsyncClient(
        transport=sdk_httpx.MockTransport(serve), trust_env=False
    ) as client:
        model = GovernedOpenAIChatModel(
            credential=OpenAICredential(
                api_key="fixture-key", base_url="https://proxy.invalid/v1"
            ),
            model="alias",
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        chunks = [c async for c in await model([UserMsg("user", "q")])]
        assert chunks[-1].is_last
        assert chunks[-1].metadata["provider_finish_reason"] == "length"
        assert chunks[-1].content[0].text == "partial"


async def test_policy_circuit_skips_open_candidate():
    from open_deep_research.models.circuit import ModelCircuitPolicy

    a, b = object(), object()
    calls = []

    async def handler(**kwargs):
        calls.append(kwargs["current_model"])
        if kwargs["current_model"] is a:
            raise ConnectionError("down")
        return response()

    policy = ModelCallPolicy(
        [a, b], attempts=1, circuit_policy=ModelCircuitPolicy(failure_threshold=1)
    )
    await policy.invoke(handler, {"messages": []}, {})
    await policy.invoke(handler, {"messages": []}, {})
    assert calls == [a, b, b]


async def test_policy_cancellation_no_fallback():
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await ModelCallPolicy([object(), object()]).invoke(
            handler, {"messages": []}, {}
        )
    assert len(calls) == 1


async def test_recovery_resumed_fragments_and_terminal_state():
    state = {
        "continuations": 1,
        "context_attempts": 0,
        "escalated": False,
        "text": "hello world",
        "finish_reasons": ["length"],
    }

    async def call(messages, limit):
        assert messages[-2].content[0].text == "hello world"
        return response("world done")

    result = await recover_output(
        call, [], requested_tokens=10, maximum_tokens=10, state=state
    )
    assert result.content[0].text == "hello world done"
    with pytest.raises(ValueError, match="completed"):
        await recover_output(
            call, [], requested_tokens=10, maximum_tokens=10, state=state
        )


async def test_sandbox_response_mismatch_is_uncertain():
    async def serve(request):
        return httpx.Response(
            200,
            json={
                "protocol_version": 2,
                "logical_operation_id": "wrong",
                "requested_model": "test",
                "status": "completed",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(serve), base_url="https://gateway.invalid"
    ) as client:
        model = SandboxChatModel(
            binding=SandboxBinding(
                "https://gateway.invalid",
                "run",
                "task",
                "researcher",
                "researching",
                SecretStr("fixture"),
            ),
            model="test",
            client=client,
        )
        with pytest.raises(GatewayCallError) as caught:
            await model([UserMsg("u", "q")])
        assert caught.value.uncertain


async def test_native_agent_uses_policy_middle_context():
    from agentscope.agent import Agent
    from agentscope.credential import CredentialBase
    from agentscope.model import ChatModelBase

    class Fake(ChatModelBase):
        def __init__(self, fail):
            super().__init__(
                CredentialBase(),
                str(fail),
                SandboxChatModel.Parameters(),
                stream=False,
                max_retries=0,
            )
            from agentscope.formatter import OpenAIChatFormatter

            self.formatter = OpenAIChatFormatter()
            self.fail = fail

        async def _call_api(self, *args, **kwargs):
            if self.fail:
                raise ConnectionError("down")
            return response("done")

    a, b = Fake(True), Fake(False)
    middleware = ModelPolicyMiddleware(ModelCallPolicy([a, b], attempts=1))
    agent = Agent(
        name="test", system_prompt="Answer concisely", model=a, middlewares=[middleware]
    )
    result = await agent.reply(UserMsg("user", "q"))
    assert result.get_text_content() == "done"
    assert agent.state.middle_context["model_route"]["active_candidate_index"] == 1


@pytest.mark.parametrize("structured", [False, True])
async def test_litellm_native_plain_and_structured_over_authorized_transport(
    structured,
):
    import httpx2 as sdk_httpx
    from agentscope.credential import OpenAICredential

    calls = []

    class Answer(BaseModel):
        value: int

    async def serve(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer fixture-key"
        message = {"role": "assistant", "content": "ok"}
        if structured:
            name = body["tools"][0]["function"]["name"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": name, "arguments": '{"value":1}'},
                    }
                ],
            }
        return sdk_httpx.Response(
            200,
            json={
                "id": "request1",
                "object": "chat.completion",
                "created": 1,
                "model": "alias",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if structured else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )

    async with sdk_httpx.AsyncClient(
        transport=sdk_httpx.MockTransport(serve), trust_env=False
    ) as client:
        model = GovernedOpenAIChatModel(
            credential=OpenAICredential(
                api_key="fixture-key", base_url="https://proxy.invalid/v1"
            ),
            model="alias",
            stream=False,
            max_retries=0,
            client_kwargs={"http_client": client, "max_retries": 0},
        )
        if structured:
            result = await model.generate_structured_output(
                [UserMsg("user", "q")], Answer
            )
            assert result.content == {"value": 1}
        else:
            result = await model([UserMsg("user", "q")])
            assert result.metadata["provider_finish_reason"] == "stop"
        assert len(calls) == 1 and result.usage.input_tokens == 3


async def test_exhausted_recovery_does_not_call_again_on_restore():
    state = {}
    count = 0

    async def call(messages, limit):
        nonlocal count
        count += 1
        return response("truncated", "length")

    for _ in range(2):
        with pytest.raises(RecoveryExhausted):
            await recover_output(
                call,
                [],
                requested_tokens=10,
                maximum_tokens=10,
                continuations=0,
                state=state,
            )
    assert count == 1


async def test_factory_complete_recovery_is_wired(monkeypatch):
    from open_deep_research.agentscope_runtime.models import ModelFactory, CredentialBinding
    from open_deep_research.agentscope_runtime.run_config import RunConfig

    spec = "openai:fixture"
    run = RunConfig.compile(
        {
            "configurable": {
                "research_model": spec,
                "output_token_escalation_enabled": False,
            }
        }
    )
    binding = CredentialBinding("ref", "run", "r", (spec,), SecretStr("key"))
    factory = ModelFactory(
        run, scope="run", owner="r", bindings={"researcher": binding}
    )
    results = iter([response("hello world", "length"), response("world done")])

    class Model:
        async def __call__(self, **kwargs):
            return next(results)

    monkeypatch.setattr(factory, "build", lambda *args: Model())
    state = {}
    result = await factory.complete_with_recovery("researcher", [], state=state)
    assert result.content[0].text == "hello world done"
    assert state["route"]["active_candidate_index"] == 0
    assert len(state["output"]["attempt_usage"]) == 2


async def test_unknown_finish_reason_is_not_assumed_complete():
    async def call(messages, limit):
        return ChatResponse(content=[TextBlock(text="maybe partial")], is_last=True)

    with pytest.raises(RecoveryExhausted, match="finish reason unavailable"):
        await recover_output(call, [], requested_tokens=10, maximum_tokens=10)

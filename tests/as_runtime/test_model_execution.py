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


@pytest.mark.parametrize("failure", ["timeout", "http500"])
async def test_lost_model_reply_reads_receipt_without_redispatch(failure):
    seen = []
    async def serve(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers["x-sandbox-nonce"]))
        if request.url.path.endswith("complete"):
            if failure == "timeout":
                raise httpx.ReadTimeout("reply lost", request=request)
            return httpx.Response(500)
        if len(seen) == 2:
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json={
            "protocol_version": 2, "logical_operation_id": body["logical_operation_id"],
            "requested_model": "openai:test", "status": "completed",
            "message": {"role": "assistant", "content": "recovered"},
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="https://gateway.invalid") as client:
        model = SandboxChatModel(binding=SandboxBinding("https://gateway.invalid", "r", "t", "researcher", "researching", SecretStr("cap")),
                                 model="openai:test", stream=False, client=client)
        result = await model([UserMsg("user", "q")])
    assert result.content[0].text == "recovered"
    assert [path for path, _, _ in seen] == ["/v2/models/complete", "/v2/models/lookup", "/v2/models/lookup"]
    assert all(body == seen[0][1] for _, body, _ in seen)
    assert len({nonce for _, _, nonce in seen}) == 3


async def test_missing_model_receipt_stays_unknown_and_lookup_is_bounded(monkeypatch):
    import open_deep_research.agentscope_runtime.gateway as gateway_module
    monkeypatch.setattr(gateway_module, "RECEIPT_LOOKUP_SECONDS", 0.02)
    paths = []
    async def serve(request):
        paths.append(request.url.path)
        if request.url.path.endswith("complete"):
            raise httpx.ReadTimeout("reply lost", request=request)
        return httpx.Response(200, content=b"null")
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="https://gateway.invalid") as client:
        model = SandboxChatModel(binding=SandboxBinding("https://gateway.invalid", "r", "t", "researcher", "researching", SecretStr("cap")),
                                 model="openai:test", stream=False, client=client)
        with pytest.raises(GatewayCallError, match="outcome_unknown"):
            await asyncio.wait_for(model([UserMsg("user", "q")]), 1)
    assert paths.count("/v2/models/complete") == 1
    assert "/v2/models/lookup" in paths


@pytest.mark.parametrize("invalid", [None, {}, "truncated"])
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_sandbox_structured_repair_is_bounded_and_has_distinct_receipts(invalid, repair_succeeds):
    import jsonschema

    from open_deep_research.agentscope_runtime.gateway import gateway_operation_scope

    seen = []

    def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        value = {"value": 1} if repair_succeeds and len(seen) == 2 else None if invalid == "truncated" else invalid
        return httpx.Response(200, json={
            "protocol_version": 2,
            "logical_operation_id": body["logical_operation_id"],
            "requested_model": "fixture", "status": "completed",
            "message": {"role": "assistant", "content": json.dumps(value) if value is not None else ""},
            "structured": value,
            "finish_reason": "length" if invalid == "truncated" and not (repair_succeeds and len(seen) == 2) else "stop",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        })

    class Answer(BaseModel):
        value: int

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="https://gateway.invalid") as client:
        model = SandboxChatModel(
            binding=SandboxBinding("https://gateway.invalid", "run", "task", "quality_evaluation", "researching", SecretStr("fixture")),
            model="fixture", client=client, structured_attempts=2,
        )
        with gateway_operation_scope("quality"):
            if repair_succeeds:
                result = await model.generate_structured_output([UserMsg("user", "judge")], Answer)
                assert result.content == {"value": 1}
            else:
                with pytest.raises((GatewayCallError, jsonschema.ValidationError)):
                    await model.generate_structured_output([UserMsg("user", "judge")], Answer)
        assert len(seen) == 2
        assert len({row["logical_operation_id"] for row in seen}) == 2
        assert "every required property" in str(seen[1]["messages"])
        assert "Validation error:" in str(seen[1]["messages"])
        if invalid == {}:
            assert "'value' is a required property" in seen[1]["messages"][-1]["content"][0]["text"]
        if invalid == "truncated":
            assert "concise field values" in str(seen[1]["messages"])


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_schema_repair_identifies_all_invalid_source_choices_within_attempt_budget(repair_succeeds):
    import jsonschema
    seen = []
    choices = [f"source-{i}:0" for i in range(200)] + [f"resource-record:{i}" for i in range(3)]
    schema = {"type": "object", "required": ["refs"], "properties": {
        "refs": {"type": "array", "items": {"type": "string", "enum": choices}}}}

    def serve(request):
        body = json.loads(request.content)
        seen.append(body)
        value = {"refs": ["resource-record:3", "resource-record:4"]}
        if len(seen) == 2:
            feedback = body["messages"][-1]["content"][0]["text"]
            for index, invalid in enumerate(value["refs"]):
                assert f'"path": ["refs", {index}]' in feedback
                assert invalid in feedback
            assert all(f"resource-record:{i}" in feedback for i in range(3))
            assert len(feedback) < 2000
            if repair_succeeds:
                value = {"refs": ["resource-record:0", "resource-record:2"]}
        return httpx.Response(200, json={"protocol_version": 2,
            "logical_operation_id": body["logical_operation_id"], "requested_model": "fixture",
            "status": "completed", "message": {"role": "assistant", "content": json.dumps(value)},
            "structured": value, "finish_reason": "stop", "usage": {"input_tokens": 2, "output_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="https://gateway.invalid") as client:
        model = SandboxChatModel(binding=SandboxBinding("https://gateway.invalid", "r", "t",
            "report_review", "writing", SecretStr("fixture")), model="fixture", client=client, structured_attempts=2)
        if repair_succeeds:
            result = await model.generate_structured_output([UserMsg("user", "judge")], schema)
            assert result.content == {"refs": ["resource-record:0", "resource-record:2"]}
        else:
            with pytest.raises(jsonschema.ValidationError):
                await model.generate_structured_output([UserMsg("user", "judge")], schema)
    assert len(seen) == 2
    assert len({row["logical_operation_id"] for row in seen}) == 2


async def test_sandbox_uncertain_is_not_retried():
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        raise GatewayCallError("uncertain", uncertain=True)

    policy = ModelCallPolicy([object(), object()])
    with pytest.raises(GatewayCallError):
        await policy.invoke(handler, {"messages": []}, {})
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", [
    '{"value":0,"value":1}',
    '{"value":1,"nested":{"score":0,"score":5}}',
    '{' + '"evidence_coverage":5,' * 1010,
])
async def test_structured_repair_rejects_duplicate_or_degenerate_arguments(invalid):
    from open_deep_research.sandbox.gateway import _wire_structured_args
    calls = []

    def serve(request):
        body = json.loads(request.content)
        calls.append(body)
        message = {"role": "assistant", "tool_calls": [{"id": "c", "type": "function",
            "function": {"name": "__insightforge_structured_output",
                         "arguments": invalid if len(calls) == 1 else '{"value":2}'}}]}
        extracted = _wire_structured_args(message)
        if len(calls) == 1:
            assert extracted is None
        else:
            assert body["tool_choice"] == "none"
            message = {"role": "assistant", "content": '{"value":2}'}
        return httpx.Response(200, json={"logical_operation_id": body["logical_operation_id"],
            "requested_model": "fixture", "status": "completed", "message": message,
            "structured": extracted, "finish_reason": "tool_calls"})

    class Answer(BaseModel):
        value: int

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="https://gateway.invalid") as client:
        model = SandboxChatModel(binding=SandboxBinding("https://gateway.invalid", "r", "t",
            "quality_evaluation", "researching", SecretStr("cap")), model="fixture", client=client,
            structured_attempts=2)
        result = await model.generate_structured_output([UserMsg("user", "judge")], Answer)
    assert result.content == {"value": 2}
    assert len(calls) == 2
    assert len({c["logical_operation_id"] for c in calls}) == 2
    feedback = str(calls[-1]["messages"][-1])
    assert "only once" in feedback and len(feedback) < 1600
    assert invalid not in feedback


@pytest.mark.parametrize("value", [
    '{"value":1,"value":1}',
    '```json\n{"value":1}\n```',
    '```\n{"value":1}\n```',
])
def test_structured_json_lossless_normalization(value):
    from open_deep_research.models.protocol_errors import parse_structured_json
    assert parse_structured_json(value) == {"value": 1}


@pytest.mark.parametrize("value", [
    '{"value":1,"value":true}',
    '{"value":[1],"value":[true]}',
    '{"value":1,"value":2}',
    'Explanation\n```json\n{"value":1}\n```',
    '```json\n{"value":1}\n```\nOther text',
    '```json\n{"value":1}\n```\n```json\n{"value":2}\n```',
    '```json\n{"value":\n```',
])
def test_structured_json_rejects_conflict_partial_or_surrounding_text(value):
    from open_deep_research.models.protocol_errors import parse_structured_json
    with pytest.raises(json.JSONDecodeError):
        parse_structured_json(value)


@pytest.mark.parametrize("index", [0, 1])
async def test_recorded_valid_example_audits_need_no_extra_model_dispatch(index):
    from pathlib import Path
    from pydantic import create_model, Field
    from open_deep_research.models.protocol_errors import parse_structured_json
    from open_deep_research.report.models import ReportExampleReview
    from open_deep_research.sandbox.gateway import _wire_structured_args
    records = json.loads((Path(__file__).parents[1] / 'fixtures/research_quality/sidecar_example_protocol_replay.json').read_text(encoding='utf-8'))
    identity = parse_structured_json(records[1]['message']['content'])['example_checks'][0]['example_id']
    example = create_model('RecordedExample', __base__=ReportExampleReview, example_id=(str, Field(default=identity)))
    schema = create_model('RecordedExampleAudit', example_checks=(list[example], ...))
    calls = []
    def serve(request):
        body = json.loads(request.content)
        calls.append(body)
        message = records[index]['message']
        if index == 0:
            assert _wire_structured_args(message)['example_checks'][0]['execution_checks'][0]['observed_lifecycle'] == 'finite'
        # A previously persisted Gateway receipt can still contain structured=null.
        return httpx.Response(200, json={'logical_operation_id': body['logical_operation_id'],
            'requested_model': 'fixture', 'status': 'completed', 'message': message,
            'structured': None, 'finish_reason': records[index]['finish_reason'],
            'usage': {'input_tokens': 10, 'output_tokens': 5}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url='https://gateway.invalid') as client:
        model = SandboxChatModel(binding=SandboxBinding('https://gateway.invalid', 'r', 't',
            'report_review', 'writing', SecretStr('cap')), model='fixture', client=client, structured_attempts=1)
        result = await model.generate_structured_output([UserMsg('user', 'Audit the example')], schema)
    assert len(calls) == 1
    check = result.content['example_checks'][0]
    assert check['status'] == 'satisfies'
    assert [r['observed_lifecycle'] for r in check['execution_checks']] == ['finite', 'persistent']


@pytest.mark.parametrize("status,code,uncertain", [
    ("failed", "model_not_dispatched", False), ("failed", "authentication", False),
    ("failed", "invalid_request", False), ("uncertain", "gateway_connection_failed", True),
])
async def test_structured_gateway_failure_preserves_dispatch_certainty(status, code, uncertain):
    calls = []
    def serve(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"logical_operation_id": body["logical_operation_id"],
            "requested_model": "fixture", "status": status, "error_code": code})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="http://gateway") as client:
        model = SandboxChatModel(binding=SandboxBinding("http://gateway", "r", "t", "web_evidence",
            "researching", SecretStr("cap")), model="fixture", client=client, structured_attempts=3)
        with pytest.raises(GatewayCallError, match=code) as raised:
            await model.generate_structured_output([UserMsg("user", "q")], {"type": "object"})
    assert raised.value.uncertain is uncertain
    assert len(calls) == 1  # Only a completed malformed response receives format repair.


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


async def test_failed_transport_attempt_is_counted_before_fallback():
    calls = []
    state = {}

    async def handler(**kwargs):
        calls.append(kwargs["current_model"])
        if len(calls) == 1:
            raise GatewayCallError("limited", status_code=429)
        return response()

    await ModelCallPolicy([object(), object()], attempts=1).invoke(
        handler, {"messages": []}, state
    )
    assert state["physical_attempts"] == len(calls) == 2
    assert len(state["attempt_failures"]) == 1


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


@pytest.mark.parametrize('identity', ['COV-allowed', 'COV-invented'])
async def test_native_structured_schema_extras_are_enforced_and_paid_response_is_retained(identity):
    import httpx2 as sdk_httpx
    import jsonschema
    from agentscope.credential import OpenAICredential
    from pydantic import Field

    class Answer(BaseModel):
        requirement_id: str = Field(json_schema_extra={'enum': ['COV-allowed']})

    calls = []
    async def serve(request):
        body = json.loads(request.content)
        calls.append(body)
        name = body['tools'][0]['function']['name']
        return sdk_httpx.Response(200, json={'id': 'enum-receipt', 'object': 'chat.completion', 'created': 1,
            'model': 'alias', 'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps({'requirement_id': identity})}}]}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})

    async with sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(serve), trust_env=False) as client:
        model = GovernedOpenAIChatModel(credential=OpenAICredential(api_key='fixture-key', base_url='https://proxy.invalid/v1'),
            model='alias', stream=False, max_retries=0, client_kwargs={'http_client': client, 'max_retries': 0})
        if identity == 'COV-allowed':
            result = await model.generate_structured_output([UserMsg('user', 'check')], Answer)
            assert result.content == {'requirement_id': identity}
        else:
            with pytest.raises(jsonschema.ValidationError) as failure:
                await model.generate_structured_output([UserMsg('user', 'check')], Answer)
            assert 'COV-allowed' in failure.value.message and 'requirement_id' in failure.value.message
            assert failure.value.billed_response.usage.input_tokens == 3
            assert failure.value.billed_response.metadata['request_id'] == 'enum-receipt'
    assert len(calls) == 1  # Repair stays with the existing caller's attempt budget.


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

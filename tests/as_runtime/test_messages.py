"""AS-A017：实际旧序列化夹具与 AgentScope 原生协议往返。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agentscope.message import (
    Base64Source,
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason

from open_deep_research.as_runtime.messages import (
    MessageCompatibilityError,
    dump_messages,
    load_messages,
    read_legacy_messages,
    validate_tool_pairs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_messages.json"


def native_message():
    return Msg(
        name="researcher",
        role="assistant",
        id="msg-1",
        content=[
            TextBlock(text="资料"),
            ThinkingBlock(thinking="检查", signature="opaque-signature"),
            DataBlock(source=Base64Source(data="aGVsbG8=", media_type="audio/wav")),
            HintBlock(hint="来源约束", source="supervisor"),
            ToolCallBlock(
                id="call-1", name="search", input='{"q":"test"}', state="finished"
            ),
            ToolResultBlock(
                id="call-1",
                name="search",
                output="failed",
                state="error",
                metadata={"evidence_id": "e1"},
            ),
        ],
        usage=Usage(
            input_tokens=11,
            output_tokens=3,
            cache_input_tokens=5,
            cache_creation_input_tokens=2,
        ),
        metadata={"provider_finish_reason": "length", "cached_output_tokens": None},
        finished_reason=ReplyFinishedReason.ERROR,
        error=ErrorInfo(type=ErrorType.UPSTREAM, message="upstream failed"),
        structured_output={"coverage": {"r1": "partial"}},
        finished_at="2026-09-14T21:00:00",
    )


def test_native_roundtrip_all_blocks_and_control_fields():
    message = native_message()
    envelope = json.loads(json.dumps(dump_messages([message])))
    restored = load_messages(envelope)
    assert restored[0].model_dump(mode="json") == message.model_dump(mode="json")
    validate_tool_pairs(restored)


def test_unknown_usage_stays_unknown():
    message = Msg(name="researcher", role="assistant", content=[])
    assert load_messages(dump_messages([message]))[0].usage is None


@pytest.mark.parametrize("where", ["envelope", "message", "block", "usage"])
def test_future_native_fields_fail_closed(where):
    value = dump_messages([native_message()])
    if where == "envelope":
        value["schema"] = "future.v2"
    elif where == "message":
        value["messages"][0]["future"] = True
    elif where == "block":
        value["messages"][0]["content"][0]["future"] = True
    else:
        value["messages"][0]["usage"]["future"] = 7
    with pytest.raises(MessageCompatibilityError):
        load_messages(value)


def test_real_legacy_serializer_fixture_is_lossless_and_readonly():
    original = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = json.dumps(original, sort_keys=True)
    history = read_legacy_messages(original, complete=True)
    assert history.original() == original
    assert json.dumps(original, sort_keys=True) == snapshot
    assert [m.id for m in history.messages] == [
        "sys-1",
        "user-1",
        "ai-1",
        "result-1",
        "ai-2",
    ]
    user = history.messages[1]
    assert [b.type for b in user.content] == ["text", "data", "data"]
    assert user.content[1].source.media_type == "image/png"
    assert user.content[2].source.media_type == "audio/wav"
    call = history.messages[2].content[-1]
    result = history.messages[3].content[0]
    assert (call.id, call.name) == (result.id, result.name) == ("call-1", "fetch_url")
    assert (
        result.state == "error"
        and result.metadata["artifact"]["source_id"] == "source-1"
    )
    assert history.messages[2].usage.cache_input_tokens == 4
    final = history.messages[-1]
    assert final.usage is None
    assert final.finished_reason is None  # length 不能误判完成
    assert final.metadata["legacy_response_metadata"]["finish_reason"] == "length"
    assert final.structured_output == {"title": "草稿", "complete": False}
    assert final.metadata["legacy_invalid_tool_calls"][0]["id"] == "bad-1"
    with pytest.raises(MessageCompatibilityError, match="read-only"):
        dump_messages(history.messages)
    copy = history.original()
    copy[0]["data"]["content"] = "changed"
    assert history.original() == original


@pytest.mark.parametrize(
    "record",
    [
        {"type": "AIMessageChunk", "data": {"content": "x"}},
        {"type": "future", "data": {}},
        {"type": "human", "data": {"type": "system", "content": "x"}},
        {"type": "human", "data": {"content": [{"type": "unknown_media"}]}},
        {"type": "human", "data": {"content": "x"}, "schema_version": 999},
        {"type": "tool", "data": {"content": "x", "tool_call_id": "orphan"}},
    ],
)
def test_unsupported_legacy_schema_is_explicit_error(record):
    with pytest.raises(MessageCompatibilityError):
        read_legacy_messages([record])


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_call", "duplicate_result", "wrong_id", "wrong_name", "missing_result"],
)
def test_pairing_rejects_invalid_transcript(mutation):
    message = native_message()
    call, result = message.content[-2:]
    if mutation == "duplicate_call":
        message.content.insert(-1, call.model_copy())
    elif mutation == "duplicate_result":
        message.content.append(result.model_copy())
    elif mutation == "wrong_id":
        result.id = "other"
    elif mutation == "wrong_name":
        result.name = "other"
    else:
        message.content.pop()
    with pytest.raises(MessageCompatibilityError):
        validate_tool_pairs([message])


def test_pending_snapshot_and_partial_raw_arguments_preserved():
    message = Msg(
        name="researcher",
        role="assistant",
        content=[ToolCallBlock(id="partial", name="search", input='{"q":')],
    )
    restored = load_messages(dump_messages([message]))
    validate_tool_pairs(restored, complete=False)
    assert restored[0].content[0].input == '{"q":'


def test_raw_provider_tool_arguments_and_thinking_signature_preserved():
    raw = [
        {
            "type": "ai",
            "data": {
                "content": [
                    {"type": "thinking", "thinking": "x", "signature": "signed"}
                ],
                "additional_kwargs": {
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "fetch_url", "arguments": "{bad json"},
                        }
                    ]
                },
            },
        }
    ]
    history = read_legacy_messages(raw)
    assert history.messages[0].content[0].signature == "signed"
    assert history.messages[0].content[1].input == "{bad json"
    assert history.original() == raw


def test_codec_does_not_import_langchain():
    source = 'import sys; from open_deep_research.as_runtime.messages import read_legacy_messages; assert not any(k.startswith("langchain") for k in sys.modules)'
    result = subprocess.run(
        [sys.executable, "-c", source],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("conflict", [False, True])
def test_anthropic_tool_use_and_normalized_call_are_not_duplicated(conflict):
    raw = [
        {
            "type": "ai",
            "data": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "search",
                        "input": {"q": "one"},
                    }
                ],
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "search",
                        "args": {"q": "two" if conflict else "one"},
                    }
                ],
            },
        }
    ]
    if conflict:
        with pytest.raises(MessageCompatibilityError, match="conflicting"):
            read_legacy_messages(raw)
    else:
        history = read_legacy_messages(raw)
        assert len(history.messages[0].content) == 1
        assert history.original() == raw

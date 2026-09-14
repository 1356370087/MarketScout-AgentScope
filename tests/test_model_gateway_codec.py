"""Gateway Wire V2 message and structured-output contracts."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from open_deep_research.models.codec import (
    STRUCTURED_OUTPUT_TOOL_NAME,
    MessageCodecError,
    decode_message,
    encode_message,
    parse_structured_output,
    structured_output_tool,
)


class Answer(BaseModel):
    value: int


def test_codec_covers_all_roles_and_tool_calls() -> None:
    assert encode_message(SystemMessage(content="policy"))["role"] == "system"
    assert encode_message(HumanMessage(content="question"))["role"] == "user"
    assert encode_message(ToolMessage(content="result", tool_call_id="call-1")) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "result",
    }
    encoded = encode_message(
        AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "search", "args": {"q": "x"}}],
        )
    )
    decoded = decode_message(encoded)
    assert isinstance(decoded, AIMessage)
    assert decoded.tool_calls[0]["args"] == {"q": "x"}


def test_codec_preserves_multimodal_content() -> None:
    encoded = encode_message(
        HumanMessage(
            content=[
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ]
        )
    )
    assert encoded["content"][1]["image_url"]["url"].endswith("a.png")


def test_codec_rejects_invalid_tool_json() -> None:
    with pytest.raises(MessageCodecError, match="valid JSON"):
        decode_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{"},
                    }
                ],
            }
        )


def test_structured_output_uses_forced_synthetic_tool() -> None:
    tool = structured_output_tool(Answer)
    assert tool["function"]["name"] == STRUCTURED_OUTPUT_TOOL_NAME
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "name": STRUCTURED_OUTPUT_TOOL_NAME,
                "args": {"value": 7},
            }
        ],
    )
    assert parse_structured_output(message, Answer) == Answer(value=7)


def test_parse_structured_output_applies_payload_transform() -> None:
    """Provider payload variants normalize before Pydantic validation."""
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-1",
                "name": STRUCTURED_OUTPUT_TOOL_NAME,
                "args": {"answer": {"value": 7}},
            }
        ],
    )

    def unwrap_single_key(payload: dict) -> dict:
        if set(payload) == {"answer"} and isinstance(payload["answer"], dict):
            return dict(payload["answer"])
        return payload

    assert (
        parse_structured_output(message, Answer, payload_transform=unwrap_single_key)
        == Answer(value=7)
    )

"""Project-owned OpenAI-compatible message and tool wire codec."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel

STRUCTURED_OUTPUT_TOOL_NAME = "__insightforge_structured_output"


from open_deep_research.models.protocol_errors import MessageCodecError


def _json_arguments(value: Any) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise MessageCodecError("tool call arguments must contain valid JSON") from exc
        return value
    try:
        return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MessageCodecError("tool call arguments are not JSON serializable") from exc


def _content(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
        raise MessageCodecError("message content must be text or a multimodal part list")
    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, Mapping):
            raise MessageCodecError("multimodal message parts must be objects")
        item = dict(part)
        part_type = str(item.get("type") or "")
        if part_type == "text" and "text" in item:
            parts.append({"type": "text", "text": str(item["text"])})
        elif part_type in {"image_url", "input_image"}:
            image = item.get("image_url") or item.get("url")
            if isinstance(image, str):
                image = {"url": image}
            if not isinstance(image, Mapping) or not image.get("url"):
                raise MessageCodecError("image content requires image_url.url")
            normalized: dict[str, Any] = {"url": str(image["url"])}
            if image.get("detail") is not None:
                normalized["detail"] = str(image["detail"])
            parts.append({"type": "image_url", "image_url": normalized})
        else:
            raise MessageCodecError(f"unsupported multimodal content type: {part_type or 'missing'}")
    return parts


def encode_message(message: BaseMessage) -> dict[str, Any]:
    """Encode one LangChain message into the bounded Gateway Wire V2 shape."""
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _content(message.content)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _content(message.content)}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": str(message.tool_call_id),
            "content": _content(message.content),
        }
    if isinstance(message, AIMessage):
        encoded: dict[str, Any] = {
            "role": "assistant",
            "content": _content(message.content),
        }
        calls = []
        for call in message.tool_calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("id") or "")
            if not name or not call_id:
                raise MessageCodecError("assistant tool calls require id and name")
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _json_arguments(call.get("args", {})),
                    },
                }
            )
        if calls:
            encoded["tool_calls"] = calls
        return encoded
    if isinstance(message, ChatMessage) and message.role in {"system", "user", "assistant", "tool"}:
        return {"role": message.role, "content": _content(message.content)}
    raise MessageCodecError(f"unsupported message type: {type(message).__name__}")


def encode_messages(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Encode a complete model conversation."""
    return [encode_message(message) for message in messages]


def decode_message(payload: Mapping[str, Any]) -> BaseMessage:
    """Decode one Gateway Wire V2 message into a LangChain message."""
    role = str(payload.get("role") or "")
    content = payload.get("content", "")
    if role == "system":
        return SystemMessage(content=content)
    if role == "user":
        return HumanMessage(content=content)
    if role == "tool":
        call_id = str(payload.get("tool_call_id") or "")
        if not call_id:
            raise MessageCodecError("tool messages require tool_call_id")
        return ToolMessage(content=content, tool_call_id=call_id)
    if role != "assistant":
        raise MessageCodecError(f"unsupported wire message role: {role or 'missing'}")
    calls: list[dict[str, Any]] = []
    for raw in payload.get("tool_calls") or []:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("function"), Mapping):
            raise MessageCodecError("assistant tool call has an invalid shape")
        function = raw["function"]
        try:
            args = json.loads(str(function.get("arguments") or "{}"))
        except json.JSONDecodeError as exc:
            raise MessageCodecError("assistant tool call arguments are invalid JSON") from exc
        calls.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(function.get("name") or ""),
                "args": args,
                "type": "tool_call",
            }
        )
    return AIMessage(content=content or "", tool_calls=calls)


def normalize_tool_definition(tool: Any) -> dict[str, Any]:
    """Normalize project tools, LangChain tools and raw definitions to OpenAI JSON."""
    if isinstance(tool, Mapping):
        value = dict(tool)
    elif hasattr(tool, "model_dump"):
        value = tool.model_dump(mode="json")
    else:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", "")
        schema = getattr(tool, "input_schema", None) or getattr(tool, "args_schema", None)
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema = schema.model_json_schema()
        elif hasattr(schema, "model_json_schema"):
            schema = schema.model_json_schema()
        value = {
            "name": name,
            "description": description,
            "parameters": schema or {"type": "object", "properties": {}},
        }
    if value.get("type") == "function" and isinstance(value.get("function"), Mapping):
        function = dict(value["function"])
    else:
        function = {
            "name": value.get("name"),
            "description": value.get("description", ""),
            "parameters": value.get("parameters")
            or value.get("input_schema")
            or value.get("args_schema")
            or {"type": "object", "properties": {}},
        }
    if not function.get("name"):
        raise MessageCodecError("tool definition requires a name")
    if isinstance(function.get("parameters"), type) and issubclass(function["parameters"], BaseModel):
        function["parameters"] = function["parameters"].model_json_schema()
    return {"type": "function", "function": function}


def structured_output_tool(
    schema: type[BaseModel],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Build the mandatory synthetic function used for structured output.

    ``strict=False`` keeps the forced function call while avoiding provider-
    specific strict-schema subsets.  LiteLLM routes can target providers such
    as DeepSeek that reject otherwise valid JSON Schema constructs; the result
    is still validated locally with the original Pydantic model.
    """
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_OUTPUT_TOOL_NAME,
            "description": "Return the response using the required schema.",
            "parameters": _strict_json_schema(schema.model_json_schema()),
            "strict": strict,
        },
    }


def _strict_json_schema(value: Any) -> Any:
    """Normalize Pydantic JSON Schema for strict function-call providers.

    OpenAI-compatible endpoints such as DeepSeek require every declared object
    property to also appear in ``required`` when a tool is marked strict.  The
    runtime model may still define defaults for backwards-compatible parsing;
    this wire schema intentionally asks the provider to emit the full shape.
    """
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {key: _strict_json_schema(item) for key, item in value.items()}
    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["required"] = list(properties)
        normalized["additionalProperties"] = False
    return normalized


def structured_tool_choice() -> dict[str, Any]:
    """Force the synthetic structured-output function."""
    return {
        "type": "function",
        "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
    }


def stream_text_delta(chunk: Any) -> str:
    """Extract the text delta of an OpenAI-compatible streaming chunk.

    Non-string or missing content yields an empty string; tool-call deltas
    are intentionally ignored (Phase 1 streams free-text writing only).
    """
    parts: list[str] = []
    choices = getattr(chunk, "choices", None) or []
    for choice in choices:
        delta = getattr(choice, "delta", None)
        text = getattr(delta, "content", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def parse_structured_output(
    message: AIMessage,
    schema: type[BaseModel],
    *,
    payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> BaseModel:
    """Validate the synthetic function arguments with Pydantic.

    ``payload_transform`` optionally normalizes provider-specific payload
    variants (e.g. single-key wrappers) before validation.
    """
    matches = [
        call for call in message.tool_calls if call.get("name") == STRUCTURED_OUTPUT_TOOL_NAME
    ]
    if len(matches) != 1:
        raise MessageCodecError("structured response must call the synthetic function exactly once")
    payload = dict(matches[0].get("args") or {})
    if payload_transform is not None:
        payload = payload_transform(payload)
    return schema.model_validate(payload)


__all__ = [
    "MessageCodecError",
    "STRUCTURED_OUTPUT_TOOL_NAME",
    "decode_message",
    "encode_message",
    "encode_messages",
    "normalize_tool_definition",
    "parse_structured_output",
    "stream_text_delta",
    "structured_output_tool",
    "structured_tool_choice",
]

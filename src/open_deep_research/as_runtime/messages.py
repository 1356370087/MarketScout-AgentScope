"""AgentScope 2.0.8 消息持久化与旧消息只读投影（AS-T017）。

旧 codec 仍服务旧引擎；本模块不导入 LangChain，也不恢复旧检查点。
原始历史独立保留，无法投影的 schema 明确报错，不能静默丢弃内容。
"""

from __future__ import annotations

import json
import mimetypes
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
    Usage,
)
from pydantic import ValidationError

SCHEMA = "insightforge.agentscope.messages.v1"


class MessageCompatibilityError(ValueError):
    """历史/原生消息与已支持协议不兼容。错误不包含原始消息内容。"""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MessageCompatibilityError("message is not valid JSON") from exc


def _check_loss(source: Any, restored: Any, path: str = "message") -> None:
    """拒绝 Pydantic 默认忽略的未来字段，避免向前兼容造成静默丢失。"""
    if isinstance(source, dict):
        for key, value in source.items():
            if not isinstance(restored, dict) or key not in restored:
                raise MessageCompatibilityError(f"unsupported field at {path}.{key}")
            _check_loss(value, restored[key], f"{path}.{key}")
    elif isinstance(source, list):
        if not isinstance(restored, list) or len(source) != len(restored):
            raise MessageCompatibilityError(f"content shape changed at {path}")
        for a, b in zip(source, restored, strict=True):
            _check_loss(a, b, path)
    elif source != restored:
        raise MessageCompatibilityError(f"value changed at {path}")


def dump_messages(messages: list[Msg] | tuple[Msg, ...]) -> dict[str, Any]:
    """写入原生消息；禁止把只读旧历史投影伪装为新框架检查点。"""
    if any(message.metadata.get("history_read_only") for message in messages):
        raise MessageCompatibilityError("legacy history is read-only")
    return {"schema": SCHEMA, "messages": [m.model_dump(mode="json") for m in messages]}


def load_messages(payload: dict[str, Any]) -> list[Msg]:
    """只读取显式版本的原生信封；usage=None、错误与结构化字段原样保留。"""
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise MessageCompatibilityError("unsupported native message schema")
    if set(payload) != {"schema", "messages"} or not isinstance(
        payload["messages"], list
    ):
        raise MessageCompatibilityError("invalid native message envelope")
    result = []
    for item in payload["messages"]:
        try:
            msg = Msg.model_validate(item)
        except (ValidationError, TypeError, ValueError) as exc:
            raise MessageCompatibilityError("invalid native message") from exc
        _check_loss(item, msg.model_dump(mode="json"))
        result.append(msg)
    return result


def validate_tool_pairs(
    messages: list[Msg] | tuple[Msg, ...], *, complete: bool = True
) -> None:
    """校验调用 ID/名称及结果配对；流中间快照可显式允许未完成调用。"""
    calls: dict[str, str] = {}
    results: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolCallBlock):
                if not block.id or block.id in calls:
                    raise MessageCompatibilityError("duplicate or empty tool call id")
                calls[block.id] = block.name
            elif isinstance(block, ToolResultBlock):
                if block.id not in calls or calls[block.id] != block.name:
                    raise MessageCompatibilityError("tool result has no matching call")
                if block.id in results:
                    raise MessageCompatibilityError("duplicate tool result")
                results.add(block.id)
    if complete and calls.keys() != results:
        raise MessageCompatibilityError("tool calls have missing results")


@dataclass(frozen=True)
class HistoricalMessages:
    """供历史展示的原生投影与原始 JSON；不提供执行/检查点恢复入口。"""

    messages: tuple[Msg, ...]
    _original_json: str = field(repr=False)

    def original(self) -> list[dict[str, Any]]:
        """返回独立副本，保留历史 artifact、provider finish_reason 等扩展字段。"""
        return json.loads(self._original_json)


def _media(url: str, media_type: str | None = None) -> DataBlock:
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        if not sep or not header.endswith(";base64"):
            raise MessageCompatibilityError("unsupported inline media encoding")
        return DataBlock(source=Base64Source(media_type=header[5:-7], data=data))
    return DataBlock(
        source=URLSource(
            url=url,
            media_type=media_type
            or mimetypes.guess_type(url.split("?", 1)[0])[0]
            or "image/*",
        )
    )


def _blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [TextBlock(text=content)]
    if not isinstance(content, list):
        raise MessageCompatibilityError("unsupported legacy content shape")
    blocks = []
    for part in content:
        if isinstance(part, str):
            blocks.append(TextBlock(text=part))
            continue
        if not isinstance(part, dict):
            raise MessageCompatibilityError("invalid legacy content block")
        kind = part.get("type")
        if kind == "text":
            blocks.append(TextBlock(text=part["text"]))
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url", part.get("url"))
            blocks.append(_media(image["url"] if isinstance(image, dict) else image))
        elif kind in {"image", "audio", "document"} and "source" in part:
            source = part["source"]
            blocks.append(DataBlock(source=source))
        elif kind == "input_audio":
            audio = part["input_audio"]
            blocks.append(
                DataBlock(
                    source=Base64Source(
                        data=audio["data"], media_type=f"audio/{audio['format']}"
                    )
                )
            )
        elif kind == "thinking":
            blocks.append(ThinkingBlock.model_validate(part))
        elif kind == "tool_use":
            blocks.append(
                ToolCallBlock(
                    id=part["id"], name=part["name"], input=_json(part["input"])
                )
            )
        elif kind == "redacted_thinking":
            blocks.append(
                ThinkingBlock(thinking="", redacted_thinking_data=part["data"])
            )
        else:
            raise MessageCompatibilityError("unsupported legacy content block type")
    return blocks


def read_legacy_messages(
    payload: list[dict[str, Any]], *, complete: bool = False
) -> HistoricalMessages:
    """读取 QueryState 的 messages_to_dict 格式（human/ai/system/tool）。

    明确拒绝 chunk、任意构造器序列化与未知角色；不加载 pickle 或 LC 对象。
    部分历史默认允许尚未返回结果的调用，但不允许错误 ID 或孤立结果。
    """
    if not isinstance(payload, list):
        raise MessageCompatibilityError("legacy history must be a list")
    original = _json(payload)
    messages = []
    names: dict[str, str] = {}
    try:
        for entry in deepcopy(payload):
            if set(entry) != {"type", "data"}:
                raise MessageCompatibilityError("unsupported legacy message envelope")
            kind, data = entry["type"], entry["data"]
            if (
                kind not in {"human", "ai", "system", "tool"}
                or data.get("type", kind) != kind
            ):
                raise MessageCompatibilityError("unsupported legacy message schema")
            content = data.get("content", "")
            metadata = {
                "history_read_only": True,
                "legacy_response_metadata": data.get("response_metadata", {}),
                "legacy_additional_kwargs": data.get("additional_kwargs", {}),
                "legacy_usage_metadata": data.get("usage_metadata"),
            }
            role = {
                "human": "user",
                "ai": "assistant",
                "system": "system",
                "tool": "assistant",
            }[kind]
            blocks = _blocks(content)
            if kind == "ai":
                content_calls = {
                    b.id: b for b in blocks if isinstance(b, ToolCallBlock)
                }
                names.update({key: block.name for key, block in content_calls.items()})
                if data.get("invalid_tool_calls"):
                    metadata["legacy_invalid_tool_calls"] = data["invalid_tool_calls"]
                calls = data.get("tool_calls") or []
                if not calls:
                    for raw in data.get("additional_kwargs", {}).get("tool_calls", []):
                        calls.append(
                            {
                                "id": raw["id"],
                                "name": raw["function"]["name"],
                                "args": raw["function"]["arguments"],
                            }
                        )
                for call in calls:
                    arguments = call.get("args", {})
                    if call["id"] in content_calls:
                        existing = content_calls[call["id"]]
                        parsed = (
                            json.loads(arguments)
                            if isinstance(arguments, str)
                            else arguments
                        )
                        if (
                            existing.name != call["name"]
                            or json.loads(existing.input) != parsed
                        ):
                            raise MessageCompatibilityError(
                                "conflicting legacy tool call representations"
                            )
                        continue
                    blocks.append(
                        ToolCallBlock(
                            id=call["id"],
                            name=call["name"],
                            input=arguments
                            if isinstance(arguments, str)
                            else _json(arguments),
                        )
                    )
                    names[call["id"]] = call["name"]
            if kind == "tool":
                call_id = data["tool_call_id"]
                name = data.get("name") or names.get(call_id)
                if not name:
                    raise MessageCompatibilityError(
                        "legacy tool result has no matching call"
                    )
                status = data.get("status", "success")
                if status not in {"success", "error"}:
                    raise MessageCompatibilityError(
                        "unsupported legacy tool result status"
                    )
                blocks = [
                    ToolResultBlock(
                        id=call_id,
                        name=name,
                        output=content if isinstance(content, str) else blocks,
                        state=status,
                        metadata={"artifact": data.get("artifact")},
                    )
                ]
            kwargs = {"id": data["id"]} if data.get("id") else {}
            msg = Msg(
                name=data.get("name") or role,
                role=role,
                content=blocks,
                metadata=metadata,
                **kwargs,
            )
            usage = data.get("usage_metadata")
            if (
                usage
                and usage.get("input_tokens") is not None
                and usage.get("output_tokens") is not None
            ):
                details = usage.get("input_token_details") or {}
                msg.usage = Usage(
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cache_input_tokens=details.get("cache_read", 0),
                    cache_creation_input_tokens=details.get("cache_creation", 0),
                )
            # provider finish_reason 不能映射成 Agent 回复完成；原值保留在元数据。
            structured = data.get("additional_kwargs", {}).get("structured_output")
            if isinstance(structured, dict):
                msg.structured_output = structured
            messages.append(msg)
        validate_tool_pairs(messages, complete=complete)
        finished = {
            b.id for m in messages for b in m.content if isinstance(b, ToolResultBlock)
        }
        for message in messages:
            for block in message.content:
                if isinstance(block, ToolCallBlock) and block.id in finished:
                    block.state = "finished"
    except MessageCompatibilityError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise MessageCompatibilityError("invalid legacy message record") from exc
    return HistoricalMessages(tuple(messages), original)

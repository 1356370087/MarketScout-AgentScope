"""AgentScope 原生模型与 Sandbox Gateway V2 边界（T020）。"""

from __future__ import annotations
import json
import secrets
import time
import uuid
from contextvars import ContextVar
from contextlib import aclosing, contextmanager
from copy import copy
from dataclasses import dataclass, field
import httpx
import jsonschema
from pydantic import BaseModel, SecretStr
from agentscope.credential import CredentialBase
from agentscope.formatter import OpenAIChatFormatter
from agentscope.tool import ToolChoice
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import (
    ChatModelBase,
    ChatResponse,
    ChatUsage,
    StructuredResponse,
    OpenAIChatModel,
)
from open_deep_research.sandbox.wire import GatewayModelRequestV2, GatewayModelOutcomeV2


_operation_scope = ContextVar("native_gateway_operation", default=None)


@contextmanager
def gateway_operation_scope(key):
    """Keep physical request identities stable when a journaled stage replays."""
    token = _operation_scope.set([key, 0])
    try:
        yield
    finally:
        _operation_scope.reset(token)


class GatewayCallError(RuntimeError):
    def __init__(self, code, *, status_code=None, uncertain=False):
        super().__init__(code)
        self.status_code, self.uncertain = status_code, uncertain


@dataclass(frozen=True)
class SandboxBinding:
    url: str
    run_id: str
    task_id: str
    role: str
    stage: str
    token: SecretStr = field(repr=False)


@dataclass(frozen=True)
class SandboxServiceBinding:
    """API 进程使用派生服务密钥；不把根密钥传给模型或沙箱。"""

    url: str
    run_id: str
    task_id: str
    role: str
    stage: str
    fence_token: int
    service_key: bytes = field(repr=False)


class SandboxChatModel(ChatModelBase):
    """V2 完整结果接入原生流协议；不假称 Gateway 提供 token 增量流。"""

    retry_owner = "gateway"

    class Parameters(BaseModel):
        max_tokens: int = 4096
        temperature: float | None = None

    def __init__(
        self,
        *,
        binding: SandboxBinding | SandboxServiceBinding,
        model: str,
        parameters=None,
        stream=True,
        client=None,
        context_size=32768,
    ):
        super().__init__(
            CredentialBase(),
            model,
            parameters or self.Parameters(),
            stream=stream,
            max_retries=0,
            context_size=context_size,
        )
        self._binding = binding
        self.client = client or httpx.AsyncClient(base_url=binding.url, timeout=120)
        self._owns_client = client is None
        self.formatter = OpenAIChatFormatter()

    async def _request(
        self, messages, tools=None, tool_choice=None, structured_schema=None, **kwargs
    ):
        operation_id = kwargs.pop("logical_operation_id", None)
        scope = _operation_scope.get()
        if operation_id is None and scope is not None:
            operation_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{self._binding.run_id}:{scope[0]}:{scope[1]}",
            ).hex
            scope[1] += 1
        operation_id = operation_id or uuid.uuid4().hex
        if set(kwargs) - {"max_tokens", "temperature"}:
            raise ValueError("unsupported sandbox model options")
        binding = self._binding
        if isinstance(tool_choice, ToolChoice):
            if tool_choice.tools:
                tools = [
                    t
                    for t in (tools or [])
                    if t["function"]["name"] in tool_choice.tools
                ]
            mode = tool_choice.mode
            tool_choice = (
                mode
                if mode in {"auto", "none", "required"}
                else {"type": "function", "function": {"name": mode}}
            )
        request = GatewayModelRequestV2(
            run_id=binding.run_id,
            task_id=binding.task_id,
            role=binding.role,
            stage=binding.stage,
            logical_operation_id=operation_id,
            model=self.model,
            messages=await self.formatter.format(messages),
            tools=tools or [],
            tool_choice=tool_choice,
            structured_schema=structured_schema,
            max_output_tokens=kwargs.get("max_tokens", self.parameters.max_tokens),
            temperature=kwargs.get("temperature", self.parameters.temperature),
        )
        timestamp, nonce = time.time(), secrets.token_urlsafe(24)
        headers = {"X-Sandbox-Timestamp": str(timestamp), "X-Sandbox-Nonce": nonce}
        if isinstance(binding, SandboxServiceBinding):
            from open_deep_research.sandbox.crypto import sign_payload

            headers["X-Sandbox-Fence-Token"] = str(binding.fence_token)
            headers["X-Sandbox-Service-Signature"] = sign_payload(
                {
                    "request": request.model_dump(mode="json"),
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "fence_token": binding.fence_token,
                },
                binding.service_key,
            )
        else:
            headers["Authorization"] = f"Bearer {binding.token.get_secret_value()}"
        try:
            response = await self.client.post(
                "/v2/models/complete",
                json=request.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GatewayCallError(
                "sandbox_gateway_http_error",
                status_code=exc.response.status_code,
                uncertain=exc.response.status_code >= 500,
            ) from None
        except httpx.RequestError:
            raise GatewayCallError(
                "sandbox_gateway_outcome_unknown", uncertain=True
            ) from None
        outcome = GatewayModelOutcomeV2.model_validate(response.json())
        if (
            outcome.logical_operation_id != operation_id
            or outcome.requested_model != self.model
        ):
            raise GatewayCallError("sandbox_gateway_response_mismatch", uncertain=True)
        if outcome.status != "completed":
            if (outcome.error_code or "").startswith("budget_exhausted:"):
                from open_deep_research.budgets import BudgetDimension, BudgetExhausted

                raise BudgetExhausted(BudgetDimension(outcome.error_code.split(":", 1)[1]))
            raise GatewayCallError(
                "sandbox_gateway_operation_not_completed", uncertain=True
            )
        return outcome

    @staticmethod
    def _metadata(outcome):
        return {
            "logical_operation_id": outcome.logical_operation_id,
            "request_id": outcome.request_id,
            "provider_finish_reason": outcome.finish_reason,
            "requested_model": outcome.requested_model,
            "served_model": outcome.served_model,
            "response_cost_usd": outcome.response_cost_usd,
            "raw_usage": outcome.usage,
            "retry_owner": "gateway",
        }

    @staticmethod
    def _usage(outcome):
        raw = outcome.usage
        if raw.get("input_tokens") is None or raw.get("output_tokens") is None:
            return None
        return ChatUsage(
            input_tokens=raw["input_tokens"],
            output_tokens=raw["output_tokens"],
            time=(outcome.latency_ms or 0) / 1000,
            cache_input_tokens=raw.get("cached_input_tokens", 0),
            metadata={"raw_usage": raw},
        )

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        outcome = await self._request(messages, tools, tool_choice, **kwargs)
        message = outcome.message
        if not message or message.get("role") != "assistant":
            raise GatewayCallError("sandbox_gateway_invalid_message", uncertain=True)
        content = message.get("content") or ""
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append(TextBlock(text=content))
        else:
            for part in content:
                if part.get("type") != "text":
                    raise GatewayCallError(
                        "unsupported_gateway_output_block", uncertain=True
                    )
                blocks.append(TextBlock(text=part["text"]))
        for call in message.get("tool_calls") or []:
            blocks.append(
                ToolCallBlock(
                    id=call["id"],
                    name=call["function"]["name"],
                    input=call["function"]["arguments"],
                )
            )
        result = ChatResponse(
            content=blocks,
            is_last=True,
            usage=self._usage(outcome),
            metadata=self._metadata(outcome),
        )
        if not self.stream:
            return result

        async def complete_stream():
            yield result

        return complete_stream()

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        schema = (
            structured_model.model_json_schema()
            if isinstance(structured_model, type)
            else structured_model
        )
        outcome = await self._request(messages, structured_schema=schema, **kwargs)
        if outcome.finish_reason in {"length", "max_tokens"}:
            raise GatewayCallError("structured_output_truncated")
        content = outcome.structured
        if content is None:
            calls = (outcome.message or {}).get("tool_calls") or []
            matches = [
                c
                for c in calls
                if c["function"]["name"] == "__insightforge_structured_output"
            ]
            if len(matches) != 1:
                raise GatewayCallError("structured_output_missing")
            content = json.loads(matches[0]["function"]["arguments"])
        jsonschema.validate(content, schema)
        if isinstance(structured_model, type):
            content = structured_model.model_validate(content).model_dump(mode="json")
        return StructuredResponse(
            content=content,
            usage=self._usage(outcome),
            metadata=self._metadata(outcome),
        )

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()


class GovernedModelMixin:
    """原生模型共用的单次结构化策略和流元数据传递。"""

    _call_metadata = ContextVar("openai_call_metadata", default=None)
    _call_streams = ContextVar("model_call_streams", default=None)

    def _track_stream(self, result):
        streams = self._call_streams.get()
        if streams is not None and not isinstance(result, ChatResponse):
            streams.append(result)
        return result

    async def _call_api(self, *args, **kwargs):
        return self._track_stream(await super()._call_api(*args, **kwargs))

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        # 显式选择工具策略，禁止框架隐式尝试多种结构化策略形成额外物理调用。
        kwargs.setdefault("tool_choice", ToolChoice(mode="generate_structured_output"))
        metadata = {}
        token = self._call_metadata.set(metadata)
        streams = []
        stream_token = self._call_streams.set(streams)
        try:
            result = await super().generate_structured_output(
                messages, structured_model, **kwargs
            )
            reason = str(metadata.get("provider_finish_reason") or "").lower()
            if reason not in {
                "stop",
                "end_turn",
                "stop_sequence",
                "tool_use",
                "tool_calls",
            }:
                raise GatewayCallError("structured_output_not_complete")
            result.metadata.update(metadata)
            return result
        finally:
            self._call_metadata.reset(token)
            self._call_streams.reset(stream_token)
            for stream in reversed(streams):
                await stream.aclose()

    def _remember_metadata(self, metadata):
        current = self._call_metadata.get()
        if current is not None:
            current.update(metadata)

    async def __call__(self, *args, **kwargs):
        metadata = {}
        token = self._call_metadata.set(metadata)
        streams = []
        stream_token = self._call_streams.set(streams)
        try:
            result = await super().__call__(*args, **kwargs)
        finally:
            self._call_metadata.reset(token)
            self._call_streams.reset(stream_token)
        if isinstance(result, ChatResponse):
            return result

        async def stream():
            try:
                while True:
                    # 首包探测与消费/取消可能位于不同 Task。ContextVar token
                    # 不能跨 yield 留存，否则关闭时会在另一 Context 中 reset。
                    token = self._call_metadata.set(metadata)
                    try:
                        chunk = await anext(result)
                    except StopAsyncIteration:
                        break
                    finally:
                        self._call_metadata.reset(token)
                    chunk.metadata.update(metadata)
                    yield chunk
            finally:
                await result.aclose()
                for source in reversed(streams):
                    await source.aclose()

        return stream()


class GovernedOpenAIChatModel(GovernedModelMixin, OpenAIChatModel):
    """保留提供商停止原因；解析依旧委托框架，未复制模型循环。"""

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        return await super()._call_api(
            model_name, messages, tools, tool_choice, **kwargs
        )

    def _parse_completion_response(self, start_datetime, response, audio_format="wav"):
        result = super()._parse_completion_response(
            start_datetime, response, audio_format
        )
        result.metadata.update(
            provider_finish_reason=response.choices[0].finish_reason,
            request_id=response.id,
            served_model=response.model,
        )
        self._remember_metadata(result.metadata)
        return result

    async def _parse_stream_response(self, start_datetime, response):
        metadata = {}

        async def observed():
            async for raw in response:
                metadata.update(request_id=raw.id, served_model=raw.model)
                for choice in raw.choices:
                    if choice.finish_reason is not None:
                        metadata["provider_finish_reason"] = choice.finish_reason
                current = self._call_metadata.get()
                if current is not None:
                    current.update(metadata)
                yield raw

        class ObservedStream:
            async def __aenter__(self):
                await response.__aenter__()
                self.iterator = observed()
                return self.iterator

            async def __aexit__(self, *exc):
                await self.iterator.aclose()
                return await response.__aexit__(*exc)

        async with aclosing(
            super()._parse_stream_response(start_datetime, ObservedStream())
        ) as parsed:
            async for chunk in parsed:
                chunk.metadata.update(metadata)
                yield chunk


class LiteLLMChatModel(GovernedOpenAIChatModel):
    """LiteLLM 逻辑模型使用通用 max_tokens，保留明确输出上限。"""

    retry_owner = "gateway"

    async def _call_api(
        self, model_name, messages, tools=None, tool_choice=None, **kwargs
    ):
        local = copy(self)
        limit = kwargs.pop(
            "max_tokens",
            kwargs.pop("max_completion_tokens", self.parameters.max_tokens),
        )
        local.parameters = self.parameters.model_copy(update={"max_tokens": None})
        if limit is not None:
            kwargs["max_tokens"] = limit
        return self._track_stream(
            await OpenAIChatModel._call_api(
                local, model_name, messages, tools, tool_choice, **kwargs
            )
        )

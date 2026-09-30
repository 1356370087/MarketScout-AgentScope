"""Native AgentScope model execution at the credential-owning Gateway."""

import os
import time
from contextvars import ContextVar

import httpx
from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.tool import ToolChoice
from openai import APIConnectionError, APIStatusError
from pydantic import SecretStr

from open_deep_research.agentscope_runtime.gateway import LiteLLMChatModel
from open_deep_research.models.protocol_errors import ModelGatewayError
from open_deep_research.models.errors import GATEWAY_TOKEN_LIMIT_MARKER, is_token_limit_exceeded
from open_deep_research.observability.trace_context import current_traceparent
from open_deep_research.sandbox.wire import GatewayModelOutcomeV2

STRUCTURED_OUTPUT_TOOL_NAME = "__insightforge_structured_output"


def wire_messages(rows):
    """Convert the supported Wire V2 protocol to native message blocks."""
    from open_deep_research.agentscope_runtime.messages import (
        _blocks,
        validate_tool_pairs,
    )

    messages, names = [], {}
    for row in rows:
        role = row["role"]
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported gateway message role")
        blocks = _blocks(row.get("content") or "")
        if role == "tool":
            call_id = row["tool_call_id"]
            blocks = [
                ToolResultBlock(
                    id=call_id,
                    name=names.get(call_id) or row.get("name", ""),
                    output=blocks,
                )
            ]
        for call in row.get("tool_calls") or []:
            function = call["function"]
            names[call["id"]] = function["name"]
            blocks.append(
                ToolCallBlock(
                    id=call["id"], name=function["name"], input=function["arguments"]
                )
            )
        messages.append(
            Msg(
                name=row.get("name") or role,
                role="assistant" if role == "tool" else role,
                content=blocks,
            )
        )
    validate_tool_pairs(messages)
    return messages


class _GatewayModel(LiteLLMChatModel):
    def _parse_completion_response(self, start_datetime, response, audio_format="wav"):
        result = super()._parse_completion_response(
            start_datetime, response, audio_format
        )
        usage = response.usage
        result.metadata["reasoning_tokens"] = int(
            getattr(
                getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0
            )
            or 0
        )
        return result


class NativeGatewayProvider:
    """Keep per-run credentials and HTTP headers outside business checkpoints."""

    def __init__(self, *, api_key, base_url=None, transport=None):
        self.base_url = base_url or os.environ["LITELLM_BASE_URL"]
        self.key = SecretStr(api_key)
        self.headers = ContextVar("gateway_response_headers", default=None)
        self.client = httpx.AsyncClient(
            transport=transport, event_hooks={"response": [self._headers]}, timeout=180
        )
        self.models = {}

    async def _headers(self, response):
        headers = self.headers.get()
        if headers is not None:
            headers.update(response.headers)

    async def complete(self, request):
        model = self.models.get(request.model)
        if model is None:
            model = _GatewayModel(
                model=request.model,
                credential=OpenAICredential(api_key=self.key, base_url=self.base_url),
                stream=False,
                max_retries=0,
                client_kwargs={"max_retries": 0, "http_client": self.client},
            )
            self.models[request.model] = model
        choice = request.tool_choice
        if isinstance(choice, dict):
            choice = ToolChoice(mode=choice["function"]["name"])
        elif choice:
            choice = ToolChoice(mode=choice)
        headers = {}
        token = self.headers.set(headers)
        started = time.monotonic()
        try:
            result = await model(
                messages=wire_messages(request.messages),
                tools=request.tools or None,
                tool_choice=choice,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                metadata={
                    **request.trace_metadata,
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "role": request.role,
                    "stage": request.stage,
                    "logical_operation_id": request.logical_operation_id,
                    "tags": [
                        f"run:{request.run_id}",
                        f"operation:{request.logical_operation_id}",
                        f"role:{request.role}",
                        f"stage:{request.stage}",
                    ],
                },
                extra_headers={
                    "x-litellm-request-id": request.logical_operation_id,
                    "traceparent": current_traceparent(request.run_id),
                },
            )
        except APIConnectionError as exc:
            raise ModelGatewayError("gateway_connection_failed") from exc
        except APIStatusError as exc:
            code = {
                400: "invalid_request",
                401: "authentication",
                403: "authentication",
                404: "model_unavailable",
                408: "timeout",
                429: "budget_or_rate_limit",
            }.get(exc.status_code, "gateway_error")
            if exc.status_code in {403, 429} and "budget" in str(exc).lower():
                code = "gateway_budget_exceeded"
            raise ModelGatewayError(
                code, status_code=exc.status_code,
                provider_error_code=GATEWAY_TOKEN_LIMIT_MARKER
                if exc.status_code == 400 and is_token_limit_exceeded(exc) else None,
            ) from exc
        finally:
            self.headers.reset(token)
        usage = result.usage
        message = {
            "role": "assistant",
            "content": "".join(
                b.text for b in result.content if isinstance(b, TextBlock)
            ),
            "tool_calls": [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": b.input},
                }
                for b in result.content
                if isinstance(b, ToolCallBlock)
            ],
        }
        cost = headers.get("x-litellm-response-cost")
        return GatewayModelOutcomeV2(
            logical_operation_id=request.logical_operation_id,
            status="completed",
            message=message,
            usage={
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "total_tokens": usage.input_tokens + usage.output_tokens
                if usage
                else 0,
                "cached_input_tokens": usage.cache_input_tokens if usage else 0,
                "reasoning_tokens": result.metadata.get("reasoning_tokens", 0),
            } if usage else {},
            response_cost_usd=float(cost) if cost is not None else None,
            request_id=headers.get("x-request-id") or result.metadata.get("request_id"),
            requested_model=request.model,
            served_model=result.metadata.get("served_model"),
            provider=headers.get("x-litellm-model-provider")
            or headers.get("x-litellm-provider"),
            deployment_id=headers.get("x-litellm-model-id"),
            finish_reason=result.metadata.get("provider_finish_reason"),
            latency_ms=(time.monotonic() - started) * 1000,
        )

    async def aclose(self):
        await self.client.aclose()

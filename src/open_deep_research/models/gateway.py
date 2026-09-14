"""Provider-neutral application boundary for LiteLLM Proxy model calls."""

from __future__ import annotations

import contextvars
import hashlib
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeVar, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage
from openai import APIConnectionError, AsyncOpenAI
from pydantic import BaseModel

from open_deep_research.models.codec import (
    decode_message,
    encode_messages,
    normalize_tool_definition,
    parse_structured_output,
    stream_text_delta,
    structured_output_tool,
    structured_tool_choice,
)
from open_deep_research.models.errors import (
    GATEWAY_TOKEN_LIMIT_MARKER,
    gateway_error_indicates_token_limit,
)
from open_deep_research.observability import current_span_ids

T = TypeVar("T", bound=BaseModel)
ModelRole = Literal[
    "supervisor",
    "researcher",
    "summarization",
    "message_summary",
    "compression",
    "final_report",
    "report_review",
    "report_reviewer",
    "report_revisor",
    "quality_evaluation",
    "evaluation",
    "memory",
    "egress_classifier",
]


class ModelGatewayError(RuntimeError):
    """Sanitized terminal Gateway failure after LiteLLM has exhausted policy."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        """Create a stable error without copying upstream response content."""
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        # Whitelisted identifier extracted from the upstream error payload (or
        # the canonical token-limit marker); never free-form provider text.
        self.provider_error_code = provider_error_code


def classify_gateway_status(status_code: int | None) -> str:
    """Classify terminal HTTP failures without matching provider error strings."""
    if status_code == 400:
        return "invalid_request"
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 404:
        return "model_unavailable"
    if status_code == 408:
        return "timeout"
    if status_code == 429:
        return "budget_or_rate_limit"
    if status_code is not None and status_code >= 500:
        return "gateway_unavailable"
    return "gateway_error"


_PROVIDER_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


def _provider_error_fields(exc: Exception) -> tuple[str, str, str]:
    """Read the sanitized code/type/message triple from an SDK HTTP error."""
    body = getattr(exc, "body", None)
    error: Any = body if isinstance(body, dict) else {}
    nested = error.get("error")
    if isinstance(nested, dict):
        error = nested
    code = str(error.get("code") or "")
    error_type = str(error.get("type") or "")
    message = str(error.get("message") or "")
    if not any((code, error_type, message)):
        # openai SDK exceptions stringify to "Error code: N - {body json}".
        message = str(exc)
    return code, error_type, message


def _gateway_provider_error_code(exc: Exception) -> str | None:
    """Classify the upstream payload into a whitelisted identifier.

    The provider message is scanned for context-limit markers but never copied
    into the exception; only the canonical marker or an identifier-shaped
    code/type string is retained.
    """
    code, error_type, message = _provider_error_fields(exc)
    if gateway_error_indicates_token_limit(code, error_type, message):
        return GATEWAY_TOKEN_LIMIT_MARKER
    for candidate in (code, error_type):
        if candidate and _PROVIDER_IDENTIFIER_RE.fullmatch(candidate):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Normalized successful-request token accounting."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """Non-secret routing identity returned by LiteLLM."""

    requested_model: str
    served_model: str | None = None
    provider: str | None = None
    deployment_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest(Generic[T]):
    """One logical application model operation."""

    run_id: str
    task_id: str
    logical_operation_id: str
    role: ModelRole | str
    stage: str
    model: str
    messages: Sequence[BaseMessage]
    tools: Sequence[Any] = field(default_factory=tuple)
    tool_choice: str | Mapping[str, Any] | None = None
    output_schema: type[T] | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    trace_metadata: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    output_payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        """Reject requests missing their idempotency and attribution identity."""
        for name in ("run_id", "task_id", "logical_operation_id", "role", "stage", "model"):
            if not str(getattr(self, name, "")).strip():
                raise ValueError(f"ModelRequest.{name} must not be empty")
        if self.output_schema is not None and self.tools:
            raise ValueError("structured output cannot be combined with ordinary tools")


@dataclass(frozen=True, slots=True)
class ModelResult(Generic[T]):
    """Normalized logical result returned to application code."""

    message: AIMessage
    structured: T | None
    usage: ModelUsage
    response_cost_usd: float | None
    request_id: str | None
    route: ModelRoute
    finish_reason: str | None
    latency_ms: float


@dataclass(frozen=True, slots=True)
class ModelStreamDelta:
    """One incremental text fragment of a streaming completion."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    """Terminal streaming event with the merged result and first-packet TTFT."""

    result: ModelResult[Any]
    first_packet_ms: float


def build_request_metadata(request: ModelRequest[Any]) -> dict[str, Any]:
    """Assemble the per-request metadata LiteLLM records into spend logs."""
    return {
        "run_id": request.run_id,
        "task_id": request.task_id,
        "logical_operation_id": request.logical_operation_id,
        "role": str(request.role),
        "stage": request.stage,
        **{str(key): value for key, value in request.trace_metadata.items()},
        # Request tags are LiteLLM's first-class, filterable spend-log
        # dimension; they power per-stage/role spend analytics and leave
        # the door open for gateway-side tag budgets.
        "tags": [
            tag
            for tag in (
                f"run:{request.run_id}" if request.run_id else None,
                f"role:{request.role}" if request.role else None,
                f"stage:{request.stage}" if request.stage else None,
            )
            if tag
        ],
    }


@runtime_checkable
class ModelGateway(Protocol):
    """Complete logical model requests through a governed gateway."""

    async def complete(self, request: ModelRequest[T]) -> ModelResult[T]:
        """Execute one logical request."""


_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "litellm_run_key", default=None
)


def bind_run_key(key: str):
    """Bind a decrypted Run Key to the current async execution context."""
    return _run_key.set(key)


def reset_run_key(token: contextvars.Token[str | None]) -> None:
    """Remove a previously bound Run Key from the execution context."""
    _run_key.reset(token)


def current_run_key() -> str:
    """Return the Run-scoped virtual key without service-key fallback."""
    key = _run_key.get()
    if not key:
        raise RuntimeError("litellm_run_key_unavailable")
    return key


def current_gateway_key() -> str:
    """Return the Run Key, falling back to a restricted non-run service key."""
    key = _run_key.get() or os.getenv("LITELLM_SERVICE_KEY")
    if not key:
        raise RuntimeError("litellm_gateway_key_unavailable")
    return key


def _header(headers: Mapping[str, str], *names: str) -> str | None:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for name in names:
        if value := lowered.get(name.lower()):
            return value
    return None


def _number_header(headers: Mapping[str, str], name: str) -> float | None:
    value = _header(headers, name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def current_traceparent(run_id: str) -> str:
    """Create a W3C trace context from the current content-free span identity."""
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        propagated = carrier.get("traceparent")
        if propagated:
            return propagated
    except Exception:  # noqa: BLE001 - deterministic fallback remains available
        pass
    current_run_id, current_span_id = current_span_ids()
    trace_seed = current_run_id or run_id
    trace_id = hashlib.sha256(trace_seed.encode("utf-8")).hexdigest()[:32]
    span_seed = (current_span_id or hashlib.sha256(os.urandom(16)).hexdigest())[-16:]
    if set(trace_id) == {"0"}:
        trace_id = "1" + trace_id[1:]
    if set(span_seed) == {"0"}:
        span_seed = "1" + span_seed[1:]
    return f"00-{trace_id}-{span_seed}-01"


class LiteLLMModelGateway:
    """OpenAI-compatible client with all transport recovery delegated to LiteLLM."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 180.0,
        extra_body: Mapping[str, Any] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        """Create a connection-pooled OpenAI client for one Run."""
        self._base_url = (base_url or os.getenv("LITELLM_BASE_URL") or "").rstrip("/")
        if not self._base_url:
            raise RuntimeError("LITELLM_BASE_URL is required")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._extra_body = dict(extra_body or {})
        self._client = client

    def _openai_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self._base_url,
                # Placeholder only: every request resolves its own key via
                # ``_request_client`` so a pooled client is never pinned to one
                # Run's key.
                api_key="insightforge-per-request-key",
                max_retries=0,
                timeout=self._timeout_seconds,
            )
        return self._client

    def _request_client(self) -> AsyncOpenAI:
        """Return the pooled client with this request's key applied."""
        if self._api_key is not None:
            # The pooled client carries only a placeholder; an injected key
            # (sandbox Gateway vault) must still be applied per request.
            return self._openai_client().with_options(api_key=self._api_key)
        return self._openai_client().with_options(api_key=current_gateway_key())

    async def complete(self, request: ModelRequest[T]) -> ModelResult[T]:
        """Execute one logical request and normalize routing and usage metadata."""
        tools = [normalize_tool_definition(tool) for tool in request.tools]
        tool_choice: Any = request.tool_choice
        if request.output_schema is not None:
            # LiteLLM may route one logical alias across providers with
            # different strict-schema subsets.  Force the synthetic function
            # but leave conformance to the local Pydantic validation/repair
            # loop instead of enabling a provider-specific extension.
            tools = [structured_output_tool(request.output_schema, strict=False)]
            tool_choice = structured_tool_choice()
        metadata = build_request_metadata(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": encode_messages(request.messages),
            "metadata": metadata,
            "extra_headers": {
                "x-litellm-request-id": request.logical_operation_id,
                "traceparent": current_traceparent(request.run_id),
            },
        }
        if request.max_output_tokens is not None:
            # LiteLLM maps this portable budget to each provider's token field.
            kwargs["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if self._extra_body:
            kwargs["extra_body"] = dict(self._extra_body)
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        started = time.monotonic()
        try:
            raw = await self._request_client().chat.completions.with_raw_response.create(**kwargs)
            response = raw.parse()
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            request_id = getattr(exc, "request_id", None)
            code = classify_gateway_status(status_code)
            if status_code in {403, 429} and "budget" in str(exc).lower():
                # LiteLLM enforces key/team budgets with 403 "Crossed budget"
                # or 429 "Budget has been exceeded!"; neither is retriable and
                # neither should masquerade as an auth or rate-limit problem.
                code = "gateway_budget_exceeded"
            if isinstance(exc, APIConnectionError):
                # A connection-level failure (incl. timeouts) never proves the
                # server skipped the call; keep budget accounting conservative.
                code = "gateway_connection_failed"
            raise ModelGatewayError(
                code,
                status_code=status_code,
                request_id=request_id,
                provider_error_code=_gateway_provider_error_code(exc),
            ) from exc
        if not response.choices:
            raise ModelGatewayError("gateway_empty_response")
        choice = response.choices[0]
        raw_message = choice.message.model_dump(mode="json", exclude_none=True)
        message = decode_message(raw_message)
        if not isinstance(message, AIMessage):
            raise ModelGatewayError("gateway_non_assistant_response")
        structured = (
            parse_structured_output(
                message,
                request.output_schema,
                payload_transform=request.output_payload_transform,
            )
            if request.output_schema is not None
            else None
        )
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        completion_details = getattr(usage, "completion_tokens_details", None) if usage else None
        headers = dict(raw.headers)
        route = ModelRoute(
            requested_model=request.model,
            served_model=response.model or _header(headers, "x-litellm-model"),
            provider=_header(headers, "x-litellm-model-provider", "x-litellm-provider"),
            deployment_id=_header(headers, "x-litellm-model-id", "x-litellm-deployment-id"),
        )
        return ModelResult(
            message=message,
            structured=structured,
            usage=ModelUsage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
                reasoning_tokens=int(getattr(completion_details, "reasoning_tokens", 0) or 0),
            ),
            response_cost_usd=_number_header(headers, "x-litellm-response-cost"),
            request_id=_header(headers, "x-request-id", "x-litellm-request-id"),
            route=route,
            finish_reason=str(choice.finish_reason) if choice.finish_reason is not None else None,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    async def complete_stream(
        self, request: ModelRequest[Any]
    ) -> AsyncIterator[ModelStreamDelta | ModelStreamCompleted]:
        """Stream one plain-text completion with normalized final accounting.

        Phase 1 scope is free-text writing only: tool-calling and structured
        output requests are rejected so delta merging never has to
        reconstruct incremental tool-call arguments. The terminal event
        carries usage from the ``include_usage`` tail chunk and routing
        identity from the initial response headers.
        """
        if request.tools or request.output_schema is not None:
            raise ValueError("gateway_stream_requires_plain_text_request")
        metadata = build_request_metadata(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": encode_messages(request.messages),
            "metadata": metadata,
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_headers": {
                "x-litellm-request-id": request.logical_operation_id,
                "traceparent": current_traceparent(request.run_id),
            },
        }
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if self._extra_body:
            kwargs["extra_body"] = dict(self._extra_body)
        started = time.perf_counter()
        try:
            raw = await self._request_client().chat.completions.with_raw_response.create(
                **kwargs
            )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            request_id = getattr(exc, "request_id", None)
            code = classify_gateway_status(status_code)
            if status_code in {403, 429} and "budget" in str(exc).lower():
                code = "gateway_budget_exceeded"
            if isinstance(exc, APIConnectionError):
                code = "gateway_connection_failed"
            if _stream_unsupported(exc):
                code = "gateway_stream_unsupported"
            raise ModelGatewayError(
                code,
                status_code=status_code,
                request_id=request_id,
                provider_error_code=_gateway_provider_error_code(exc),
            ) from exc
        headers = dict(raw.headers)
        stream = raw.parse()
        content_parts: list[str] = []
        usage = None
        finish_reason: str | None = None
        served_model: str | None = None
        first_packet_ms = 0.0
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                chunk_model = getattr(chunk, "model", None)
                if chunk_model:
                    served_model = str(chunk_model)
                if not getattr(chunk, "choices", None):
                    continue
                if first_packet_ms == 0.0:
                    first_packet_ms = (time.perf_counter() - started) * 1000.0
                choice = chunk.choices[0]
                if choice.finish_reason is not None:
                    finish_reason = str(choice.finish_reason)
                text = stream_text_delta(chunk)
                if text:
                    content_parts.append(text)
                    yield ModelStreamDelta(text=text)
        except Exception as exc:
            raise ModelGatewayError("gateway_stream_interrupted") from exc
        if first_packet_ms == 0.0:
            first_packet_ms = (time.perf_counter() - started) * 1000.0
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        completion_details = (
            getattr(usage, "completion_tokens_details", None) if usage else None
        )
        route = ModelRoute(
            requested_model=request.model,
            served_model=served_model or _header(headers, "x-litellm-model"),
            provider=_header(headers, "x-litellm-model-provider", "x-litellm-provider"),
            deployment_id=_header(headers, "x-litellm-model-id", "x-litellm-deployment-id"),
        )
        yield ModelStreamCompleted(
            result=ModelResult(
                message=AIMessage(content="".join(content_parts)),
                structured=None,
                usage=ModelUsage(
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                    cached_input_tokens=int(getattr(details, "cached_tokens", 0) or 0),
                    reasoning_tokens=int(
                        getattr(completion_details, "reasoning_tokens", 0) or 0
                    ),
                ),
                response_cost_usd=_number_header(headers, "x-litellm-response-cost"),
                request_id=_header(headers, "x-request-id", "x-litellm-request-id"),
                route=route,
                finish_reason=finish_reason,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
            first_packet_ms=first_packet_ms,
        )

    async def aclose(self) -> None:
        """Close the shared OpenAI connection pool."""
        if self._client is not None:
            await self._client.close()


def _stream_unsupported(exc: Exception) -> bool:
    """Detect providers that reject streaming for the requested deployment."""
    text = str(exc).lower()
    return "stream" in text and (
        "not supported" in text or "unsupported" in text or "does not support" in text
    )


def result_usage_metadata(result: ModelResult[Any]) -> dict[str, Any]:
    """Project Gateway accounting onto the AIMessage compatibility metadata."""
    return {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "total_tokens": result.usage.total_tokens,
        "input_token_details": {"cache_read": result.usage.cached_input_tokens},
        "output_token_details": {"reasoning": result.usage.reasoning_tokens},
        "cost_usd": result.response_cost_usd,
        "request_id": result.request_id,
        "requested_model": result.route.requested_model,
        "model": result.route.served_model,
        "provider": result.route.provider,
        "deployment_id": result.route.deployment_id,
        "finish_reason": result.finish_reason,
    }


def attach_result_metadata(result: ModelResult[Any]) -> AIMessage:
    """Return the normalized AI message with existing usage readers supported."""
    result.message.usage_metadata = {
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "total_tokens": result.usage.total_tokens,
        "input_token_details": {"cache_read": result.usage.cached_input_tokens},
        "output_token_details": {"reasoning": result.usage.reasoning_tokens},
    }
    result.message.response_metadata.update(result_usage_metadata(result))
    return result.message


__all__ = [
    "LiteLLMModelGateway",
    "ModelGateway",
    "ModelGatewayError",
    "ModelRequest",
    "ModelResult",
    "ModelRoute",
    "ModelStreamCompleted",
    "ModelStreamDelta",
    "ModelUsage",
    "attach_result_metadata",
    "bind_run_key",
    "build_request_metadata",
    "classify_gateway_status",
    "current_traceparent",
    "current_gateway_key",
    "current_run_key",
    "reset_run_key",
    "result_usage_metadata",
]

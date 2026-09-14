"""Application-facing helpers for the single ModelGateway invocation path."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, TypeVar, cast, overload

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ValidationError

from open_deep_research.configuration import Configuration
from open_deep_research.events.task_activity import publish_task_activity
from open_deep_research.models.codec import (
    MessageCodecError,
    encode_messages,
    normalize_tool_definition,
)
from open_deep_research.models.gateway import (
    LiteLLMModelGateway,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelResult,
    ModelStreamCompleted,
    ModelStreamDelta,
    attach_result_metadata,
)
from open_deep_research.observability import TokenUsage, get_trace_recorder
from open_deep_research.observability.telemetry import get_prometheus_metrics

T = TypeVar("T", bound=BaseModel)
_RUN_GATEWAYS: dict[str, ModelGateway] = {}

# Errors that justify a one-shot downgrade to the non-streaming path; anything
# after the first delta propagates because retrying would double-bill tokens.
_STREAM_DOWNGRADE_CODES = {"gateway_stream_unsupported"}


def _identity(config: RunnableConfig, role: str, stage: str, span_name: str) -> tuple[str, str, str]:
    metadata = config.get("metadata", {})
    configurable = config.get("configurable", {})
    run_id = str(metadata.get("run_id") or configurable.get("thread_id") or "service")
    task_id = str(metadata.get("task_id") or role)
    return run_id, task_id, span_name or f"{role}.{stage}"


def _logical_operation_id(
    *,
    run_id: str,
    task_id: str,
    operation: str,
    model: str,
    messages: Sequence[BaseMessage],
    tools: Sequence[Any],
    tool_choice: str | Mapping[str, Any] | None,
    output_schema: type[BaseModel] | None,
    max_output_tokens: int | None,
    temperature: float | None,
) -> str:
    payload = {
        "run_id": run_id,
        "task_id": task_id,
        "operation": operation,
        "model": model,
        "messages": encode_messages(messages),
        "tools": [normalize_tool_definition(tool) for tool in tools],
        "tool_choice": tool_choice,
        "schema": output_schema.model_json_schema() if output_schema is not None else None,
        "max_output_tokens": max_output_tokens,
        "temperature": temperature,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"model:{digest}"


def get_model_gateway(config: RunnableConfig) -> ModelGateway:
    """Return the connection-pooled Gateway for one Run or Sandbox task."""
    run_id = str(
        config.get("metadata", {}).get("run_id")
        or config.get("configurable", {}).get("thread_id")
        or "service"
    )
    sandbox_worker = bool(os.getenv("SANDBOX_TASK_TOKEN"))
    cache_key = f"sandbox:{run_id}" if sandbox_worker else f"litellm:{run_id}"
    gateway = _RUN_GATEWAYS.get(cache_key)
    if gateway is None:
        if sandbox_worker:
            from open_deep_research.sandbox.model_gateway import SandboxModelGateway

            gateway = SandboxModelGateway()
        else:
            gateway = LiteLLMModelGateway()
        _RUN_GATEWAYS[cache_key] = gateway
    return gateway


async def close_run_gateway(run_id: str) -> None:
    """Close and remove API/Worker connection pools owned by one Run."""
    for key in (f"sandbox:{run_id}", f"litellm:{run_id}"):
        gateway = _RUN_GATEWAYS.pop(key, None)
        close = getattr(gateway, "aclose", None)
        if close is not None:
            await close()


@overload
async def complete_model(
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    role: str,
    stage: str,
    model: str,
    max_output_tokens: int | None,
    span_name: str,
    tools: Sequence[Any] = (),
    tool_choice: str | Mapping[str, Any] | None = None,
    output_schema: type[T],
    temperature: float | None = None,
    trace_metadata: Mapping[str, str | int | float | bool] | None = None,
    budget_gate: Any = None,
    output_payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> T: ...


@overload
async def complete_model(
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    role: str,
    stage: str,
    model: str,
    max_output_tokens: int | None,
    span_name: str,
    tools: Sequence[Any] = (),
    tool_choice: str | Mapping[str, Any] | None = None,
    output_schema: None = None,
    temperature: float | None = None,
    trace_metadata: Mapping[str, str | int | float | bool] | None = None,
    budget_gate: Any = None,
    output_payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> AIMessage: ...


async def complete_model(
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    role: str,
    stage: str,
    model: str,
    max_output_tokens: int | None,
    span_name: str,
    tools: Sequence[Any] = (),
    tool_choice: str | Mapping[str, Any] | None = None,
    output_schema: type[T] | None = None,
    temperature: float | None = None,
    trace_metadata: Mapping[str, str | int | float | bool] | None = None,
    budget_gate: Any = None,
    output_payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> T | AIMessage:
    """Execute a model call, retrying only application schema repair failures."""
    request_messages = list(messages)
    max_attempts = (
        max(1, Configuration.from_runnable_config(config).max_structured_output_retries)
        if output_schema is not None
        else 1
    )
    last_structured_error: MessageCodecError | ValidationError | None = None
    for attempt in range(max_attempts):
        run_id, task_id, operation = _identity(config, role, stage, span_name)
        logical_id = _logical_operation_id(
            run_id=run_id,
            task_id=task_id,
            operation=operation,
            model=model,
            messages=request_messages,
            tools=tools,
            tool_choice=tool_choice,
            output_schema=output_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        operation_key = logical_id
        if budget_gate is not None:
            budget_gate.reserve_model_call(
                operation_key,
                estimated_input_tokens=max(
                    1, count_tokens_approximately(request_messages)
                ),
                estimated_output_tokens=max(1, int(max_output_tokens or 1024)),
                model_name=model,
            )
        recorder = get_trace_recorder(config)
        await publish_task_activity(
            config, "model.started", kind="model", phase="reasoning",
            status="running", title="模型调用", summary="已开始模型请求。",
            iteration=None, duration_ms=None,
            payload={"provider": "litellm", "model": model, "attempt": attempt + 1},
            dedupe_key=f"activity:{logical_id}:started",
        )
        with recorder.start_span(
            name=span_name,
            kind="llm",
            agent_role=role,
            attributes={"stage": stage, "gateway": "litellm"},
            input_payload=request_messages,
            provider="litellm",
            model=model,
        ) as span:
            try:
                result = await get_model_gateway(config).complete(
                    ModelRequest(
                        run_id=run_id,
                        task_id=task_id,
                        logical_operation_id=logical_id,
                        role=role,
                        stage=stage,
                        model=model,
                        messages=request_messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        output_schema=output_schema,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                        trace_metadata=trace_metadata or {},
                        output_payload_transform=output_payload_transform,
                    )
                )
            except Exception as exc:
                if budget_gate is not None:
                    uncertain = isinstance(exc, ModelGatewayError) and exc.code in {
                        "gateway_unavailable",
                        "sandbox_gateway_unavailable",
                        "gateway_connection_failed",
                    }
                    budget_gate.fail_model_call(operation_key, uncertain=uncertain)
                span.record_outcome(
                    error_type=(
                        exc.code if isinstance(exc, ModelGatewayError) else type(exc).__name__
                    )
                )
                will_retry = (
                    output_schema is not None
                    and isinstance(exc, MessageCodecError | ValidationError)
                    and attempt + 1 < max_attempts
                )
                await publish_task_activity(
                    config, "model.retrying" if will_retry else "model.failed",
                    kind="model" if will_retry else "error", phase="reasoning",
                    status="warning" if will_retry else "error",
                    title="模型输出需重试" if will_retry else "模型调用失败",
                    summary="模型请求已按运行策略处理。",
                    iteration=None, duration_ms=None,
                    payload={
                        "provider": "litellm", "model": model, "attempt": attempt + 1,
                        "error_code": exc.code if isinstance(exc, ModelGatewayError) else type(exc).__name__,
                        "error_class": type(exc).__name__,
                    },
                    dedupe_key=f"activity:{logical_id}:failed",
                )
                if will_retry:
                    last_structured_error = cast(MessageCodecError | ValidationError, exc)
                    validation_feedback = ""
                    if isinstance(exc, ValidationError):
                        # Return field locations and error codes, never raw input
                        # or exception prose that may contain sensitive content.
                        issues = [
                            {"field": list(error["loc"]), "type": error["type"]}
                            for error in exc.errors(include_input=False, include_context=False)
                        ]
                        validation_feedback = " Correct these validation errors: " + json.dumps(issues)
                    request_messages = [
                        *request_messages,
                        HumanMessage(
                            content=(
                                "The previous response did not satisfy the required "
                                "structured schema. Return exactly one call to the required "
                                "function with valid arguments."
                                + validation_feedback
                            )
                        ),
                    ]
                    continue
                raise
            span.provider = result.route.provider or "litellm"
            span.model = result.route.served_model or model
            span.add_usage(
                TokenUsage(
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    total_tokens=result.usage.total_tokens,
                    cached_input_tokens=result.usage.cached_input_tokens,
                    reasoning_tokens=result.usage.reasoning_tokens,
                    estimated_cost_usd=float(result.response_cost_usd or 0),
                    cost_source=(
                        "provider_reported"
                        if result.response_cost_usd is not None
                        else "unavailable"
                    ),
                ),
                result.route.provider or "litellm",
                result.route.served_model or model,
                event_key=logical_id,
                attempt_index=attempt + 1,
                stage=stage,
                task_id=task_id,
                operation=span_name,
                duration_ms=int(result.latency_ms),
            )
            span.set_output(result.message)
        await publish_task_activity(
            config, "model.completed", kind="model", phase="reasoning",
            status="success", title="模型调用完成", summary="模型已返回结果。",
            iteration=None, duration_ms=int(result.latency_ms),
            payload={
                "provider": result.route.provider or "litellm",
                "model": result.route.served_model or model,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "reasoning_tokens": result.usage.reasoning_tokens,
                "tool_call_count": len(result.message.tool_calls),
                "retry_count": attempt,
            },
            dedupe_key=f"activity:{logical_id}:completed",
            update_run_summary=True,
        )
        if budget_gate is not None:
            budget_gate.settle_model_call(
                operation_key,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                model_name=model,
            )
        message = attach_result_metadata(result)
        message.response_metadata["model_routed"] = {
            "requested_model": result.route.requested_model,
            "served_model": result.route.served_model,
            "deployment_id": result.route.deployment_id,
            "latency_ms": result.latency_ms,
            "logical_retry_count": attempt,
        }
        if run_id != "service":
            try:
                from open_deep_research.events.public import event_publisher_from_config

                await event_publisher_from_config(config).publish(
                    "model.routed",
                    stage=stage,
                    payload=dict(message.response_metadata["model_routed"]),
                    dedupe_key=f"model-routed:{logical_id}",
                )
            except Exception:  # noqa: BLE001 - observability must fail open
                pass
        if output_schema is not None:
            if result.structured is None:
                raise RuntimeError("structured_model_result_missing")
            return result.structured
        return message
    assert last_structured_error is not None
    raise last_structured_error


async def _complete_model_non_streaming(
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    role: str,
    stage: str,
    model: str,
    span_name: str,
    max_output_tokens: int | None,
    temperature: float | None,
    trace_metadata: Mapping[str, str | int | float | bool] | None,
    budget_gate: Any,
) -> AIMessage:
    """Plain complete_model passthrough shared by the streaming downgrade."""
    return await complete_model(
        messages,
        config,
        role=role,
        stage=stage,
        model=model,
        max_output_tokens=max_output_tokens,
        span_name=span_name,
        temperature=temperature,
        trace_metadata=trace_metadata,
        budget_gate=budget_gate,
    )


async def complete_model_stream(
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    role: str,
    stage: str,
    model: str,
    span_name: str,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
    trace_metadata: Mapping[str, str | int | float | bool] | None = None,
    budget_gate: Any = None,
) -> AIMessage:
    """Stream one free-text model call with first-packet and idle timeouts.

    A first packet that exceeds ``model_first_packet_timeout_seconds`` or a
    deployment that rejects streaming downgrades once to the non-streaming
    path (recorded via the streaming-fallback metric). Failures after the
    first delta propagate: the request may already be billed upstream, so
    silently retrying would double-spend the budget.
    """
    configurable = Configuration.from_runnable_config(config)
    gateway = get_model_gateway(config)
    stream_factory = getattr(gateway, "complete_stream", None)
    if stream_factory is None:
        # Sandboxed Wire V2 execution stays non-streaming by design.
        return await _complete_model_non_streaming(
            messages, config, role=role, stage=stage, model=model,
            span_name=span_name, max_output_tokens=max_output_tokens,
            temperature=temperature, trace_metadata=trace_metadata,
            budget_gate=budget_gate,
        )

    async def downgrade(reason: str) -> AIMessage:
        metrics = get_prometheus_metrics(configurable)
        if metrics is not None:
            metrics.observe_streaming_fallback("litellm", model, reason)
        return await _complete_model_non_streaming(
            messages, config, role=role, stage=stage, model=model,
            span_name=span_name, max_output_tokens=max_output_tokens,
            temperature=temperature, trace_metadata=trace_metadata,
            budget_gate=budget_gate,
        )

    run_id, task_id, operation = _identity(config, role, stage, span_name)
    logical_id = _logical_operation_id(
        run_id=run_id,
        task_id=task_id,
        operation=operation,
        model=model,
        messages=messages,
        tools=(),
        tool_choice=None,
        output_schema=None,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )
    operation_key = logical_id
    if budget_gate is not None:
        budget_gate.reserve_model_call(
            operation_key,
            estimated_input_tokens=max(1, count_tokens_approximately(list(messages))),
            estimated_output_tokens=max(1, int(max_output_tokens or 1024)),
            model_name=model,
        )
    recorder = get_trace_recorder(config)
    with recorder.start_span(
        name=span_name,
        kind="llm",
        agent_role=role,
        attributes={"stage": stage, "gateway": "litellm", "streaming": True},
        input_payload=list(messages),
        provider="litellm",
        model=model,
    ) as span:
        events: AsyncIterator[ModelStreamDelta | ModelStreamCompleted] = stream_factory(
            ModelRequest(
                run_id=run_id,
                task_id=task_id,
                logical_operation_id=logical_id,
                role=role,
                stage=stage,
                model=model,
                messages=list(messages),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                trace_metadata=trace_metadata or {},
            )
        )
        result: ModelResult[Any] | None = None
        first_packet_ms = 0.0
        downgrade_reason: str | None = None
        downgrade_uncertain = False
        try:
            try:
                first_event = await asyncio.wait_for(
                    events.__anext__(),
                    timeout=configurable.model_first_packet_timeout_seconds,
                )
            except (asyncio.TimeoutError, TimeoutError):
                # The request was dispatched, so tokens may still be billed;
                # keep the budget conservative on the downgrade path.
                downgrade_reason = "first_packet_timeout"
                downgrade_uncertain = True
            except StopAsyncIteration:
                raise ModelGatewayError("gateway_empty_response") from None
            except ModelGatewayError as exc:
                if exc.code in _STREAM_DOWNGRADE_CODES:
                    downgrade_reason = "stream_unsupported"
                else:
                    raise
            if downgrade_reason is None:
                if isinstance(first_event, ModelStreamCompleted):
                    result = first_event.result
                    first_packet_ms = first_event.first_packet_ms
                else:
                    while result is None:
                        try:
                            event = await asyncio.wait_for(
                                events.__anext__(),
                                timeout=configurable.model_call_timeout_seconds,
                            )
                        except (asyncio.TimeoutError, TimeoutError) as exc:
                            raise ModelGatewayError("gateway_stream_idle_timeout") from exc
                        except StopAsyncIteration:
                            break
                        if isinstance(event, ModelStreamCompleted):
                            result = event.result
                            first_packet_ms = event.first_packet_ms
                if result is None:
                    raise ModelGatewayError("gateway_stream_missing_result")
        except ModelGatewayError as exc:
            if budget_gate is not None:
                uncertain = exc.code in {
                    "gateway_unavailable",
                    "sandbox_gateway_unavailable",
                    "gateway_connection_failed",
                }
                budget_gate.fail_model_call(operation_key, uncertain=uncertain)
            span.record_outcome(error_type=exc.code)
            raise
        except Exception as exc:
            if budget_gate is not None:
                budget_gate.fail_model_call(operation_key, uncertain=False)
            span.record_outcome(error_type=type(exc).__name__)
            raise
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()
        if downgrade_reason is not None:
            if budget_gate is not None:
                budget_gate.fail_model_call(
                    operation_key, uncertain=downgrade_uncertain
                )
            span.record_outcome(error_type=f"stream_downgrade:{downgrade_reason}")
            return await downgrade(downgrade_reason)

        metrics = get_prometheus_metrics(configurable)
        if metrics is not None and first_packet_ms > 0:
            metrics.observe_first_token(
                provider=result.route.provider or "litellm",
                model=result.route.served_model or model,
                agent_role=role,
                operation=span_name,
                duration_seconds=first_packet_ms / 1000.0,
                probe_mode="stream",
                slow=(
                    first_packet_ms / 1000.0
                    > configurable.model_slow_first_packet_threshold_seconds
                ),
            )
        span.provider = result.route.provider or "litellm"
        span.model = result.route.served_model or model
        span.add_usage(
            TokenUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                total_tokens=result.usage.total_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                estimated_cost_usd=float(result.response_cost_usd or 0),
                cost_source=(
                    "provider_reported"
                    if result.response_cost_usd is not None
                    else "unavailable"
                ),
            ),
            result.route.provider or "litellm",
            result.route.served_model or model,
            event_key=logical_id,
            attempt_index=1,
            stage=stage,
            task_id=task_id,
            operation=span_name,
            duration_ms=int(result.latency_ms),
        )
        span.set_output(result.message)
        if budget_gate is not None:
            budget_gate.settle_model_call(
                operation_key,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                model_name=model,
            )
        message = attach_result_metadata(result)
        message.response_metadata["model_routed"] = {
            "requested_model": result.route.requested_model,
            "served_model": result.route.served_model,
            "deployment_id": result.route.deployment_id,
            "latency_ms": result.latency_ms,
            "first_packet_latency_ms": round(first_packet_ms, 3),
            "streamed": True,
            "logical_retry_count": 0,
        }
        if run_id != "service":
            try:
                from open_deep_research.events.public import event_publisher_from_config

                await event_publisher_from_config(config).publish(
                    "model.routed",
                    stage=stage,
                    payload=dict(message.response_metadata["model_routed"]),
                    dedupe_key=f"model-routed:{logical_id}",
                )
            except Exception:  # noqa: BLE001 - observability must fail open
                pass
        return message


__all__ = [
    "close_run_gateway",
    "complete_model",
    "complete_model_stream",
    "get_model_gateway",
]

"""Fail-open tracing and usage collection for Deep Research runs."""

# ruff: noqa: D102,D105,D107,UP037

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    get_buffer_string,
    message_chunk_to_message,
)
from langchain_core.messages.utils import count_tokens_approximately
from open_deep_research.config_types import RuntimeConfig
from pydantic import BaseModel

from open_deep_research.budgets import BudgetGate
from open_deep_research.configuration import Configuration
from open_deep_research.events.public import event_publisher_from_config
from open_deep_research.events.task_activity import (
    gateway_physical_process,
    publish_task_activity,
)
from open_deep_research.models.circuit import (
    CircuitFailureKind,
    CircuitOpenError,
    CircuitPermit,
    CircuitTransition,
    ModelCircuitBreaker,
    ModelCircuitState,
    get_model_circuit_registry,
    model_circuit_policy_from_configuration,
)
from open_deep_research.observability.telemetry import (
    monotonic_time,
)
from open_deep_research.security.redaction import redact_text as _redact_text

from open_deep_research.observability.tracing import (  # noqa: F401
    NoopSpanContext,
    SQLiteTraceStore,
    SpanContext,
    TokenUsage,
    TraceRecorder,
    _current_langfuse_span_id,
    _current_run_id,
    _current_span_ctx,
    _current_span_id,
    _exc_message,
    _get_store,
    _json,
    _message_preview,
    _now,
    _safe_int,
    _stores,
    _stores_lock,
    bind_span_context,
    current_span_ids,
    get_trace_recorder,
)

logger = logging.getLogger(__name__)

_gateway_tool_model_semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}

# The currently-entered SpanContext (set in SpanContext.__enter__). Governance
# retry code uses TraceRecorder.active_span() to reach it and record retries on
# the span opened by observe_tool_call, without needing the span handle itself.


def _safe_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction from an SDK/LangChain exception."""
    for attr in ("status_code", "status"):
        status = getattr(exc, attr, None)
        if isinstance(status, int):
            return status
    return None


def _exception_failure_usage(exc: BaseException) -> dict[str, int] | None:
    """Return usage counts a raised model error still carries.

    Covers two carriers: the sandbox gateway attaches ``failure_usage`` to its
    RuntimeError for failed outcomes, and the OpenAI SDK's truncation errors
    keep the finished completion on ``exc.completion``.
    """
    attached = getattr(exc, "failure_usage", None)
    if isinstance(attached, dict) and (
        attached.get("input_tokens") or attached.get("output_tokens")
    ):
        return attached
    completion = getattr(exc, "completion", None)
    usage = getattr(completion, "usage", None)
    if usage is not None:
        try:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            return None
        if input_tokens > 0 or output_tokens > 0:
            return {"input_tokens": input_tokens, "output_tokens": output_tokens}
    return None


def _is_uncertain_model_failure(exc: BaseException) -> bool:
    """Return whether the provider may have executed an indeterminate request."""
    if isinstance(exc, TimeoutError | ConnectionError | asyncio.CancelledError):
        return True
    name = type(exc).__name__.lower()
    message = _exc_message(exc).lower()
    uncertain_markers = (
        "timeout",
        "connection",
        "disconnect",
        "network",
        "transport",
        "brokenpipe",
        "incomplete read",
    )
    return any(marker in name or marker in message for marker in uncertain_markers)


def _gateway_tool_model_semaphore(
    configurable: Configuration,
    model_name: str | None,
) -> asyncio.Semaphore | None:
    """Limit direct provider fan-out from concurrent Gateway tool helpers."""
    if not gateway_physical_process():
        return None
    loop = asyncio.get_running_loop()
    limit = configurable.gateway_tool_model_max_concurrency
    key = (id(loop), model_name or "unknown", limit)
    semaphore = _gateway_tool_model_semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _gateway_tool_model_semaphores[key] = semaphore
    return semaphore


def _provider_model(model_name: str | None) -> tuple[str | None, str | None]:
    if not model_name:
        return None, None
    if ":" not in model_name:
        return None, model_name
    provider, model = model_name.split(":", 1)
    return provider, model


_CIRCUIT_ROLE_STAGE = {
    "supervisor": "planning",
    "researcher": "researching",
    "summarization": "researching",
    "message_summary": "researching",
    "compression": "synthesizing",
    "final_report": "writing",
    "report_review": "writing",
    "report_reviewer": "writing",
    "report_revisor": "writing",
    "quality_evaluator": "finalizing",
    "quality_evaluation": "finalizing",
    "egress_classifier": "researching",
}


async def observe_model_circuit_transition(
    transition: CircuitTransition | None,
    config: RuntimeConfig | None,
    *,
    agent_role: str | None = None,
) -> None:
    """Best-effort publish one circuit transition to every configured sink."""
    if transition is None:
        return
    try:
        recorder = get_trace_recorder(config)
        if recorder.prometheus is not None:
            recorder._safe(  # noqa: SLF001 - shared fail-open recorder boundary
                recorder.prometheus.observe_model_circuit_transition,
                transition,
            )
        provider, model = _provider_model(transition.model_id)
        payload = {
            "provider": provider or "unknown",
            "model": model or transition.model_id,
            "from_state": transition.from_state.value,
            "to_state": transition.to_state.value,
            "reason": transition.reason,
            "failure_count": transition.failure_count,
            "slow_count": transition.slow_count,
            "sample_count": transition.sample_count,
            "slow_ratio": transition.slow_ratio,
            "cooldown_seconds": transition.cooldown_seconds,
            "forced_probe": transition.forced_probe,
        }
        metadata = (config or {}).get("metadata") or {}
        if metadata.get("run_id"):
            await event_publisher_from_config(config or {}).publish(
                "model.circuit_state",
                stage=_CIRCUIT_ROLE_STAGE.get(agent_role or "", "researching"),
                payload=payload,
                dedupe_key=(
                    f"model-circuit:{transition.model_id}:"
                    f"{transition.timestamp}:{transition.to_state.value}"
                ),
            )
        if metadata.get("task_id") and transition.to_state in {
            ModelCircuitState.OPEN,
            ModelCircuitState.CLOSED,
        }:
            recovered = transition.to_state is ModelCircuitState.CLOSED
            await publish_task_activity(
                config or {},
                "model.circuit_recovered" if recovered else "model.circuit_open",
                kind="model",
                phase="reasoning",
                status="success" if recovered else "warning",
                title="模型线路已恢复" if recovered else "模型线路已暂时隔离",
                summary=(
                    "半开探针成功，后续调用已恢复。"
                    if recovered
                    else "连续可恢复故障达到阈值，后续调用将优先切换候选。"
                ),
                iteration=None,
                duration_ms=None,
                payload=payload,
                dedupe_key=(
                    f"activity:model-circuit:{transition.model_id}:"
                    f"{transition.timestamp}:{transition.to_state.value}"
                ),
                update_run_summary=True,
            )
    except Exception as exc:  # noqa: BLE001 - circuit observability fails open
        logger.debug("Model circuit transition publication failed open: %s", exc)


def _is_sandbox_gateway_proxy(model: Any) -> bool:
    """Recognize a Gateway proxy through LangChain binding wrappers."""
    pending = [model]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if getattr(candidate, "is_sandbox_gateway_model", False):
            return True
        if isinstance(candidate, dict):
            pending.extend(candidate.values())
            continue
        if isinstance(candidate, list | tuple | set | frozenset):
            pending.extend(candidate)
            continue
        for attribute in (
            "bound",
            "model",
            "runnable",
            "first",
            "middle",
            "last",
            "steps",
            "steps__",
            "mapper",
        ):
            nested = getattr(candidate, attribute, None)
            if nested is not None and nested is not candidate:
                pending.append(nested)
    return False


class UsageCaptureCallback(BaseCallbackHandler):
    """Capture raw provider usage before structured-output parsers discard it."""

    def __init__(
        self,
        *,
        recorder: "TraceRecorder | None" = None,
        config: RuntimeConfig | None = None,
        messages: list[BaseMessage] | None = None,
        model: Any = None,
        model_name: str | None = None,
        span_id: str | None = None,
        attempt_index: int = 1,
        agent_role: str | None = None,
        budget_gate: BudgetGate | None = None,
    ) -> None:
        self.raise_error = True
        self._seen_run_ids: set[str] = set()
        self._terminal_run_ids: set[str] = set()
        self.records: list[TokenUsage] = []
        self._budget_keys: dict[str, str] = {}
        self._settled_budget_keys: set[str] = set()
        self._pending_physical_ids: list[str] = []
        self._physical_counter = 0
        self._model_name = model_name or "unknown"
        self._model = model
        self._messages = list(messages or [])
        self._estimation_enabled = bool(
            recorder is not None
            and recorder.configuration.token_usage_estimation_enabled
        )
        self._estimated_input = 0
        self._estimated_output = 1
        remote_budget_authority = _is_sandbox_gateway_proxy(model)
        self._budget_gate: BudgetGate | None = (
            None if remote_budget_authority else budget_gate
        )
        if recorder is not None:
            metadata = (config or {}).get("metadata") or {}
            run_id = str(metadata.get("run_id") or "")
            if run_id and self._budget_gate is None and not remote_budget_authority:
                started_at = None
                if recorder.store is not None:
                    stored = recorder._safe(recorder.store.get_run, run_id) or {}
                    started_at = stored.get("started_at")
                self._budget_gate = BudgetGate.from_config(
                    recorder.configuration,
                    run_id,
                    started_at=float(started_at) if started_at else None,
                )
            try:
                self._estimated_input = max(
                    1, int(count_tokens_approximately(messages or []))
                )
            except Exception:  # noqa: BLE001
                self._estimated_input = max(
                    1, len(get_buffer_string(messages or [])) // 4
                )
            output_fields = {
                "researcher": "research_model_max_tokens",
                "supervisor": "research_model_max_tokens",
                "summarization": "summarization_model_max_tokens",
                "message_summary": "message_summary_model_max_tokens",
                "compression": "compression_model_max_tokens",
                "final_report": "final_report_model_max_tokens",
                "report_review": "report_review_model_max_tokens",
                "report_reviewer": "report_review_model_max_tokens",
                "report_revisor": "final_report_model_max_tokens",
                "quality_evaluator": "quality_evaluation_model_max_tokens",
            }
            self._estimated_output = max(
                1,
                int(
                    getattr(
                        recorder.configuration,
                        output_fields.get(agent_role or "", "research_model_max_tokens"),
                        1,
                    )
                    or 1
                ),
            )
        self._budget_key_prefix = f"usage:{span_id or 'unknown'}:{attempt_index}"

    def begin_physical_attempt(self) -> None:
        """Pre-reserve a physical request before adapter callbacks can run."""
        self._physical_counter += 1
        placeholder = f"outer-{self._physical_counter}"
        operation_key = f"{self._budget_key_prefix}:{placeholder}"
        if self._budget_gate is not None:
            self._budget_gate.reserve_model_call(
                operation_key,
                estimated_input_tokens=self._estimated_input,
                estimated_output_tokens=self._estimated_output,
                model_name=self._model_name,
            )
        self._budget_keys[placeholder] = operation_key
        self._pending_physical_ids.append(placeholder)

    def _reserve_budget(self, run_id: Any) -> None:
        callback_run_id = str(run_id)
        if callback_run_id in self._budget_keys:
            return
        if self._pending_physical_ids:
            placeholder = self._pending_physical_ids.pop(0)
            self._budget_keys[callback_run_id] = self._budget_keys.pop(placeholder)
            return
        if self._budget_gate is None:
            return
        operation_key = f"{self._budget_key_prefix}:{callback_run_id}"
        self._budget_gate.reserve_model_call(
            operation_key,
            estimated_input_tokens=self._estimated_input,
            estimated_output_tokens=self._estimated_output,
            model_name=self._model_name,
        )
        self._budget_keys[callback_run_id] = operation_key

    def on_chat_model_start(
        self, _serialized: dict[str, Any], _messages: list[list[BaseMessage]], *, run_id: Any, **_kwargs: Any
    ) -> None:
        self._reserve_budget(run_id)

    def on_llm_start(
        self, _serialized: dict[str, Any], _prompts: list[str], *, run_id: Any, **_kwargs: Any
    ) -> None:
        self._reserve_budget(run_id)

    def on_llm_end(self, response: Any, *, run_id: Any, **_kwargs: Any) -> None:
        callback_run_id = str(run_id)
        if callback_run_id in self._pending_physical_ids:
            self._pending_physical_ids.remove(callback_run_id)
        if callback_run_id in self._seen_run_ids:
            return
        self._seen_run_ids.add(callback_run_id)
        self._terminal_run_ids.add(callback_run_id)
        candidates: list[Any] = []
        for generation_group in getattr(response, "generations", None) or []:
            for generation in generation_group or []:
                candidates.append(getattr(generation, "message", generation))
        candidates.append(response)
        for candidate in candidates:
            usage = TokenUsage.from_response(candidate)
            if usage.has_reported_tokens:
                usage.usage_source = (
                    "provider_reported"
                    if usage.input_tokens > 0 and usage.output_tokens > 0
                    else "provider_partial"
                )
                self.records.append(usage)
                operation_key = self._budget_keys.get(callback_run_id)
                if operation_key and self._budget_gate is not None:
                    self._budget_gate.settle_model_call(
                        operation_key,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        model_name=self._model_name,
                    )
                    self._settled_budget_keys.add(operation_key)
                return
        usage = (
            _estimated_usage(
                self._model,
                self._messages,
                candidates[0] if candidates else response,
            )
            if self._estimation_enabled
            else TokenUsage(usage_source="missing")
        )
        self.records.append(usage)
        operation_key = self._budget_keys.get(callback_run_id)
        if operation_key and self._budget_gate is not None:
            self._budget_gate.settle_model_call(
                operation_key,
                input_tokens=usage.estimated_input_tokens,
                output_tokens=usage.estimated_output_tokens,
                model_name=self._model_name,
            )
            self._settled_budget_keys.add(operation_key)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **_kwargs: Any) -> None:
        """Record a physical provider attempt even when a wrapper falls back."""
        self._record_failed_run(str(run_id), error)

    def _record_failed_run(self, callback_run_id: str, error: BaseException) -> None:
        if callback_run_id in self._terminal_run_ids:
            return
        if callback_run_id in self._pending_physical_ids:
            self._pending_physical_ids.remove(callback_run_id)
        self._terminal_run_ids.add(callback_run_id)
        self._seen_run_ids.add(callback_run_id)
        uncertain = _is_uncertain_model_failure(error)
        failure_usage = _exception_failure_usage(error)
        if failure_usage:
            # The provider finished (and billed) the generation before the
            # client raised; keep those tokens visible in failed-run totals
            # so retry storms do not vanish from usage dashboards.
            self.records.append(
                TokenUsage(
                    input_tokens=failure_usage.get("input_tokens", 0),
                    output_tokens=failure_usage.get("output_tokens", 0),
                    total_tokens=(
                        failure_usage.get("input_tokens", 0)
                        + failure_usage.get("output_tokens", 0)
                    ),
                    usage_source="provider_reported",
                    response_status="unknown_failed" if uncertain else "rejected",
                )
            )
        else:
            self.records.append(
                TokenUsage(
                    usage_source="missing",
                    response_status="unknown_failed" if uncertain else "rejected",
                )
            )
        operation_key = self._budget_keys.get(callback_run_id)
        if operation_key and self._budget_gate is not None:
            self._budget_gate.fail_model_call(operation_key, uncertain=uncertain)
            self._settled_budget_keys.add(operation_key)

    def settle_outer_failure(self, error: BaseException) -> None:
        """Finalize callback reservations when cancellation bypasses callbacks."""
        unresolved = [
            callback_run_id
            for callback_run_id in self._budget_keys
            if callback_run_id not in self._terminal_run_ids
        ]
        for callback_run_id in unresolved:
            self._record_failed_run(callback_run_id, error)
        if not self._budget_keys and not self.records:
            uncertain = _is_uncertain_model_failure(error)
            self.records.append(
                TokenUsage(
                    usage_source="missing",
                    response_status="unknown_failed" if uncertain else "rejected",
                )
            )

    def settle_outer_success(self, response: Any) -> None:
        """Capture adapters that return without dispatching LangChain callbacks."""
        unresolved = [
            callback_run_id
            for callback_run_id in self._budget_keys
            if callback_run_id not in self._terminal_run_ids
        ]
        for callback_run_id in unresolved:
            self.on_llm_end(response, run_id=callback_run_id)

    def settle_estimated_success(self, usage: TokenUsage) -> None:
        """Settle successful no-usage calls with the local fallback estimate."""
        if self._budget_gate is None:
            return
        for operation_key in self._budget_keys.values():
            if operation_key in self._settled_budget_keys:
                continue
            self._budget_gate.settle_model_call(
                operation_key,
                input_tokens=usage.estimated_input_tokens or usage.input_tokens,
                output_tokens=usage.estimated_output_tokens or usage.output_tokens,
                model_name=self._model_name,
            )
            self._settled_budget_keys.add(operation_key)

    async def flush_budget(self) -> None:
        """Flush an optional async remote authority at physical boundaries."""
        flush = getattr(self._budget_gate, "flush_pending", None)
        if not callable(flush):
            return
        result = flush()
        if inspect.isawaitable(result):
            await result


def _estimated_usage(model: Any, messages: list[BaseMessage], response: Any) -> TokenUsage:
    """Build a content-free fallback estimate for a successful model response."""
    input_tokens = 0
    output_tokens = 0
    counter = getattr(model, "get_num_tokens_from_messages", None)
    if callable(counter):
        try:
            input_tokens = max(0, int(counter(messages)))
        except Exception:  # noqa: BLE001 - tokenizer support varies by adapter
            input_tokens = 0
    if not input_tokens:
        try:
            input_tokens = max(0, int(count_tokens_approximately(messages)))
        except Exception:  # noqa: BLE001
            input_tokens = max(1, len(get_buffer_string(messages)) // 4)
    output_text = _message_preview(response, None, redact=False) or ""
    token_counter = getattr(model, "get_num_tokens", None)
    if callable(token_counter):
        try:
            output_tokens = max(0, int(token_counter(output_text)))
        except Exception:  # noqa: BLE001
            output_tokens = 0
    if not output_tokens and output_text:
        output_tokens = max(1, len(output_text) // 4)
    return TokenUsage(
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_total_tokens=input_tokens + output_tokens,
        usage_source="tokenizer_estimated",
    )


def _langchain_invoke_config(
    recorder: TraceRecorder,
    config: RuntimeConfig | None,
    capture: UsageCaptureCallback,
) -> RuntimeConfig:
    """Attach usage and optional Langfuse callbacks without mutating caller config."""
    invoke_config: dict[str, Any] = dict(config or {})
    callbacks = list(invoke_config.get("callbacks") or [])
    callbacks.append(capture)
    if (
        recorder.langfuse is not None
        and recorder.configuration.langfuse_langchain_callback_enabled
    ):
        handler = recorder._safe(recorder.langfuse.callback_handler)
        if handler is not None:
            callbacks.append(handler)
    invoke_config["callbacks"] = callbacks
    return cast(RuntimeConfig, invoke_config)


@dataclass(frozen=True, slots=True)
class _ModelInvocationResult:
    """Carry a model response with optional first-packet probe metadata."""

    response: Any
    ttft_seconds: float | None = None
    probe_status: str = "off"
    usage_records: tuple[TokenUsage, ...] = ()
    usage_capture: UsageCaptureCallback | None = None


def _observe_first_packet_metrics(
    recorder: TraceRecorder,
    result: _ModelInvocationResult,
    *,
    provider: str | None,
    model: str | None,
    agent_role: str | None,
    operation: str,
) -> None:
    """Best-effort record TTFT and safe streaming downgrade metrics."""
    metrics = recorder.prometheus
    if metrics is None:
        return
    if result.ttft_seconds is not None:
        recorder._safe(  # noqa: SLF001 - shared fail-open recorder boundary
            metrics.observe_first_token,
            provider=provider or "unknown",
            model=model or "unknown",
            agent_role=agent_role or "unknown",
            operation=operation,
            duration_seconds=result.ttft_seconds,
            probe_mode=recorder.configuration.model_first_packet_probe,
            slow=(
                result.ttft_seconds
                > recorder.configuration.model_slow_first_packet_threshold_seconds
            ),
        )
    if result.probe_status == "fallback":
        recorder._safe(  # noqa: SLF001
            metrics.observe_streaming_fallback,
            provider or "unknown",
            model or "unknown",
            "unsupported",
        )


async def _call_model_ainvoke(
    model: Any,
    messages: list[BaseMessage],
    invoke_config: RuntimeConfig | None,
) -> Any:
    """Invoke a model while preserving the optional callback config."""
    try:
        signature = inspect.signature(model.ainvoke)
        accepts_config = "config" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_config = True
    if invoke_config is None or not accepts_config:
        return await model.ainvoke(messages)
    return await model.ainvoke(messages, config=invoke_config)


def _streaming_unsupported(exc: BaseException) -> bool:
    """Recognize bounded capability errors that are safe to downgrade."""
    if isinstance(exc, NotImplementedError | AttributeError):
        return True
    text = _exc_message(exc).lower()
    return any(
        marker in text
        for marker in (
            "astream not implemented",
            "streaming is not supported",
            "streaming not supported",
            "does not support streaming",
        )
    )


async def _close_async_iterator(iterator: Any) -> None:
    """Best-effort close a model stream after success, error, or timeout."""
    close = getattr(iterator, "aclose", None)
    if callable(close):
        try:
            await close()
        except Exception:  # noqa: BLE001 - cleanup must preserve the real outcome
            pass


async def _ainvoke_model(
    model: Any,
    messages: list[BaseMessage],
    recorder: TraceRecorder,
    config: RuntimeConfig | None,
    *,
    span_id: str | None = None,
    attempt_index: int = 1,
    model_name: str | None = None,
    agent_role: str | None = None,
    usage_capture: UsageCaptureCallback | None = None,
) -> _ModelInvocationResult:
    """Invoke with optional TTFT streaming and conservative fallback."""
    capture = usage_capture or UsageCaptureCallback(
        recorder=recorder,
        config=config,
        messages=messages,
        model=model,
        model_name=model_name,
        span_id=span_id,
        attempt_index=attempt_index,
        agent_role=agent_role,
    )
    invoke_config = _langchain_invoke_config(recorder, config, capture)

    async def begin_attempt() -> None:
        capture.begin_physical_attempt()
        await capture.flush_budget()

    async def settle_success(response: Any) -> None:
        capture.settle_outer_success(response)
        await capture.flush_budget()

    async def settle_failure(exc: BaseException) -> None:
        capture.settle_outer_failure(exc)
        await capture.flush_budget()

    async def call_model() -> Any:
        await begin_attempt()
        try:
            response = await _call_model_ainvoke(model, messages, invoke_config)
            await settle_success(response)
            return response
        except BaseException as exc:
            await settle_failure(exc)
            setattr(exc, "usage_capture_records", tuple(capture.records))
            raise

    configuration = recorder.configuration
    probe_mode = (
        configuration.model_first_packet_probe
        if configuration.model_circuit_breaker_enabled
        else "off"
    )
    if probe_mode == "off":
        return _ModelInvocationResult(
            await call_model(),
            usage_records=tuple(capture.records),
            usage_capture=capture,
        )

    stream_method = getattr(model, "astream", None)
    if not callable(stream_method):
        return _ModelInvocationResult(
            await call_model(),
            probe_status="fallback",
            usage_records=tuple(capture.records),
            usage_capture=capture,
        )

    iterator: Any = None
    received_first = False
    started = monotonic_time()
    await begin_attempt()
    try:
        try:
            stream_signature = inspect.signature(stream_method)
            stream_accepts_config = "config" in stream_signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in stream_signature.parameters.values()
            )
        except (TypeError, ValueError):
            stream_accepts_config = True
        iterator = (
            stream_method(messages)
            if invoke_config is None or not stream_accepts_config
            else stream_method(messages, config=invoke_config)
        )
        if inspect.isawaitable(iterator):
            iterator = await iterator
        try:
            first = (
                await asyncio.wait_for(
                    anext(iterator),
                    timeout=configuration.model_first_packet_timeout_seconds,
                )
                if probe_mode == "enforced"
                else await anext(iterator)
            )
        except StopAsyncIteration as exc:
            raise RuntimeError("model stream returned no chunks") from exc
        received_first = True
        first_elapsed = monotonic_time() - started

        if isinstance(first, AIMessageChunk):
            merged = first
            shape_ok = True
            async for chunk in iterator:
                if not isinstance(chunk, AIMessageChunk):
                    shape_ok = False
                    break
                merged = merged + chunk
            if shape_ok:
                response = message_chunk_to_message(merged)
                await settle_success(response)
                return _ModelInvocationResult(
                    response,
                    ttft_seconds=max(0.0, first_elapsed),
                    probe_status="streamed",
                    usage_records=tuple(capture.records),
                    usage_capture=capture,
                )
        else:
            trailing = [item async for item in iterator]
            if not trailing:
                await settle_success(first)
                return _ModelInvocationResult(
                    first,
                    probe_status="non_streaming_wrapper",
                    usage_records=tuple(capture.records),
                    usage_capture=capture,
                )
            # Structured-output runnables stream incremental parser partials
            # of one result; every item is the same pydantic type and the
            # final item is the complete parsed value (verified against
            # ``with_structured_output(..., method="function_calling")``).
            if isinstance(first, BaseModel) and all(
                isinstance(item, type(first)) for item in trailing
            ):
                response = trailing[-1]
                await settle_success(response)
                return _ModelInvocationResult(
                    response,
                    ttft_seconds=max(0.0, first_elapsed),
                    probe_status="non_streaming_wrapper",
                    usage_records=tuple(capture.records),
                    usage_capture=capture,
                )
        # The stream shape cannot be merged faithfully (mixed chunk types or
        # fragment-style non-message items). The probe must never change call
        # semantics, so degrade to the plain non-streaming invoke instead of
        # raising — this also keeps shadow mode observation-only.
        if iterator is not None:
            await _close_async_iterator(iterator)
            iterator = None
        await settle_failure(
            RuntimeError("stream disconnected before final usage metadata")
        )
        return _ModelInvocationResult(
            await call_model(),
            probe_status="fallback",
            usage_records=tuple(capture.records),
            usage_capture=capture,
        )
    except Exception as exc:
        if not received_first and (
            probe_mode == "shadow" or _streaming_unsupported(exc)
        ):
            await settle_failure(exc)
            return _ModelInvocationResult(
                await call_model(),
                probe_status="fallback",
                usage_records=tuple(capture.records),
                usage_capture=capture,
            )
        await settle_failure(exc)
        setattr(exc, "usage_capture_records", tuple(capture.records))
        raise
    finally:
        if iterator is not None:
            await _close_async_iterator(iterator)


def _usage_stage(agent_role: str | None, attributes: dict[str, Any]) -> str:
    stage = str(attributes.get("stage") or "")
    if stage in {"preparing", "planning", "researching", "synthesizing", "writing", "finalizing"}:
        return stage
    return _CIRCUIT_ROLE_STAGE.get(agent_role or "", "preparing")


def _usage_attributes(
    attributes: dict[str, Any] | None,
    stage: str | None,
) -> dict[str, Any]:
    result = dict(attributes or {})
    if stage is not None:
        if stage not in {
            "preparing",
            "planning",
            "researching",
            "synthesizing",
            "writing",
            "finalizing",
        }:
            raise ValueError(f"Unsupported token accounting stage: {stage}")
        result["stage"] = stage
    return result


def _sum_usage(records: list[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in records),
        output_tokens=sum(item.output_tokens for item in records),
        total_tokens=sum(item.total_tokens for item in records),
        cached_input_tokens=sum(item.cached_input_tokens for item in records),
        cache_creation_input_tokens=sum(
            item.cache_creation_input_tokens for item in records
        ),
        reasoning_tokens=sum(item.reasoning_tokens for item in records),
        estimated_input_tokens=sum(item.estimated_input_tokens for item in records),
        estimated_output_tokens=sum(item.estimated_output_tokens for item in records),
        estimated_total_tokens=sum(item.estimated_total_tokens for item in records),
        estimated_cost_usd=sum(item.estimated_cost_usd for item in records),
        usage_source=(records[0].usage_source if len(records) == 1 else "provider_reported"),
    )


from open_deep_research.observability.usage_events import _publish_usage_revision, _run_accounting_status


async def _forward_gateway_tool_usage(
    config: RuntimeConfig | None,
    *,
    span_id: str,
    provider: str | None,
    model_id: str | None,
    agent_role: str | None,
    operation: str,
    stage: str,
    records: list[tuple[TokenUsage, str]],
    attempt_index: int,
    duration_ms: int | None,
) -> None:
    """Report gateway tool-side model usage to the API's single writer.

    The Gateway container has no trace store by design, so model calls made
    while executing tools (search summarization, semantic rerank, evidence
    extraction) would otherwise vanish from the usage projection. Rows are
    forwarded over the signed internal API instead; logical model RPCs are
    excluded by the caller (the journaled transition backfills those).
    Fail-open by contract.
    """
    try:
        from open_deep_research.sandbox.internal_api import (
            SandboxInternalClient,
            UsageReportRequest,
        )

        metadata = (config or {}).get("metadata") or {}
        run_id = str(metadata.get("run_id") or "").strip()
        base_url = os.getenv("SANDBOX_API_INTERNAL_URL", "").rstrip("/")
        fence_token = int(metadata.get("run_fence_token") or 0)
        if not run_id or not base_url or fence_token < 1:
            return
        root_key = Configuration.from_runnable_config(config).sandbox_root_signing_key
        if not root_key:
            return
        client = SandboxInternalClient(base_url, root_key)
        # The Gateway runs with observability disabled, so its spans are noop
        # contexts whose span_id is None; a stable-but-shared key would make
        # every forwarded row collide on the (run, event_key) unique index.
        origin = str(span_id) if span_id else uuid.uuid4().hex
        for physical_index, (usage, response_status) in enumerate(records, start=1):
            physical_attempt_index = attempt_index + physical_index - 1
            request = client.signed(
                UsageReportRequest,
                run_id=run_id,
                task_id=str(metadata.get("task_id") or ""),
                fence_token=fence_token,
                stage=stage,
                agent_role=agent_role,
                provider=provider,
                model=model_id,
                operation=operation,
                event_key=(
                    f"gateway-tool:{origin}:{physical_attempt_index}:{response_status}"
                ),
                attempt_index=max(1, physical_attempt_index),
                duration_ms=duration_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                estimated_input_tokens=usage.estimated_input_tokens,
                estimated_output_tokens=usage.estimated_output_tokens,
                estimated_total_tokens=usage.estimated_total_tokens,
                usage_source=usage.usage_source,
                response_status=response_status,
            )
            await client.post("/internal/sandbox/usage/report", request)
    except Exception as exc:  # noqa: BLE001 - accounting is fail-open
        logger.debug("Gateway tool usage forward failed: %s", exc)


def _should_forward_gateway_tool_usage(
    attributes: dict[str, Any] | None,
    *,
    gateway_operation: str,
) -> bool:
    """Detect gateway tool-execution model calls needing remote reporting."""
    return (
        not gateway_operation
        and gateway_physical_process()
        and not (attributes or {}).get("gateway")
    )


async def _record_successful_invocation(
    *,
    recorder: TraceRecorder,
    span: Any,
    invocation_result: _ModelInvocationResult,
    response: Any,
    model: Any,
    messages: list[BaseMessage],
    config: RuntimeConfig | None,
    provider: str | None,
    model_id: str | None,
    agent_role: str | None,
    operation: str,
    attributes: dict[str, Any],
    attempt_index: int,
    duration_ms: int | None,
) -> TokenUsage:
    records = list(invocation_result.usage_records)
    if not records:
        fallback = TokenUsage.from_response(response)
        if fallback.has_reported_tokens:
            fallback.usage_source = (
                "provider_reported"
                if fallback.input_tokens > 0 and fallback.output_tokens > 0
                else "provider_partial"
            )
            records = [fallback]
    if not records and recorder.configuration.token_usage_estimation_enabled:
        records = [_estimated_usage(model, messages, response)]
    if not records:
        records = [TokenUsage(usage_source="missing")]
    task_id = str((config or {}).get("metadata", {}).get("task_id") or "") or None
    stage = _usage_stage(agent_role, attributes)
    # Gateway-mediated calls are journaled by the Gateway and backfilled into
    # usage_events by the authenticated operations/transition endpoint; the
    # caller-side row would double count the same logical operation.
    gateway_operation = str(
        (getattr(response, "response_metadata", None) or {}).get(
            "gateway_logical_operation_id"
        )
        or ""
    )
    forward_usage = _should_forward_gateway_tool_usage(
        attributes, gateway_operation=gateway_operation
    )
    revisions: list[int] = []
    if forward_usage:
        await _forward_gateway_tool_usage(
            config,
            span_id=span.span_id,
            provider=provider,
            model_id=model_id,
            agent_role=agent_role,
            operation=operation,
            stage=stage,
            records=[
                (usage, usage.response_status or "success") for usage in records
            ],
            attempt_index=attempt_index,
            duration_ms=duration_ms,
        )
    elif not gateway_operation:
        for physical_index, usage in enumerate(records, start=1):
            physical_attempt_index = attempt_index + physical_index - 1
            revision = span.add_usage(
                usage,
                provider,
                model_id,
                event_key=f"{span.span_id}:{physical_attempt_index}:success",
                attempt_index=physical_attempt_index,
                stage=stage,
                task_id=task_id,
                operation=operation,
                duration_ms=duration_ms,
                response_status=usage.response_status,
            )
            if revision:
                revisions.append(int(revision))
    aggregate = _sum_usage(records)
    if invocation_result.usage_capture is not None:
        invocation_result.usage_capture.settle_estimated_success(aggregate)
    status = _run_accounting_status(recorder, span.run_id)
    await _publish_usage_revision(config, max(revisions, default=None), status)
    return aggregate


async def _record_failed_invocation(
    *,
    recorder: TraceRecorder,
    span: Any,
    exc: BaseException,
    config: RuntimeConfig | None,
    provider: str | None,
    model_id: str | None,
    agent_role: str | None,
    operation: str,
    attributes: dict[str, Any],
    attempt_index: int,
    usage_capture: UsageCaptureCallback | None = None,
) -> int:
    if usage_capture is not None:
        usage_capture.settle_outer_failure(exc)
    captured = list(
        usage_capture.records
        if usage_capture is not None
        else (getattr(exc, "usage_capture_records", ()) or ())
    )
    task_id = str((config or {}).get("metadata", {}).get("task_id") or "") or None
    status = "unknown_failed" if _is_uncertain_model_failure(exc) else "rejected"
    records = captured or [
        TokenUsage(
            usage_source="missing",
            response_status=status,
        )
    ]
    revisions: list[int] = []
    # Same single-writer rule as the success path: gateway-mediated failures
    # are backfilled from the journaled outcome by the API process.
    gateway_operation = str(getattr(exc, "gateway_logical_operation_id", "") or "")
    forward_usage = _should_forward_gateway_tool_usage(
        attributes, gateway_operation=gateway_operation
    )
    if forward_usage:
        await _forward_gateway_tool_usage(
            config,
            span_id=span.span_id,
            provider=provider,
            model_id=model_id,
            agent_role=agent_role,
            operation=operation,
            stage=_usage_stage(agent_role, attributes),
            records=[
                (
                    usage,
                    usage.response_status if captured else status,
                )
                for usage in records
            ],
            attempt_index=attempt_index,
            duration_ms=None,
        )
    elif not gateway_operation:
        for physical_index, usage in enumerate(records, start=1):
            physical_attempt_index = attempt_index + physical_index - 1
            revision = span.add_usage(
                usage,
                provider,
                model_id,
                event_key=f"{span.span_id}:{physical_attempt_index}:failed",
                attempt_index=physical_attempt_index,
                stage=_usage_stage(agent_role, attributes),
                task_id=task_id,
                operation=operation,
                response_status=usage.response_status if captured else status,
            )
            if revision:
                revisions.append(int(revision))
    await _publish_usage_revision(
        config,
        max(revisions, default=None),
        _run_accounting_status(recorder, span.run_id),
    )
    return len(records)


def apply_helicone_config(
    model_config: dict[str, Any],
    runtime_config: RuntimeConfig | None,
    *,
    span_name: str,
    agent_role: str | None = None,
) -> dict[str, Any]:
    """Attach Helicone headers/base URL when configured.

    LangChain providers differ in which transport fields they accept. This
    function only enriches the config dict; callers and model providers may
    ignore unsupported fields without affecting local SQLite tracing.
    """
    configuration = Configuration.from_runnable_config(runtime_config)
    if not configuration.helicone_enabled or not configuration.helicone_api_key:
        return dict(model_config)

    run_id, parent_span_id = current_span_ids()
    headers = {
        "Helicone-Auth": f"Bearer {configuration.helicone_api_key}",
        "Helicone-Session-Id": run_id or runtime_config.get("metadata", {}).get("run_id", "default")
        if runtime_config
        else "default",
        "Helicone-Session-Path": f"/{agent_role or 'agent'}/{span_name}",
        "Helicone-Property-Run-Id": run_id or "",
        "Helicone-Property-Parent-Span-Id": parent_span_id or "",
        "Helicone-Property-Agent-Role": agent_role or "",
        "Helicone-Property-Stage": span_name,
        "Helicone-Property-Model": str(model_config.get("model", "")),
    }
    enriched = dict(model_config)
    if configuration.helicone_headers_enabled:
        existing_headers = dict(enriched.get("default_headers") or enriched.get("headers") or {})
        existing_headers.update(headers)
        enriched["default_headers"] = existing_headers
        enriched["headers"] = existing_headers
    if configuration.helicone_base_url:
        enriched["base_url"] = configuration.helicone_base_url
    return enriched


async def invoke_model_with_observability(
    model: Any,
    messages: list[BaseMessage],
    config: RuntimeConfig | None,
    *,
    span_name: str,
    agent_role: str | None = None,
    model_name: str | None = None,
    stage: str | None = None,
    attributes: dict[str, Any] | None = None,
    budget_gate: BudgetGate | None = None,
) -> Any:
    """Invoke a model while recording a local LLM span and token usage."""
    provider, model_id = _provider_model(model_name)
    recorder = get_trace_recorder(config)
    span_attributes = _usage_attributes(attributes, stage)
    if recorder.langfuse is not None and recorder.configuration.langfuse_langchain_callback_enabled:
        span_attributes["langfuse_callback_managed"] = True
    with recorder.start_span(
        name=span_name,
        kind="llm",
        agent_role=agent_role,
        attributes=span_attributes,
        input_payload=messages,
        provider=provider,
        model=model_id or model_name,
    ) as span:
        usage_capture = UsageCaptureCallback(
            recorder=recorder,
            config=config,
            messages=messages,
            model=model,
            model_name=model_name,
            span_id=span.span_id,
            attempt_index=1,
            agent_role=agent_role,
            budget_gate=budget_gate,
        )
        try:
            invocation_result = await _ainvoke_model(
                model,
                messages,
                recorder,
                config,
                span_id=span.span_id,
                attempt_index=1,
                model_name=model_name,
                agent_role=agent_role,
                usage_capture=usage_capture,
            )
        except Exception as exc:
            await _record_failed_invocation(
                recorder=recorder,
                span=span,
                exc=exc,
                config=config,
                provider=provider,
                model_id=model_id or model_name,
                agent_role=agent_role,
                operation=span_name,
                attributes=span_attributes,
                attempt_index=1,
                usage_capture=usage_capture,
            )
            raise
        response = invocation_result.response
        span.attributes["llm.first_token_probe_status"] = (
            invocation_result.probe_status
        )
        if invocation_result.ttft_seconds is not None:
            span.attributes["llm.first_token_latency_seconds"] = (
                invocation_result.ttft_seconds
            )
        _observe_first_packet_metrics(
            recorder,
            invocation_result,
            provider=provider,
            model=model_id or model_name,
            agent_role=agent_role,
            operation=span_name,
        )
        await _record_successful_invocation(
            recorder=recorder,
            span=span,
            invocation_result=invocation_result,
            response=response,
            model=model,
            messages=messages,
            config=config,
            provider=provider,
            model_id=model_id or model_name,
            agent_role=agent_role,
            operation=span_name,
            attributes=span_attributes,
            attempt_index=1,
            duration_ms=int((monotonic_time() - span.started_monotonic) * 1000),
        )
        if (
            recorder.configuration.observability_enabled
            and getattr(recorder.configuration, "trace_payload_mode", "preview") != "none"
        ):
            span.output_preview = _message_preview(
                response,
                None if recorder.configuration.trace_payload_mode == "full" else recorder.configuration.trace_preview_chars,
                redact=recorder.configuration.trace_redaction_enabled,
            )
        return response


async def invoke_model_with_retry_observability(
    model: Any,
    messages: list[BaseMessage],
    config: RuntimeConfig | None,
    *,
    span_name: str,
    agent_role: str | None = None,
    model_name: str | None = None,
    stage: str | None = None,
    attributes: dict[str, Any] | None = None,
    budget_gate: BudgetGate | None = None,
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float | None = None,
    sleeper: Callable[[float], Awaitable[Any]] | None = None,
) -> Any:
    """Invoke a model with retry + a local LLM span, recording each retry.

    Replaces LangChain ``.with_retry`` so that retries and 429s land in the
    observability store. The retry budget defaults to
    ``configurable.model_transport_max_attempts`` (total attempts). Structured
    output parsing and context recovery use independent counters. The backoff
    reuses the tool-retry delay settings. Non-retryable and exhausted errors
    are surfaced unchanged.

    The classification helper is imported lazily from ``tools.governance`` to
    avoid a circular import (governance imports observability at module level).
    """
    from open_deep_research.tools.governance import classify_llm_retryable_error

    provider, model_id = _provider_model(model_name)
    recorder = get_trace_recorder(config)
    configurable = recorder.configuration
    if max_attempts is None:
        max_attempts = configurable.model_transport_max_attempts
    if base_delay is None:
        base_delay = configurable.tool_retry_base_delay
    if max_delay is None:
        max_delay = configurable.tool_retry_max_delay
    sleeper = sleeper or asyncio.sleep

    span_attributes = _usage_attributes(attributes, stage)
    if recorder.langfuse is not None and recorder.configuration.langfuse_langchain_callback_enabled:
        span_attributes["langfuse_callback_managed"] = True
    task_id = str((config or {}).get("metadata", {}).get("task_id") or "")
    activity_call_id = uuid.uuid4().hex
    activity_started = monotonic_time()
    activity_is_quality = agent_role in {
        "quality_evaluator",
        "report_review",
        "report_reviewer",
        "report_revisor",
    }
    if task_id:
        await publish_task_activity(
            config or {},
            "quality.started" if activity_is_quality else "model.started",
            kind="quality" if activity_is_quality else "model",
            phase="quality_check" if activity_is_quality else "reasoning",
            status="running",
            title="质量复核" if activity_is_quality else "模型规划",
            summary=(
                "正在依据质量合同复核当前证据。"
                if activity_is_quality
                else "Subagent 正在分析证据并规划下一步动作。"
            ),
            iteration=None,
            duration_ms=None,
            payload={
                "evaluation_type": span_name,
                "provider": provider,
                "model": model_id or model_name,
                "attempt": 1,
            },
            dedupe_key=f"activity:model:{activity_call_id}:started",
            update_run_summary=True,
        )
    with recorder.start_span(
        name=span_name,
        kind="llm",
        agent_role=agent_role,
        attributes=span_attributes,
        input_payload=messages,
        provider=provider,
        model=model_id or model_name,
    ) as span:
        circuit_breaker: ModelCircuitBreaker | None = None
        circuit_permit: CircuitPermit | None = None
        if configurable.model_circuit_breaker_enabled and model_name:
            try:
                circuit_breaker = get_model_circuit_registry().get_or_create(
                    model_name,
                    model_circuit_policy_from_configuration(configurable),
                )
                if circuit_breaker is not None:
                    circuit_permit, transition = await circuit_breaker.before_call()
                    await observe_model_circuit_transition(
                        transition,
                        config,
                        agent_role=agent_role,
                    )
            except CircuitOpenError as exc:
                span.record_outcome(error_type="model_circuit_open")
                if recorder.prometheus is not None:
                    recorder._safe(  # noqa: SLF001
                        recorder.prometheus.observe_model_circuit_rejection,
                        provider or "unknown",
                        model_id or model_name or "unknown",
                        exc.reason,
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - circuit governance fails open
                circuit_breaker = None
                circuit_permit = None
                logger.debug("Model circuit before_call failed open: %s", exc)
        attempt = 0  # attempts made so far (0 == first try in progress)
        physical_attempts_recorded = 0
        while True:
            usage_capture = UsageCaptureCallback(
                recorder=recorder,
                config=config,
                messages=messages,
                model=model,
                model_name=model_name,
                span_id=span.span_id,
                attempt_index=attempt + 1,
                agent_role=agent_role,
                budget_gate=budget_gate,
            )
            try:
                semaphore = _gateway_tool_model_semaphore(
                    configurable,
                    model_name,
                )
                if semaphore is not None:
                    await semaphore.acquire()
                try:
                    invocation = _ainvoke_model(
                        model,
                        messages,
                        recorder,
                        config,
                        span_id=span.span_id,
                        attempt_index=attempt + 1,
                        model_name=model_name,
                        agent_role=agent_role,
                        usage_capture=usage_capture,
                    )
                    invocation_result = await asyncio.wait_for(
                        invocation,
                        timeout=configurable.model_call_timeout_seconds,
                    )
                finally:
                    if semaphore is not None:
                        semaphore.release()
                response = invocation_result.response
            except Exception as exc:  # noqa: BLE001 -- classify then decide
                physical_attempts_recorded += await _record_failed_invocation(
                    recorder=recorder,
                    span=span,
                    exc=exc,
                    config=config,
                    provider=provider,
                    model_id=model_id or model_name,
                    agent_role=agent_role,
                    operation=span_name,
                    attributes=span_attributes,
                    attempt_index=physical_attempts_recorded + 1,
                    usage_capture=usage_capture,
                )
                error_type, retryable = classify_llm_retryable_error(exc)
                attempts_made = attempt + 1
                if not retryable or attempts_made >= max_attempts:
                    if circuit_breaker is not None and circuit_permit is not None:
                        try:
                            from open_deep_research.models.fallback import (
                                classify_model_error,
                            )

                            circuit_kind = classify_model_error(exc, model_name)
                            transition = None
                            if isinstance(exc, CircuitOpenError):
                                pass
                            elif circuit_kind.value in {
                                kind.value for kind in CircuitFailureKind
                            }:
                                transition = await circuit_breaker.record_failure(
                                    circuit_permit,
                                    failure_kind=CircuitFailureKind(
                                        circuit_kind.value
                                    ),
                                )
                            else:
                                transition = await circuit_breaker.record_inconclusive(
                                    circuit_permit
                                )
                            await observe_model_circuit_transition(
                                transition,
                                config,
                                agent_role=agent_role,
                            )
                        except Exception as circuit_exc:  # noqa: BLE001
                            logger.debug(
                                "Model circuit failure recording failed open: %s",
                                circuit_exc,
                            )
                    span.record_outcome(
                        error_type=error_type.value,
                        http_status=_safe_http_status(exc),
                    )
                    if task_id:
                        await publish_task_activity(
                            config or {},
                            "quality.failed" if activity_is_quality else "model.failed",
                            kind="quality" if activity_is_quality else "error",
                            phase="quality_check" if activity_is_quality else "reasoning",
                            status="error",
                            title="质量复核失败" if activity_is_quality else "模型调用失败",
                            summary="调用未能在重试预算内完成。",
                            iteration=None,
                            duration_ms=int((monotonic_time() - activity_started) * 1000),
                            payload={
                                "evaluation_type": span_name,
                                "provider": provider,
                                "model": model_id or model_name,
                                "error_code": error_type.value,
                                "error_class": type(exc).__name__,
                                "error_detail": _redact_text(_exc_message(exc))[:500],
                                "retry_count": attempt,
                            },
                            dedupe_key=f"activity:model:{activity_call_id}:failed",
                            update_run_summary=True,
                        )
                    raise
                delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, base_delay)
                span.record_retry(
                    attempt=attempts_made,
                    error_type=error_type.value,
                    http_status=_safe_http_status(exc),
                    retryable=True,
                    delay_s=delay,
                    message=_exc_message(exc),
                )
                if task_id:
                    await publish_task_activity(
                        config or {},
                        "model.retrying",
                        kind="quality" if activity_is_quality else "model",
                        phase="quality_check" if activity_is_quality else "reasoning",
                        status="warning",
                        title="质量复核重试" if activity_is_quality else "模型调用重试",
                        summary="遇到可恢复错误，正在按退避策略重试。",
                        iteration=None,
                        duration_ms=None,
                        payload={
                            "provider": provider,
                            "model": model_id or model_name,
                            "attempt": attempts_made,
                            "error_code": error_type.value,
                            "error_class": type(exc).__name__,
                            "error_detail": _redact_text(_exc_message(exc))[:500],
                            "delay_ms": int(delay * 1000),
                        },
                        dedupe_key=f"activity:model:{activity_call_id}:retry:{attempts_made}",
                        update_run_summary=True,
                    )
                await sleeper(delay)
                attempt += 1
                continue
            if circuit_breaker is not None and circuit_permit is not None:
                try:
                    transition = await circuit_breaker.record_success(
                        circuit_permit,
                        ttft_seconds=invocation_result.ttft_seconds,
                    )
                    await observe_model_circuit_transition(
                        transition,
                        config,
                        agent_role=agent_role,
                    )
                except Exception as exc:  # noqa: BLE001 - circuit governance fails open
                    logger.debug("Model circuit success recording failed open: %s", exc)
            span.attributes["llm.first_token_probe_status"] = (
                invocation_result.probe_status
            )
            if invocation_result.ttft_seconds is not None:
                span.attributes["llm.first_token_latency_seconds"] = (
                    invocation_result.ttft_seconds
                )
            _observe_first_packet_metrics(
                recorder,
                invocation_result,
                provider=provider,
                model=model_id or model_name,
                agent_role=agent_role,
                operation=span_name,
            )
            usage = await _record_successful_invocation(
                recorder=recorder,
                span=span,
                invocation_result=invocation_result,
                response=response,
                model=model,
                messages=messages,
                config=config,
                provider=provider,
                model_id=model_id or model_name,
                agent_role=agent_role,
                operation=span_name,
                attributes=span_attributes,
                attempt_index=physical_attempts_recorded + 1,
                duration_ms=int((monotonic_time() - activity_started) * 1000),
            )
            if (
                recorder.configuration.observability_enabled
                and getattr(recorder.configuration, "trace_payload_mode", "preview") != "none"
            ):
                span.output_preview = _message_preview(
                    response,
                    None if recorder.configuration.trace_payload_mode == "full" else recorder.configuration.trace_preview_chars,
                    redact=recorder.configuration.trace_redaction_enabled,
                )
            if task_id:
                await publish_task_activity(
                    config or {},
                    "quality.completed" if activity_is_quality else "model.completed",
                    kind="quality" if activity_is_quality else "model",
                    phase="quality_check" if activity_is_quality else "reasoning",
                    status="success",
                    title="质量复核响应完成" if activity_is_quality else "模型规划完成",
                    summary=(
                        "质量评估模型已返回结构化结果。"
                        if activity_is_quality
                        else "模型已完成本轮分析。"
                    ),
                    iteration=None,
                    duration_ms=int((monotonic_time() - activity_started) * 1000),
                    payload={
                        "evaluation_type": span_name,
                        "provider": provider,
                        "model": model_id or model_name,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "reasoning_tokens": usage.reasoning_tokens,
                        "retry_count": attempt,
                    },
                    dedupe_key=f"activity:model:{activity_call_id}:completed",
                    update_run_summary=True,
                )
            if (
                invocation_result.ttft_seconds is not None
                and isinstance(response, BaseMessage)
            ):
                response.response_metadata = {
                    **response.response_metadata,
                    "provider_ttft_ms": invocation_result.ttft_seconds * 1000,
                }
            return response


async def observe_tool_call(
    tool_call: dict[str, Any],
    role: str,
    config: RuntimeConfig,
    invoke: Callable[[], Awaitable[Any]],
) -> Any:
    """Run a tool call inside a tool span."""
    recorder = get_trace_recorder(config)
    name = tool_call.get("name", "unknown_tool")
    task_id = str(config.get("metadata", {}).get("task_id") or "")
    activity_call_id = str(tool_call.get("id") or uuid.uuid4().hex)
    activity_started = monotonic_time()
    category = (
        "search" if "search" in str(name).lower() else
        "fetch" if any(token in str(name).lower() for token in ("fetch", "browser")) else
        "completion" if str(name) == "ResearchComplete" else
        "reasoning" if str(name) == "think_tool" else
        "tool"
    )
    if task_id:
        args = tool_call.get("args") or {}
        args_summary = ""
        for key in ("query", "search_query", "url"):
            value = args.get(key) if isinstance(args, dict) else None
            if isinstance(value, str) and value.strip():
                args_summary = " ".join(value.split())[:240]
                break
        await publish_task_activity(
            config,
            "tool.started",
            kind="tool",
            phase="tool_execution",
            status="running",
            title=f"执行工具 · {name}",
            summary=(args_summary or f"正在调用 {name}。"),
            iteration=None,
            duration_ms=None,
            payload={
                "tool_call_id": activity_call_id,
                "tool_name": name,
                "tool_category": category,
                "args_summary": args_summary,
                "args_keys": sorted(args.keys()) if isinstance(args, dict) else [],
            },
            dedupe_key=f"activity:tool:{activity_call_id}:started",
            update_run_summary=True,
        )
    with recorder.start_span(
        name=f"tool.{name}",
        kind="tool",
        agent_role=role,
        attributes={
            "tool_call_id": tool_call.get("id"),
            "tool_name": name,
            "args_keys": sorted((tool_call.get("args") or {}).keys()),
        },
        input_payload=tool_call,
    ) as span:
        try:
            result = await invoke()
        except Exception as exc:
            if task_id:
                await publish_task_activity(
                    config,
                    "tool.failed",
                    kind="error",
                    phase="tool_execution",
                    status="error",
                    title=f"工具失败 · {name}",
                    summary="工具调用未能完成。",
                    iteration=None,
                    duration_ms=int((monotonic_time() - activity_started) * 1000),
                    payload={
                        "tool_call_id": activity_call_id,
                        "tool_name": name,
                        "tool_category": category,
                        "error_code": type(exc).__name__,
                        "retry_count": getattr(span, "retry_count", 0),
                    },
                    dedupe_key=f"activity:tool:{activity_call_id}:failed",
                    update_run_summary=True,
                )
            raise
        result_content = getattr(getattr(result, "message", result), "content", result)
        result_text = str(result_content)
        result_urls = set(re.findall(r"https?://[^\s\]\)>'\"}]+", result_text))
        span.attributes["result_chars"] = len(result_text)
        span.attributes["source_count"] = len(result_urls)
        task_id = str(config.get("metadata", {}).get("task_id", ""))
        if role == "researcher" and task_id:
            try:
                from open_deep_research.tasks.registry import get_task_registry

                record = get_task_registry().get(task_id)
                expected_run = str(config.get("metadata", {}).get("run_id", "default"))
                if record is not None and record.run_id == expected_run:
                    if "search" in str(name).lower():
                        record.query_count += 1
                    record.source_urls.update(result_urls)
                    record.source_count = max(record.source_count, len(record.source_urls))
                    record.citation_count += len(result_urls)
                    record.retry_count += getattr(span, "retry_count", 0)
            except Exception as exc:  # noqa: BLE001 - metrics must stay fail-open
                logger.debug("Unable to update task research metrics: %s", exc)
        if getattr(recorder.configuration, "trace_payload_mode", "preview") != "none":
            if (
                role == "researcher"
                and getattr(
                    recorder.configuration,
                    "prompt_injection_protection_enabled",
                    True,
                )
            ):
                span.output_preview = json.dumps(
                    {
                        "content_hash": hashlib.sha256(
                            result_text.encode("utf-8", errors="replace")
                        ).hexdigest(),
                        "result_chars": len(result_text),
                        "source_count": len(result_urls),
                    },
                    sort_keys=True,
                )
            else:
                span.output_preview = _message_preview(
                    result,
                    None
                    if recorder.configuration.trace_payload_mode == "full"
                    else recorder.configuration.trace_preview_chars,
                    redact=recorder.configuration.trace_redaction_enabled,
                )
        if task_id:
            governed_error = getattr(result, "error", None)
            await publish_task_activity(
                config,
                "tool.failed" if governed_error is not None else "tool.completed",
                kind="error" if governed_error is not None else "tool",
                phase="tool_execution",
                status="error" if governed_error is not None else "success",
                title=(f"工具失败 · {name}" if governed_error is not None else f"工具完成 · {name}"),
                summary=(
                    "工具返回了受治理的错误结果。"
                    if governed_error is not None
                    else f"工具已完成，识别到 {len(result_urls)} 个来源链接。"
                ),
                iteration=None,
                duration_ms=int((monotonic_time() - activity_started) * 1000),
                payload={
                    "tool_call_id": activity_call_id,
                    "tool_name": name,
                    "tool_category": category,
                    "source_count": len(result_urls),
                    "result_chars": len(result_text),
                    "retry_count": getattr(span, "retry_count", 0),
                    "error_code": getattr(getattr(governed_error, "error_type", None), "value", None),
                    "urls": sorted(result_urls),
                },
                dedupe_key=(
                    f"activity:tool:{activity_call_id}:failed"
                    if governed_error is not None
                    else f"activity:tool:{activity_call_id}:completed"
                ),
                update_run_summary=True,
            )
        return result


async def await_with_observability_timeout(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Tiny wrapper to keep timeout call sites readable."""
    return await asyncio.wait_for(awaitable, timeout=timeout)

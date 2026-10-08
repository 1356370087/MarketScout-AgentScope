"""Tool governance: permission control, validation, egress policy, and retry.

This module centralizes all cross-cutting concerns for *how* tools are invoked
by the AgentScope runtime in this project:

* **Origin policy** -- using origin declared on the project ``Tool`` Interface.
* **Permission control** -- a per-role gate combining a tool-name whitelist with
  an origin blocklist, plus an MCP auth-required token presence check.
* **Parameter validation** -- configured JSON-Schema constraints followed by
  Pydantic model validation before any side effect.
* **Retry with exponential backoff** -- for retryable tool errors (network,
  timeout, rate-limit/429, service-unavailable/503), returning a structured
  ``ToolError`` (machine-readable JSON) to the model when retries are exhausted.

The public entry point is :func:`execute_governed_tool_call`. It returns both a
transport message and the original ``ToolResult`` while containing tool failures
so one call cannot abort a concurrent batch. Run lifecycle interruptions propagate
to the owning recovery session.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

if TYPE_CHECKING:
    from open_deep_research.config_types import RuntimeConfig
else:
    RuntimeConfig = dict[str, Any]
from pydantic import BaseModel, Field, ValidationError

from open_deep_research.configuration import Configuration
from open_deep_research.documents.contracts import selection_from_config
from open_deep_research.sandbox.policy import (
    allowed_domains,
    egress_host_from_url,
    is_enforced_mode,
    network_policy_mode,
)
from open_deep_research.tasks.events import EventType, JSONLEventWriter, ResearchEvent
from open_deep_research.tools.base import (
    Tool,
    ToolContext,
    ToolEffect,
    ToolOrigin,
    ToolResult,
    serialize_tool_output,
)

# Native OpenAI/Anthropic SDKs are optional direct dependencies. They are guarded
# so this module imports cleanly even when the SDKs are absent; classification
# just falls back to the shared error classifier in that case.
try:  # pragma: no cover - import guard
    import openai as _openai_sdk
except ImportError:  # pragma: no cover
    _openai_sdk = None
try:  # pragma: no cover - import guard
    import anthropic as _anthropic_sdk
except ImportError:  # pragma: no cover
    _anthropic_sdk = None

logger = logging.getLogger(__name__)

##########################
# Enums
##########################


def get_trace_recorder(config: RuntimeConfig):
    """Load the legacy trace adapter only for legacy callers."""
    from open_deep_research.observability import get_trace_recorder as get_recorder

    return get_recorder(config)




@dataclass(frozen=True, slots=True)
class ToolOutcomeMessage:
    """Framework-neutral, rendered tool outcome used by both transports."""

    content: str
    name: str
    tool_call_id: str


class AgentRole(str, Enum):
    """Which graph is invoking the tool -- drives whitelist/origin resolution."""

    SUPERVISOR = "supervisor"
    RESEARCHER = "researcher"


class ToolErrorType(str, Enum):
    """Stable, LLM-parseable error categories emitted in structured errors."""

    permission_denied = "permission_denied"
    validation_error = "validation_error"
    rate_limited = "rate_limited"
    timeout = "timeout"
    network_error = "network_error"
    service_unavailable = "service_unavailable"
    max_retries_exceeded = "max_retries_exceeded"
    tool_not_found = "tool_not_found"
    egress_domain_denied = "egress_domain_denied"
    egress_domain_pending = "egress_domain_pending"
    sensitive_tool_approval_required = "sensitive_tool_approval_required"
    interaction_required = "interaction_required"
    runtime_missing_result = "runtime_missing_result"
    runtime_duplicate_result = "runtime_duplicate_result"
    runtime_hook_error = "runtime_hook_error"
    task_capacity_exceeded = "task_capacity_exceeded"
    cancelled = "cancelled"
    deadline_exceeded = "deadline_exceeded"
    budget_exhausted = "budget_exhausted"
    internal_error = "internal_error"
    unknown = "unknown"


##########################
# Origin labeling
##########################

def get_tool_retryable(tool: Tool) -> bool:
    """Return retry policy declared directly by the Tool Interface."""
    return tool.retryable


def get_tool_origin(tool: Tool) -> ToolOrigin:
    """Return origin declared directly by the Tool Interface."""
    return tool.origin


def get_tool_effect(tool: Tool) -> ToolEffect:
    """Return the tool effect, failing closed for undeclared MCP tools."""
    effect = getattr(tool, "effect", None)
    if isinstance(effect, ToolEffect):
        return effect
    if get_tool_origin(tool) is ToolOrigin.MCP:
        return ToolEffect.DESTRUCTIVE
    return ToolEffect.READ_ONLY


def get_tool_concurrency_safe(tool: Tool) -> bool:
    """Return whether the tool explicitly permits concurrent execution."""
    return getattr(tool, "concurrency_safe", False) is True


def get_tool_supports_idempotency(tool: Tool) -> bool:
    """Return whether the tool honors a stable operation id."""
    return getattr(tool, "supports_idempotency", False) is True


##########################
# Structured error
##########################


class ToolError(BaseModel):
    """A structured, LLM-parseable tool failure rendered into a ``ToolMessage``."""

    error_type: ToolErrorType
    tool_name: str
    message: str
    attempts: Optional[int] = None
    """Number of execution attempts made (set on retryable/max_retries_exceeded)."""
    retryable: bool = False
    """Whether a retry could plausibly help (hint for the model)."""
    detail: dict[str, Any] = Field(default_factory=dict)
    """Machine-readable context, e.g. ``{"status": 503}`` or ``{"missing": ["queries"]}``."""

    def to_tool_message(self, tool_call_id: str) -> ToolOutcomeMessage:
        """Render this error as a ``ToolMessage`` whose content is the JSON payload."""

        return ToolOutcomeMessage(
            content=self.model_dump_json(),
            name=self.tool_name,
            tool_call_id=tool_call_id,
        )


@dataclass(frozen=True, slots=True)
class GovernedToolCallResult:
    """Transport message plus the original typed result of one invocation."""

    message: ToolOutcomeMessage
    result: Optional[ToolResult[Any]] = None
    error: Optional[ToolError] = None
    confirmed_outcome: bool = False


class ToolOutcomeError(RuntimeError):
    """A trusted remote executor returned a definite structured failure."""

    def __init__(self, error: ToolError) -> None:
        """Keep the remote error without mistaking it for a lost response."""
        super().__init__(error.message)
        self.error = error


def _error_result(error: ToolError, tool_call_id: str, *, confirmed_outcome=False) -> GovernedToolCallResult:
    """Build a governed outcome for a structured failure."""
    return GovernedToolCallResult(
        message=ToolOutcomeMessage(error.model_dump_json(), error.tool_name, tool_call_id),
        error=error,
        confirmed_outcome=confirmed_outcome,
    )


def _safe_exc_str(exc: BaseException) -> str:
    """Render an exception to a string, tolerating exceptions whose ``__str__`` itself raises."""
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 -- e.g. aiohttp ClientResponseError with request_info=None
        text = f"{type(exc).__name__} (message unavailable)"
    return text or type(exc).__name__


def _safe_status(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of an HTTP status from an exception."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    return None


class ApprovalPendingError(Exception):
    """A sandbox egress approval was created and awaits a human decision.

    Raised by gateway-backed tool proxies when the gateway returns the
    ``approval_required`` wire status instead of blocking the RPC. The
    researcher loop treats the affected turn as approval-pending (no budget
    burn) and prompts the model to re-issue the same call, which then
    attaches to the still-pending approval and waits for the decision.
    """

    def __init__(self, message: str, *, approval_id: str = "", domain: str = "") -> None:
        """Store the human-facing message plus approval identity markers."""
        super().__init__(message)
        self.approval_id = approval_id
        self.domain = domain


class ToolExecutionFailure(Exception):
    """Internal signal raised by :func:`invoke_tool_with_retry` when retries are exhausted.

    Carries the classified :class:`ToolErrorType` and attempt count so the entry
    point can render a single structured error without re-classifying.
    """

    def __init__(self, error_type: ToolErrorType, attempts: int, inner: BaseException) -> None:
        """Store the classified type, attempt count, and original exception."""
        super().__init__(f"{error_type.value} after {attempts} attempt(s): {_safe_exc_str(inner)}")
        self.error_type = error_type
        self.attempts = attempts
        self.inner = inner


##########################
# Error classification
##########################


def classify_retryable_error(exc: BaseException) -> tuple[ToolErrorType, bool]:
    """Classify an exception into a (error_type, retryable) pair.

    Retryable categories: timeout, network errors, rate-limit (429),
    service-unavailable (503 and other 5xx). Non-retryable for client 4xx.
    Recurses into ``__cause__`` / ``__context__`` and ``ExceptionGroup`` so that
    errors wrapped by libraries (e.g. Tavily, MCP adapters) are still detected.
    """
    # 1. Timeouts.
    if isinstance(exc, asyncio.TimeoutError):
        return ToolErrorType.timeout, True

    # 1b. Approval waits are surfaced as a distinct, non-retryable category:
    # an immediate in-process retry would hot-loop against a human decision.
    if isinstance(exc, ApprovalPendingError):
        return ToolErrorType.egress_domain_pending, False

    # 2. aiohttp HTTP response errors carrying a status code.
    if isinstance(exc, aiohttp.ClientResponseError):
        status = getattr(exc, "status", None)
        if status == 429:
            return ToolErrorType.rate_limited, True
        if status == 503 or (status is not None and status >= 500):
            return ToolErrorType.service_unavailable, True
        if status in (408, 425):
            return ToolErrorType.rate_limited, True
        return ToolErrorType.unknown, False

    # 3. Generic connection / network errors.
    if isinstance(exc, aiohttp.ClientError | ConnectionError | OSError):
        return ToolErrorType.network_error, True

    # 4. Interaction requests from the native MCP adapter carry a validated URL for the approval layer;
    # they are never retried in-process.
    if getattr(exc, "interaction_url", None):
        return ToolErrorType.interaction_required, False


    # 6. Recurse into the cause chain (errors wrapped by libraries).
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return classify_retryable_error(cause)

    # 7. ExceptionGroup (Python 3.11+) -- check each sub-exception.
    if hasattr(exc, "exceptions"):
        for sub in exc.exceptions:
            et, retryable = classify_retryable_error(sub)
            if retryable:
                return et, retryable

    return ToolErrorType.unknown, False


def _classify_http_status(status: Any) -> Optional[tuple[ToolErrorType, bool]]:
    """Map an HTTP status to (error_type, retryable); None if not retryable-coded."""
    if not isinstance(status, int):
        return None
    if status == 429:
        return ToolErrorType.rate_limited, True
    if status == 503 or status >= 500:
        return ToolErrorType.service_unavailable, True
    if status in (408, 425):
        return ToolErrorType.rate_limited, True
    return None


def _is_llm_parse_failure(exc: BaseException) -> bool:
    """Return whether a non-transient schema or parse failure occurred."""
    if isinstance(exc, ValidationError):
        return True
    return False


def _classify_openai_error(exc: BaseException) -> Optional[tuple[ToolErrorType, bool]]:
    """Classify a native OpenAI SDK exception, or None if it is not one."""
    if _openai_sdk is None:
        return None
    if isinstance(exc, _openai_sdk.RateLimitError):
        return ToolErrorType.rate_limited, True
    if isinstance(exc, _openai_sdk.APITimeoutError):
        return ToolErrorType.timeout, True
    if isinstance(exc, _openai_sdk.APIConnectionError):
        return ToolErrorType.network_error, True
    if isinstance(exc, _openai_sdk.APIStatusError):
        mapped = _classify_http_status(getattr(exc, "status_code", None))
        if mapped is not None:
            return mapped
        return ToolErrorType.unknown, False
    return None


def _classify_anthropic_error(exc: BaseException) -> Optional[tuple[ToolErrorType, bool]]:
    """Classify a native Anthropic SDK exception, or None if it is not one."""
    if _anthropic_sdk is None:
        return None
    if isinstance(exc, _anthropic_sdk.RateLimitError):
        return ToolErrorType.rate_limited, True
    if isinstance(exc, _anthropic_sdk.APITimeoutError):
        return ToolErrorType.timeout, True
    if isinstance(exc, _anthropic_sdk.APIConnectionError):
        return ToolErrorType.network_error, True
    if isinstance(exc, _anthropic_sdk.APIStatusError):
        mapped = _classify_http_status(getattr(exc, "status_code", None))
        if mapped is not None:
            return mapped
        return ToolErrorType.unknown, False
    return None


def classify_llm_retryable_error(exc: BaseException) -> tuple[ToolErrorType, bool]:
    """Classify an LLM / native-SDK exception into (error_type, retryable).

    Extends :func:`classify_retryable_error` with explicit branches for the
    native OpenAI/Anthropic SDK exception types so that LLM 429s and transient
    transport errors are retried (and recorded by the observability retry loop).
    Schema/parse failures (``OutputParserException``, pydantic ``ValidationError``)
    are explicitly non-retryable -- retrying them would spin. Anything not matched
    here falls through to the shared classifier, which handles aiohttp status
    codes, ``ConnectionError``/``TimeoutError``, ``ToolException`` keyword hints,
    and the ``__cause__``/``__context__``/``ExceptionGroup`` chains.
    """
    # 1. Schema/parse failures are not transient.
    if _is_llm_parse_failure(exc):
        return ToolErrorType.unknown, False

    # 2. Native OpenAI SDK errors.
    result = _classify_openai_error(exc)
    if result is not None:
        return result

    # 3. Native Anthropic SDK errors.
    result = _classify_anthropic_error(exc)
    if result is not None:
        return result

    # 4. Fall back to the shared classifier (aiohttp/connection/timeout/cause chain).
    return classify_retryable_error(exc)


##########################
# Parameter validation (dependency-free JSON-Schema subset)
##########################


# JSON-Schema types mapped to Python type checks.
_PY_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _check_value(name: str, value: Any, spec: dict[str, Any]) -> Optional[ToolError]:
    """Validate a single argument value against its JSON-Schema property spec.

    Covers the subset emitted by LangChain's ``tool.args``: type, enum, array
    item type, and the standard numeric/string/array bound constraints
    (``minimum``/``maximum``, ``minLength``/``maxLength``, ``minItems``/
    ``maxItems``). Lenient on anything unrecognized -- returns ``None`` rather
    than raising.
    """
    # Union inputs (e.g. text | structured team message) must retain every branch.
    # Nested model refs are validated by the tool's Pydantic boundary afterwards.
    if isinstance(spec.get("anyOf"), list) and spec["anyOf"]:
        constraints = {key: value for key, value in spec.items() if key != "anyOf"}
        errors = [_check_value(name, value, {**branch, **constraints})
                  for branch in spec["anyOf"] if isinstance(branch, dict)]
        return None if not errors or any(error is None for error in errors) else errors[0]
    type_name = spec.get("type")
    if isinstance(type_name, list):
        errors = [_check_value(name, value, {**spec, "type": branch}) for branch in type_name]
        return None if any(error is None for error in errors) else errors[0]

    # Enum constraint (checked before/after type -- order tolerant).
    if "enum" in spec and value not in spec["enum"]:
        return ToolError(
            error_type=ToolErrorType.validation_error,
            tool_name="",
            message=f"Argument '{name}' must be one of {spec['enum']}; got {value!r}",
            detail={"argument": name, "allowed": spec["enum"], "got": value},
        )

    if type_name:
        checker = _PY_TYPE_CHECKS.get(type_name)
        if checker is not None and not checker(value):
            return ToolError(
                error_type=ToolErrorType.validation_error,
                tool_name="",
                message=f"Argument '{name}' expected type '{type_name}'; got {type(value).__name__}",
                detail={"argument": name, "expected_type": type_name, "got_type": type(value).__name__},
            )

    # Numeric bounds (integer/number).
    if type_name in ("integer", "number") and isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' must be >= {spec['minimum']}; got {value}",
                detail={"argument": name, "constraint": "minimum", "minimum": spec["minimum"], "got": value},
            )
        if "maximum" in spec and value > spec["maximum"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' must be <= {spec['maximum']}; got {value}",
                detail={"argument": name, "constraint": "maximum", "maximum": spec["maximum"], "got": value},
            )

    # String length bounds.
    if type_name == "string" and isinstance(value, str):
        if "minLength" in spec and len(value) < spec["minLength"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' length must be >= {spec['minLength']}; got length {len(value)}",
                detail={"argument": name, "constraint": "minLength", "minLength": spec["minLength"], "got_length": len(value)},
            )
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' length must be <= {spec['maxLength']}; got length {len(value)}",
                detail={"argument": name, "constraint": "maxLength", "maxLength": spec["maxLength"], "got_length": len(value)},
            )

    # Array bounds and element type.
    if type_name == "array" and isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' must have >= {spec['minItems']} items; got {len(value)}",
                detail={"argument": name, "constraint": "minItems", "minItems": spec["minItems"], "got_items": len(value)},
            )
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            return ToolError(
                error_type=ToolErrorType.validation_error, tool_name="",
                message=f"Argument '{name}' must have <= {spec['maxItems']} items; got {len(value)}",
                detail={"argument": name, "constraint": "maxItems", "maxItems": spec["maxItems"], "got_items": len(value)},
            )
        if "items" in spec and isinstance(spec["items"], dict):
            item_spec = spec["items"]
            item_type = item_spec.get("type")
            if item_type:
                item_checker = _PY_TYPE_CHECKS.get(item_type)
                if item_checker is not None:
                    for i, item in enumerate(value):
                        if not item_checker(item):
                            return ToolError(
                                error_type=ToolErrorType.validation_error, tool_name="",
                                message=f"Argument '{name}[{i}]' expected type '{item_type}'; got {type(item).__name__}",
                                detail={"argument": name, "index": i, "expected_type": item_type, "got_type": type(item).__name__},
                            )
                # Per-element string-length bounds (e.g. per-query max length).
                if item_type == "string":
                    for i, item in enumerate(value):
                        if isinstance(item, str):
                            err = _check_value(f"{name}[{i}]", item, item_spec)
                            if err is not None:
                                return err
    return None


def validate_tool_args(
    tool: Tool, args: dict[str, Any], config: Optional[RuntimeConfig] = None,
) -> Optional[ToolError]:
    """Validate ``args`` against the tool's LLM-facing input schema.

    Uses ``tool.input_schema.model_json_schema()``, which yields a standard
    JSON-Schema object with ``properties`` and ``required`` keys. Returns
    ``None`` on success, or a :class:`ToolError` describing the first problem.
    Never raises.

    ``InjectedToolArg`` parameters (e.g. ``config``, ``max_results`` for
    ``tavily_search``) are excluded from the LLM-facing schema by LangChain and
    are *not* in ``required``, so the validator never demands them. They may
    still appear in ``properties``; since they are injected at runtime rather
    than emitted by the model, we ignore any properties not present in ``args``.

    When ``config`` is provided and ``tool_param_constraints`` is set, per-tool
    parameter bounds (``minItems``/``maxItems``/``minLength``/``maxLength``/
    ``minimum``/``maximum``) are layered on top of the schema's own constraints.
    """
    try:
        schema = tool.input_schema.model_json_schema()
    except Exception:  # noqa: BLE001 -- malformed adapters fail leniently here
        return None
    if not isinstance(schema, dict):
        return None  # Unknown schema shape -- lenient.

    required = schema.get("required", []) or []
    properties = schema.get("properties", {}) or {}
    additional_props_false = schema.get("additionalProperties") is False

    # Missing required arguments.
    missing = [k for k in required if k not in args]
    if missing:
        return ToolError(
            error_type=ToolErrorType.validation_error,
            tool_name=tool.name,
            message=f"Missing required arguments: {missing}",
            detail={"missing": missing},
        )

    # Resolve per-tool configured constraints (layered on top of the schema).
    extra_constraints: dict[str, dict[str, Any]] = {}
    if config is not None:
        configurable = Configuration.from_runnable_config(config)
        if configurable.tool_param_constraints:
            extra_constraints = configurable.tool_param_constraints.get(tool.name, {}) or {}

    for key, val in args.items():
        spec = properties.get(key)
        if spec is None:
            if additional_props_false:
                return ToolError(
                    error_type=ToolErrorType.validation_error,
                    tool_name=tool.name,
                    message=f"Unexpected argument '{key}'; tool does not accept it",
                    detail={"unexpected": [key]},
                )
            continue  # Lenient: ignore unknown args by default (LLMs add noise).
        if not isinstance(spec, dict):
            continue
        # Merge configured constraints into the spec copy for this argument.
        # Top-level bounds (minItems/maxItems/minLength/maxLength/minimum/maximum)
        # override; the 'items' sub-spec is deep-merged so a configured per-element
        # bound does not drop the schema's element type.
        merged_spec = spec
        if key in extra_constraints:
            merged_spec = dict(spec)
            for ck, cv in extra_constraints[key].items():
                if ck == "items" and isinstance(cv, dict) and isinstance(spec.get("items"), dict):
                    merged_spec["items"] = {**spec["items"], **cv}
                else:
                    merged_spec[ck] = cv
        err = _check_value(key, val, merged_spec)
        if err is not None:
            err.tool_name = tool.name
            return err
    return None


##########################
# Retry with exponential backoff
##########################


def _is_runtime_control_error(exc: Exception) -> bool:
    """Preserve lifecycle interruptions for the run's recovery session."""
    # RecoveryStore imports domain stages; defer this import to tool execution.
    from open_deep_research.agentscope_runtime.recovery_store import (
        FenceLost,
        UnknownOperation,
    )
    from open_deep_research.budgets import BudgetExhausted, DeadlineExceeded

    return isinstance(exc, (BudgetExhausted, DeadlineExceeded, FenceLost, UnknownOperation))


async def _call_with_limits(tool, input, context):
    from open_deep_research.agentscope_runtime.runtime_limits import attributed, limited
    from open_deep_research.tools.base import ToolExecutionZone

    cfg = Configuration.from_runnable_config(context.config)
    with attributed(tool_call_id=context.tool_call_id, purpose="tool:" + tool.name):
        callback = lambda: tool.call(input, context)
        if (cfg.research_efficiency_mode == "bounded"
                and not getattr(tool, "remote_execution", False)
                and tool.execution_zone is not ToolExecutionZone.HOST_CONTROL):
            seconds = cfg.research_tool_call_timeout_seconds if tool.name in {
                "web_research", "fetch_url", "web_search", "source_discovery", "search_documents",
            } else cfg.tool_call_timeout_seconds
            return await limited(callback, seconds,
                deadline_at=context.config.get("metadata", {}).get("execution_deadline_at"))
        return await callback()


async def invoke_tool_with_retry(
    tool: Tool,
    input: BaseModel,
    context: ToolContext,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleeper: Optional[Callable[[float], Any]] = None,
    recorder: Any = None,
) -> ToolResult[Any]:
    """Invoke ``Tool.call`` with exponential backoff on retryable errors.

    Backoff: ``delay = min(max_delay, base_delay * 2**attempt) + jitter``, where
    jitter is ``random.uniform(0, base_delay)``. Retries only for retryable
    errors (see :func:`classify_retryable_error`) and only while
    ``attempt < max_retries``.

    Raises :class:`ToolExecutionFailure` when retries are exhausted or the error
    is non-retryable -- the caller (entry point) renders the structured error.
    ``sleeper`` is injectable for tests to avoid real delays.
    """
    sleeper = sleeper or asyncio.sleep
    attempt = 0
    while True:
        try:
            return await _call_with_limits(tool, input, replace(context, attempt=context.attempt + attempt))
        except Exception as exc:  # noqa: BLE001 -- classify then decide
            if _is_runtime_control_error(exc):
                raise
            error_type, retryable = classify_retryable_error(exc)
            if not retryable or attempt >= max_retries:
                final_type = (
                    ToolErrorType.max_retries_exceeded
                    if retryable and attempt >= max_retries
                    else error_type
                )
                raise ToolExecutionFailure(final_type, attempt + 1, exc) from exc
            delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, base_delay)
            logger.debug(
                "Tool %s failed with %s (retryable); retry %d/%d after %.2fs",
                tool.name, error_type.value, attempt + 1, max_retries, delay,
            )
            # Record this retry on the span opened by observe_tool_call (noop if
            # observability is disabled or no span is active).
            (recorder if recorder is not None else get_trace_recorder(context.config)).active_span().record_retry(
                attempt=attempt + 1,
                error_type=error_type.value,
                http_status=_safe_status(exc),
                retryable=True,
                delay_s=delay,
                message=_safe_exc_str(exc),
            )
            await sleeper(delay)
            attempt += 1


##########################
# Permission check
##########################


def _origin_blocklist(role: AgentRole, config: RuntimeConfig) -> set[ToolOrigin]:
    """Resolve the set of blocked origins for ``role`` from config."""
    configurable = Configuration.from_runnable_config(config)
    raw = (
        configurable.supervisor_blocked_origins
        if role is AgentRole.SUPERVISOR
        else configurable.researcher_blocked_origins
    )
    if not raw:
        return set()
    blocked: set[ToolOrigin] = set()
    for value in raw:
        try:
            blocked.add(ToolOrigin(value))
        except ValueError:
            logger.warning("Unknown tool origin %r in %s_blocked_origins; ignored", value, role.value)
    return blocked


def resolve_allowed_tools(
    role: AgentRole, config: RuntimeConfig, assembled_names: set[str]
) -> Optional[set[str]]:
    """Resolve the per-role tool-name whitelist.

    Returns ``None`` when no whitelist is configured (backward compatible: all
    assembled tools allowed). Otherwise returns the intersection with
    ``assembled_names`` so a stale whitelist cannot reference tools that are not
    actually present.
    """
    configurable = Configuration.from_runnable_config(config)
    whitelist = (
        configurable.supervisor_tool_whitelist
        if role is AgentRole.SUPERVISOR
        else configurable.researcher_tool_whitelist
    )
    if whitelist is None:
        return None
    return {name for name in whitelist if name in assembled_names}


def _peek_mcp_tokens(config: RuntimeConfig) -> Optional[Any]:
    """Check whether an MCP access token is present in config (no exchange work)."""
    configurable = config.get("configurable", {}) or {} if isinstance(config, dict) else {}
    tokens = configurable.get("mcp_tokens")
    return tokens if tokens else None


def get_user_permissions(config: RuntimeConfig) -> list[str]:
    """Read the authenticated user's role codes from the run config.

    Self-hosted IAM injects a runtime identity into
    ``config["configurable"]["langgraph_auth_user"]`` whose ``roles`` and
    ``effective_permissions`` fields remain distinct. Legacy identities that
    carried role codes in ``permissions`` and the plain
    ``configurable["user_permissions"]`` fallback remain readable. Returns
    ``[]`` when no user/roles are present (anonymous -> only agent-scope policy
    applies).
    """
    configurable = config.get("configurable", {}) or {} if isinstance(config, dict) else {}
    auth_user = configurable.get("langgraph_auth_user")
    if auth_user is not None:
        # BaseUser instance (server path) or a plain dict (tests).
        perms = getattr(auth_user, "roles", None)
        if perms is None and isinstance(auth_user, dict):
            perms = auth_user.get("roles") or auth_user.get("permissions")
        if isinstance(perms, list | tuple):
            return [str(p) for p in perms]
    fallback = configurable.get("user_permissions")
    if isinstance(fallback, list | tuple):
        return [str(p) for p in fallback]
    return []


def get_effective_permissions(config: RuntimeConfig) -> tuple[bool, set[str]]:
    """Return ``(authenticated, effective_permissions)`` from runtime config."""
    configurable = config.get("configurable", {}) or {} if isinstance(config, dict) else {}
    auth_user = configurable.get("langgraph_auth_user")
    if auth_user is None:
        return False, set()
    values = getattr(auth_user, "effective_permissions", None)
    if values is None and isinstance(auth_user, dict):
        values = auth_user.get("effective_permissions") or auth_user.get("permissions")
    if isinstance(values, list | tuple | set | frozenset):
        return True, {str(item) for item in values}
    return True, set()


_ORIGIN_PERMISSION = {
    ToolOrigin.SEARCH: "research.tool.search",
    ToolOrigin.PROVIDER_NATIVE: "research.tool.provider_native",
    ToolOrigin.MCP: "research.tool.mcp",
    ToolOrigin.BROWSER: "research.tool.browser",
    ToolOrigin.SKILL: "research.tool.skill",
    ToolOrigin.LOCAL_DOCUMENT: "research.tool.document",
}


def check_permission(
    tool_name: str,
    tool: Tool,
    role: AgentRole,
    allowed: Optional[set[str]],
    config: RuntimeConfig,
) -> Optional[ToolError]:
    """Run the permission gate: whitelist membership + origin policy + MCP auth.

    Returns ``None`` when permitted, or a ``permission_denied`` :class:`ToolError`.
    """
    # 1. Whitelist membership.
    if allowed is not None and tool_name not in allowed:
        return ToolError(
            error_type=ToolErrorType.permission_denied,
            tool_name=tool_name,
            message=f"Tool '{tool_name}' is not in the {role.value} tool whitelist.",
            detail={"role": role.value},
        )

    # 2. Origin policy.
    origin = get_tool_origin(tool)
    authenticated, effective_permissions = get_effective_permissions(config)
    origin_permission = _ORIGIN_PERMISSION.get(origin)
    if authenticated and origin_permission and origin_permission not in effective_permissions:
        return ToolError(
            error_type=ToolErrorType.permission_denied,
            tool_name=tool_name,
            message=f"Tool '{tool_name}' is not permitted for this user.",
            detail={"origin": origin.value, "required_permission": origin_permission},
        )
    blocked = _origin_blocklist(role, config)
    if origin in blocked:
        return ToolError(
            error_type=ToolErrorType.permission_denied,
            tool_name=tool_name,
            message=f"Tool '{tool_name}' (origin={origin.value}) is not permitted for the {role.value}.",
            detail={"role": role.value, "origin": origin.value},
        )

    # 3. MCP auth-required: a tool is only permitted if it was loaded with a
    #    valid token. load_mcp_tools tags the tool's metadata with
    #    ``mcp_auth_satisfied=True`` when auth_required and fetch_tokens
    #    succeeded, so we trust that marker rather than probing config (tokens
    #    are never written back into the run config). Fall back to a config token
    #    peek only for tools that predate the marker.
    if origin is ToolOrigin.MCP:
        configurable = Configuration.from_runnable_config(config)
        if configurable.mcp_config and configurable.mcp_config.auth_required:
            auth_satisfied = bool(getattr(tool, "auth_satisfied", False))
            if not auth_satisfied and not _peek_mcp_tokens(config):
                return ToolError(
                    error_type=ToolErrorType.permission_denied,
                    tool_name=tool_name,
                    message="MCP tool requires authentication but was not loaded with a valid token.",
                    detail={"origin": ToolOrigin.MCP.value, "auth_required": True},
                )

    # 4. User-role blacklist (deny). Layered on top of the agent-scope policy:
    # for each role the authenticated user holds, blocklisted tool names and
    # origins are denied. Users with no roles (anonymous) skip this layer, so
    # existing deployments and local Studio runs are unaffected.
    user_roles = get_user_permissions(config)
    if user_roles:
        configurable = Configuration.from_runnable_config(config)
        role_tool_bl = configurable.role_tool_blacklist or {}
        role_origin_bl = configurable.role_blocked_origins or {}
        for user_role in user_roles:
            role_blocked_tools = role_tool_bl.get(user_role) or []
            if tool_name in role_blocked_tools:
                return ToolError(
                    error_type=ToolErrorType.permission_denied,
                    tool_name=tool_name,
                    message=f"Tool '{tool_name}' is blocked for user role '{user_role}'.",
                    detail={"user_role": user_role, "scope": "tool"},
                )
            role_blocked_origins = role_origin_bl.get(user_role) or []
            if origin.value in role_blocked_origins:
                return ToolError(
                    error_type=ToolErrorType.permission_denied,
                    tool_name=tool_name,
                    message=f"Tool '{tool_name}' (origin={origin.value}) is blocked for user role '{user_role}'.",
                    detail={"user_role": user_role, "scope": "origin", "origin": origin.value},
                )
    return None


def filter_tools_by_permission(
    tools: list[Tool],
    role: AgentRole,
    config: RuntimeConfig,
) -> list[Any]:
    """Filter assembled ``tools`` down to those the ``role`` is permitted to bind.

    Applies the permission gate (whitelist membership + origin policy + user-role
    blacklist + MCP auth) to each tool and returns only the permitted ones. This
    runs *before* ``bind_tools`` so disallowed tool names and schemas are never
    exposed to the model. Parameter validation is intentionally not done here --
    it is an execution-time concern handled by :func:`execute_governed_tool_call`.

    Provider-native search ``dict`` tools are filtered by name like the rest.

    """
    names = {t.name for t in tools}
    allowed = resolve_allowed_tools(role, config, names)
    filtered: list[Tool] = []
    for t in tools:
        name = t.name
        if check_permission(name, t, role, allowed, config) is None:
            filtered.append(t)
    return filtered


##########################
# Egress domain allowlist
##########################


def _egress_hosts_for_tool(
    tool: Tool, args: dict[str, Any], configurable: Configuration
) -> list[str]:
    """Return every egress host a URL-bearing tool targets.

    Project tools declare URL extraction through ``Tool.egress_urls``. Browser
    tools retain a schema-key fallback because their remote schemas are not
    controlled here, while MCP retains its configured server URL fallback.
    """
    hosts: list[str] = []

    def add_url(value: str) -> None:
        host = egress_host_from_url(value)
        if host is not None and host not in hosts:
            hosts.append(host)

    extract_urls = getattr(tool, "egress_urls", None)
    if callable(extract_urls):
        for url in extract_urls(args):
            add_url(url)
    origin = get_tool_origin(tool)
    if origin is ToolOrigin.BROWSER:
        for key in ("url", "target_url", "href"):
            value = args.get(key)
            if isinstance(value, str):
                add_url(value)
    if origin is ToolOrigin.MCP:
        if configurable.mcp_config and configurable.mcp_config.url:
            add_url(configurable.mcp_config.url)
    return hosts


def _serialize_governed_output(
    tool: Tool,
    output: Any,
    configurable: Configuration,
) -> str:
    """Serialize output and apply the strictest global/per-tool character budget."""
    content = serialize_tool_output(output)
    declared_limit = getattr(tool, "max_output_chars", None)
    limit = min(
        declared_limit or configurable.max_mcp_output_chars,
        configurable.max_mcp_output_chars,
    )
    if len(content) <= limit:
        return content
    omitted = len(content) - limit
    return f"{content[:limit]}\n[truncated {omitted} chars]"


async def check_egress_domain_native(
    tool_call: dict[str, Any],
    tool: Tool,
    args: dict[str, Any],
    config: RuntimeConfig,
) -> Optional[ToolOutcomeMessage]:
    """Enforce the V7 egress allowlist, denying unknown hosts during M1."""
    tool_call_id = tool_call["id"]
    configurable = Configuration.from_runnable_config(config)
    if not is_enforced_mode(configurable):
        return None

    hosts = _egress_hosts_for_tool(tool, args, configurable)
    if not hosts:
        return None
    authorized_hosts = {
        str(value).lower()
        for value in config.get("metadata", {}).get(
            "sandbox_gateway_authorized_hosts", []
        )
    }
    unapproved_hosts = [host for host in hosts if host not in authorized_hosts]
    if not unapproved_hosts:
        return None
    mode = network_policy_mode(configurable)
    if mode == "offline":
        host = unapproved_hosts[0]
        error = ToolError(
            error_type=ToolErrorType.egress_domain_denied,
            tool_name=getattr(tool, "name", "unknown"),
            message="Network access is disabled for this run.",
            detail={
                "domain": host,
                "domains": unapproved_hosts,
                "network_mode": mode,
            },
        )
        return ToolOutcomeMessage(error.model_dump_json(), error.tool_name, tool_call_id)
    configured_domains = set(allowed_domains(configurable))
    denied_hosts = [host for host in unapproved_hosts if host not in configured_domains]
    if not denied_hosts:
        return None

    host = denied_hosts[0]
    run_id = str(config.get("metadata", {}).get("run_id", "default"))
    error = ToolError(
        error_type=ToolErrorType.egress_domain_denied,
        tool_name=getattr(tool, "name", "unknown"),
        message=(
            f"Domain '{host}' is not allowed by sandbox profile "
            f"'{configurable.sandbox_profile_id}'."
        ),
        detail={
            "domain": host,
            "domains": denied_hosts,
            "run_id": run_id,
            "network_mode": mode,
            "profile_id": configurable.sandbox_profile_id,
            "denied": True,
        },
    )
    return ToolOutcomeMessage(error.model_dump_json(), error.tool_name, tool_call_id)


def _is_preapproved_local_document_read(
    tool: Tool,
    effect: ToolEffect,
    config: RuntimeConfig,
) -> bool:
    """Trust the server-validated frozen selection for owner-scoped document reads."""
    if (
        tool.name not in {"search_documents", "knowledge_facts", "knowledge_wiki"}
        or get_tool_origin(tool) is not ToolOrigin.LOCAL_DOCUMENT
        or effect is not ToolEffect.SENSITIVE_READ
    ):
        return False
    try:
        return selection_from_config(config).documents_enabled and (tool.name == "search_documents" or bool((config.get("metadata") or {}).get("knowledge_manifest")))
    except ValueError:
        return False


async def execute_governed_tool_call_native(
    tool_call: dict[str, Any],
    tools_by_name: dict[str, Tool],
    role: AgentRole,
    config: RuntimeConfig,
    *,
    allowed_tools: Optional[set[str]] = None,
    apply_retry: bool = True,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleeper: Optional[Callable[[float], Any]] = None,
    operation_id: str = "",
    operation_attempt: int = 1,
    recorder: Any = None,
) -> GovernedToolCallResult:
    """Execute a single tool call under full governance.

    Tool errors return a ``GovernedToolCallResult``; runtime controls propagate.

    Pipeline:
    1. ``tool_not_found`` if the named tool is not registered for this role.
    2. Permission gate (whitelist + origin + MCP auth).
    3. Configured constraints and Pydantic input validation.
    4. Egress policy.
    5. ``Tool.call`` with optional retry and stable result rendering.
    """
    recorder = recorder if recorder is not None else get_trace_recorder(config)
    name = tool_call["name"]
    tool_call_id = tool_call["id"]
    args = tool_call.get("args", {}) or {}

    def observed_error(error: ToolError) -> GovernedToolCallResult:
        recorder.active_span().record_outcome(
            error_type=error.error_type.value,
            http_status=error.detail.get("status") if isinstance(error.detail, dict) else None,
        )
        return _error_result(error, tool_call_id)

    # tool_not_found
    tool = tools_by_name.get(name)
    if tool is None:
        return observed_error(ToolError(
            error_type=ToolErrorType.tool_not_found,
            tool_name=name,
            message=f"No tool named '{name}' is registered for the {role.value}.",
            detail={"role": role.value},
        ))

    # Permission gate.
    perm_err = check_permission(name, tool, role, allowed_tools, config)
    if perm_err is not None:
        return observed_error(perm_err)

    # Parameter validation (with per-tool configured constraints).
    val_err = validate_tool_args(tool, args, config)
    if val_err is not None:
        return observed_error(val_err)
    try:
        validated_input = tool.input_schema.model_validate(args)
    except ValidationError as exc:
        return observed_error(
            ToolError(
                error_type=ToolErrorType.validation_error,
                tool_name=name,
                message="Input failed Pydantic validation.",
                detail={"errors": exc.errors(include_url=False)},
            ),
        )

    # Capability policy. Researchers may autonomously use only read-only
    # tools. A host integration can approve an exact generated call id through
    # trusted metadata; HTTP clients cannot set this administrator-owned state.
    configurable = Configuration.from_runnable_config(config)
    effect = get_tool_effect(tool)
    approved_call_ids = {
        str(value)
        for value in config.get("metadata", {}).get("approved_sensitive_tool_call_ids", [])
    }
    if (
        role is AgentRole.RESEARCHER
        and configurable.require_sensitive_tool_approval
        and effect is not ToolEffect.READ_ONLY
        and effect is not ToolEffect.COORDINATION_WRITE
        and tool_call_id not in approved_call_ids
        and not _is_preapproved_local_document_read(tool, effect, config)
    ):
        recorder.active_span().score(
            "security.sensitive_tool_blocked", True, effect.value
        )
        if configurable.event_log_enabled:
            run_id = str(config.get("metadata", {}).get("run_id", "default"))
            writer = JSONLEventWriter(run_id=run_id, runs_dir=configurable.runs_dir)
            try:
                writer.write(ResearchEvent(
                    event_type=EventType.SENSITIVE_TOOL_BLOCKED,
                    task_id=str(config.get("metadata", {}).get("task_id", "researcher")),
                    run_id=run_id,
                    data={
                        "tool": name,
                        "effect": effect.value,
                        "tool_call_id": tool_call_id,
                    },
                ))
            finally:
                writer.close()
        return observed_error(
            ToolError(
                error_type=ToolErrorType.sensitive_tool_approval_required,
                tool_name=name,
                message=(
                    f"Tool '{name}' has effect '{effect.value}' and requires "
                    "explicit approval for this exact tool call."
                ),
                detail={"effect": effect.value, "tool_call_id": tool_call_id},
            )
        )

    # Egress domain allowlist for URL-bearing tools. May
    # block inline (in-process) until a supervisor decision arrives.
    egress_err = await check_egress_domain_native(tool_call, tool, args, config)
    if egress_err is not None:
        try:
            egress_error = ToolError.model_validate_json(str(egress_err.content))
        except (TypeError, ValueError):
            egress_error = ToolError(
                error_type=ToolErrorType.egress_domain_denied,
                tool_name=name,
                message="Tool egress policy denied this call.",
            )
        recorder.active_span().record_outcome(
            error_type=egress_error.error_type.value,
        )
        return GovernedToolCallResult(
            message=egress_err,
            error=egress_error,
        )

    context = ToolContext(
        config=config,
        role=role.value,
        tool_call_id=tool_call_id,
        operation_id=operation_id,
        attempt=operation_attempt,
    )

    # Automatic retries are safe for reads and for effectful tools that promise
    # to reuse the stable operation id supplied through ToolContext.
    retry_is_safe = effect in {ToolEffect.READ_ONLY, ToolEffect.SENSITIVE_READ} or (
        get_tool_supports_idempotency(tool) and bool(operation_id)
    )
    effective_retry = apply_retry and get_tool_retryable(tool) and retry_is_safe
    if not effective_retry:
        try:
            result = await _call_with_limits(tool, validated_input, context)
            return GovernedToolCallResult(
                message=ToolOutcomeMessage(
                    content=_serialize_governed_output(tool, result.output, configurable),
                    name=name,
                    tool_call_id=tool_call_id,
                ),
                result=result,
            )
        except Exception as exc:
            if _is_runtime_control_error(exc):
                raise
            if isinstance(exc, ToolOutcomeError):
                return _error_result(exc.error, tool_call_id, confirmed_outcome=True)
            error_type, _ = classify_retryable_error(exc)
            recorder.active_span().record_outcome(
                error_type=error_type.value,
                http_status=_safe_status(exc),
            )
            return _error_result(ToolError(
                error_type=error_type,
                tool_name=name,
                message=f"Tool execution failed: {_safe_exc_str(exc)}",
                detail={"status": _safe_status(exc)},
            ), tool_call_id)

    try:
        result = await invoke_tool_with_retry(
            tool, validated_input, context,
            max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleeper=sleeper, recorder=recorder,
        )
        return GovernedToolCallResult(
            message=ToolOutcomeMessage(
                content=_serialize_governed_output(tool, result.output, configurable),
                name=name,
                tool_call_id=tool_call_id,
            ),
            result=result,
        )
    except ToolExecutionFailure as failure:
        recorder.active_span().record_outcome(
            error_type=failure.error_type.value,
            http_status=_safe_status(failure.inner),
            retry_count=failure.attempts - 1,
        )
        return _error_result(ToolError(
            error_type=failure.error_type,
            tool_name=name,
            message=f"Tool execution failed after {failure.attempts} attempt(s): {_safe_exc_str(failure.inner)}",
            attempts=failure.attempts,
            retryable=False,
            detail={
                "status": _safe_status(failure.inner),
                "interaction_url": getattr(failure.inner, "interaction_url", None),
            },
        ), tool_call_id)
    except Exception as exc:  # non-retryable, surfaced directly
        if _is_runtime_control_error(exc):
            raise
        if isinstance(exc, ToolOutcomeError):
            return _error_result(exc.error, tool_call_id, confirmed_outcome=True)
        error_type, _ = classify_retryable_error(exc)
        recorder.active_span().record_outcome(
            error_type=error_type.value,
            http_status=_safe_status(exc),
        )
        return _error_result(ToolError(
            error_type=error_type,
            tool_name=name,
            message=f"Tool execution failed: {_safe_exc_str(exc)}",
            detail={"status": _safe_status(exc)},
        ), tool_call_id)






check_egress_domain = check_egress_domain_native
execute_governed_tool_call = execute_governed_tool_call_native

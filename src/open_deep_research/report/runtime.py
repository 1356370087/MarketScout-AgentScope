"""Report protocol messages and the required AgentScope execution port."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

native_report = ContextVar("native_report", default=None)
BaseMessage = Any
RunnableConfig = dict


class NativeReportRuntimeMissing(RuntimeError):
    """A report model call requires a bound, governed AgentScope runtime."""


def require_report_runtime():
    port = native_report.get()
    if port is None:
        raise NativeReportRuntimeMissing("native_report_runtime_required")
    return port


@dataclass
class ReportMessage:
    content: str
    type: str = "human"
    name: str | None = None
    additional_kwargs: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)


def AIMessage(**kwargs):
    return ReportMessage(type="ai", **kwargs)


def HumanMessage(**kwargs):
    return ReportMessage(type="human", **kwargs)


def SystemMessage(**kwargs):
    return ReportMessage(type="system", **kwargs)


def get_buffer_string(messages):
    return "\n".join(f"{getattr(m, 'type', 'user')}: {m.content}" for m in messages)


def count_tokens_approximately(messages):
    # Conservative UTF-8 bound; cannot under-budget CJK with chars/4.
    return sum(len(str(m.content).encode("utf-8")) + 16 for m in messages)


def resolve_model_context_window(model_name, *, overrides=None, unknown_default=32768):
    from open_deep_research.models.limits import get_model_token_limit

    port = native_report.get()
    if port is not None:
        catalog = port.models.factory.run.get("model_catalog_snapshot") or {}
        if model_name in catalog:
            return max(1, int(catalog[model_name]["context_window"]))
    if overrides and model_name in overrides:
        return max(1, int(overrides[model_name]))
    return max(1, int(get_model_token_limit(model_name) or unknown_default))


def get_trace_recorder(config):
    port = native_report.get()
    if port is not None:
        return port
    # Pure rendering can record domain scores without constructing a model.
    from open_deep_research.observability.tracing import get_trace_recorder
    return get_trace_recorder(config)


def get_today_str():
    from datetime import date

    port = native_report.get()
    value = date.fromisoformat(port.snapshot.report_date) if port is not None else date.today()
    return f"{value:%a} {value:%b} {value.day}, {value:%Y}"

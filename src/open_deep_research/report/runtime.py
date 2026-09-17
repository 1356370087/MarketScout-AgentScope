"""Report model boundary; native runs never import the legacy model stack."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

native_report = ContextVar("native_report", default=None)
BaseMessage = Any
RunnableConfig = dict


@dataclass
class ReportMessage:
    content: str
    type: str = "human"
    name: str | None = None
    additional_kwargs: dict = field(default_factory=dict)
    response_metadata: dict = field(default_factory=dict)


def _message(kind, **kwargs):
    if native_report.get() is not None:
        return ReportMessage(
            type={
                "AIMessage": "ai",
                "HumanMessage": "human",
                "SystemMessage": "system",
            }[kind],
            **kwargs,
        )
    return getattr(import_module("langchain_core.messages"), kind)(**kwargs)


def AIMessage(**kwargs):
    return _message("AIMessage", **kwargs)


def HumanMessage(**kwargs):
    return _message("HumanMessage", **kwargs)


def SystemMessage(**kwargs):
    return _message("SystemMessage", **kwargs)


def get_buffer_string(messages):
    if native_report.get() is not None:
        return "\n".join(f"{getattr(m, 'type', 'user')}: {m.content}" for m in messages)
    return import_module("langchain_core.messages").get_buffer_string(messages)


def count_tokens_approximately(messages):
    if native_report.get() is not None:
        # Conservative UTF-8 bound; cannot under-budget CJK with chars/4.
        return sum(len(str(m.content).encode("utf-8")) + 16 for m in messages)
    return import_module("langchain_core.messages.utils").count_tokens_approximately(
        messages
    )


def resolve_model_context_window(model_name, *, overrides=None, unknown_default=32768):
    from open_deep_research.models.limits import get_model_token_limit

    if overrides and model_name in overrides:
        return max(1, int(overrides[model_name]))
    return max(1, int(get_model_token_limit(model_name) or unknown_default))


def get_trace_recorder(config):
    port = native_report.get()
    if port is not None:
        return port
    return import_module("open_deep_research.observability").get_trace_recorder(config)


def get_today_str():
    port = native_report.get()
    if port is not None:
        from datetime import date
        value = date.fromisoformat(port.snapshot.report_date)
        return f"{value:%a} {value:%b} {value.day}, {value:%Y}"
    return import_module("open_deep_research.tools.legacy_shims").get_today_str()


def _lazy(module, name):
    def call(*args, **kwargs):
        return getattr(import_module(module), name)(*args, **kwargs)

    return call


invoke_with_output_recovery = _lazy(
    "open_deep_research.agents.model_recovery", "invoke_with_output_recovery"
)
resolve_model_max_output_tokens = _lazy(
    "open_deep_research.agents.model_recovery", "resolve_model_max_output_tokens"
)
invoke_with_model_fallback = _lazy(
    "open_deep_research.models.fallback", "invoke_with_model_fallback"
)
complete_model = _lazy("open_deep_research.models.invocation", "complete_model")
complete_model_stream = _lazy(
    "open_deep_research.models.invocation", "complete_model_stream"
)
build_model_config = _lazy("open_deep_research.models.resolution", "build_model_config")
get_configurable_model_template = _lazy(
    "open_deep_research.models.resolution", "get_configurable_model_template"
)
apply_helicone_config = _lazy(
    "open_deep_research.observability", "apply_helicone_config"
)
invoke_model_with_retry_observability = _lazy(
    "open_deep_research.observability", "invoke_model_with_retry_observability"
)
response_was_truncated = _lazy(
    "open_deep_research.agents.research_context", "response_was_truncated"
)
_evaluate_json = _lazy("open_deep_research.quality.gate", "_evaluate_json")


class LazyWriterTemplate:
    def with_config(self, *args, **kwargs):
        return get_configurable_model_template().with_config(*args, **kwargs)

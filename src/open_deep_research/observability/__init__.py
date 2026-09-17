"""Local observability helpers for research runs."""

__all__ = [
    "SQLiteTraceStore",
    "SpanContext",
    "TokenUsage",
    "TraceRecorder",
    "apply_helicone_config",
    "bind_span_context",
    "current_span_ids",
    "get_trace_recorder",
    "invoke_model_with_observability",
    "invoke_model_with_retry_observability",
    "observe_model_circuit_transition",
    "observe_tool_call",
]


def __getattr__(name):
    """Load legacy callback helpers only when requested, not for metrics imports."""
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    shared = ['NoopSpanContext', 'SQLiteTraceStore', 'SpanContext', 'TokenUsage', 'TraceRecorder', 'bind_span_context', 'current_span_ids', 'get_trace_recorder']
    module = import_module("open_deep_research.observability." + ("tracing" if name in shared else "core"))
    value = getattr(module, name)
    globals()[name] = value
    return value

"""Local observability helpers for research runs."""

__all__ = [
    "SQLiteTraceStore",
    "SpanContext",
    "TokenUsage",
    "TraceRecorder",
    "bind_span_context",
    "current_span_ids",
    "get_trace_recorder",
]


def __getattr__(name):
    """Load framework-independent tracing and metrics without model callbacks."""
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    module = import_module("open_deep_research.observability.tracing")
    value = getattr(module, name)
    globals()[name] = value
    return value

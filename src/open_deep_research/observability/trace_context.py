"""Framework-independent W3C model request correlation."""

import hashlib
import os

from open_deep_research.observability.tracing import current_span_ids


def current_traceparent(run_id: str) -> str:
    """Create a W3C trace context from the current content-free span identity."""
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        propagated = carrier.get("traceparent")
        if propagated:
            return propagated
    except Exception:  # noqa: BLE001,S110 - deterministic fallback remains available
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

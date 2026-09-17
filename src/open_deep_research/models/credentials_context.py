"""Framework-independent run credential context shared by model and embedding adapters."""

import contextvars
import os

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


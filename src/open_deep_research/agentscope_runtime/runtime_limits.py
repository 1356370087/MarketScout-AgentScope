"""Scoped deadlines and non-content model attribution for native execution."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

call_context = ContextVar("native_call_context", default=MappingProxyType({}))
structured_attempt_budget = ContextVar("native_structured_attempt_budget", default=None)


@contextmanager
def attributed(**values):
    """Propagate stable purpose and parent call identity, not prompt content."""
    token = call_context.set({**call_context.get(), **values})
    try:
        yield
    finally:
        call_context.reset(token)


async def limited(callback, seconds, *, deadline_at=None, receipt_grace=0):
    """Bound an operation by its own limit and the caller's remaining time."""
    candidates = [time.time() + seconds]
    candidates.extend(
        x for x in (deadline_at, call_context.get().get("deadline_at")) if x is not None
    )
    deadline = min(candidates)
    with attributed(deadline_at=deadline):
        async with asyncio.timeout(max(0, deadline - time.time()) + receipt_grace):
            return await callback()


def consume_structured_attempt():
    """Format and semantic repairs share one physical dispatch allowance."""
    budget = structured_attempt_budget.get()
    if budget is not None:
        if budget[0] <= 0:
            from open_deep_research.agentscope_runtime.recovery import (
                ModelOutputProtocolError,
            )

            raise ModelOutputProtocolError("structured_attempt_budget_exhausted")
        budget[0] -= 1

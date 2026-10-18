"""Scoped deadlines and non-content model attribution for native execution."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

call_context = ContextVar("native_call_context", default=MappingProxyType({}))
structured_attempt_budget = ContextVar("native_structured_attempt_budget", default=None)
_tool_clocks = ContextVar("native_tool_execution_clocks", default=())


@dataclass
class _ToolClock:
    timer: asyncio.Timeout
    execution_deadline: float
    hard_deadline: float | None
    waiters: int = 0
    wait_started: float = 0.0
    waited_seconds: float = 0.0

    def deadline(self):
        if self.waiters:
            return self.hard_deadline
        return min(self.execution_deadline, self.hard_deadline) if self.hard_deadline is not None else self.execution_deadline

    def reschedule(self):
        if not self.timer.expired():
            deadline = self.deadline()
            self.timer.reschedule(None if deadline is None else asyncio.get_running_loop().time() + max(0, deadline - time.time()))


def current_deadline_at():
    """Include active execution clocks without charging human waits as work."""
    deadlines = [call_context.get().get("deadline_at"), *(clock.deadline() for clock in _tool_clocks.get())]
    return min(value for value in deadlines if value is not None) if any(value is not None for value in deadlines) else None


@contextmanager
def waiting_for_approval():
    """Suspend tool execution timers, keeping task/run deadlines in force.

    Parallel requests share their operation's clock, so overlapping approval
    waits extend the deadline once rather than once per query.
    """
    clocks = _tool_clocks.get()
    for clock in clocks:
        if not clock.waiters:
            clock.wait_started = time.monotonic()
        clock.waiters += 1
        clock.reschedule()
    try:
        yield clocks[-1] if clocks else None
    finally:
        for clock in reversed(clocks):
            clock.waiters -= 1
            if not clock.waiters:
                waited = time.monotonic() - clock.wait_started
                clock.execution_deadline += waited
                clock.waited_seconds += waited
            clock.reschedule()


async def limited_tool(callback, seconds, *, deadline_at=None):
    """Limit active tool time independently of bounded human authorization."""
    hard_limits = [value for value in (deadline_at, call_context.get().get("deadline_at")) if value is not None]
    hard_deadline = min(hard_limits) if hard_limits else None
    clock = _ToolClock(asyncio.timeout(None), time.time() + seconds, hard_deadline)
    token = _tool_clocks.set((*_tool_clocks.get(), clock))
    try:
        with attributed(deadline_at=hard_deadline):
            async with clock.timer:
                clock.reschedule()
                return await callback()
    finally:
        _tool_clocks.reset(token)


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
        x for x in (deadline_at, current_deadline_at()) if x is not None
    )
    deadline = min(candidates)
    remaining = deadline - time.time()
    if remaining <= 0:
        # asyncio.timeout(0) cancels at the next suspension, after callback code
        # can already dispatch an external effect. Receipt grace only lets an
        # operation started in time finish recording its response.
        raise TimeoutError("operation deadline elapsed before dispatch")
    with attributed(deadline_at=deadline):
        async with asyncio.timeout(remaining + receipt_grace):
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

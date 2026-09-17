"""Durable SSE transport; run, publication and task cursors remain independent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import portalocker

from open_deep_research.events.public import (
    PublicEvent,
    RunEventStore,
    is_terminal_event,
)
from open_deep_research.events.publications import (
    PublicationEvent,
    PublicationEventStore,
)
from open_deep_research.events.task_activity import (
    TASK_TERMINAL_TYPES,
    PublicTaskActivityEvent,
    TaskActivityStore,
)


@dataclass
class StreamOptions:
    """Application-owned lifecycle, timing and live authorization dependencies."""

    configuration: Any
    shutdown: asyncio.Event
    authorize: Callable[[Any], Awaitable[bool]]
    reauth_interval: float
    publisher_settings: Any = None


def _sse(event: PublicEvent) -> str:
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.public_dict(), ensure_ascii=False, default=str)}\n\n"
    )


def _publication_sse(event: PublicationEvent) -> str:
    """Serialize one publication event on its independent cursor domain."""
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.public_dict(), ensure_ascii=False, default=str)}\n\n"
    )


def _task_activity_sse(event: PublicTaskActivityEvent) -> str:
    """Serialize one browser-safe task activity as SSE."""
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(event.public_dict(), ensure_ascii=False, default=str)}\n\n"
    )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


async def _public_event_iterator(
    store: RunEventStore,
    *,
    after: int = 0,
    principal: Any = None,
    options: StreamOptions,
):
    """Replay then tail a durable public stream, including cross-worker writes."""
    configurable = options.configuration
    poll_seconds = configurable.sse_poll_interval_ms / 1000
    heartbeat_seconds = configurable.sse_heartbeat_seconds
    cursor = after
    last_output_at = asyncio.get_running_loop().time()
    last_auth_at = last_output_at
    while True:
        if options.shutdown.is_set():
            return
        now = asyncio.get_running_loop().time()
        if (
            principal is not None
            and principal.session_id is not None
            and now - last_auth_at >= options.reauth_interval
        ):
            if not await options.authorize(principal):
                return
            last_auth_at = now
        events = await asyncio.to_thread(store.read, cursor)
        for event in events:
            yield _sse(event)
            cursor = event.sequence
            last_output_at = asyncio.get_running_loop().time()
            if is_terminal_event(event):
                return
        if not events:
            last_sequence = await asyncio.to_thread(store.last_sequence)
            if last_sequence and cursor >= last_sequence:
                latest = await asyncio.to_thread(store.read, last_sequence - 1)
                if latest and is_terminal_event(latest[-1]):
                    return
            now = asyncio.get_running_loop().time()
            if now - last_output_at >= heartbeat_seconds:
                yield ": keep-alive\n\n"
                last_output_at = now
        await asyncio.sleep(poll_seconds)


async def _publication_event_iterator(
    store: PublicationEventStore,
    *,
    after: int = 0,
    principal: Any = None,
    options: StreamOptions,
):
    """Replay and briefly tail post-run publication events."""
    configurable = options.configuration
    publisher_settings = options.publisher_settings
    poll_seconds = configurable.sse_poll_interval_ms / 1000
    heartbeat_seconds = configurable.sse_heartbeat_seconds
    cursor = after
    loop = asyncio.get_running_loop()
    last_output_at = loop.time()
    last_event_at = last_output_at
    last_auth_at = last_output_at
    while True:
        if options.shutdown.is_set():
            return
        now = loop.time()
        if (
            principal is not None
            and principal.session_id is not None
            and now - last_auth_at >= options.reauth_interval
        ):
            if not await options.authorize(principal):
                return
            last_auth_at = now
        try:
            events = await asyncio.to_thread(store.read, cursor)
        except OSError, ValueError, portalocker.exceptions.LockException:
            # A corrupt or temporarily locked publication log is fail-closed;
            # the client can reconnect after the storage issue is repaired.
            return
        for event in events:
            yield _publication_sse(event)
            cursor = event.sequence
            last_output_at = loop.time()
            last_event_at = last_output_at
        now = loop.time()
        idle_due = now - last_event_at >= publisher_settings.sse_idle_seconds
        # Emit a due heartbeat before applying the idle close boundary.  The
        # generator pauses at ``yield`` so a client can observe one final
        # keep-alive even when a very small idle window and filesystem polling
        # overhead elapse in the same iteration.
        if not events and now - last_output_at >= heartbeat_seconds:
            yield ": keep-alive\n\n"
            last_output_at = loop.time()
            if idle_due:
                return
        if idle_due:
            return
        await asyncio.sleep(poll_seconds)


async def _task_activity_iterator(
    store: TaskActivityStore,
    *,
    after: int = 0,
    principal: Any = None,
    options: StreamOptions,
):
    """Replay then tail one task-local durable activity stream."""
    configurable = options.configuration
    poll_seconds = configurable.sse_poll_interval_ms / 1000
    heartbeat_seconds = configurable.sse_heartbeat_seconds
    cursor = after
    last_output_at = asyncio.get_running_loop().time()
    last_auth_at = last_output_at
    while True:
        if options.shutdown.is_set():
            return
        now = asyncio.get_running_loop().time()
        if (
            principal is not None
            and principal.session_id is not None
            and now - last_auth_at >= options.reauth_interval
        ):
            if not await options.authorize(principal):
                return
            last_auth_at = now
        events = await asyncio.to_thread(store.read, cursor)
        terminal_seen = False
        for event in events:
            yield _task_activity_sse(event)
            cursor = event.sequence
            last_output_at = asyncio.get_running_loop().time()
            if event.type in TASK_TERMINAL_TYPES:
                terminal_seen = True
        if terminal_seen:
            return
        if not events:
            last_sequence = await asyncio.to_thread(store.last_sequence)
            if last_sequence and cursor >= last_sequence:
                history = await asyncio.to_thread(store.read)
                if any(event.type in TASK_TERMINAL_TYPES for event in history):
                    return
            now = asyncio.get_running_loop().time()
            if now - last_output_at >= heartbeat_seconds:
                yield ": keep-alive\n\n"
                last_output_at = now
        await asyncio.sleep(poll_seconds)

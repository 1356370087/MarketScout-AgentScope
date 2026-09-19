"""Three durable browser streams: replay, heartbeats and live revocation."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from open_deep_research.api.streams import (
    StreamOptions,
    _publication_event_iterator,
    _public_event_iterator,
    _task_activity_iterator,
)
from open_deep_research.events.public import RunEventStore
from open_deep_research.events.publications import PublicationEventStore
from open_deep_research.events.task_activity import TaskActivityStore

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["run", "task", "publication"])
def domain(request, tmp_path):
    name = request.param
    if name == "run":
        store = RunEventStore("run-1", runs_dir=str(tmp_path))
        iterator = _public_event_iterator
    elif name == "task":
        store = TaskActivityStore("run-1", "task-1", runs_dir=str(tmp_path))
        iterator = _task_activity_iterator
    else:
        store = PublicationEventStore("run-1", runs_dir=tmp_path)
        iterator = _publication_event_iterator

    async def append(terminal=False):
        kind = name + (".completed" if terminal else ".started")
        kwargs = {"dedupe_key": kind, "payload": {"status": "completed" if terminal else "running"}}
        if name == "task":
            kwargs.update(kind="lifecycle", phase="terminal" if terminal else "initializing",
                          status="success" if terminal else "running", title="Task", summary="Public",
                          iteration=None, duration_ms=None)
        if name == "publication":
            return await asyncio.to_thread(store.append, kind, publication_id="pub-1", **kwargs)
        return await store.append(kind, **kwargs)

    return name, store, iterator, append


def stream_options(authorize):
    return StreamOptions(
        configuration=SimpleNamespace(sse_poll_interval_ms=1, sse_heartbeat_seconds=0),
        shutdown=asyncio.Event(), authorize=authorize, reauth_interval=0,
        publisher_settings=SimpleNamespace(sse_idle_seconds=30),
    )


async def allowed(principal):
    return True


async def test_heartbeat_then_cross_writer_event_then_shutdown(domain):
    _, store, iterator, append = domain
    options = stream_options(allowed)
    stream = iterator(store, options=options)
    try:
        assert await asyncio.wait_for(anext(stream), 2) == ": keep-alive\n\n"
        event = await append()
        frame = await asyncio.wait_for(anext(stream), 2)
        assert f"id: {event.sequence}\n" in frame
        assert json.loads(frame.split("data: ", 1)[1]) == event.public_dict()
        options.shutdown.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 2)
    finally:
        await stream.aclose()


async def test_reconnect_replays_only_after_domain_cursor(domain):
    name, store, iterator, append = domain
    first = await append()
    duplicate = await append()
    last = await append(terminal=True)
    assert first.sequence == duplicate.sequence == 1
    assert last.sequence == 2
    options = stream_options(allowed)
    options.publisher_settings.sse_idle_seconds = 0
    frames = [frame async for frame in iterator(store, after=1, options=options)]
    assert len(frames) == 1
    payload = json.loads(frames[0].split("data: ", 1)[1])
    assert payload["sequence"] == 2
    assert payload["type"] == f"{name}.completed"
    assert "dedupe_key" not in payload
    # A fully consumed terminal cursor must not replay another event.
    tail = [frame async for frame in iterator(store, after=2, options=options)]
    assert all(frame.startswith(":") for frame in tail)


async def test_session_revoked_between_batches_stops_before_new_event(domain):
    _, store, iterator, append = domain
    valid = True
    checks = []

    async def authorize(principal):
        checks.append(principal.session_id)
        return valid

    await append()
    stream = iterator(store, principal=SimpleNamespace(session_id="session-1"),
                      options=stream_options(authorize))
    try:
        assert "id: 1\n" in await asyncio.wait_for(anext(stream), 2)
        valid = False
        await append(terminal=True)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), 2)
        assert checks == ["session-1", "session-1"]
    finally:
        await stream.aclose()


async def test_revoked_session_cannot_replay_persisted_events(domain):
    _, store, iterator, append = domain
    await append(terminal=True)

    async def denied(principal):
        return False

    frames = [frame async for frame in iterator(
        store, principal=SimpleNamespace(session_id="revoked"), options=stream_options(denied),
    )]
    assert frames == []

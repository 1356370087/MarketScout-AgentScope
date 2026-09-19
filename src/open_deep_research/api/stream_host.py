"""Bind domain event streams to application shutdown and IAM reauthorization."""

from __future__ import annotations

import asyncio

from open_deep_research.api import streams
from open_deep_research.api.streams import StreamOptions
from open_deep_research.configuration import Configuration
from open_deep_research.report.publication_store import get_publisher_settings
from security.rbac.database import session_scope
from security.rbac.dependencies import reauthorize_session
from security.rbac.settings import get_settings as get_iam_settings


async def _reauthorize_stream(principal):
    async with session_scope() as db:
        return await reauthorize_session(db, principal) is not None


class ApplicationStreams:
    """Attach process lifecycle and current policy to shared stream readers."""

    def __init__(self, shutdown: asyncio.Event) -> None:
        self.shutdown = shutdown

    def _stream_options(self):
        return StreamOptions(
            configuration=Configuration.from_runnable_config(None),
            shutdown=self.shutdown,
            authorize=_reauthorize_stream,
            reauth_interval=get_iam_settings().sse_reauth_interval,
            publisher_settings=get_publisher_settings(),
        )

    async def _public_event_iterator(self, store, *, after=0, principal=None):
        async for frame in streams._public_event_iterator(
            store, after=after, principal=principal, options=self._stream_options()
        ):
            yield frame

    async def _publication_event_iterator(self, store, *, after=0, principal=None):
        async for frame in streams._publication_event_iterator(
            store, after=after, principal=principal, options=self._stream_options()
        ):
            yield frame

    async def _task_activity_iterator(self, store, *, after=0, principal=None):
        async for frame in streams._task_activity_iterator(
            store, after=after, principal=principal, options=self._stream_options()
        ):
            yield frame

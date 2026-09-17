"""Crash-window subprocess for the real publication Worker."""

import asyncio
import sys
import time
from pathlib import Path

from open_deep_research.report import publisher_worker
from open_deep_research.report.publication_store import (
    PublicationJobStore,
    PublisherSettings,
)

root, window = Path(sys.argv[1]), sys.argv[2]


def stop_here():
    (root / "ready").write_text(window)
    time.sleep(120)


original_render = publisher_worker.render_publication
original_commit = PublicationJobStore.commit_file
original_event = publisher_worker.PublisherWorker._publish_event


def render(*args, **kwargs):
    if window == "before_render":
        stop_here()
    return original_render(*args, **kwargs)


def commit(*args, **kwargs):
    result = original_commit(*args, **kwargs)
    if window == "file_committed":
        stop_here()
    return result


async def event(self, job, event_type, *args, **kwargs):
    if event_type == "publication.completed" and window == "job_completed":
        stop_here()
    return await original_event(self, job, event_type, *args, **kwargs)


publisher_worker.render_publication = render
PublicationJobStore.commit_file = commit
publisher_worker.PublisherWorker._publish_event = event
asyncio.run(
    publisher_worker.PublisherWorker(
        PublisherSettings(runs_dir=root, lease_seconds=1)
    ).run_once()
)

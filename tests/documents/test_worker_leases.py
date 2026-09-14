"""Document worker heartbeat and lease-renewal tests."""

from __future__ import annotations

import asyncio

import pytest

from open_deep_research.documents import worker


@pytest.mark.asyncio
async def test_job_lease_renews_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewed = asyncio.Event()

    async def renew(job_id: str, worker_id: str, lease_seconds: int) -> bool:
        assert (job_id, worker_id, lease_seconds) == ("job-1", "worker-1", 300)
        renewed.set()
        return True

    monkeypatch.setattr(worker, "renew_job_lease", renew)
    lease_lost = asyncio.Event()
    task = asyncio.create_task(
        worker._maintain_job_lease(  # noqa: SLF001
            "job-1",
            "worker-1",
            interval_seconds=0.01,
            lease_seconds=300,
            lease_lost=lease_lost,
        )
    )
    await asyncio.wait_for(renewed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lease_lost.is_set()


@pytest.mark.asyncio
async def test_job_lease_loss_stops_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_renewal(
        job_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        return False

    monkeypatch.setattr(worker, "renew_job_lease", reject_renewal)
    lease_lost = asyncio.Event()
    await asyncio.wait_for(
        worker._maintain_job_lease(  # noqa: SLF001
            "job-2",
            "worker-2",
            interval_seconds=0.01,
            lease_seconds=300,
            lease_lost=lease_lost,
        ),
        timeout=1,
    )
    assert lease_lost.is_set()

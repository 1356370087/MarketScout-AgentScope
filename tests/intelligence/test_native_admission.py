"""Automatic recovery, approvals and creates share the same native slots."""

import asyncio

import pytest
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore, RecoveryConflict
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.native_runs import NativeRuns


@pytest.mark.asyncio
async def test_all_automation_start_paths_respect_global_and_plan_capacity(tmp_path, monkeypatch):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "slots.db").as_posix())
    await store.create_tables()
    service = NativeRuns(store, None, None, runs_dir=tmp_path)
    release = asyncio.Event()
    async def execute(recovery):
        try:
            await release.wait()
        finally:
            await recovery.close()
    monkeypatch.setattr(service, "_execute", execute)
    try:
        config = RunConfig.compile({"configurable": {"max_run_cost_micro_usd": 1_000_000}})
        for index in range(6):
            await store.create_from_config("owner", f"auto-{index}", config, application={"configuration": config.snapshot(),
                "identity": {"kind": "intelligence", "plan_id": f"plan-{index if index<5 else 0}"}})
        assert await service.start("auto-0", "owner")
        assert not await service.start("auto-5", "owner")  # Same plan, other slots free.
        results = await asyncio.gather(*(service.start(f"auto-{index}", "owner") for index in range(1, 5)))
        assert sum(results) == 3 and len(service.tasks) == 4
        with pytest.raises(RecoveryConflict, match="via_work_item"):
            await service.resume("auto-4", "owner")
        release.set()
        await asyncio.gather(*list(service.tasks.values()))
        assert await service.start("auto-4", "owner")
    finally:
        release.set()
        await service.aclose()
        await store.aclose()

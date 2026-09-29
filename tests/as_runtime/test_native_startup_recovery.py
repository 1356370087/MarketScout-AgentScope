"""Native startup discovers SQL work without waking archived or unanswered runs."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from agentscope.message import UserMsg
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import PendingDecision, ResearchPipeline
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.api.native_runs import NativeRuns

pytestmark = pytest.mark.asyncio


async def test_startup_recovers_shutdown_and_durable_decision_without_repeating_completed_stages(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    calls = []
    ready = asyncio.Event()
    interrupted = True

    class Stages:
        async def execute(self, stage, state):
            calls.append((state.run_id, stage))
            if stage == "write_research_brief" and interrupted:
                ready.set()
                await asyncio.Event().wait()
            if stage == "plan_approval":
                return PendingDecision(stage=stage, question="Confirm")
            if stage == "final_report_generation":
                state.final_report = "Recovered report"

    @asynccontextmanager
    async def factory(state, config, recovery):
        yield ResearchPipeline(state, Stages(), recovery.save,
                               config_fingerprint=state.config_fingerprint, recovery=recovery)

    async def prepare(request, principal):
        return {"configurable": request.configurable}

    config = RunConfig.compile({"configurable": {}})
    service = NativeRuns(store, factory, prepare, runs_dir=tmp_path)
    restarted = NativeRuns(store, factory, prepare, runs_dir=tmp_path)
    try:
        await store.create_from_config("alice", "run", config, messages=[UserMsg("user", "Research")],
                                       application={"configuration": config.snapshot()})
        await service.start("run", "alice")
        await asyncio.wait_for(ready.wait(), 2)
        await service.aclose()
        interrupted = False
        assert await restarted.recover_interrupted() == 1
        await asyncio.gather(*list(restarted.tasks.values()))
        state, _ = await store.load("run", "alice")
        assert state.status == "waiting"
        assert calls.count(("run", "summarize_messages")) == 1
        assert await restarted.recover_interrupted() == 0
        await store.submit_decision("run", "alice", "approved", state.pending.id,
                                    {"action": "approve", "feedback": ""})
        assert await restarted.recover_interrupted() == 1
        await asyncio.gather(*list(restarted.tasks.values()))
        state, _ = await store.load("run", "alice")
        assert state.status == "completed"
        assert state.final_report == "Recovered report"
        assert await restarted.recover_interrupted() == 0
    finally:
        await service.aclose()
        await restarted.aclose()
        await store.aclose()


async def test_startup_does_not_steal_lease_or_retry_terminal_failure(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    service = NativeRuns(store, None, None)
    config = RunConfig.compile({"configurable": {}})
    try:
        for name in ("live", "failed", "cancelled", "completed"):
            state = await store.create_from_config("alice", name, config,
                                                   application={"configuration": config.snapshot()})
            lease = await store.acquire(name, "alice")
            if name == "live":
                continue
            state.status = name
            await store.save(lease, state)
            await store.release(lease)
        assert await service.recover_interrupted() == 0
        assert not service.tasks
    finally:
        await service.aclose()
        await store.aclose()


async def test_startup_keeps_unknown_effect_quarantined(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {}})
    provider_calls = []

    @asynccontextmanager
    async def factory(state, config, recovery):
        class Stages:
            async def execute(self, stage, current):
                if stage == "summarize_messages":
                    async def provider():
                        provider_calls.append(True)
                    await recovery.operation("model:summary", {}, provider, key="unknown", replay_safe=False)
        yield ResearchPipeline(state, Stages(), recovery.save,
                               config_fingerprint=state.config_fingerprint, recovery=recovery)

    service = NativeRuns(store, factory, None)
    try:
        await store.create_from_config("alice", "run", config, messages=[UserMsg("user", "Research")],
                                       application={"configuration": config.snapshot()})
        lease = await store.acquire("run", "alice")
        await store.begin_operation(lease, "unknown", "model:summary", {}, replay_safe=False)
        await store.release(lease)
        assert await service.recover_interrupted() == 1
        await asyncio.gather(*list(service.tasks.values()))
        state, _ = await store.load("run", "alice")
        assert state.status == "failed"
        assert state.error == "UnknownOperation"
        assert provider_calls == []
        assert await service.recover_interrupted() == 0
    finally:
        await service.aclose()
        await store.aclose()

"""Native startup discovers SQL work without waking archived or unanswered runs."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from agentscope.message import UserMsg
from sqlalchemy import func, select, update

from open_deep_research.agentscope_runtime.recovery_store import (
    RecoveryConflict,
    RecoveryStore,
)
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
)
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


async def test_explicit_resume_restarts_failure_before_pipeline_enter(tmp_path):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {}})
    stages = []
    fail_startup = True
    entries = []

    class Stages:
        async def execute(self, stage, state):
            stages.append(stage)
            if stage == "final_report_generation":
                state.final_report = "Recovered fixture report"

    @asynccontextmanager
    async def factory(state, config, recovery):
        entries.append(state.status)
        if fail_startup:
            raise RuntimeError("fixture missing MQ topic")
        yield ResearchPipeline(state, Stages(), recovery.save,
                               config_fingerprint=state.config_fingerprint, recovery=recovery)

    service = NativeRuns(store, factory, None)
    try:
        await store.create_from_config("owner", "run", config, messages=[UserMsg("user", "Research")],
                                       application={"configuration": config.snapshot()})
        await service.start("run", "owner")
        await asyncio.gather(*list(service.tasks.values()))
        state, _ = await store.load("run", "owner")
        assert state.status == "failed" and state.inflight is None
        assert state.error == "RuntimeError"
        assert stages == []
        async with store.engine.connect() as db:
            assert await db.scalar(select(func.count()).select_from(store.ops)) == 0
        assert await service.recover_interrupted() == 0
        fail_startup = False
        await service.resume("run", "owner")
        await asyncio.gather(*list(service.tasks.values()))
        state, _ = await store.load("run", "owner")
        assert entries == ["ready", "ready"], "Explicit resume entered factory with terminal failed snapshot"
        assert stages, "Successful resume returned but did not execute any research stage"
        assert state.status == "completed"
        assert state.final_report == "Recovered fixture report"
    finally:
        await service.aclose()
        await store.aclose()


@pytest.mark.parametrize("problem", ["UnknownOperation", "BudgetExhausted", "DeadlineExceeded", "ResearchTerminated"])
async def test_explicit_resume_preserves_terminal_boundaries(tmp_path, problem):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {}})
    calls = []

    @asynccontextmanager
    async def factory(state, config, recovery):
        calls.append(state.status)
        yield None

    service = NativeRuns(store, factory, None)
    try:
        state = await store.create_from_config("owner", "run", config, messages=[UserMsg("user", "Research")],
                                             application={"configuration": config.snapshot()})
        lease = await store.acquire("run", "owner")
        if problem == "UnknownOperation":
            await store.begin_operation(lease, "external", "tool", {}, replay_safe=False)
        async with store.transaction(lease) as (db, _row):
            if problem == "BudgetExhausted":
                await db.execute(update(store.runs).where(store.runs.c.run_id == "run").values(limits={"model_calls": 0}))
            elif problem == "DeadlineExceeded":
                await db.execute(update(store.runs).where(store.runs.c.run_id == "run").values(deadline=time.time() - 60))
        state.status, state.error, state.inflight = "failed", problem, None
        if problem == "ResearchTerminated":
            state.error = "research_quality_exhausted"
            state.completion_outcome = {"action": "terminate", "reason": state.error, "gaps": []}
        await store.save(lease, state)
        await store.release(lease)
        budget_before = await store.budget("run", "owner")
        async with store.engine.connect() as db:
            operations_before = (await db.execute(select(store.ops))).mappings().all()
        with pytest.raises(RecoveryConflict, match="run_not_recoverable"):
            await service.resume("run", "owner")
        assert not service.tasks
        persisted, _ = await store.load("run", "owner")
        assert calls == []
        assert persisted.status == "failed" and persisted.error == state.error
        assert persisted.completion_outcome == state.completion_outcome
        assert await store.budget("run", "owner") == budget_before
        async with store.engine.connect() as db:
            assert (await db.execute(select(store.ops))).mappings().all() == operations_before
    finally:
        await service.aclose()
        await store.aclose()


@pytest.mark.parametrize("boundary", ["stage", "inflight", "model", "tool", "budget", "deadline"])
async def test_failed_resume_requires_unused_live_initialization(tmp_path, boundary):
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "runs.db").as_posix())
    await store.create_tables()
    config = RunConfig.compile({"configurable": {}})
    service = NativeRuns(store, None, None)
    try:
        state = await store.create_from_config("owner", "run", config, messages=[UserMsg("user", "Research")],
                                             application={"configuration": config.snapshot()})
        lease = await store.acquire("run", "owner")
        state.status, state.error = "failed", "RuntimeError"
        if boundary == "stage":
            state.completed = ["summarize_messages"]
        elif boundary == "inflight":
            state.inflight = "summarize_messages"
        elif boundary in {"model", "tool"}:
            await store.begin_operation(lease, "external", boundary, {}, replay_safe=False)
            await store.commit_operation(lease, "external", {"committed": True})
        elif boundary in {"budget", "deadline"}:
            async with store.transaction(lease) as (db, _row):
                values = {"limits": {"model_calls": 0}} if boundary == "budget" else {"deadline": time.time() - 60}
                await db.execute(update(store.runs).where(store.runs.c.run_id == "run").values(**values))
        await store.save(lease, state)
        await store.release(lease)
        with pytest.raises(RecoveryConflict, match="run_not_recoverable"):
            await service.resume("run", "owner")
        assert not service.tasks
        persisted, _ = await store.load("run", "owner")
        assert persisted.status == "failed" and persisted.error == "RuntimeError"
        assert persisted.completed == state.completed
        assert persisted.inflight == state.inflight
    finally:
        await service.aclose()
        await store.aclose()

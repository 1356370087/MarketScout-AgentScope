"""T043/T046 acceptance at PostgreSQL commit and wakeup boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_recovery as recovery_cases

from open_deep_research.agentscope_runtime.app import ASRuntime
from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict, RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import PendingDecision

pytestmark = pytest.mark.asyncio


async def test_postgres_outbox_cursor_and_public_delivery_crash_replay(pg_url, tmp_path):
    store = RecoveryStore(pg_url)
    try:
        await store.create_tables()
        await recovery_cases.test_outbox_projection_and_cursor_rollback_together(store)
        await recovery_cases.test_public_log_delivery_deduplicates_after_publish_before_cursor_crash(store, tmp_path)
    finally:
        await store.aclose()


async def test_postgres_decision_write_failure_and_old_decision_cannot_touch_new_approval(pg_url, monkeypatch):
    store = RecoveryStore(pg_url)
    try:
        await store.create_tables()
        state, lease = await recovery_cases.create(store)
        state.pending = PendingDecision(stage="plan_approval", question="first")
        state.status = "waiting"
        await store.save(lease, state)
        runtime = SimpleNamespace(message_bus=SimpleNamespace(queue_push=AsyncMock()))
        original = store._event

        async def fail(conn, row, kind, *args, **kwargs):
            if kind == "research.decision_queued":
                raise RuntimeError("decision inserted but transaction failed")
            return await original(conn, row, kind, *args, **kwargs)

        old_id = state.pending.id
        kwargs = dict(run_id=state.run_id, user_id="owner", command_id="approve-first",
                      action_id=old_id, payload={"action": "approve"})
        monkeypatch.setattr(store, "_event", fail)
        with pytest.raises(RuntimeError, match="transaction failed"):
            await ASRuntime.submit_research_decision(runtime, store, **kwargs)
        runtime.message_bus.queue_push.assert_not_called()
        assert await store.pending_decisions(lease) == []
        assert not any(e["payload"]["type"] == "research.decision_queued"
                       for e in await store.events(state.run_id, "owner"))

        monkeypatch.setattr(store, "_event", original)
        await ASRuntime.submit_research_decision(runtime, store, **kwargs)
        runtime.message_bus.queue_push.assert_awaited_once()
        state.pending = PendingDecision(stage="outline_approval", question="second")
        new_id = state.pending.id
        await store.save(lease, state, command_id="approve-first")
        await store.release(lease)
        lease = await store.acquire(state.run_id, "owner")
        assert await store.submit_decision(state.run_id, "owner", "approve-first", old_id,
                                           {"action": "approve"}) == "applied"
        assert (await store.load(state.run_id, "owner"))[0].pending.id == new_id
        assert await store.pending_decisions(lease) == []
        with pytest.raises(RecoveryConflict):
            await store.submit_decision(state.run_id, "owner", "conflicting-old", old_id, {"action": "cancel"})
        with pytest.raises(RecoveryConflict, match="foreign task"):
            await store.submit_decision(state.run_id, "owner", "foreign-feedback", "feedback:foreign",
                                        {"action": "feedback", "task_id": "foreign", "feedback": "ignore"})
        await store.release(lease)
        await recovery_cases.test_task_feedback_enters_next_new_model_call_and_replays_consistently(store)
    finally:
        await store.aclose()

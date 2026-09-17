"""T023 real SQL receipts around native policy attempts, not fabricated totals."""

# ruff: noqa: F811 -- imported pytest fixture is injected by name

import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from sqlalchemy import select
from test_recovery import (
    create,
    store,  # noqa: F401 -- pytest fixture
)

from open_deep_research.agentscope_runtime.gateway import GatewayCallError
from open_deep_research.agentscope_runtime.model_policy import ModelCallPolicy
from open_deep_research.agentscope_runtime.recovery import (
    ApprovalPending,
    RecoverySession,
)

pytestmark = pytest.mark.asyncio


def reply(tokens=2, cached=0, last=True):
    return ChatResponse(
        content=[TextBlock(text="ok")], is_last=last,
        usage=ChatUsage(input_tokens=tokens, output_tokens=1, cache_input_tokens=cached, time=0.1),
    )


async def run(session, call, **kwargs):
    with session.scope("research", 0):
        return await session.model("researcher", [UserMsg("user", "q")], call,
                                   max_tokens=10, account_attempts=True, **kwargs)


async def receipts(store, lease):
    async with store.engine.connect() as conn:
        return (await conn.execute(select(store.ops).where(
            store.ops.c.run_id == lease.run_id, store.ops.c.kind == "model_attempt"
        ).order_by(store.ops.c.key))).mappings().all()


async def test_retry_reserved_before_call_and_unknown_usage_is_not_zero(store):
    state, lease = await create(store, limits={"model_calls": 1})
    session = RecoverySession(store, lease, state)
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        raise GatewayCallError("busy", status_code=503)

    async def call():
        return await ModelCallPolicy([object()], attempts=3).invoke(handler, {"messages": []}, {})

    with pytest.raises(ApprovalPending):
        await run(session, call)
    assert calls == [1]
    rows = await receipts(store, lease)
    assert len(rows) == 1
    assert rows[0]["result"]["observed_usage"] is None
    assert rows[0]["actual"]["output_tokens"] == 10
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 1
    await session.close()


async def test_fallback_prices_cache_and_replay_are_per_attempt(store):
    state, lease = await create(store, limits={"model_calls": 3, "cost_micro_usd": 100000})
    session = RecoverySession(store, lease, state)
    a = SimpleNamespace(model="a", accounting_price=(2, 3))
    b = SimpleNamespace(model="b", accounting_price=(5, 7))
    calls = []

    async def handler(current_model, **kwargs):
        calls.append(current_model.model)
        if current_model is a:
            error = GatewayCallError("billed transient", status_code=503)
            error.completion = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2))
            raise error
        return reply(cached=1)

    async def call():
        return await ModelCallPolicy([a, b], attempts=1).invoke(handler, {"messages": []}, {})

    await run(session, call, pricing=(2, 3))
    await session.close()
    session = await RecoverySession.open(store, state.run_id, "owner")
    await run(session, call, pricing=(2, 3))
    assert calls == ["a", "b"]
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"]["model_calls"] == 2
    assert budget["used"]["input_tokens"] == 6
    assert budget["used"]["cost_micro_usd"] == 31  # 4*2+2*3 + 2*5+1*7
    rows = await receipts(store, session.lease)
    assert rows[1]["result"]["cached_input_tokens"] == 1
    assert all(not value for value in budget["reserved"].values())
    await session.close()


async def test_stream_failure_retains_observed_usage_and_never_replays(store):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    calls = []

    async def stream():
        yield reply(tokens=3, last=False)
        raise ConnectionError("stream broke")

    async def handler(**kwargs):
        calls.append(1)
        return stream()

    async def call():
        return await ModelCallPolicy([object(), object()]).invoke(handler, {"messages": []}, {})

    with pytest.raises(ConnectionError):
        await run(session, call)
    assert calls == [1]
    row = (await receipts(store, lease))[0]
    assert row["result"]["observed_usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert row["result"]["usage_status"] == "estimated"
    assert row["actual"]["output_tokens"] == 10
    await session.close()


async def test_continuation_each_reserves_and_settles_once(store):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)

    async def handler(**kwargs):
        return reply()

    async def call():
        policy = ModelCallPolicy([object()])
        await policy.invoke(handler, {"messages": []}, {})
        return await policy.invoke(handler, {"messages": [], "accounting_max_tokens": 20}, {})

    await run(session, call)
    rows = await receipts(store, lease)
    assert [r["reservation"]["output_tokens"] for r in rows] == [10, 20]
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 2
    await session.close()


async def test_concurrent_attempts_cannot_exceed_model_call_limit(store):
    state, lease = await create(store, limits={"model_calls": 1})
    session = RecoverySession(store, lease, state)
    calls = []

    async def handler(**kwargs):
        calls.append(1)
        await asyncio.sleep(0.02)
        return reply()

    async def call():
        return await ModelCallPolicy([object()]).invoke(handler, {"messages": []}, {})

    async def task(name):
        with session.task(name):
            return await session.model("researcher", [], call, account_attempts=True)

    results = await asyncio.gather(task("a"), task("b"), return_exceptions=True)
    assert len(calls) == 1
    assert sum(isinstance(item, ApprovalPending) for item in results) == 1
    await session.close()


async def test_committed_attempt_survives_outer_crash_without_repeating_call(store):
    state, lease = await create(store)
    calls = []

    async def failpoint(name):
        if name == "model_attempt_committed":
            raise SystemExit("crash after receipt")

    session = RecoverySession(store, lease, state, failpoint=failpoint)

    async def handler(**kwargs):
        calls.append(1)
        return reply()

    async def call():
        return await ModelCallPolicy([object()]).invoke(handler, {"messages": []}, {})

    with pytest.raises(SystemExit):
        await run(session, call)
    await session.close()
    session = await RecoverySession.open(store, state.run_id, "owner")
    await run(session, call)
    assert calls == [1]
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 1
    await session.close()


async def test_missing_usage_can_be_reconciled_once_without_recounting_call(store):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryConflict
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)

    async def handler(**kwargs):
        return ChatResponse(content=[TextBlock(text="ok")], is_last=True)

    async def call():
        return await ModelCallPolicy([object()]).invoke(handler, {"messages": []}, {})

    await run(session, call, pricing=(2, 3))
    row = (await receipts(store, lease))[0]
    assert row["result"]["usage_status"] == "estimated"
    for _ in range(2):
        await store.reconcile_model_usage(lease, row["key"], receipt_id="provider-1",
                                          input_tokens=4, output_tokens=2)
    used = (await store.budget(state.run_id, "owner"))["used"]
    assert used == {"model_calls": 1, "input_tokens": 4, "output_tokens": 2, "cost_micro_usd": 14}
    with pytest.raises(RecoveryConflict):
        await store.reconcile_model_usage(lease, row["key"], receipt_id="provider-1",
                                          input_tokens=5, output_tokens=2)
    assert len([e for e in await store.events(state.run_id, "owner")
                if e["payload"]["type"] == "research.usage_reconciled"]) == 1
    await session.close()


async def test_real_factory_continuation_uses_two_receipts(monkeypatch, store):
    from pydantic import SecretStr

    from open_deep_research.agentscope_runtime.models import (
        CredentialBinding,
        ModelFactory,
    )
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    spec = "openai:fixture"
    config = RunConfig.compile({"configurable": {
        "research_model": spec, "output_token_escalation_enabled": False,
    }})
    factory = ModelFactory(config, scope="run", owner="r", bindings={
        "researcher": CredentialBinding("ref", "run", "r", (spec,), SecretStr("key")),
    })
    count = 0

    class Model:
        async def __call__(self, **kwargs):
            nonlocal count
            count += 1
            value = reply()
            value.metadata["provider_finish_reason"] = "length" if count == 1 else "stop"
            return value

    monkeypatch.setattr(factory, "build", lambda *args: Model())
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    models = ResearchModels(factory, recovery=session)
    await models.text("researcher", "q", {})
    assert count == 2
    assert len(await receipts(store, lease)) == 2
    await session.close()


async def test_native_agent_journal_wraps_policy(store):
    from agentscope.agent import Agent
    from agentscope.credential import CredentialBase
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.model import ChatModelBase

    from open_deep_research.agentscope_runtime.gateway import SandboxChatModel
    from open_deep_research.agentscope_runtime.model_policy import ModelPolicyMiddleware
    from open_deep_research.agentscope_runtime.recovery import JournalMiddleware

    class Model(ChatModelBase):
        def __init__(self):
            super().__init__(CredentialBase(), "fixture", SandboxChatModel.Parameters(),
                             stream=False, max_retries=0)
            self.formatter = OpenAIChatFormatter()

        async def _call_api(self, *args, **kwargs):
            return reply()

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    model = Model()
    agent = Agent(name="fixture", system_prompt="Answer", model=model, middlewares=[
        JournalMiddleware(session, "researcher", 10, None, account_attempts=True),
        ModelPolicyMiddleware(ModelCallPolicy([model])),
    ])
    assert (await agent.reply(UserMsg("user", "q"))).get_text_content() == "ok"
    assert len(await receipts(store, lease)) == 1
    await session.close()


async def test_unknown_attempt_is_quarantined_without_redispatch(store, monkeypatch):
    from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    calls = []
    original = store.commit_operation

    async def interrupt(lease, key, result, **kwargs):
        if ":attempt:" in key:
            raise SystemExit("response received, receipt not committed")
        await original(lease, key, result, **kwargs)

    monkeypatch.setattr(store, "commit_operation", interrupt)

    async def handler(**kwargs):
        calls.append(1)
        return reply()

    async def call():
        return await ModelCallPolicy([object()]).invoke(handler, {"messages": []}, {})

    with pytest.raises(SystemExit):
        await run(session, call)
    await session.close()
    monkeypatch.setattr(store, "commit_operation", original)
    session = await RecoverySession.open(store, state.run_id, "owner")
    with pytest.raises(UnknownOperation):
        await run(session, call)
    assert calls == [1]
    row = (await receipts(store, session.lease))[0]
    assert row["state"] == "quarantined"
    await session.close()


async def test_native_partial_usage_and_explicit_zero_cache(store):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)

    async def handler(**kwargs):
        return ChatResponse(content=[TextBlock(text="ok")], is_last=True,
                            metadata={"raw_usage": {"input_tokens": 0, "cached_input_tokens": 0}})

    async def call():
        return await ModelCallPolicy([object()]).invoke(handler, {"messages": []}, {})

    await run(session, call)
    row = (await receipts(store, lease))[0]
    assert row["actual"]["input_tokens"] == 0
    assert row["actual"]["output_tokens"] == 10
    assert row["result"]["usage_status"] == "estimated"
    assert row["result"]["cached_input_tokens"] == 0
    assert row["result"]["cost_status"] == "unknown"
    await session.close()


async def test_postgres_attempt_budget_and_reconciliation(pg_url):
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    database = RecoveryStore(pg_url)
    try:
        await database.create_tables()
        await test_concurrent_attempts_cannot_exceed_model_call_limit(database)
        await test_missing_usage_can_be_reconciled_once_without_recounting_call(database)
    finally:
        await database.aclose()

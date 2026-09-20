"""Exercise SDK compression and durable summary receipts through public APIs."""

import asyncio
import re
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent
from agentscope.message import AssistantMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatUsage, StructuredResponse
from test_recovery import create, store  # noqa: F401
from test_research_migration import ScriptedModel

from open_deep_research.agentscope_runtime.context import RunContextOffloader
from open_deep_research.agentscope_runtime.model_policy import ModelCallPolicy, ModelPolicyMiddleware
from open_deep_research.agentscope_runtime.native_context import ContextControlError, NativeResearchContext
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import FenceLost
from open_deep_research.agentscope_runtime.research_models import ResearchModels

pytestmark = pytest.mark.asyncio


class SummaryModel(ScriptedModel):
    def __init__(self, error=None, responses=None):
        super().__init__(responses or [[TextBlock(text="done")]])
        self.context_size = 10000
        self.summary_calls = []
        self.error = error

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        self.summary_calls.append(messages)
        if self.error:
            raise self.error
        return StructuredResponse(
            content={name: "保留 COV-01 EVID-01" for name in structured_model["properties"]},
            usage=ChatUsage(input_tokens=31, output_tokens=17, time=0.1),
        )


def build(model, session=None, offloader=None):
    class Factory:
        accounts_physical_attempts = True
        run = SimpleNamespace(get=lambda name: {})

        def descriptor(self, role):
            return {"model": "fixture", "max_output_tokens": 1000}

        def policy_middleware(self, role, candidates=None):
            return ModelPolicyMiddleware(ModelCallPolicy([model], attempts=1, circuit_enabled=False))

    models = ResearchModels(Factory(), recovery=session)
    context = NativeResearchContext(models, "researcher", model)
    return Agent(
        name="researcher", model=context.model, system_prompt="Research the assigned task.",
        context_config=context.config, injection_config=context.injection,
        middlewares=[context], offloader=offloader,
    )


def history(repeats=1100):
    return [
        UserMsg("user", "COV-01 EVID-01 来源限制 https://example.org 最新反馈。", metadata={"research_protected": True}),
        AssistantMsg("researcher", "research detail " * repeats),
        UserMsg("user", "additional evidence " * repeats),
        AssistantMsg("researcher", "recent finding"),
        UserMsg("user", "continue"),
    ]


async def test_native_summary_is_accounted_once_and_replays_committed_state(store, tmp_path):
    state, lease = await create(store)
    model = SummaryModel()
    session = RecoverySession(store, lease, state)
    offloader = RunContextOffloader(tmp_path, session)
    agent = build(model, session, offloader)
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    await agent.compress_context()
    assert "COV-01" in str(agent.state.summary)
    assert len(model.summary_calls) == 1
    assert agent.state.middle_context["context_compression"]["fallback_to_truncation"] is False
    ref = re.search(r"run-context://[^\s<>\"']+\.json", str(agent.state.summary)).group()
    page = await offloader.read(ref, session_id=agent.state.session_id)
    assert "COV-01" in page["content"]
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"] == {"model_calls": 1, "input_tokens": 31, "output_tokens": 17}
    replay = build(model, RecoverySession(store, lease, state), offloader)
    replay.state = original
    await replay.compress_context()
    assert replay.state == agent.state
    assert len(model.summary_calls) == 1
    assert await store.budget(state.run_id, "owner") == budget


async def test_archive_failure_preserves_history_and_reuses_summary_receipt(store, tmp_path):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    model = SummaryModel()

    class UnavailableArchive:
        async def offload_context(self, session_id, **kwargs):
            raise OSError("archive unavailable")

    agent = build(model, session, UnavailableArchive())
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    with pytest.raises(ContextControlError) as caught:
        await agent.compress_context()
    assert isinstance(caught.value.error, OSError)
    assert agent.state == original
    await store.release(lease)
    lease = await store.acquire(state.run_id, "owner")
    replay_session = RecoverySession(store, lease, state)
    replay = build(model, replay_session, RunContextOffloader(tmp_path, replay_session))
    replay.state = original.model_copy(deep=True)
    await replay.compress_context()
    assert replay.state.summary
    assert len(model.summary_calls) == 1
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 1


async def test_known_summary_failure_uses_sdk_truncation_and_replays(store, tmp_path):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    model = SummaryModel(error=TimeoutError("provider timeout"))
    offloader = RunContextOffloader(tmp_path, session)
    agent = build(model, session, offloader)
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    await agent.compress_context()
    assert session.problem is None
    assert agent.state.middle_context["context_compression"]["fallback_to_truncation"] is True
    assert len(agent.state.context) < len(original.context)
    assert list(offloader.directory.glob("*.json"))
    calls = len(model.summary_calls)
    budget = await store.budget(state.run_id, "owner")
    assert budget["used"]["model_calls"] == calls
    replay = build(model, RecoverySession(store, lease, state), offloader)
    replay.state = original
    await replay.compress_context()
    assert len(model.summary_calls) == calls
    assert await store.budget(state.run_id, "owner") == budget


async def test_control_failure_cannot_become_truncation():
    model = SummaryModel(error=PermissionError("credentials rejected"))
    agent = build(model)
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    with pytest.raises(ContextControlError) as caught:
        await agent.compress_context()
    assert isinstance(caught.value.error, PermissionError)
    assert agent.state == original


async def test_agentic_compression_uses_framework_lower_threshold():
    model = SummaryModel(responses=[
        [ToolCallBlock(id="compress", name="CompressContext", input='{}')],
        [TextBlock(text="done")],
    ])
    agent = build(model)
    # Use the actual tokenizer to put history between the agentic/auto thresholds.
    messages = history(1000)
    tokens = await model.count_tokens(messages, tools=[])
    model.context_size = int(tokens / 0.65)
    agent.state.context = messages
    await agent.reply(UserMsg("user", "continue"))
    assert len(model.summary_calls) == 1, str(agent.state.context[-3:])
    assert agent.state.middle_context["context_compression"]["trigger_ratio"] == pytest.approx(0.6)


async def test_budget_denial_does_not_call_summary_or_drop_history(store):
    state, lease = await create(store, limits={"model_calls": 0})
    session = RecoverySession(store, lease, state)
    model = SummaryModel()
    agent = build(model, session)
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    with pytest.raises(ContextControlError):
        await agent.compress_context()
    assert agent.state == original
    assert model.summary_calls == []
    assert session.problem is not None


async def test_lost_lease_prevents_compression(store):
    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    model = SummaryModel()
    agent = build(model, session)
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    await store.release(lease)
    with pytest.raises(ContextControlError) as caught:
        await agent.compress_context()
    assert isinstance(caught.value.error, FenceLost)
    assert agent.state == original
    assert not model.summary_calls


async def test_cancellation_is_not_summary_failure():
    agent = build(SummaryModel(error=asyncio.CancelledError()))
    agent.state.context = history(3000)
    original = agent.state.model_copy(deep=True)
    with pytest.raises(asyncio.CancelledError):
        await agent.compress_context()
    assert agent.state == original


@pytest.mark.parametrize("window", ["model_attempt_committed", "operation_committed"])
@pytest.mark.parametrize("invalid_summary", [False, True])
async def test_resume_after_summary_commit_does_not_repeat_provider_call(store, tmp_path, window, invalid_summary):
    import jsonschema
    class Crash(BaseException):
        pass

    async def crash(name):
        if name == window:
            raise Crash()

    state, lease = await create(store)
    session = RecoverySession(store, lease, state)
    session.failpoint = crash
    model = SummaryModel(error=jsonschema.ValidationError("invalid summary") if invalid_summary else None)
    agent = build(model, session, RunContextOffloader(tmp_path, session))
    agent.state.context = history(900)
    original = agent.state.model_copy(deep=True)
    with pytest.raises(Crash):
        await agent.compress_context()
    assert agent.state == original
    await store.release(lease)
    lease = await store.acquire(state.run_id, "owner")
    resumed_session = RecoverySession(store, lease, state)
    resumed = build(model, resumed_session, RunContextOffloader(tmp_path, resumed_session))
    resumed.state = original
    await resumed.compress_context()
    assert resumed.state.summary
    assert len(model.summary_calls) == 1
    assert (await store.budget(state.run_id, "owner"))["used"]["model_calls"] == 1


async def test_repeated_native_summaries_preserve_assignment_projection():
    model = SummaryModel()
    agent = build(model)
    agent.state.context = history(3000)
    await agent.compress_context()
    first = agent.state.summary
    agent.state.context.extend(history(3000)[1:])
    await agent.compress_context()
    assert len(model.summary_calls) == 2
    second_input = str(model.summary_calls[-1])
    assert "COV-01" in second_input and "https://example.org" in second_input
    assert "Task Overview" in str(first)
    assert agent.state.middle_context["research_context_authority"]["assignment"].startswith("COV-01")

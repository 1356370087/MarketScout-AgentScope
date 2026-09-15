"""T033–T037: native stage, agent, recovery and context contracts."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent
from agentscope.credential import CredentialBase
from agentscope.event import ConfirmResult, UserConfirmResultEvent, UserInterruptEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.state import AgentState
from pydantic import BaseModel

from open_deep_research.agentscope_runtime.context import ResearchContextMiddleware
from open_deep_research.agentscope_runtime.gateway import SandboxChatModel
from open_deep_research.agentscope_runtime.research_agents import (
    ResearchAssignment,
    Researcher,
    ResearchHandoff,
    Supervisor,
)
from open_deep_research.agentscope_runtime.research_pipeline import (
    STAGES,
    FileResearchCheckpoint,
    PendingDecision,
    ResearchPipeline,
    ResearchPipelineMiddleware,
    ResearchSnapshot,
)
from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.state import ClarifyWithUser
from open_deep_research.tools.base import (
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)

pytestmark = pytest.mark.asyncio


def cfg(**values):
    return {
        "configurable": {
            "event_log_enabled": False,
            "quality_evaluation_enabled": False,
            "allow_clarification": False,
            **values,
        },
        "metadata": {},
    }


class MemoryCheckpoint:
    def __init__(self, fail=None):
        self.saved = []
        self.fail = fail

    async def save(self, state):
        if self.fail and self.fail(state):
            raise OSError("checkpoint failed")
        self.saved.append(state.model_copy(deep=True))


class Stages:
    def __init__(self, pause=None):
        self.calls = []
        self.pause = pause

    async def execute(self, stage, state):
        self.calls.append(stage)
        if stage == self.pause:
            return PendingDecision(stage=stage, question="请确认范围")
        if stage == "final_report_generation":
            state.final_report = "report"


def pipeline(stages=None, checkpoint=None, state=None):
    return ResearchPipeline(
        state or ResearchSnapshot(run_id="run", config_fingerprint="frozen"),
        stages or Stages(),
        (checkpoint or MemoryCheckpoint()).save,
        config_fingerprint="frozen",
    )


async def consume(flow, inputs=None):
    return [event async for event in flow.reply_stream(inputs)]


async def test_all_stages_commit_and_completed_replay_is_read_only():
    stages, store = Stages(), MemoryCheckpoint()
    flow = pipeline(stages, store)
    await consume(flow, UserMsg("user", "研究市场"))
    assert stages.calls == list(STAGES)
    assert flow.state.status == "completed"
    resumed = pipeline(
        Stages(),
        store,
        ResearchSnapshot.model_validate_json(flow.state.model_dump_json()),
    )
    events = await consume(resumed)
    assert resumed.stages.calls == []
    assert events[-1].get_text_content() == "report"


@pytest.mark.parametrize("stage", STAGES)
async def test_resume_skips_committed_stage_prefix(stage):
    state = ResearchSnapshot(
        run_id="run",
        config_fingerprint="frozen",
        messages=[UserMsg("user", "q")],
        completed=list(STAGES[: STAGES.index(stage)]),
    )
    flow = pipeline(state=state)
    await consume(flow)
    assert flow.stages.calls == list(STAGES[STAGES.index(stage) :])


async def test_failed_checkpoint_does_not_advance_or_replay_unknown_stage():
    store = MemoryCheckpoint(fail=lambda s: "write_research_brief" in s.completed)
    stages = Stages()
    flow = pipeline(stages, store)
    with pytest.raises(OSError):
        await consume(flow, UserMsg("user", "q"))
    assert stages.calls[-1] == "write_research_brief"
    assert "plan_approval" not in stages.calls
    with pytest.raises(ValueError, match="reconciliation"):
        pipeline(Stages(), store, store.saved[-1])


async def test_file_checkpoint_roundtrip_and_engine_fence(tmp_path):
    store = FileResearchCheckpoint(tmp_path / "research.json")
    state = ResearchSnapshot(run_id="run", config_fingerprint="frozen")
    await store.save(state)
    assert store.load() == state
    assert not store.path.with_name("research.json.tmp").exists()
    with pytest.raises(ValueError, match="fingerprint"):
        ResearchPipeline(state, Stages(), store.save, config_fingerprint="different")
    with pytest.raises(ValueError):
        ResearchSnapshot.model_validate({**state.model_dump(), "engine": "legacy"})


async def test_plan_revision_receipt_and_cancellation():
    flow = pipeline(Stages("plan_approval"))
    await consume(flow, UserMsg("user", "q"))
    first = flow.state.pending.id
    await flow.decide(first, "revise", "只研究国内市场")
    await flow.decide(first, "revise", "只研究国内市场")
    with pytest.raises(ValueError, match="conflicting"):
        await flow.decide(first, "approve")
    await consume(flow)
    assert flow.stages.calls.count("write_research_brief") == 2
    assert flow.stages.calls.count("memory_recall") == 1
    with pytest.raises(ValueError, match="stale"):
        await flow.decide(first + "other", "approve")
    await consume(flow, UserInterruptEvent(reply_id=flow.state.reply_id))
    assert flow.state.status == "cancelled"
    assert "research_supervisor" not in flow.stages.calls


async def test_native_confirmation_continues_correct_pending_stage():
    flow = pipeline(Stages("outline_approval"))
    events = await consume(flow, UserMsg("user", "q"))
    required = next(e for e in events if hasattr(e, "tool_calls"))
    event = UserConfirmResultEvent(
        reply_id=flow.state.reply_id,
        confirm_results=[
            ConfirmResult(confirmed=True, tool_call=required.tool_calls[0])
        ],
    )
    await consume(flow, event)
    assert flow.state.status == "completed"
    assert flow.stages.calls.count("research_supervisor") == 1
    assert flow.stages.calls.count("outline_approval") == 1


class ScriptedModel(ChatModelBase):
    def __init__(self, responses):
        super().__init__(
            CredentialBase(),
            "fixture",
            SandboxChatModel.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = OpenAIChatFormatter()
        self.responses = responses
        self.calls = []

    async def _call_api(self, *args, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        value = self.responses[min(index, len(self.responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return ChatResponse(
            content=[block.model_copy(deep=True) for block in value], is_last=True
        )


class Models:
    context_chars = 1000

    def __init__(self, scripts=None, clarification=False, text="发现：市场规模增长。"):
        self.scripts = scripts or {}
        self.created = []
        self.structured_calls = []
        self.text_calls = []
        self.clarification = clarification
        self.output_text = text

    def agent_model(self, role, task_id):
        model = ScriptedModel(self.scripts[role])
        self.created.append((role, task_id, model))
        return model

    def agent_middlewares(self, role, model):
        return []

    async def structured(self, role, prompt, schema, state):
        self.structured_calls.append((role, schema.__name__))
        if schema is ClarifyWithUser:
            return schema(
                need_clarification=self.clarification,
                question="哪个地区？",
                verification="开始研究",
            )
        return schema(research_brief="分析中国市场规模与竞争")

    async def text(self, role, prompt, state):
        self.text_calls.append((role, prompt))
        return self.output_text


class FakeSupervisor:
    def __init__(self):
        self.calls = []

    async def run(self, *args):
        self.calls.append(args)
        return [{"compressed_research": "findings"}], {}


async def test_clarification_answer_builds_native_coverage_once():
    models, supervisor, config = (
        Models(clarification=True),
        FakeSupervisor(),
        cfg(allow_clarification=True),
    )
    stages = NativeResearchStages(models, supervisor, lambda: config)
    flow = pipeline(stages)
    question = UserMsg("user", "请分析市场规模和竞争格局")
    await consume(flow, question)
    assert flow.state.pending.stage == "clarify_with_user"
    resumed = pipeline(
        stages, state=ResearchSnapshot.model_validate_json(flow.state.model_dump_json())
    )
    await consume(resumed, UserMsg("user", "中国市场"))
    assert resumed.state.status == "completed"
    assert models.structured_calls.count(("supervisor", "ClarifyWithUser")) == 1
    expected = build_research_coverage_contract(
        [
            {"role": "user", "content": question.get_text_content()},
            {"role": "user", "content": "中国市场"},
        ],
        advisory_dimensions=[resumed.state.research_brief],
    )
    assert resumed.state.coverage_contract == expected.model_dump(mode="json")


async def test_disabled_clarification_empty_writer_and_missing_memory_fail_closed():
    models, supervisor = Models(), FakeSupervisor()

    async def bad_writer(*args):
        return ""

    flow = pipeline(
        NativeResearchStages(
            models, supervisor, lambda: cfg(), report_writer=bad_writer
        )
    )
    with pytest.raises(ValueError, match="no report"):
        await consume(flow, UserMsg("user", "市场规模"))
    assert not any(name == "ClarifyWithUser" for _, name in models.structured_calls)
    assert flow.state.final_report == ""
    assert "memory_extract_and_write" not in flow.state.completed


def tool_call(name, call_id="c", **kwargs):
    return ToolCallBlock(id=call_id, name=name, input=json.dumps(kwargs))


def evidence():
    return {
        "evidence_id": "ev1",
        "claim": "增长",
        "supporting_excerpt": "市场增长。",
        "document_id": "doc1",
        "chunk_id": "chunk1",
        "locator": "1",
        "source_url": "https://example.test/source",
        "security_status": "accepted",
    }


class Empty(BaseModel):
    pass


async def research_tools(assignment):
    async def call(input, context, progress):
        assert context.config["metadata"]["task_id"] == assignment.task_id
        return ToolResult(output={"padding": "x" * 1000, "evidence": [evidence()]})

    return [
        build_tool(
            name="web_research",
            input_schema=Empty,
            description="Research sources",
            prompt="Search sources",
            call=call,
            origin=ToolOrigin.SEARCH,
            max_output_chars=100,
            execution_zone=ToolExecutionZone.HOST_CONTROL,
        )
    ]


def contract(query="请研究市场规模和竞争格局"):
    return build_research_coverage_contract(
        [{"role": "user", "content": query}]
    ).model_dump(mode="json")


async def test_researcher_isolation_typed_evidence_and_completion_signal():
    scripts = {
        "researcher": [
            [tool_call("web_research")],
            [tool_call("ResearchComplete", "done")],
        ]
    }
    models = Models(scripts)
    researcher = Researcher(models, lambda: cfg(), research_tools, run_id="run")
    one = await researcher.run(ResearchAssignment(research_topic="中国"), contract())
    two = await researcher.run(ResearchAssignment(research_topic="欧洲"), contract())
    assert one.evidence_registry[0]["evidence_id"] == "ev1"
    assert len(one.evidence_registry) == 1
    assert one.termination == "research_complete"
    assert all(len(model.calls) == 2 for _, _, model in models.created)
    assert "中国" not in json.dumps(two.agent_state, ensure_ascii=False)
    assert one.agent_state["session_id"] != two.agent_state["session_id"]


async def test_researcher_iteration_limit_and_unregistered_citation():
    models = Models({"researcher": [[tool_call("think_tool", reflection="think")]]})
    researcher = Researcher(
        models, lambda: cfg(max_react_tool_calls=2), research_tools, run_id="run"
    )
    result = await researcher.run(ResearchAssignment(research_topic="市场"), contract())
    assert result.termination == "exceed_max_iters"
    assert len(models.created[0][2].calls) == 2
    models.output_text = "见 https://invented.test/source"
    with pytest.raises(ValueError, match="unregistered"):
        await researcher.run(ResearchAssignment(research_topic="市场"), contract())


@pytest.mark.parametrize("async_mode", [False, True])
async def test_supervisor_parallel_bound_and_join(async_mode):
    active = peak = 0

    class Worker:
        async def run(self, assignment, contract, feedback):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ResearchHandoff(
                **assignment.model_dump(), compressed_research="done"
            )

    name = "TaskCreate" if async_mode else "ConductResearch"
    models = Models(
        {
            "supervisor": [
                [
                    tool_call(name, str(i), research_topic=f"market {i}")
                    for i in range(3)
                ],
                [TextBlock(text="done")],
            ]
        }
    )
    supervisor = Supervisor(
        models,
        lambda: cfg(enable_async_research=async_mode, max_concurrent_research_units=2),
        Worker(),
        run_id="run",
    )
    results, _state = await supervisor.run("brief", contract())
    assert len(results) == 3
    assert peak == 2 and active == 0
    assert len({r["task_id"] for r in results}) == 3
    assert all(r["requirement_ids"] for r in results)


async def test_supervisor_cancellation_cleans_up_workers():
    entered = asyncio.Event()
    finished = asyncio.Event()

    class Worker:
        async def run(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    models = Models(
        {"supervisor": [[tool_call("ConductResearch", research_topic="q")]]}
    )
    supervisor = Supervisor(models, lambda: cfg(), Worker(), run_id="run")
    task = asyncio.create_task(supervisor.run("brief", contract()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set()


async def test_pipeline_runs_inside_native_agent_middleware():
    flow = pipeline()
    model = ScriptedModel([[TextBlock(text="must not call")]])
    agent = Agent(
        name="service",
        model=model,
        system_prompt="research",
        middlewares=[ResearchPipelineMiddleware(flow)],
    )
    result = await agent.reply(UserMsg("user", "q"))
    assert result.get_text_content() == "report"
    assert not model.calls
    assert agent.state.middle_context["research"]["completed"] == list(STAGES)


async def test_context_preserves_tool_pairs_feedback_and_offload_reference():
    protected = UserMsg("user", "需求及反馈", metadata={"research_protected": True})
    call = AssistantMsg("assistant", [tool_call("search")])
    result = AssistantMsg(
        "assistant", [ToolResultBlock(id="c", name="search", output="result")]
    )
    old = UserMsg("user", "x" * 10000)
    latest = UserMsg("user", "continue")
    messages = [protected, call, result, old, latest]

    class Offload:
        async def offload_context(self, session, items):
            self.messages = items
            return "workspace://w/context.json"

    offload = Offload()
    agent = SimpleNamespace(state=AgentState(context=messages))
    middleware = ResearchContextMiddleware(max_chars=3000, offloader=offload)
    await middleware.on_compress_context(agent, {}, None)
    kept = {m.id for m in agent.state.context}
    assert protected.id in kept and latest.id in kept
    assert (call.id in kept) == (result.id in kept)
    assert old.id not in kept
    assert len(offload.messages) == 5
    assert agent.state.middle_context["research_context_refs"] == [
        "workspace://w/context.json"
    ]


async def test_context_offload_failure_does_not_drop_original_messages():
    class BadOffloader:
        async def offload_context(self, *args):
            raise OSError("write failed")

    messages = [
        UserMsg("user", "first"),
        UserMsg("user", "x" * 10000),
        UserMsg("user", "last"),
    ]
    agent = SimpleNamespace(state=AgentState(context=messages))
    with pytest.raises(OSError):
        await ResearchContextMiddleware(
            max_chars=2000, offloader=BadOffloader()
        ).on_compress_context(agent, {}, None)
    assert agent.state.context == messages
    with pytest.raises(ValueError, match="protected"):
        await ResearchContextMiddleware(
            max_chars=1, offloader=BadOffloader()
        ).on_compress_context(agent, {}, None)


async def test_full_native_pipeline_approval_resume_preserves_research():
    models = Models(
        {
            "supervisor": [
                [tool_call("ConductResearch", research_topic="中国市场规模")],
                [tool_call("ResearchComplete")],
            ],
            "researcher": [
                [tool_call("web_research")],
                [tool_call("ResearchComplete")],
            ],
        }
    )
    config = lambda: cfg(enable_human_in_loop=True)
    worker = Researcher(models, config, research_tools, run_id="run")
    supervisor = Supervisor(models, config, worker, run_id="run")
    stages = NativeResearchStages(models, supervisor, config)
    flow = pipeline(stages)
    await consume(flow, UserMsg("user", "分析中国市场规模与竞争"))
    assert flow.state.pending.stage == "plan_approval"
    await flow.decide(flow.state.pending.id, "approve")
    await consume(flow)
    assert flow.state.pending.stage == "outline_approval"
    assert len(flow.state.findings) == 1
    assert flow.state.findings[0]["evidence_registry"]
    calls = len(models.created)
    resumed = pipeline(
        stages, state=ResearchSnapshot.model_validate_json(flow.state.model_dump_json())
    )
    await resumed.decide(resumed.state.pending.id, "approve")
    await consume(resumed)
    assert resumed.state.status == "completed"
    assert resumed.state.final_report
    assert len(models.created) == calls


async def test_false_native_confirmation_cannot_approve():
    flow = pipeline(Stages("plan_approval"))
    events = await consume(flow, UserMsg("user", "q"))
    required = next(e for e in events if hasattr(e, "tool_calls"))
    event = UserConfirmResultEvent(
        reply_id=flow.state.reply_id,
        confirm_results=[
            ConfirmResult(confirmed=False, tool_call=required.tool_calls[0])
        ],
        metadata={"action": "approve"},
    )
    with pytest.raises(ValueError, match="contradicts"):
        await consume(flow, event)
    assert flow.state.status == "waiting"


async def test_model_boundary_preserves_fallback_and_routes_sandbox_stages():
    from open_deep_research.agentscope_runtime.research_models import ResearchModels

    class Factory:
        def __init__(self):
            self.routes = []

        def policy_middleware(self, role, candidates=None):
            self.routes.append((role, candidates))
            return SimpleNamespace(policy=self)

        async def invoke(self, handler, kwargs, state):
            return SimpleNamespace(
                content={
                    "need_clarification": False,
                    "question": "",
                    "verification": "ok",
                }
            )

        async def complete_with_recovery(self, role, messages, **kwargs):
            self.routes.append((role, kwargs["candidates"]))
            return ChatResponse(content=[TextBlock(text="report")], is_last=True)

    factory = Factory()
    direct = ResearchModels(factory)
    direct.agent_middlewares("researcher", "primary")
    assert factory.routes[-1] == ("researcher", None)
    resolved = []

    def resolve(role, task):
        resolved.append((role, task))
        return "sandbox-model"

    sandbox = ResearchModels(factory, model_for=resolve)
    await sandbox.structured("supervisor", "q", ClarifyWithUser, {})
    await sandbox.text("compression", "q", {"task_id": "worker-1"})
    assert resolved == [("supervisor", "pipeline"), ("compression", "worker-1")]
    assert factory.routes[-2:] == [
        ("supervisor", ["sandbox-model"]),
        ("compression", ["sandbox-model"]),
    ]


@pytest.mark.parametrize("failure", ["cancelled", "failed"])
async def test_supervisor_retains_successful_handoffs_when_one_child_stops(failure):
    class Worker:
        async def run(self, assignment, contract, feedback):
            if assignment.research_topic == "stop":
                if failure == "cancelled":
                    raise asyncio.CancelledError()
                raise RuntimeError("fixture worker error")
            return ResearchHandoff(
                **assignment.model_dump(), compressed_research="done"
            )

    models = Models(
        {
            "supervisor": [
                [
                    tool_call("TaskCreate", "one", research_topic="stop"),
                    tool_call("TaskCreate", "two", research_topic="continue"),
                ],
                [TextBlock(text="done")],
            ]
        }
    )
    results, state = await Supervisor(
        models, lambda: cfg(enable_async_research=True), Worker(), run_id="run"
    ).run("brief", contract())
    assert len(results) == 1 and results[0]["research_topic"] == "continue"
    assert {row["status"] for row in state["middle_context"]["research_tasks"]} == {
        failure,
        "completed",
    }


async def test_single_task_owns_every_delegable_requirement():
    coverage = contract("研究市场规模、竞争格局、增长趋势、政策与风险")
    coverage["single_research_task"] = True
    from open_deep_research.quality.contract import ResearchCoverageContract

    expected = list(
        ResearchCoverageContract.model_validate(coverage).delegable_requirement_ids()
    )

    class Worker:
        async def run(self, assignment, contract, feedback):
            assert assignment.requirement_ids == expected
            return ResearchHandoff(
                **assignment.model_dump(), compressed_research="done"
            )

    models = Models(
        {
            "supervisor": [
                [tool_call("ConductResearch", research_topic="all")],
                [tool_call("ResearchComplete")],
            ]
        }
    )
    results, _ = await Supervisor(models, lambda: cfg(), Worker(), run_id="run").run(
        "brief", coverage
    )
    assert len(results) == 1

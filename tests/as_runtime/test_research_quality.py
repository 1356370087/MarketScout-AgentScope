"""M5 source contracts, native Judge gates and finite completion tests."""

import json
from open_deep_research.quality.context import context_payload

import pytest
from agentscope.message import TextBlock
from test_research_migration import (
    Models,
    ResearchAssignment,
    cfg,
    contract,
    evidence,
    tool_call,
)

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, Supervisor
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.evidence import source_scoped_evidence_records
from open_deep_research.quality.contract import (
    AdmissionStatus,
    RequirementCoverage,
    build_research_coverage_contract,
    merge_coverage_ledger,
)
from open_deep_research.quality.gate import (
    HandoffAssessment,
    ToolResultAssessment,
)
from open_deep_research.quality.policy import QualityEvaluationRigor

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("instruction", [
    "团队执行要求：Lead 显式 TeamCreate", "创建 db（direct）", "mq（plan_approval）两个成员",
    "分别派发上述两个任务", "blockedBy 前两项", "不设置 owner", "让成员自主认领",
    "使用 TaskGet 读取上游已接纳的证据工件", "mq 首版计划须由 Lead 明确驳回",
    "修订后再通过结构化 plan_response 批准", "成员相互发送研究发现消息",
    "质量拒绝不能解锁依赖", "补证时 Lead 创建补证任务", "最后完成质量检查",
])
async def test_team_instructions_are_process_requirements(instruction):
    from open_deep_research.quality.contract import classify_requirement_kind, is_delegable_requirement

    assert classify_requirement_kind(instruction) == "process"
    # Frozen historical contracts also receive the existing classification fallback.
    assert not is_delegable_requirement({"kind": "factual", "text": instruction})


@pytest.mark.parametrize("question", [
    "研究 Agent Teams 的 TeamCreate 与 TaskGet 实现", "解释 PostgreSQL CAS 原子领取任务的原理",
    "RocketMQ 消费者如何处理重复消息", "比较 direct 和 plan_approval 的功能差异",
])
async def test_team_research_questions_remain_factual(question):
    from open_deep_research.quality.contract import classify_requirement_kind

    assert classify_requirement_kind(question) == "factual"


class Judge:
    def __init__(self, score=5):
        self.score = score
        self.calls = []

    async def structured(self, role, prompt, schema, state, *, messages=None):
        prompt = messages[1].get_text_content() if messages is not None else prompt
        self.calls.append(prompt)
        fields = {
            "relevance": self.score,
            "source_quality": self.score,
            "evidence_coverage": self.score,
            "reason": "fixture",
        }
        if schema is ToolResultAssessment:
            return schema(
                decision="complete" if self.score == 5 else "continue",
                missing_information=[] if self.score == 5 else ["low quality"],
                corroboration=self.score,
                **fields,
            )
        body = prompt if prompt.startswith("<research_context") else prompt.split("Evaluate this JSON research payload:\n", 1)[1]
        payload = context_payload(body.split("\nCorrect these", 1)[0])
        ids = payload.get("owned_requirement_ids", [])
        return schema(
            accepted=True,
            groundedness=self.score,
            requirement_coverage=[
                RequirementCoverage(
                    requirement_id=key,
                    status="supported",
                    evidence_ids=["ev1"],
                    explanation="supported",
                )
                for key in ids
            ],
            **fields,
        )


@pytest.mark.parametrize("rigor", list(QualityEvaluationRigor))
async def test_native_judge_uses_every_rigor_and_rejects_low_scores(rigor):
    config = lambda: cfg(
        quality_evaluation_enabled=True,
        quality_evaluation_rigor=rigor,
        quality_evaluation_min_sources=1,
    )
    assignment = ResearchAssignment(research_topic="市场规模")
    for score, expected in [(5, True), (1, False)]:
        quality = NativeResearchQuality(Judge(score), config)
        result = await quality.batch(
            assignment,
            contract(),
            [
                {
                    "name": "web_research",
                    "output": "市场增长 https://example.test/source",
                }
            ],
            [evidence()],
        )
        assert result["accepted"] is expected
        assert result["quality_rigor"] == rigor.value
        assert result["deterministic_checks"]["structured_evidence_count"] == 1


async def test_restricted_scope_and_exclusion_use_same_contract():
    query = (
        "只使用 https://example.test/source 核查市场规模。不研究发布日期、后续版本。"
    )
    first = build_research_coverage_contract([{"role": "user", "content": query}])
    second = build_research_coverage_contract([{"role": "user", "content": query}])
    assert first == second
    assert all(
        "发布日期" not in r.text
        for r in first.requirements
        if r.requirement_id in first.delegable_requirement_ids()
    )
    denied = {
        **evidence(),
        "source_url": "https://evil.test/untrusted",
        "evidence_id": "bad",
    }
    admitted = source_scoped_evidence_records([evidence(), denied], first)
    assert [row["evidence_id"] for row in admitted] == ["ev1"]


async def test_native_gate_hard_checks_override_optimistic_judge():
    quality = NativeResearchQuality(
        Judge(), lambda: cfg(quality_evaluation_min_sources=1)
    )
    outcome = ResearchHandoff(
        task_id="t",
        research_topic="市场",
        requirement_ids=[],
        compressed_research="没有来源的断言",
    )
    result = await quality.handoff(outcome, contract())
    assert not result.accepted
    assert not result.deterministic_checks["passed"]
    assert merge_coverage_ledger({}, task_id="t", assessment=result) == {}


async def test_ledger_is_monotonic_and_rejected_handoff_cannot_write():
    key = next(
        iter(
            build_research_coverage_contract(
                [{"role": "user", "content": "市场规模"}]
            ).delegable_requirement_ids()
        )
    )

    def assessment(status, admission=AdmissionStatus.ACCEPTED):
        return HandoffAssessment(
            accepted=admission != AdmissionStatus.REJECTED,
            admission_status=admission,
            relevance=5,
            source_quality=5,
            evidence_coverage=5,
            groundedness=5,
            reason="ok",
            requirement_coverage=[
                RequirementCoverage(
                    requirement_id=key,
                    status=status,
                    evidence_ids=["ev1"],
                    explanation="fixture",
                )
            ],
        )

    ledger = merge_coverage_ledger(
        {},
        task_id="t1",
        assessment=assessment("supported"),
        owned_requirement_ids=[key],
    )
    merged = merge_coverage_ledger(
        ledger,
        task_id="t2",
        assessment=assessment("partial"),
        owned_requirement_ids=[key],
    )
    assert merged[key]["status"] == "supported"
    assert (
        merge_coverage_ledger(
            merged,
            task_id="bad",
            assessment=assessment("supported", AdmissionStatus.REJECTED),
        )
        == merged
    )


async def test_completion_no_evidence_stops_at_model_limit():
    class Worker:
        async def run(self, assignment, contract, feedback):
            return ResearchHandoff(
                **assignment.model_dump(), compressed_research="no evidence"
            )

    models = Models(
        {
            "supervisor": [
                [tool_call("ConductResearch", research_topic="q")],
                [TextBlock(text="finish")],
            ]
        }
    )
    supervisor = Supervisor(
        models,
        lambda: cfg(max_researcher_iterations=3),
        Worker(),
        run_id="run",
        completion_policy=True,
    )
    with pytest.raises(ValueError, match="research terminated"):
        await supervisor.run("brief", contract())
    assert len(models.created[0][2].calls) == 3


@pytest.mark.parametrize("budget", [True, False])
async def test_completion_partial_preserves_evidence_and_reports_gaps(budget):
    remaining = True

    class Worker:
        async def run(self, assignment, contract, feedback):
            nonlocal remaining
            remaining = budget
            return ResearchHandoff(
                **assignment.model_dump(),
                compressed_research="partial",
                evidence_registry=[evidence()],
            )

    models = Models(
        {
            "supervisor": [
                [tool_call("ConductResearch", research_topic="q")],
                [TextBlock(text="finish")],
            ]
        }
    )
    supervisor = Supervisor(
        models,
        lambda: cfg(max_researcher_iterations=2, quality_evaluation_min_sources=2),
        Worker(),
        run_id="run",
        completion_policy=True,
        budget_available=lambda: remaining,
    )
    results, state = await supervisor.run("brief", contract())
    assert results[0]["evidence_registry"]
    completion = state["middle_context"]["business_completion"]
    assert completion["action"] == "complete_partial"
    assert "independent_sources" in completion["gaps"]
    assert len(models.created[0][2].calls) == (2 if budget else 1)


async def test_native_gate_import_does_not_load_legacy_models():
    import asyncio
    import subprocess
    import sys

    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            "import sys; import open_deep_research.agentscope_runtime.research; assert not any(k.startswith('langchain') for k in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_structured_adapter_uses_real_policy_keyword_contract():
    from types import SimpleNamespace

    from agentscope.model import StructuredResponse

    from open_deep_research.agentscope_runtime.model_policy import ModelCallPolicy
    from open_deep_research.agentscope_runtime.research_models import ResearchModels
    from open_deep_research.state import ResearchQuestion

    class Model:
        async def generate_structured_output(self, messages, schema):
            return StructuredResponse(content={"research_brief": "fixture brief"})

    class Factory:
        def policy_middleware(self, role, candidates=None):
            return SimpleNamespace(
                policy=ModelCallPolicy([Model()], circuit_enabled=False)
            )

    result = await ResearchModels(Factory()).structured(
        "supervisor", "q", ResearchQuestion, {}
    )
    assert result.research_brief == "fixture brief"


async def test_full_gated_supervisor_admits_evidence_and_merges_coverage():
    from test_research_migration import research_tools

    from open_deep_research.agentscope_runtime.research_agents import Researcher

    class AllModels(Models):
        async def structured(self, role, prompt, schema, state, *, messages=None):
            return await Judge().structured(role, prompt, schema, state, messages=messages)

    models = AllModels(
        {
            "researcher": [
                [tool_call("web_research")],
                [tool_call("ResearchComplete", "finish")],
            ],
            "supervisor": [
                [tool_call("ConductResearch", research_topic="市场规模")],
                [tool_call("ResearchComplete", "finish")],
            ],
        },
        text="市场增长符合给定的测试资料，但属于虚构夹具不能用于现实决策。" * 20
        + " https://example.test/source",
    )
    config = lambda: cfg(
        quality_evaluation_enabled=True, quality_evaluation_min_sources=1
    )
    quality = NativeResearchQuality(models, config)
    worker = Researcher(models, config, research_tools, run_id="run", quality=quality)
    results, state = await Supervisor(
        models, config, worker, run_id="run", quality=quality, completion_policy=True
    ).run("brief", contract("市场规模"))
    assert results[0]["assessment"]["handoff"]["accepted"]
    assert all(
        row["status"] == "supported"
        for row in state["middle_context"]["coverage_ledger"].values()
    )
    assert state["middle_context"]["business_completion"]["action"] == "complete"


@pytest.mark.parametrize(
    "mode,sources,expected",
    [
        ("web", [], {"web_research", "fetch_url"}),
        ("documents", [{"type": "document", "id": "doc"}], {"search_documents"}),
        (
            "hybrid",
            [{"type": "document", "id": "doc"}],
            {"search_documents", "web_research", "fetch_url"},
        ),
        (
            "specific",
            [{"type": "url", "url": "https://example.test/source"}],
            {"web_research", "fetch_url"},
        ),
    ],
)
async def test_researcher_source_mode_filters_native_tool_catalog(
    mode, sources, expected
):
    from test_research_migration import Empty

    from open_deep_research.agentscope_runtime.research_agents import Researcher
    from open_deep_research.tools.base import ToolOrigin, ToolResult, build_tool

    async def call(*args):
        return ToolResult(output="fixture")

    async def tools_for(assignment):
        return [
            build_tool(
                name=name,
                input_schema=Empty,
                description=name,
                call=call,
                origin=ToolOrigin.SYSTEM,
            )
            for name in ("web_research", "fetch_url", "search_documents")
        ]

    config = cfg()
    config["metadata"]["source_selection"] = {"mode": mode, "sources": sources}
    models = Models({"researcher": [[TextBlock(text="no tools needed")]]})
    await Researcher(models, lambda: config, tools_for, run_id="run").run(
        ResearchAssignment(research_topic="q"), contract()
    )
    schemas = models.created[0][2].calls[0]["tools"]
    # SDK context compression is present in every mode; source filters apply
    # to research tools and must not remove this framework-owned capability.
    assert "CompressContext" in {item["function"]["name"] for item in schemas}
    names = {item["function"]["name"] for item in schemas} - {
        "think_tool",
        "ResearchComplete",
        "CompressContext",
    }
    assert names == expected


async def test_native_coalesced_context_can_offload_older_complete_round():
    from types import SimpleNamespace

    from agentscope.message import AssistantMsg, ToolResultBlock, UserMsg
    from agentscope.state import AgentState

    from open_deep_research.agentscope_runtime.context import ResearchContextMiddleware
    from open_deep_research.agentscope_runtime.messages import validate_tool_pairs

    first = UserMsg("user", "需求", metadata={"research_protected": True})
    coalesced = AssistantMsg(
        "researcher",
        [
            tool_call("search", "old"),
            ToolResultBlock(id="old", name="search", output="x" * 9000),
            tool_call("search", "new"),
            ToolResultBlock(id="new", name="search", output="recent evidence"),
        ],
    )

    class Offloader:
        async def offload_context(self, session_id, msgs):
            self.saved = msgs
            return "workspace://test/archive"

    offloader = Offloader()
    agent = SimpleNamespace(state=AgentState(context=[first, coalesced]))
    await ResearchContextMiddleware(
        max_chars=3500, offloader=offloader
    ).on_compress_context(agent, {}, None)
    validate_tool_pairs(agent.state.context, complete=True)
    ids = {
        b.id
        for m in agent.state.context
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }
    assert ids == {"new"}
    assert len(offloader.saved) == 2


@pytest.mark.parametrize("skill", ["medical", "legal", "finance"])
async def test_native_researcher_receives_selected_domain_skill(skill):
    from open_deep_research.agentscope_runtime.research_agents import Researcher
    from open_deep_research.skills import get_skill_researcher_context

    async def tools_for(assignment):
        return []

    models = Models({"researcher": [[TextBlock(text="Domain findings")]]})
    config = cfg(skills=[skill])
    await Researcher(models, lambda: config, tools_for, run_id="run").run(
        ResearchAssignment(research_topic="q"), contract()
    )
    prompt = str(models.created[0][2].calls[0]["messages"])
    assert get_skill_researcher_context([skill]) in prompt


async def test_terminal_no_evidence_is_durable_and_never_reenters_research():
    from agentscope.message import UserMsg
    from test_research_migration import MemoryCheckpoint, Stages, consume, pipeline

    from open_deep_research.agentscope_runtime.research_agents import ResearchTerminated
    from open_deep_research.completion import CompletionDecision, CompletionPolicyResult

    class Terminate(Stages):
        async def execute(self, stage, state):
            await super().execute(stage, state)
            if stage == "research_supervisor":
                raise ResearchTerminated(
                    CompletionPolicyResult(
                        CompletionDecision.TERMINATE,
                        "budget_exhausted",
                        ("accepted_evidence",),
                    ),
                    [],
                )

    store = MemoryCheckpoint()
    flow = pipeline(Terminate(), store)
    with pytest.raises(ResearchTerminated):
        await consume(flow, UserMsg("user", "q"))
    assert store.saved[-1].status == "failed"
    assert store.saved[-1].inflight is None
    restored = pipeline(Stages(), state=store.saved[-1])
    events = await consume(restored)
    assert not restored.stages.calls
    assert any(
        str(getattr(event, "finished_reason", "")) == "error" for event in events
    )


async def test_completion_cancellation_does_not_schedule_remediation():
    import asyncio

    from test_research_migration import Models

    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Worker:
        async def run(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    models = Models(
        {"supervisor": [[tool_call("ConductResearch", research_topic="q")]]}
    )
    supervisor = Supervisor(
        models, lambda: cfg(), Worker(), run_id="run", completion_policy=True
    )
    task = asyncio.create_task(supervisor.run("brief", contract()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()
    assert len(models.created[0][2].calls) == 1

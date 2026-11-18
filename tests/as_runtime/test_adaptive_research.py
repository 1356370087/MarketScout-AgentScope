"""Exercise native tool loops, delegated evidence and frozen configuration."""

import asyncio
from copy import deepcopy

import pytest
from test_research_migration import Models, ScriptedModel, cfg, contract, research_tools, tool_call
from test_efficiency import cache  # noqa: F401 -- shared durable ResearchCache fixture

from open_deep_research.agentscope_runtime.adaptive_research import AdaptiveResearch
from open_deep_research.agentscope_runtime.research_agents import Researcher
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.configuration import Configuration, RUN_CONFIG_FROZEN_FIELDS_V21, run_config_fingerprint
from open_deep_research.skills.runtime import run_skills


def test_strategy_defaults_and_explicit_teams():
    assert Configuration().use_adaptive_research
    assert not Configuration(enable_async_research=True).use_durable_research_team
    assert Configuration(enable_async_research=True, async_research_mode="teams").use_durable_research_team
    assert Configuration(research_execution_strategy="delegated", enable_async_research=True).use_durable_research_team


def test_v21_restores_delegated_without_changing_old_fingerprint(monkeypatch):
    saved = RunConfig.compile().snapshot()
    old = saved["contract"]
    old["metadata"]["run_config_schema_version"] = 21
    old["metadata"].pop("research_skill_snapshot")
    old["configurable"] = {k: v for k, v in old["configurable"].items() if k in RUN_CONFIG_FROZEN_FIELDS_V21}
    old["metadata"]["run_config_fingerprint"] = run_config_fingerprint(old)
    monkeypatch.setenv("RESEARCH_EXECUTION_STRATEGY", "adaptive")
    restored = RunConfig.restore(saved)
    assert restored.get("research_execution_strategy") == "delegated"
    assert restored.snapshot() == saved
    with pytest.raises(ValueError, match="frozen configuration conflict"):
        RunConfig.restore(saved, overrides={"research_execution_strategy": "adaptive"})


def test_skills_frozen_and_fingerprint_protected(monkeypatch):
    from open_deep_research.skills import runtime
    run = RunConfig.compile({"configurable": {"skills": ["finance"]}})
    original = run_skills(run.compatibility_projection())
    monkeypatch.setattr(runtime, "METHODS", ())
    assert run_skills(RunConfig.restore(run.snapshot()).compatibility_projection()) == original
    assert "finance" in original
    from open_deep_research.skills import get_skill_report_context
    assert get_skill_report_context(["finance"], snapshot=original) == original["finance"]["report_context"]
    modified = deepcopy(run.snapshot())
    modified["contract"]["metadata"]["research_skill_snapshot"]["finance"]["researcher_context"] = "changed"
    with pytest.raises(ValueError, match="fingerprint"):
        RunConfig.restore(modified)


def test_builtin_skill_context_does_not_require_contributed_tool_permission():
    from open_deep_research.skills.runtime import skill_tools
    from open_deep_research.tools.governance import AgentRole, check_permission
    config = cfg(langgraph_auth_user={"permissions": ["research.tool.search"]})
    tool = skill_tools(config)[0]
    assert check_permission(tool.name, tool, AgentRole.RESEARCHER, None, config) is None


@pytest.mark.parametrize("prefix,expected", [("将", True), ("不要将", False)])
def test_question_inside_delegation_directive_remains_delegable(prefix, expected):
    from open_deep_research.quality.planning import compile_planned_requirements
    from open_deep_research.quality.contract import validate_requirement_ids, coverage_bound_input_schema
    from open_deep_research.agentscope_runtime.adaptive_research import DelegationInput
    text = f"核实 VACUUM 如何回收空间；{prefix} ANALYZE 如何影响查询规划委派给一个独立子研究员。"
    coverage = compile_planned_requirements([{"role": "user", "content": text}], [
        {"source_text": "核实 VACUUM 如何回收空间", "kind": "factual"},
        {"source_text": prefix + " ANALYZE 如何影响查询规划委派给一个独立子研究员", "kind": "process"},
    ], atomic=True, task_scopes=True, delegated_questions=True)
    if not expected:
        assert not any(row.kind == "factual" and row.text == "ANALYZE 如何影响查询规划" for row in coverage.requirements)
        return
    question = next(row for row in coverage.requirements if row.text == "ANALYZE 如何影响查询规划")
    assert question.kind == "factual"
    assert text[question.source_start:question.source_end] == question.text
    assert validate_requirement_ids([question.requirement_id], coverage, required=True) == [question.requirement_id]
    schema = coverage_bound_input_schema(DelegationInput, coverage).model_json_schema()
    assert "requirement_ids" in schema["required"]
    assert set(schema["properties"]["requirement_ids"]["items"]["enum"]) == set(coverage.delegable_requirement_ids())


def test_single_delegation_sentence_still_contains_a_factual_question():
    from open_deep_research.quality.planning import compile_planned_requirements
    text = "将 ANALYZE 如何影响查询规划委派给一个独立子研究员"
    coverage = compile_planned_requirements([{"role": "user", "content": text}],
        [{"source_text": text, "kind": "process"}], atomic=True, task_scopes=True, delegated_questions=True)
    assert any(row.kind == "factual" and row.text == "ANALYZE 如何影响查询规划" for row in coverage.requirements)


def test_explicit_single_main_task_is_enforced_without_prohibiting_a_lead_with_workers():
    assert contract("只用一个主研究任务，之后核实两款产品。")["single_research_task"]
    assert not contract("安排一个主研究任务，再委派两个子研究员。")["single_research_task"]


def test_v22_keeps_fallback_when_model_misclassifies_every_atom():
    from open_deep_research.quality.planning import compile_planned_requirements
    text = "核实 VACUUM 如何回收空间"
    coverage = compile_planned_requirements([{"role": "user", "content": text}],
        [{"source_text": text, "kind": "process"}], atomic=True, task_scopes=True, delegated_questions=True)
    assert any(row.kind == "factual" for row in coverage.requirements)


@pytest.mark.asyncio
async def test_source_quota_retries_before_judge_without_claiming_factual_gaps(monkeypatch):
    from unittest.mock import AsyncMock
    from test_research_migration import evidence
    from open_deep_research.agentscope_runtime import research_quality
    from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment
    configuration = cfg(quality_evaluation_min_sources=3)
    configuration["metadata"]["run_config_schema_version"] = 22
    judge = AsyncMock(side_effect=RuntimeError("judge_reached"))
    monkeypatch.setattr(research_quality, "evaluate_tool_results", judge)
    quality = research_quality.NativeResearchQuality(None, lambda: configuration)
    records = [{**evidence(), "evidence_id": f"ev{n}", "document_id": f"doc{n}", "source_type": "web",
                "source_url": f"https://source{n}.example.test/"} for n in range(3)]
    assignment = ResearchAssignment(task_id="main-research", research_topic="Market facts")
    rows = [{"name": "ReadResearchEvidence", "content": "https://unregistered1.test/ https://unregistered2.test/ https://unregistered3.test/", "error": False}]
    result = await quality.batch(assignment, contract(), rows, records[:2])
    assert not result["accepted"] and result["decision"] == "retry"
    assert result["deterministic_checks"]["required_source_count"] == 3
    assert result["evaluator_error"] is None and not result["gaps"]
    judge.assert_not_called()
    with pytest.raises(RuntimeError, match="judge_reached"):
        await quality.batch(assignment, contract(), rows, records)


@pytest.mark.asyncio
async def test_main_source_quota_uses_global_corpus_while_leaf_can_own_one(monkeypatch):
    from unittest.mock import AsyncMock
    from test_research_migration import evidence
    from open_deep_research.agentscope_runtime import research_quality
    from open_deep_research.agentscope_runtime.research_agents import ResearchAssignment
    configuration = cfg(quality_evaluation_min_sources=3)
    configuration["metadata"]["run_config_schema_version"] = 22
    judge = AsyncMock(side_effect=RuntimeError("judge_reached"))
    monkeypatch.setattr(research_quality, "evaluate_tool_results", judge)
    quality = research_quality.NativeResearchQuality(None, lambda: configuration)
    coverage = contract()
    coverage["source_selection"] = {"mode": "specific", "sources": [
        {"type": "url", "url": "https://example.test/source"}, {"type": "url", "url": "https://example.test/other"}]}
    rows = [{"name": "fetch_url", "content": "observed content", "error": False}]
    main = ResearchAssignment(task_id="main-research", research_topic="Market facts")
    result = await quality.batch(main, coverage, rows, [evidence()])
    assert not result["accepted"] and result["deterministic_checks"]["required_source_count"] == 2
    with pytest.raises(RuntimeError, match="judge_reached"):
        await quality.batch(ResearchAssignment(research_topic="One fact"), coverage, rows, [evidence()])


@pytest.mark.asyncio
async def test_main_stage_retains_original_research_instructions():
    from agentscope.message import UserMsg
    from test_research_migration import FakeSupervisor
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
    original = "先读取 source_verification 技能，然后研究市场规模。"
    supervisor = FakeSupervisor()
    state = ResearchSnapshot(run_id="run", config_fingerprint="fixture",
                             messages=[UserMsg("user", original)], research_brief="研究市场规模",
                             coverage_contract=contract(original))
    await NativeResearchStages(Models(), supervisor, lambda: cfg()).research_supervisor(state)
    assert supervisor.original_messages == [original]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 26])
async def test_direct_native_loop_loads_skill_without_child_or_compression(version):
    models = Models({"researcher": [
        [tool_call("ReadSkill", "skill", key="source_verification")],
        [tool_call("web_research", "fetch")],
        [tool_call("ResearchComplete", "finish", summary="Supported finding [ev1].")],
    ]})
    from test_enforced_quality_execution import Quality
    options = cfg(quality_evaluation_min_sources=1, quality_evaluation_enabled=version is not None)
    if version is not None:
        options["metadata"]["run_config_schema_version"] = version
    flow = AdaptiveResearch(Researcher(models, lambda: options, research_tools, run_id="run",
                                       quality=Quality() if version is not None else None))
    findings, state = await flow.run("使用一个研究任务。\n研究市场规模。", contract("使用一个研究任务。\n研究市场规模。"))
    assert len(models.created) == 1
    assert models.created[0][1] == "main-research"
    assert models.text_calls == []
    assert findings[0]["evidence_registry"][0]["evidence_id"] == "ev1"
    assert findings[0]["compressed_research"] == "Supported finding [ev1]."
    assert state["middle_context"]["business_completion"]["action"] == "complete"
    calls = str(models.created[0][2].calls)
    assert all(tool["function"]["name"] != "ConductResearch"
               for call in models.created[0][2].calls for tool in call["tools"])
    assert "source_verification" in calls
    assert "先读取用户允许的原始资料" in calls
    authority = state["middle_context"].get("research_context_authority", {})
    if version == 26:
        loaded = authority["loaded_skills"]
        assert list(loaded) == ["source_verification"]
        assert loaded["source_verification"]["instructions"] == run_skills(options)["source_verification"]["researcher_context"]
        assert len(loaded["source_verification"]["version"]) == 64
        assert "Before the first source tool call" in flow.prompt
    else:
        assert "loaded_skills" not in authority


@pytest.mark.asyncio
async def test_main_delivery_updates_durable_progress_and_public_sources(cache):
    models = Models({"researcher": [[tool_call("web_research", "read")],
                                    [tool_call("ResearchComplete", "finish", summary="Evidence ev1 supports the finding.")]]})
    models.recovery = cache.recovery
    coverage = contract("研究市场规模。")
    flow = AdaptiveResearch(Researcher(models, lambda: cfg(), research_tools, run_id="efficiency"))
    await flow.run("研究市场规模。", coverage)
    progress = await cache.progress()
    task = progress["tasks"]["main-research"]
    assert task["status"] == "completed"
    assert task["evaluation_status"] == "skipped"
    assert task["requirements"]
    assert "ev1" in progress["admitted"]
    events = await cache.recovery.store.events("efficiency", "owner")
    assert any(row["payload"].get("public_type") == "research.source.discovered" for row in events)


@pytest.mark.asyncio
async def test_delegated_evidence_returns_to_main_for_followup(cache):
    coverage = contract("研究市场规模。")
    rid = next(row["requirement_id"] for row in coverage["requirements"] if row["kind"] == "factual")
    models = Models()
    models.recovery = cache.recovery
    main_script = [
        [tool_call("ConductResearch", "child", research_topic="Independent market evidence", requirement_ids=[rid], independence_reason="Separate primary dataset")],
        [tool_call("web_research", "followup")],
        [tool_call("ResearchComplete", "finish", summary="Both findings support the conclusion [ev1].")],
    ]

    def model_for(role, task_id):
        script = main_script if task_id == "main-research" else [[tool_call("web_research", "child-fetch")], [tool_call("ResearchComplete", "done")]]
        model = ScriptedModel(script)
        models.created.append((role, task_id, model))
        return model

    models.agent_model = model_for
    flow = AdaptiveResearch(Researcher(models, lambda: cfg(), research_tools, run_id="efficiency"))
    flow.original_messages = ["Original full request retained only by the main Agent."]
    findings, _ = await flow.run("研究市场规模。", coverage)
    agents = [row for row in models.created if row[0] == "researcher"]
    assert len(agents) == 2
    assert len(findings) == 2
    assert len(models.text_calls) == 1  # Only the isolated child compresses a handoff.
    assert "ev1" in str(agents[0][2].calls[1])
    assert flow.original_messages[0] in str(agents[0][2].calls)
    assert flow.original_messages[0] not in str(agents[1][2].calls)
    assert findings[1]["assessment"]["handoff"]["evaluation_status"] == "skipped"
    events = await cache.recovery.store.events("efficiency", "owner")
    source_events = [row["payload"]["public_payload"] for row in events
                     if row["payload"].get("public_type") == "research.source.discovered"]
    assert source_events and all(row["task_id"] == findings[1]["task_id"] for row in source_events)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ReadSkill", "ProposeOfficialSource"])
async def test_context_and_source_proposals_are_not_evaluated_as_evidence(name):
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime.research_agents import _Observations
    from open_deep_research.tools.base import ToolResult
    observations = _Observations()
    outcome = SimpleNamespace(error=None, result=ToolResult(output={"instructions": "verify sources"}),
                              message=SimpleNamespace(content=[]))
    await observations.capture(name, "control", outcome)
    assert not observations.pending
    assert not observations.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["ReadResearchEvidence", "fetch_url"])
async def test_evidence_rereads_do_not_trigger_another_research_quality_round(reader):
    import json
    from types import SimpleNamespace
    from test_research_migration import evidence
    from open_deep_research.agentscope_runtime.research_agents import _Observations
    from open_deep_research.tools.base import ToolResult

    evaluated = []

    async def assess(rows):
        evaluated.append([row["id"] for row in rows])
        return {"decision": "continue"}

    observations = _Observations(assess=assess)
    record = evidence()
    result = SimpleNamespace(error=None, result=ToolResult(output={"evidence": [record]}),
                             message=SimpleNamespace(content=[]))
    await observations.capture("fetch_url", "fetch", result)
    assert await observations.assess_pending() == {"decision": "continue"}
    payload = ({"evidence": [record]} if reader == "ReadResearchEvidence" else
               {"kind": "web_markdown", "evidence_eligible": False, "url": record["source_url"],
                "markdown": "Raw page text for navigation", "offset": 0})
    reread = SimpleNamespace(error=None, result=ToolResult(output=json.dumps(payload)),
                             message=SimpleNamespace(content=[]))
    for index in range(2):
        await observations.capture(reader, f"reread-{index}", reread)
        assert await observations.assess_pending() is None
    assert evaluated == [["fetch"]]
    assert set(observations.evidence) == {record["evidence_id"]}
    # Actual source-tool work still triggers a new quality round.
    await observations.capture("fetch_url", "next-fetch", result)
    await observations.assess_pending()
    assert evaluated == [["fetch"], ["next-fetch"]]


@pytest.mark.asyncio
async def test_parallel_delegation_respects_limit_and_cancels_children():
    from test_research_migration import Empty, evidence
    from open_deep_research.tools.base import ToolExecutionZone, ToolOrigin, ToolResult, build_tool
    from open_deep_research.quality.contract import CoverageRequirement, ResearchCoverageContract

    coverage = ResearchCoverageContract(original_query_sha256="fixture", requirements=tuple(
        CoverageRequirement(requirement_id=key, text=key, source_message_index=0, source_start=0, source_end=1)
        for key in ("one", "two", "three")
    )).model_dump(mode="json")
    models = Models()
    entered, finished = [], []
    waiting = asyncio.Event()
    main_script = [[tool_call("ConductResearch", key, research_topic=key, requirement_ids=[key],
                             independence_reason="Independent source set") for key in ("one", "two", "three")]]

    def model_for(role, task_id):
        model = ScriptedModel(main_script if task_id == "main-research" else [[tool_call("web_research", "read")]])
        models.created.append((role, task_id, model))
        return model

    async def tools(assignment):
        async def read(*args):
            entered.append(assignment.task_id)
            if len(entered) == 2:
                waiting.set()
            try:
                await asyncio.Event().wait()
                return ToolResult(output={"evidence": [evidence()]})
            finally:
                finished.append(assignment.task_id)
        return [build_tool(name="web_research", description="Read evidence", input_schema=Empty, call=read,
                           origin=ToolOrigin.SEARCH, execution_zone=ToolExecutionZone.HOST_CONTROL)]

    models.agent_model = model_for
    flow = AdaptiveResearch(Researcher(models, lambda: cfg(max_concurrent_research_units=2), tools, run_id="run"))
    task = asyncio.create_task(flow.run("Three independent sources", coverage))
    await asyncio.wait_for(waiting.wait(), 20)
    assert len(entered) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(finished) == set(entered)
    assert not flow.active

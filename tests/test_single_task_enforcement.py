"""User-requested single-task execution survives batches and recovery."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.agents import deep_researcher as graph
from open_deep_research.events.public import RunEventStore
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.run_context import RunContextStore
from open_deep_research.tools.governance import GovernedToolCallResult
from open_deep_research.tools.supervisor.common import (
    coverage_bound_input_schema,
    validate_requirement_ids,
)
from open_deep_research.tools.supervisor.conduct_research.definition import (
    ConductResearch,
)


@pytest.mark.parametrize("directive", [
    "采用单一研究任务。", "只创建一个实质研究任务。",
    "请用一个子研究任务完成研究。", "Use a single research task.",
])
def test_explicit_single_task_is_compiled(directive):
    contract = build_research_coverage_contract([HumanMessage(content=directive)])
    assert contract.single_research_task
    assert not contract.delegable_requirement_ids()


@pytest.mark.parametrize("directive", [
    "不采用单一研究任务。", "单一研究任务的优缺点是什么？",
    "请比较两个方案。", "Do not use a single research task.",
])
def test_other_requests_do_not_enable_single_task(directive):
    contract = build_research_coverage_contract([HumanMessage(content=directive)])
    assert not contract.single_research_task


def test_single_task_owns_all_requirements_beyond_normal_per_task_cap():
    contract = build_research_coverage_contract([HumanMessage(content=(
        "采用单一研究任务。核查默认状态。核查实验性状态。核查平台支持。核查安装方式。"
    ))])
    ids = list(contract.delegable_requirement_ids())
    assert len(ids) == 4
    assert validate_requirement_ids(ids[:1], contract, required=True) == ids
    schema = coverage_bound_input_schema(ConductResearch, contract)
    assert schema.model_validate({"research_topic": "全部问题", "requirement_ids": ids})
    normalized = graph._canonicalize_supervisor_tool_call_requirements([
        {"name": "ConductResearch", "id": "one", "args": {"requirement_ids": ids[:1]}},
    ], contract)
    assert normalized[0]["args"]["requirement_ids"] == ids


def test_single_slot_is_atomic_and_survives_store_recreation(tmp_path):
    def claim(call_id):
        return RunContextStore("single", runs_dir=str(tmp_path)).claim_single_research_task(call_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["first", "second"]))
    assert sum(results) == 1
    winner = ["first", "second"][results.index(True)]
    assert claim(winner)
    assert not claim("later-turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("single", [False, True])
async def test_scheduler_rejects_extra_tasks_before_events_or_execution(
    tmp_path, monkeypatch, async_mode, single,
):
    contract = build_research_coverage_contract([HumanMessage(content=(
        ("采用单一研究任务。" if single else "") + "核查默认状态。核查实验性状态。"
    ))])
    config = {
        "configurable": {"runs_dir": str(tmp_path), "search_api": "none",
                         "quality_evaluation_enabled": False, "observability_enabled": False,
                         "sandbox_enabled": False, "enable_async_research": async_mode},
        "metadata": {"run_id": "single-scheduler"},
    }
    executed = []

    async def execute(call, *args, **kwargs):
        executed.append(call)
        return GovernedToolCallResult(message=ToolMessage(content="started", tool_call_id=call["id"]))

    monkeypatch.setattr(graph, "execute_governed_tool_call", execute)
    name = "StartResearchTask" if async_mode else "ConductResearch"
    def call(call_id):
        return {"name": name, "id": call_id, "args": {
            "research_topic": "核查所有问题", "requirement_ids": [],
        }}

    state = {"coverage_contract": contract.model_dump(), "enable_async_research": async_mode,
             "research_iterations": 1, "research_brief": "核查所有问题"}
    for calls in [[call("first"), call("second")], [call("third")]]:
        state["supervisor_messages"] = [AIMessage(content="", tool_calls=calls)]
        command = await graph._execute_supervisor_tools(state, config)
        if single:
            assert any("single_research_task_limit" in str(m.content)
                       for m in command.update["supervisor_messages"])
    assert len(executed) == (1 if single else 3)
    if single:
        assert executed[0]["args"]["requirement_ids"] == list(contract.delegable_requirement_ids())
        events = RunEventStore("single-scheduler", runs_dir=str(tmp_path)).read()
        assert all(e.payload.get("task_id") not in {"second", "third"}
                   for e in events if e.type == "research.task.created")

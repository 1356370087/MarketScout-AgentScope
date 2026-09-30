"""User-requested single-task execution survives batches and recovery."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from agentscope.message import UserMsg, TextBlock
from open_deep_research.agentscope_runtime.research_agents import _Topic, Supervisor, ResearchHandoff
from open_deep_research.quality.contract import (
    build_research_coverage_contract, coverage_bound_input_schema, validate_requirement_ids,
)
from open_deep_research.run_context import RunContextStore
from tests.as_runtime.test_research_migration import Models, cfg, tool_call


@pytest.mark.parametrize("directive", [
    "采用单一研究任务。", "只创建一个实质研究任务。",
    "请用一个子研究任务完成研究。", "Use a single research task.",
])
def test_explicit_single_task_is_compiled(directive):
    contract = build_research_coverage_contract([UserMsg("user", directive)])
    assert contract.single_research_task
    assert not contract.delegable_requirement_ids()


@pytest.mark.parametrize("directive", [
    "不采用单一研究任务。", "单一研究任务的优缺点是什么？",
    "请比较两个方案。", "Do not use a single research task.",
])
def test_other_requests_do_not_enable_single_task(directive):
    contract = build_research_coverage_contract([UserMsg("user", directive)])
    assert not contract.single_research_task


def test_single_task_owns_all_requirements_beyond_normal_per_task_cap():
    contract = build_research_coverage_contract([UserMsg("user", (
        "采用单一研究任务。核查默认状态。核查实验性状态。核查平台支持。核查安装方式。"
    ))])
    ids = list(contract.delegable_requirement_ids())
    assert len(ids) == 4
    assert validate_requirement_ids(ids[:1], contract, required=True) == ids
    schema = coverage_bound_input_schema(_Topic, contract)
    assert schema.model_validate({"research_topic": "全部问题", "requirement_ids": ids})


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
async def test_scheduler_rejects_extra_tasks_before_execution(async_mode, single):
    contract = build_research_coverage_contract([UserMsg("user",
        ("采用单一研究任务。" if single else "") + "核查默认状态。核查实验性状态。")])
    executed = []

    class Worker:
        async def run(self, assignment, _contract, feedback):
            executed.append(assignment)
            return ResearchHandoff(**assignment.model_dump(), compressed_research="Evidence")

    name = "TaskCreate" if async_mode else "ConductResearch"
    def call(identifier):
        return tool_call(name, identifier, research_topic="核查所有问题", requirement_ids=[])

    models = Models({"supervisor": [[call("first"), call("second")], [call("third")],
                                      [TextBlock(text="done")]]})
    results, state = await Supervisor(models, lambda: cfg(enable_async_research=async_mode,
        async_research_mode="collaborator"), Worker(), run_id="single-scheduler").run(
            "核查所有问题", contract.model_dump(mode="json"))
    assert len(executed) == (1 if single else 3)
    assert len(results) == len(executed)
    if single:
        assert executed[0].requirement_ids == list(contract.delegable_requirement_ids())
        assert "single research task" in str(state["context"])

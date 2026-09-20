"""Native contexts retain bounded tool traces in published evaluation snapshots."""

import json

import pytest
from agentscope.message import AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState

from open_deep_research.agentscope_runtime.report import NativeReportWriter
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.evaluation.snapshot import build_evaluation_snapshot
from tests.as_runtime.test_report_native import Factory, state, config
from tests.supervisor_parallel_evaluation import right_parallelism_evaluator


def trace(name="fetch_url"):
    return AssistantMsg("researcher", [
        ToolCallBlock(id="one", name=name, input=json.dumps({"query": "evidence", "token": "PRIVATE_TOKEN"})),
        ToolResultBlock(id="one", name=name, output="api_key=PRIVATE_RESULT", state=ToolResultState.SUCCESS),
    ])


def test_native_object_and_checkpoint_traces_are_redacted_and_keep_identity():
    snapshot = build_evaluation_snapshot({
        "supervisor_messages": [trace("ConductResearch")],
        "completed_task_outputs": [{"task_id": "task-a", "agent_state": {"context": [trace().model_dump(mode="json")]}}],
    })
    assert snapshot.tool_trace.supervisor_tool_calls[0].name == "ConductResearch"
    call = snapshot.tool_trace.researcher_tool_calls[0]
    assert (call.task_id, call.name, call.id) == ("task-a", "fetch_url", "one")
    assert snapshot.tool_trace.researcher_tool_results[0].status == "success"
    assert snapshot.tool_trace.availability.researcher_tool_names_retained is True
    assert "PRIVATE" not in snapshot.model_dump_json()
    assert "offloaded history is not expanded" in snapshot.tool_trace.scope_note


@pytest.mark.asyncio
async def test_report_product_keeps_native_trace_after_notes_are_cleared(tmp_path):
    snapshot = state()
    snapshot.agent_states["supervisor"] = {"context": [trace("ConductResearch").model_dump(mode="json")]}
    snapshot.findings[0]["task_id"] = "task-a"
    snapshot.findings[0]["agent_state"] = {"context": [trace().model_dump(mode="json")]}
    await NativeReportWriter(ResearchModels(Factory()))(snapshot, config(tmp_path))
    retained = snapshot.report_product["evaluation_snapshot"]["tool_trace"]
    assert retained["supervisor_tool_calls"][0]["name"] == "ConductResearch"
    assert retained["researcher_tool_calls"][0]["name"] == "fetch_url"


def test_first_wave_parallelism_stops_before_later_coalesced_rounds():
    message = AssistantMsg("supervisor", [
        ToolCallBlock(id="one", name="ConductResearch", input="{}"),
        ToolCallBlock(id="two", name="ConductResearch", input="{}"),
        ToolResultBlock(id="one", name="ConductResearch", output="done", state=ToolResultState.SUCCESS),
        ToolResultBlock(id="two", name="ConductResearch", output="done", state=ToolResultState.SUCCESS),
        ToolCallBlock(id="three", name="ConductResearch", input="{}"),
    ])
    assert right_parallelism_evaluator({"supervisor_messages": [message]}, {"parallel": 2})["score"] is True

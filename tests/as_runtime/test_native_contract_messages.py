"""Coverage IDs and source boundaries come from the same native user text."""

import pytest
from agentscope.message import AssistantMsg, UserMsg

from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.quality.gate import NativeQualityRuntimeMissing, evaluate_tool_results


def test_native_messages_compile_identical_contract_to_http_messages():
    content = "仅使用 https://example.org/source，分析市场规模和竞争情况。"
    native = build_research_coverage_contract([
        AssistantMsg("assistant", "Discard this invented requirement"), UserMsg("user", content),
    ])
    http = build_research_coverage_contract([
        {"role": "assistant", "content": "Discard this invented requirement"}, {"role": "user", "content": content},
    ])
    assert native.model_dump(mode="json") == http.model_dump(mode="json")
    assert native.requirements


@pytest.mark.asyncio
async def test_unbound_quality_evaluator_cannot_fail_open():
    with pytest.raises(NativeQualityRuntimeMissing, match="native_quality_evaluator_required"):
        await evaluate_tool_results("Research", [{"name": "fetch_url", "output": "https://example.org/source"}],
            {"configurable": {"quality_evaluation_enabled": True, "quality_evaluation_fail_open": True}})

"""Coverage IDs and source boundaries come from the same native user text."""

import pytest
from agentscope.message import AssistantMsg, UserMsg

from open_deep_research.quality.contract import build_research_coverage_contract, classify_requirement_kind
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


def test_live_single_url_directives_do_not_require_external_factual_evidence():
    contract = build_research_coverage_contract([UserMsg("user",
        "只用一个研究任务。仅直接读取指定 URL，说明 Bucardo 是哪类项目，保留该网页引用。不得搜索或访问其他网站。"
    )])
    factual = [r for r in contract.requirements if r.requirement_id in contract.delegable_requirement_ids()]
    assert [r.text for r in factual] == ["Bucardo 是哪类项目"]
    kinds = {r.text: r.kind for r in contract.requirements}
    assert kinds["仅直接读取指定 URL"] == "process"
    assert kinds["不得搜索或访问其他网站"] == "process"
    assert kinds["保留该网页引用"] == "deliverable"


@pytest.mark.parametrize("text", [
    "说明某网站禁止访问其他网站的原因", "保留网页引用会影响检索准确率吗", "指定 URL 的发布日期是什么",
])
def test_questions_about_sources_remain_factual(text):
    assert classify_requirement_kind(text) == "factual"

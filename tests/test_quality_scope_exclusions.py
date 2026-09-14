"""Excluding a research topic must not create positive evidence requirements."""
import pytest
from langchain_core.messages import HumanMessage

from open_deep_research.quality.contract import (
    build_research_coverage_contract,
    classify_requirement_kind,
    is_delegable_requirement,
)


@pytest.mark.parametrize("exclusion", [
    "不研究发布日期、性能数字、后续版本或其他问题",
    "不要讨论发布日期、性能数字和后续版本",
    "无需研究发布日期、性能数字",
    "不扩展到发布日期、性能数字以及后续版本",
    "Do not discuss release dates, performance figures, or later versions",
])
def test_exclusion_list_keeps_negation_and_needs_no_web_evidence(exclusion):
    prompt = f"核查 free-threaded 的默认状态。{exclusion}。"
    contract = build_research_coverage_contract([HumanMessage(content=prompt)])
    exclusions = [r for r in contract.requirements if r.text == exclusion]
    assert len(exclusions) == 1
    assert exclusions[0].kind == "process"
    assert not is_delegable_requirement(exclusions[0])
    assert prompt[exclusions[0].source_start:exclusions[0].source_end] == exclusion
    assert len([r for r in contract.requirements if is_delegable_requirement(r)]) == 1


@pytest.mark.parametrize("separator", ["，", ",", "。"])
def test_exclusion_does_not_swallow_adjacent_positive_requirements(separator):
    prompt = f"核查默认状态{separator}不研究发布日期、性能数字，比较实验性状态。"
    contract = build_research_coverage_contract([HumanMessage(content=prompt)])
    factual = [r.text for r in contract.requirements if is_delegable_requirement(r)]
    assert factual == ["核查默认状态", "实验性状态"]
    assert any(r.text == "不研究发布日期、性能数字" and r.kind == "process"
               for r in contract.requirements)


def test_negative_factual_claim_is_still_researchable():
    assert classify_requirement_kind("不支持自由线程的平台有哪些") == "factual"
    assert classify_requirement_kind("研究不支持自由线程的平台") == "factual"

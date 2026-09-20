"""User-bounded corpora must remain usable without changing the global floor."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from open_deep_research.agentscope_runtime.research_agents import Researcher, Supervisor
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.evidence import required_source_count, source_scoped_evidence_records
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.quality.gate import HandoffAssessment, RequirementCoverage, deterministic_handoff_checks, deterministic_tool_checks
from open_deep_research.tools.base import ToolExecutionZone, ToolOrigin, ToolResult, build_tool
from tests.as_runtime.test_research_migration import Models, cfg, evidence, tool_call
from tests.as_runtime.test_research_quality import Judge


def source_contract(selection):
    return build_research_coverage_contract([{"role": "user", "content": "说明目标响应时间"}]).model_copy(
        update={"source_selection": SourceSelection.model_validate(selection)}
    ).model_dump(mode="json")


@pytest.mark.parametrize("selection,expected", [
    ({"mode": "specific", "sources": [{"type": "url", "url": "https://example.test/source"}]}, 1),
    ({"mode": "specific", "sources": [{"type": "url", "url": "https://example.test/one"},
                                       {"type": "url", "url": "https://example.test/two"}]}, 2),
    ({"mode": "documents", "sources": [{"type": "document", "id": "d1"}, {"type": "document", "id": "d2"}]}, 2),
    ({"mode": "specific", "sources": [{"type": "domain", "domain": "example.test"}]}, 3),
    ({"mode": "hybrid", "sources": [{"type": "document", "id": "d1"}]}, 3),
    ({"mode": "web"}, 3),
])
def test_corpus_diversity_is_derived_from_authorized_scope(selection, expected):
    contract = source_contract(selection)
    assert required_source_count(3, contract) == expected
    assert SourceSelection.model_validate(contract["source_selection"]).model_dump(mode="json") == SourceSelection.model_validate(selection).model_dump(mode="json")


@pytest.mark.parametrize("selection,snapshots", [
    ({"mode": "specific", "sources": [{"type": "url", "url": "https://example.test/source"}]}, []),
    ({"mode": "documents", "sources": [{"type": "document", "id": "d1"}]}, [{"id": "d1"}]),
    ({"mode": "documents", "sources": [{"type": "knowledge_base", "id": "kb"}]}, [{"id": "d1"}]),
])
def test_frozen_selection_survives_contract_checkpoint_restore(selection, snapshots):
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages

    contract = build_research_coverage_contract([{"role": "user", "content": "说明目标响应时间"}])
    state = ResearchSnapshot(run_id="source-restore", config_fingerprint="frozen",
                             coverage_contract=contract.model_dump(mode="json"),
                             application={"selected_source_snapshots": snapshots})
    stages = NativeResearchStages(None, None, lambda: {"metadata": {"source_selection": selection}})
    stages._bind_source_selection(state)
    restored = ResearchSnapshot.model_validate_json(state.model_dump_json())
    stages._bind_source_selection(restored)
    assert restored.coverage_contract == state.coverage_contract
    assert restored.coverage_contract["requirements"] == contract.model_dump(mode="json")["requirements"]
    assert required_source_count(3, restored.coverage_contract) == 1


def test_exact_selection_rejects_other_page_and_foreign_document():
    record = evidence()
    contract = source_contract({"mode": "specific", "sources": [{"type": "url", "url": record["source_url"]}]})
    admitted = source_scoped_evidence_records([record], contract)
    assert len(admitted) == 1 and admitted[0]["source_kind"] == "explicit_url"
    assert source_scoped_evidence_records([{**record, "source_url": "https://example.test/other"}], contract) == []
    contract = source_contract({"mode": "documents", "sources": [{"type": "document", "id": "d1"}]})
    local = {**record, "source_type": "local_document", "document_id": "d1", "source_url": "/documents/d1?chunk=a"}
    assert len(source_scoped_evidence_records([local], contract)) == 1
    assert source_scoped_evidence_records([record, {**local, "document_id": "d2", "source_url": "/documents/d2?chunk=a"}], contract) == []


def test_chinese_citation_punctuation_does_not_become_an_out_of_scope_url():
    record = evidence()
    contract = source_contract({"mode": "specific", "sources": [{"type": "url", "url": record["source_url"]}]})
    handoff = {"compressed_research": "研究发现及其限制。" * 30 + record["source_url"] + "（官方资料）。",
               "evidence_registry": [record]}
    checks = deterministic_handoff_checks(handoff, min_sources=3, coverage_contract=contract)
    assert checks["passed"] and checks["out_of_scope_source_count"] == 0
    handoff["compressed_research"] += " https://example.test/other（越界资料）。"
    checks = deterministic_handoff_checks(handoff, min_sources=3, coverage_contract=contract)
    assert "handoff_contains_out_of_scope_source_url" in checks["failures"]


def test_exact_url_fetch_without_evidence_still_fails_the_source_requirement():
    contract = source_contract({"mode": "specific", "sources": [{"type": "url", "url": "https://example.test/source"}]})
    checks = deterministic_tool_checks(
        [{"name": "fetch_url", "content": '{"documents": [], "evidence": []}'}],
        min_sources=3, coverage_contract=contract,
    )
    assert checks["required_source_count"] == 1
    assert checks["source_count"] == 0
    assert checks["failures"] == ["insufficient_traceable_sources"]


@pytest.mark.asyncio
async def test_supervisor_counts_pages_consistently_with_handoff_gate():
    from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff

    records = [{**evidence(), "evidence_id": f"ev{i}", "document_id": f"page{i}",
                "source_url": f"https://example.test/page{i}", "source_type": "web"} for i in range(3)]
    class Worker:
        async def run(self, assignment, contract, feedback):
            return ResearchHandoff(**assignment.model_dump(), compressed_research="已核实三页原始资料。" * 30,
                                   evidence_registry=records)

    models = Models({"supervisor": [[tool_call("ConductResearch", research_topic="目标响应时间")],
                                   [tool_call("ResearchComplete", "finished")]]})
    supervisor = Supervisor(models, lambda: cfg(quality_evaluation_min_sources=3), Worker(),
                            run_id="run", completion_policy=True)
    handoffs, state = await supervisor.run("目标响应时间", source_contract({"mode": "web"}))
    assert deterministic_handoff_checks(handoffs[0], min_sources=3)["source_count"] == 3
    assert state["middle_context"]["business_completion"]["action"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["documents", "specific"])
async def test_real_document_or_exact_url_reaches_handoff_and_completion(mode):
    if mode == "documents":
        payload = json.loads((Path(__file__).parents[1] / "fixtures/document-research-tool-result.json").read_text(encoding="utf8"))
        record = payload["evidence"][0]
        selection = {"mode": mode, "sources": [{"type": "document", "id": record["document_id"]}]}
        tool_name = "search_documents"
    else:
        record = evidence()
        payload = {"documents": [{"source_url": record["source_url"], "document_id": record["document_id"]}], "evidence": [record]}
        selection = {"mode": mode, "sources": [{"type": "url", "url": record["source_url"]}]}
        tool_name = "fetch_url"
    contract = source_contract(selection)
    config = cfg(quality_evaluation_enabled=True, quality_evaluation_min_sources=3)
    config["metadata"]["source_selection"] = selection
    calls = []

    class Input(BaseModel):
        pass

    async def search(input, context, progress):
        calls.append(tool_name)
        return ToolResult(output=payload)

    async def tools(assignment):
        return [build_tool(name=tool_name, input_schema=Input, description="fixture", call=search,
                           origin=ToolOrigin.LOCAL_DOCUMENT if mode == "documents" else ToolOrigin.SYSTEM,
                           execution_zone=ToolExecutionZone.HOST_CONTROL)]

    class Model(Models):
        async def structured(self, role, prompt, schema, state, *, messages=None):
            result = await Judge().structured(role, prompt, schema, state, messages=messages)
            if schema is HandoffAssessment:
                result.requirement_coverage = [RequirementCoverage(
                    **{**item.model_dump(), "evidence_ids": [record["evidence_id"]]}
                ) for item in result.requirement_coverage]
            return result

    models = Model({
        "researcher": [[tool_call(tool_name)], [tool_call("ResearchComplete", "done")]],
        "supervisor": [[tool_call("ConductResearch", research_topic="说明目标响应时间")],
                       [tool_call("ResearchComplete", "finished")]],
    }, text="已找到目标响应时间及其适用范围，结论仅针对用户选定资料，不外推为普遍性能。" * 8)
    quality = NativeResearchQuality(models, lambda: config)
    researcher = Researcher(models, lambda: config, tools, run_id="run", quality=quality)
    supervisor = Supervisor(models, lambda: config, researcher, run_id="run", quality=quality, completion_policy=True)
    handoffs, state = await supervisor.run("说明目标响应时间", contract)
    assert calls == [tool_name]
    assert config["configurable"]["quality_evaluation_min_sources"] == 3
    assert handoffs[0]["termination"] == "research_complete"
    assert handoffs[0]["assessment"]["handoff"]["accepted"]
    assert handoffs[0]["evidence_registry"][0]["locator"] == record["locator"]
    checks = deterministic_handoff_checks(handoffs[0], min_sources=3, coverage_contract=contract)
    assert checks["passed"] and checks["source_count"] == 1 and checks["required_source_count"] == 1
    assert state["middle_context"]["business_completion"]["action"] == "complete"
    from open_deep_research.evaluation.snapshot import build_evaluation_snapshot

    snapshot = build_evaluation_snapshot({"evidence_registry": handoffs[0]["evidence_registry"]})
    assert snapshot.evidence_registry[0].locator == record["locator"]

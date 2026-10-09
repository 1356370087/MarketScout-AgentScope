"""Regressions for the persisted October 8 E2E failures, without live models."""

from types import SimpleNamespace

import pytest
import asyncio
import time

from open_deep_research.agentscope_runtime.efficiency import handoff_prompt
from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff, _citation_identity
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.quality.contract import build_research_coverage_contract


class Counter:
    context_size = 64000

    async def count_tokens(self, messages, tools):
        return sum(len(message.get_text_content()) // 3 + 1 for message in messages)


def evidence():
    return {"evidence_id": "ev1", "claim": "The server supports a standalone deployment.",
            "supporting_excerpt": "Standalone deployment. [Other documentation](https://unread.example/docs)",
            "source_url": "https://official.example/deploy", "document_id": "doc1", "chunk_id": "chunk1",
            "locator": "Deployment", "security_status": "accepted"}


@pytest.mark.asyncio
async def test_bounded_compression_does_not_promote_navigation_links():
    row = evidence()
    before = dict(row)
    prompt = await handoff_prompt(SimpleNamespace(agent_model=lambda *_: Counter()),
        SimpleNamespace(task_id="t", requirement_ids=[]), {"requirements": []}, [row], [], {})
    assert "https://unread.example/docs" not in prompt
    assert "https://official.example/deploy" in prompt
    assert "ev1" in prompt
    assert row == before


@pytest.mark.parametrize("url", [
    "https://OFFICIAL.example:443/deploy/#install",
    "https://official.example/deploy/?utm_source=docs",
    "https://official.example/deploy/).",
])
def test_registered_source_aliases_have_one_citation_identity(url):
    assert _citation_identity(url) == "https://official.example/deploy"
    assert _citation_identity("https://official.example.evil/deploy") != _citation_identity(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("version,expected_calls", [("quality-gate-v5", 1), ("quality-gate-v4", 2)])
async def test_semantic_rejection_retains_coverage_instead_of_becoming_protocol_failure(version, expected_calls):
    # Run 914d985e: accepted_contains_structured_gap was repeated until an
    # otherwise usable handoff became quality_evaluator_unavailable.
    contract = build_research_coverage_contract([{"role": "user", "content": "Explain deployment forms."}])
    rid = contract.delegable_requirement_ids()[0]

    class Judge:
        calls = 0

        async def structured(self, role, prompt, schema, state, *, messages):
            self.calls += 1
            return schema(accepted=True, relevance=5, source_quality=5, evidence_coverage=5, groundedness=5,
                reason="One deployment form is supported; the other remains a factual gap.",
                requirement_coverage=[{"requirement_id": rid, "status": "partial", "evidence_ids": ["ev1"]}],
                gaps=[{"requirement_id": rid, "kind": "factual", "reason": "Second deployment form missing",
                       "checked_evidence_ids": ["ev1"], "next_query": "distributed deployment official documentation"}])

    model = Judge()
    config = {"metadata": {"quality_policy_version": version}, "configurable": {
        "quality_evaluation_enabled": True, "quality_evaluation_min_sources": 1,
        "quality_evaluation_fail_open": False, "max_structured_output_retries": 2}}
    outcome = ResearchHandoff(task_id="t", research_topic="Deployment forms", requirement_ids=[rid],
        compressed_research="Standalone deployment is supported [ev1].", evidence_registry=[evidence()])
    result = await NativeResearchQuality(model, lambda: config).handoff(outcome, contract.model_dump(mode="json"))
    assert model.calls == expected_calls
    assert not result.accepted
    if version == "quality-gate-v5":
        assert result.evaluator_error is None
        assert result.protocol_repair_count == 0
        assert result.requirement_coverage[0].status.value == "partial"
        assert result.gaps[0].checked_evidence_ids == ["ev1"]
        assert "unresolved_requirement_gap:" + rid in result.hard_rejection_reasons
    else:
        assert "accepted_contains_structured_gap" in result.evaluator_error


@pytest.mark.asyncio
@pytest.mark.parametrize("has_limit_evidence", [True, False])
async def test_handoff_carries_uncited_support_and_keeps_real_missing_facts_blocking(has_limit_evidence):
    from open_deep_research.quality.context import context_payload
    from open_deep_research.quality.gate import HANDOFF_EVIDENCE_COVERAGE_RULES

    contract = build_research_coverage_contract([{"role": "user", "content": "Explain standalone deployment limits."}])
    rid = contract.delegable_requirement_ids()[0]
    records = [evidence()]
    if has_limit_evidence:
        records.append({**evidence(), "evidence_id": "ev-limit", "claim": "Standalone mode has no failover.",
                        "supporting_excerpt": "Standalone mode has no failover."})

    class Judge:
        async def structured(self, role, prompt, schema, state, *, messages):
            assert HANDOFF_EVIDENCE_COVERAGE_RULES in messages[0].get_text_content()
            payload = context_payload(messages[1].get_text_content())
            included = {row["evidence_id"] for row in payload["evidence_registry"]}
            assert ("ev-limit" in included) is has_limit_evidence
            assert "ev-limit" not in payload["compressed_research"]
            return schema(accepted=has_limit_evidence,
                admission_status="accepted_with_caveats" if has_limit_evidence else "rejected",
                relevance=5, source_quality=5, evidence_coverage=5 if has_limit_evidence else 2, groundedness=5,
                requirement_coverage=[{"requirement_id": rid, "status": "supported" if has_limit_evidence else "partial",
                                       "evidence_ids": sorted(included)}],
                gaps=[{"requirement_id": rid, "kind": "advisory" if has_limit_evidence else "factual",
                       "reason": "Writer must include the evidenced limit." if has_limit_evidence else "The limit is not evidenced.",
                       "checked_evidence_ids": sorted(included), "next_query": "" if has_limit_evidence else "deployment limits"}],
                reason="Assess the registry independently of summary omissions.")

    config = {"metadata": {"quality_policy_version": "quality-gate-v5"}, "configurable": {
        "quality_evaluation_enabled": True, "quality_evaluation_min_sources": 1, "quality_evaluation_fail_open": False}}
    outcome = ResearchHandoff(task_id="t", research_topic="Deployment", requirement_ids=[rid],
        compressed_research="The official deployment guide describes a standalone mode [ev1]. This is the documented deployment form; unsupported performance comparisons are not asserted. The associated structured evidence records accompany this summary for further report synthesis.", evidence_registry=records)
    result = await NativeResearchQuality(Judge(), lambda: config).handoff(outcome, contract.model_dump(mode="json"))
    assert result.accepted is has_limit_evidence, str((result.hard_rejection_reasons, result.deterministic_checks, result.evaluator_error))
    assert result.evaluator_error is None
    if has_limit_evidence:
        assert result.caveats
        assert not result.follow_up_tasks
    else:
        assert "unresolved_requirement_gap:" + rid in result.hard_rejection_reasons


@pytest.mark.parametrize("review,enabled,scope,expected", [
    ({"decision": "revise", "status": "degraded", "degraded": True}, True, "delivered_report", "degraded"),
    ({"decision": "fail", "status": "completed"}, True, "delivered_report", "failed"),
    ({}, False, "delivered_report", "unreviewed"),
    ({"decision": "pass", "status": "completed"}, True, "review_draft_diagnostic", "diagnostic"),
    ({"decision": "pass", "status": "failed"}, True, "delivered_report", "degraded"),
    ({"decision": "pass", "status": "completed"}, True, "delivered_report", "passed"),
])
def test_offline_high_scores_preserve_actual_delivery_gate(review, enabled, scope, expected):
    from open_deep_research.evaluation.legacy_cli import apply_quality_assessment

    result = {"status": "success", "final_report": "Original report.", "aggregate_score": .95,
        "configuration": {"report_review_enabled": enabled}, "evaluation_scope": scope,
        "run_result": {"status": "success", "report_review": review},
        "metrics": [{"key": key, "score": .95, "status": "scored"} for key in (
            "source_quality_score", "source_authority_score", "groundedness_score",
            "citation_accuracy_score", "completeness_score", "judge_consistency_score")]}
    apply_quality_assessment(result)
    assert result["offline_quality_gate_passed"]
    assert result["quality_gate"]["status"] == expected
    assert result["quality_gate_passed"] is (expected == "passed")
    assert result["aggregate_score"] == .95
    assert result["status"] == "success"
    assert result["final_report"] == "Original report."


def test_native_evaluation_keeps_reviewer_provenance_with_nullable_gate():
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.evaluation.local_runtime import evaluation_state

    snapshot = ResearchSnapshot(run_id="run", config_fingerprint="frozen", status="completed")
    snapshot.report_product = {"final_report": "Report", "quality_gate": None,
        "report_review": {"decision": "revise", "status": "degraded", "degraded": True,
                          "skipped": False, "draft_sha256": "unchanged"}}
    state = evaluation_state(snapshot)
    assert state["result"]["report_review"]["decision"] == "revise"
    assert state["result"]["report_review"]["draft_sha256"] == "unchanged"


def test_owned_documentation_and_linked_repository_are_not_competing_identities():
    from open_deep_research.agentscope_runtime.source_planning import SourceVerification, verified_entry

    page = "Atlas is a database. Built by Atlas. [Docs](https://atlas.example/docs). Source: [Atlas repository](https://code.example/atlas)."
    verdict = SourceVerification(entity_quote="Atlas is a database.", ownership_quote="Built by Atlas.",
        documentation_urls=["https://atlas.example/docs"],
        competing_websites=["https://atlas.example/docs/", "https://code.example/atlas"],
        related_websites=[{"url": "https://code.example/atlas", "relationship_quote":
                           "Source: [Atlas repository](https://code.example/atlas)."}])
    entry = verified_entry("Atlas", "https://atlas.example", page, verdict,
                           discovered_urls=["https://atlas.example/docs"])
    assert entry["status"] == "verified"
    assert entry["competing_websites"] == []
    assert entry["documentation_urls"] == ["https://atlas.example/docs"]
    # A claimed relationship cannot remove ambiguity without its actual PAGE quote.
    verdict.related_websites[0].relationship_quote = "Invented ownership proof."
    ambiguous = verified_entry("Atlas", "https://atlas.example", page, verdict)
    assert ambiguous["status"] == "needs_confirmation"
    assert ambiguous["competing_websites"] == ["https://code.example/atlas"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_incomplete_reviewer_issue_uses_protocol_repair_not_content_revision(monkeypatch, repaired):
    from open_deep_research.report import reviewer

    contract = build_research_coverage_contract([{"role": "user", "content": "Explain deployment forms."}])
    rid = contract.delegable_requirement_ids()[0]
    calls = []
    incomplete = {"category": "citation_correctness", "severity": "medium", "description": "来源 [14"}

    async def invoke(payload, config, cfg, *, attempt):
        calls.append(payload)
        issue = ({**incomplete, "description": "The deployment claim needs its inline source.",
                  "location": "Deployment paragraph", "evidence_ids": ["ev1"],
                  "revision_instruction": "Move the existing deployment citation beside the claim."}
                 if repaired and len(calls) == 2 else incomplete)
        return {"decision": "revise", "dimensions": {key: .95 for key in reviewer._DIMENSIONS},
                "coverage": [{"requirement_id": rid, "status": "covered"}],
                "citation_audit": [{"claim": "Standalone deployment", "citation_target": "https://official.example/deploy",
                                    "supported": True, "evidence_ids": ["ev1"]}], "issues": [issue]}

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    state = {"evidence_registry": [evidence()], "coverage_contract": contract.model_dump(mode="json")}
    result = await reviewer.review_report("Standalone deployment [source](https://official.example/deploy).", state,
        {"metadata": {"quality_policy_version": "quality-gate-v5"}, "configurable": {"max_structured_output_retries": 3}})
    assert len(calls) == 2
    assert "review_issue_not_actionable" in calls[1]["review_protocol_feedback"]["errors"]
    if repaired:
        assert result.decision == "revise"
        assert "review_issue_not_actionable" not in result.deterministic_failures
    else:
        assert result.decision == "fail"
        assert "review_issue_not_actionable" in result.deterministic_failures


@pytest.mark.asyncio
async def test_failed_bing_retry_preserves_prior_filters_and_every_http_request():
    import httpx
    from open_deep_research.agentscope_runtime.search_providers import SearchResources, SearchService
    from open_deep_research.web.models import SearchRequest

    requests, events = [], []

    def response(request):
        requests.append(request)
        if request.url.host == "cn.bing.com":
            return httpx.Response(503)
        if request.url.params["q"].endswith("documentation"):
            return httpx.Response(302, headers={"Location": str(request.url).replace("www.bing.com", "cn.bing.com")})
        return httpx.Response(200, text='<li class="b_algo"><h2><a href="https://outside.example/a">Outside scope</a></h2></li>')

    class Tavily:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://official.example/a", "title": "Atlas docs"}]}

    resources = SearchResources({"bing": lambda _: httpx.AsyncClient(transport=httpx.MockTransport(response)),
                                 "tavily": lambda _: Tavily()})

    async def progress(phase, **payload):
        events.append((phase, payload))

    try:
        service = SearchService({"configurable": {"search_providers": ["tavily", "bing"]}}, None, resources, progress=progress)
        result = await service.discover(SearchRequest(objective="Atlas", queries=["Atlas hybrid query fusion RRF"],
            allowed_domains=["official.example"]))
        bing = next(row for row in result.provider_results if row.provider == "bing")
        assert len(requests) == bing.provider_requests == 3
        assert result.search_calls == 4  # Three Bing HTTP requests plus Tavily.
        assert bing.raw_result_count == bing.filtered_result_count == 1
        assert bing.parsed_result_count == 1
        assert bing.status == "failed" and bing.result_status == "failed"
        assert bing.query_outcomes[0]["http_status"] == 503
        assert len(bing.query_outcomes[0]["attempted_queries"]) == 2
        assert result.candidates[0].canonical_url == "https://official.example/a"
        failure = next(payload for phase, payload in events if phase == "provider_failed")
        assert failure["provider_requests"] == 3 and failure["search_phase"] == "research"
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_source_discovery_candidates_have_preparation_provenance():
    from open_deep_research.agentscope_runtime.search_providers import SearchResources, SearchService
    from open_deep_research.web.models import SearchRequest

    class Tavily:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://candidate.example/a", "title": "Atlas docs"}]}

    events = []

    async def progress(phase, **payload):
        events.append(payload)

    resources = SearchResources({"tavily": lambda _: Tavily()})
    service = SearchService({"metadata": {"task_id": "source-planning"},
        "configurable": {"search_providers": ["tavily"]}}, None, resources, progress=progress)
    result = await service.discover(SearchRequest(objective="Atlas", queries=["Atlas official website"]))
    assert result.search_phase == "preparation"
    assert result.provider_results[0].search_phase == "preparation"
    assert all(row["search_phase"] == "preparation" for row in events)


def test_comparison_facets_cannot_hide_one_products_missing_dependency():
    from open_deep_research.quality.planning import compile_planned_requirements
    from open_deep_research.quality.contract import aggregate_dimension_coverage
    from open_deep_research.state import PlannedRequirement, ResearchEntity
    from open_deep_research.quality.context import research_context_xml
    from xml.etree.ElementTree import fromstring

    axes = [
        ("（1）稠密与稀疏向量混合检索及结果融合方式", ["稠密与稀疏向量混合检索", "结果融合方式"]),
        ("（2）元数据过滤与过滤索引机制", ["元数据过滤", "过滤索引机制"]),
        ("（3）多租户的数据隔离方式、适用限制及权限隔离边界", ["多租户的数据隔离方式", "适用限制", "权限隔离边界"]),
        ("（4）自托管部署形态和主要运维依赖", ["自托管部署形态", "主要运维依赖"]),
    ]
    question = "比较 Qdrant 与 Milvus 的技术选型。分别核实：" + "；".join(text for text, _ in axes) + "。报告使用中文，约1800字。"
    atoms = [PlannedRequirement(source_text=text, kind="factual", facets=facets) for text, facets in axes]
    entities = [ResearchEntity(name="Milvus"), ResearchEntity(name="Qdrant")]
    contract = compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True, entities=entities)
    facts = [row for row in contract.requirements if row.kind == "factual"]
    assert contract.schema_version == 4
    assert len(facts) == 18 and len(contract.dimensions) == 4
    assert all(question[row.source_start:row.source_end] == row.text for row in facts)
    assert {row.subject for row in facts} == {"Qdrant", "Milvus"}
    missing = next(row.requirement_id for row in facts if row.subject == "Qdrant" and row.text == "主要运维依赖")
    ledger = {row.requirement_id: {"status": "supported"} for row in facts if row.requirement_id != missing}
    coverage = aggregate_dimension_coverage(contract, ledger)
    assert sum(row.status.value == "supported" for row in coverage) == 3
    assert missing in coverage[-1].missing_requirement_ids
    xml = fromstring(research_context_xml(contract))
    assert xml.find(f"research_requirements/requirement[@id='{missing}']").attrib["subject"] == "Qdrant"
    assert contract == compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True, entities=reversed(entities))
    legacy = compile_planned_requirements([{"role": "user", "content": question}], atoms, entities=entities)
    assert legacy.schema_version == 3 and len(legacy.delegable_requirement_ids()) == 4


def test_incomplete_model_facets_preserve_the_unsplit_original_obligation():
    from open_deep_research.quality.planning import compile_planned_requirements
    from open_deep_research.state import PlannedRequirement

    text = "（1）部署形态和主要运维依赖；（2）权限隔离边界。"
    result = compile_planned_requirements([{"role": "user", "content": text}], [
        PlannedRequirement(source_text="（1）部署形态和主要运维依赖", kind="factual", facets=["部署形态", "模型新增的性能测试"]),
    ], atomic=True)
    assert any(row.text == "（1）部署形态和主要运维依赖" for row in result.requirements)
    assert all("模型新增" not in row.text for row in result.requirements)


def test_unnumbered_facets_keep_the_original_parent_qualifiers():
    from open_deep_research.quality.planning import compile_planned_requirements
    from open_deep_research.state import PlannedRequirement

    text = "仅针对版本 2.5 分析部署形态和运维依赖"
    result = compile_planned_requirements([{"role": "user", "content": text}], [
        PlannedRequirement(source_text=text, kind="factual", facets=["部署形态", "运维依赖"]),
    ], atomic=True)
    assert len(result.dimensions) == 1
    assert result.dimensions[0].text == text
    assert all(row.dimension_id == result.dimensions[0].dimension_id for row in result.requirements if row.kind == "factual")


def test_persisted_scope_bindings_do_not_change_when_current_grouping_changes(monkeypatch):
    from open_deep_research.quality import contract as contracts
    from open_deep_research.quality.planning import compile_planned_requirements
    from open_deep_research.state import PlannedRequirement

    text = "（1）部署形态和主要运维依赖；（2）权限隔离边界。"
    original = compile_planned_requirements([{"role": "user", "content": text}], [
        PlannedRequirement(source_text="（1）部署形态和主要运维依赖", kind="factual", facets=["部署形态", "主要运维依赖"])
    ], atomic=True)
    expected = contracts.research_delegation_scopes(original)
    restored = contracts.ResearchCoverageContract.model_validate_json(original.model_dump_json())
    monkeypatch.setattr(contracts, "MAX_REQUIREMENTS_PER_RESEARCH_TASK", 1)
    assert contracts.research_delegation_scopes(restored) == expected


def test_smaller_writer_input_cannot_turn_omitted_support_into_a_research_gap():
    import json
    from open_deep_research.configuration import Configuration
    from open_deep_research.report.writing import ReportInputBudgetExceeded, fit_writing_messages, writing_messages

    rows = [{"evidence_id": str(i), "claim": "Grounded claim. " * 25,
             "supporting_excerpt": "Evidence with qualifications. " * 30,
             "source_url": f"https://official.example/{i}"} for i in range(20)]
    payload = {"requirement_to_evidence": {"first": ["0"], "last": ["19"]},
               "required_requirement_ids": ["first", "last"]}
    cfg = Configuration()
    legacy = writing_messages("Write a report.", payload, rows)
    fitted, count = fit_writing_messages(legacy, "openai:gpt-4.1", cfg, output_tokens=1000, context_window=20000, fraction=.2)
    assert 0 < count < len(rows)
    assert "19" not in {r["evidence_id"] for r in json.loads(fitted[-1].content)["records"]}
    current = writing_messages("Write a report.", {**payload, "_quality_policy_version": "quality-gate-v5"}, rows)
    with pytest.raises(ReportInputBudgetExceeded, match="report_requirement_evidence_exceeds_budget"):
        fit_writing_messages(current, "openai:gpt-4.1", cfg, output_tokens=1000, context_window=20000, fraction=.2)


@pytest.mark.asyncio
async def test_actual_writer_fallback_window_triggers_sectioned_assembly(monkeypatch):
    from open_deep_research.configuration import Configuration
    from open_deep_research.report.assembly import AssemblyResult, OneShotStrategy, SectionedStrategy
    from open_deep_research.report.writing import ReportInputBudgetExceeded, writing_messages

    expected = AssemblyResult(body_markdown="Both requirements written in separate sections.")
    calls = []

    async def write(*args, **kwargs):
        raise ReportInputBudgetExceeded("report_requirement_evidence_exceeds_budget")

    async def sections(self, ctx):
        calls.append(ctx)
        return expected

    monkeypatch.setattr(SectionedStrategy, "build", sections)
    ctx = SimpleNamespace(config={"metadata": {"run_config_schema_version": 18}}, configurable=Configuration(),
        state={}, prompt_template="Write.", requirement_to_evidence={}, sources=[],
        stage_messages=lambda *_: writing_messages("Write.", {}, [evidence()]),
        invoke_writer_with_output_recovery=write)
    result = await OneShotStrategy().build(ctx)
    assert result is expected and calls == [ctx]


@pytest.mark.asyncio
async def test_human_approval_wait_does_not_exhaust_tool_execution_time():
    from open_deep_research.agentscope_runtime.runtime_limits import limited_tool, waiting_for_approval, current_deadline_at

    hard_deadline = time.time() + 2
    async def operation():
        with waiting_for_approval():
            assert current_deadline_at() == hard_deadline
            await asyncio.sleep(.08)
        assert current_deadline_at() > time.time()
        await asyncio.sleep(.005)
        return "executed once"

    assert await limited_tool(operation, .04, deadline_at=hard_deadline) == "executed once"


@pytest.mark.asyncio
async def test_approval_wait_keeps_hard_deadline_and_cancellation():
    from open_deep_research.agentscope_runtime.runtime_limits import limited_tool, waiting_for_approval

    async def waiting():
        with waiting_for_approval():
            await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        await limited_tool(waiting, .01, deadline_at=time.time() + .04)
    task = asyncio.create_task(limited_tool(waiting, .01, deadline_at=time.time() + 10))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_parallel_approval_waits_count_once_and_resume_execution_limit():
    from open_deep_research.agentscope_runtime.runtime_limits import limited_tool, waiting_for_approval

    clocks = []
    async def waiter():
        with waiting_for_approval() as clock:
            clocks.append(clock)
            await asyncio.sleep(.06)

    async def operation():
        started = time.monotonic()
        await asyncio.gather(waiter(), waiter())
        elapsed = time.monotonic() - started
        assert clocks[0] is clocks[1]
        assert .04 <= clocks[0].waited_seconds <= elapsed + .01
        await asyncio.sleep(.10)

    with pytest.raises(TimeoutError):
        await limited_tool(operation, .04, deadline_at=time.time() + 2)


@pytest.mark.parametrize("decision", ["allow_once", "allow_run", "deny"])
def test_run_authorization_covers_only_matching_live_pending_requests(tmp_path, decision):
    from open_deep_research.sandbox.approvals import SecurityApprovalStore

    store = SecurityApprovalStore("run", runs_dir=str(tmp_path))
    def request(task, domain="api.example", port=443, capability="search.provider", fence=1, expiry=None):
        return store.request(task_id=task, fence_token=fence, kind="network", capability=capability,
            target={"domain": domain, "port": port}, operation_id=task,
            expires_at=expiry if expiry is not None else time.time() + 60)
    original = request("a")
    peer = request("b")
    other_domain = request("c", domain="other.example")
    other_port = request("d", port=8443)
    other_capability = request("e", capability="external.extract")
    other_fence = request("f", fence=2)
    expired = request("g", expiry=time.time() - 1)
    store.resolve(original.approval_id, decision=decision, actor="owner", reason="inspected target", expected_fence_token=1)
    _, records = store.list()
    rows = {row.approval_id: row for row in records}
    assert rows[peer.approval_id].status == ("resolved" if decision == "allow_run" else "pending")
    if decision == "allow_run":
        assert rows[peer.approval_id].inherited_from == f"approval:{original.approval_id}"
        assert rows[peer.approval_id].resolved_by == "owner"
    for row in (other_domain, other_port, other_capability, other_fence):
        assert rows[row.approval_id].status == "pending"
    assert rows[expired.approval_id].status == "expired"


def test_researcher_view_keeps_sources_but_excludes_sibling_work():
    from open_deep_research.quality.contract import CoverageRequirement, ResearchCoverageContract, researcher_coverage_view
    from open_deep_research.quality.context import research_context_xml, context_payload
    from xml.etree.ElementTree import fromstring

    contract = ResearchCoverageContract(schema_version=4, original_query_sha256="fixture", requirements=(
        CoverageRequirement(requirement_id="q", text="部署依赖", subject="Qdrant", source_message_index=0, source_start=0, source_end=4),
        CoverageRequirement(requirement_id="m", text="部署依赖", subject="Milvus", source_message_index=0, source_start=0, source_end=4),
        CoverageRequirement(requirement_id="s", text="仅使用官方资料", kind="process", source_message_index=0, source_start=0, source_end=7),
    ), source_plan={"status": "confirmed", "entries": []}, delegation_scopes={"S01": ("q",), "S02": ("m",)})
    view = researcher_coverage_view(contract, ["m"])
    xml = research_context_xml(view, payload={"coverage_contract": view})
    root = fromstring(xml)
    assert [row.attrib["id"] for row in root.findall("research_requirements/requirement")] == ["m"]
    assert root.find("research_requirements/requirement").attrib["subject"] == "Milvus"
    assert root.find("execution_constraints/requirement").text == "仅使用官方资料"
    assert view["source_plan"] == contract.source_plan
    assert "delegation_scopes" not in context_payload(xml)["coverage_contract"]
    assert len(contract.requirements) == 3


def test_target_allow_run_releases_pending_peers_and_revocation_still_wins(tmp_path):
    from open_deep_research.sandbox.approvals import SecurityApprovalStore

    store = SecurityApprovalStore("run", runs_dir=str(tmp_path))
    a = store.request(task_id="task", fence_token=1, kind="network", capability="search.provider",
        target={"domain": "api.example", "port": 443}, operation_id="operation", expires_at=time.time() + 60)
    target = store.observe_target(a.capability, a.target, 1)
    decision = store.decide_target(target["target_id"], decision="allow_run", reason="current run", actor="owner",
        expected_version=target["version"], fence_token=1)
    _, records = store.list()
    assert records[0].status == "resolved"
    assert records[0].inherited_from == f"target:{target['target_id']}:{decision['version']}"
    store.decide_target(target["target_id"], decision="revoke", reason="withdrawn", actor="owner",
        expected_version=decision["version"], fence_token=1)
    with pytest.raises(ValueError, match="revoked"):
        store.consume(a.approval_id, operation_id="operation", expected_fence_token=1)


@pytest.mark.parametrize("version", ["quality-gate-v4", "quality-gate-v5"])
@pytest.mark.parametrize("source_type", ["web", "domain", "url"])
def test_query_requirement_matches_actual_web_corpus(version, source_type):
    from pydantic import ValidationError
    from open_deep_research.agentscope_runtime.web_tools import WebFetchLedger, web_research_tool

    selection = {"mode": "web", "sources": []} if source_type == "web" else {
        "mode": "specific", "sources": [{"type": source_type, source_type:
            "https://official.example/deploy" if source_type == "url" else "official.example"}]}
    config = {"configurable": {"research_efficiency_mode": "bounded"}, "metadata": {
        "quality_policy_version": version, "source_selection": selection}}
    tool = web_research_tool(lambda: config, SimpleNamespace(), WebFetchLedger())
    required = version == "quality-gate-v5" and source_type != "url"
    assert ("queries" in tool.input_schema.model_json_schema()["required"]) == required
    if required:
        with pytest.raises(ValidationError):
            tool.input_schema(objective="deployment")
        with pytest.raises(ValidationError):
            tool.input_schema(objective="deployment", queries=[])
        assert "omit queries" not in tool.prompt(config)
    else:
        assert tool.input_schema(objective="deployment").queries == []


def test_supplement_cache_does_not_reassign_sibling_evidence():
    from open_deep_research.agentscope_runtime.efficiency import merge_progress, task_evidence_candidates

    state = {"candidates": {
        "q": {"evidence_id": "q", "requirement_ids": ["Qdrant-deployment"]},
        "m": {"evidence_id": "m", "requirement_ids": ["Milvus-deployment"]},
        "unbound": {"evidence_id": "unbound"},
        "shared": {"evidence_id": "shared", "requirement_ids": ["Qdrant-deployment", "Milvus-deployment"]},
    }}
    selected = task_evidence_candidates(state["candidates"].values(), ["Milvus-deployment"])
    assert {row["evidence_id"] for row in selected} == {"m", "shared"}
    merge_progress(state, {"candidates": {row["evidence_id"]: {**row, "requirement_ids": ["Milvus-deployment"]} for row in selected}})
    assert state["candidates"]["q"]["requirement_ids"] == ["Qdrant-deployment"]
    assert state["candidates"]["shared"]["requirement_ids"] == ["Milvus-deployment", "Qdrant-deployment"]


@pytest.mark.asyncio
async def test_restricted_writer_schema_rejects_e2e_requirement_prose(monkeypatch):
    from pydantic import ValidationError
    from open_deep_research.report import evidence_synthesis, orchestrator

    rows = [
        {"requirement_id": "COV-07-64e0b1ed5585", "text": "Tenant isolation limits", "kind": "factual"},
        {"requirement_id": "format", "text": "1800 Chinese words", "kind": "deliverable"},
        {"requirement_id": "date", "text": "as of October 7", "kind": "process"},
    ]
    contract = {"schema_version": 4, "requirements": rows}
    assert orchestrator._uncovered_requirement_ids({"coverage_contract": contract, "coverage_ledger": {}}) == [rows[0]["requirement_id"]]
    schemas = []

    class Runtime:
        async def invoke(self, role, messages, cfg, *, span_name, schema):
            schemas.append(schema)
            return schema(title="Report", summary="Known facts", summary_evidence_ids=["e"], sections=[],
                          unresolved_requirements=[rows[0]["requirement_id"]])

    monkeypatch.setattr(evidence_synthesis, "require_report_runtime", lambda: Runtime())
    good = await evidence_synthesis._invoke_draft({"coverage_contract": contract, "eligible_evidence": []},
        {"metadata": {"quality_policy_version": "quality-gate-v5"}})
    for invalid in ["COV-07-64e0b1ed5585：缺少限制条件", "format", "自由文本不属于需求 ID", "COV-07-invented"]:
        with pytest.raises(ValidationError):
            schemas[0].model_validate({**good.model_dump(), "unresolved_requirements": [invalid]})


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["tools", "handoff", "restricted_writer"])
@pytest.mark.parametrize("error_type", ["FenceLost", "UnknownOperation"])
async def test_quality_boundaries_preserve_control_errors(monkeypatch, boundary, error_type):
    from open_deep_research.agentscope_runtime import recovery_store
    from open_deep_research.quality import gate
    from open_deep_research.report import evidence_synthesis

    error = getattr(recovery_store, error_type)("fixture control failure")
    async def fail(*args, **kwargs):
        raise error

    contract = build_research_coverage_contract([{"role": "user", "content": "Explain deployment."}])
    config = {"metadata": {"quality_policy_version": "quality-gate-v5"}, "configurable": {
        "quality_evaluation_enabled": True, "quality_evaluation_fail_open": True}}
    with pytest.raises(type(error)):
        if boundary == "handoff":
            await gate.evaluate_subagent_handoff("Deployment", {"compressed_research": "summary", "evidence_registry": [evidence()]},
                config, coverage_contract=contract, requirement_ids=list(contract.delegable_requirement_ids()), evaluator=fail)
        elif boundary == "tools":
            await gate.evaluate_tool_results("Deployment", [{"evidence": [evidence()]}], config, evaluator=fail)
        else:
            monkeypatch.setattr(evidence_synthesis, "_invoke_draft", fail)
            await evidence_synthesis.build_evidence_limited_report([evidence()], coverage_contract=contract,
                coverage_ledger={}, caveats=[], uncovered_requirement_ids=[], rejection_reasons=[], artifact_refs=[], config=config)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["quality-gate-v4", "quality-gate-v5"])
async def test_source_scope_denial_is_known_before_acquiring_network_resources(monkeypatch, version):
    from open_deep_research.agentscope_runtime import web_tools
    from open_deep_research.tools.base import ToolContext
    from open_deep_research.tools.governance import ToolErrorType, ToolOutcomeError

    config = {"metadata": {"quality_policy_version": version, "source_selection": {
        "mode": "specific", "sources": [{"type": "domain", "domain": "official.example"}]}}}
    def forbidden(*args, **kwargs):
        pytest.fail("Source rejection must precede network/resource creation")
    monkeypatch.setattr(web_tools, "SearchResources", forbidden)
    tool = web_tools.fetch_url_tool(lambda: config, SimpleNamespace(), web_tools.WebFetchLedger())
    context = ToolContext(config=config, role="researcher", tool_call_id="call", operation_id="operation")
    if version == "quality-gate-v5":
        with pytest.raises(ToolOutcomeError) as error:
            await tool.call(web_tools.FetchUrlInput(url="https://outside.example/page"), context)
        assert error.value.error.error_type is ToolErrorType.validation_error
        assert error.value.error.detail["reason_code"] == "source_scope_denied"
    else:
        with pytest.raises(ValueError, match="specific-source boundary"):
            await tool.call(web_tools.FetchUrlInput(url="https://outside.example/page"), context)

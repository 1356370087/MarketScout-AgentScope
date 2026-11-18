"""Replay the handoff, pricing and accounting boundaries seen in competitor runs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_efficiency import cache  # noqa: F401 - real fenced research progress fixture

from open_deep_research.web.passages import passages, source_excerpt


@pytest.mark.asyncio
async def test_committed_uncertain_gateway_cost_remains_held_until_exact_bill(pg_url, monkeypatch):
    from uuid import uuid4
    from sqlalchemy import select
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore, provisional_gateway_cost
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.agentscope_runtime.spend_reconciliation import reconcile_spend
    from open_deep_research.platform import quota, runtime, schema
    from open_deep_research.platform.store import PlatformStore
    native = RecoveryStore(pg_url)
    platform = PlatformStore(native.engine)
    monkeypatch.setattr(platform, "limits", AsyncMock(return_value={"monthly_cost_micro_usd": 100}))
    monkeypatch.setattr(quota, "get_store", lambda: platform)
    monkeypatch.setattr(runtime, "enabled", lambda: True)
    run, owner = uuid4().hex, str(uuid4())
    key = "gateway:model:review"
    try:
        await native.create_tables()
        async with native.engine.begin() as conn:
            await conn.run_sync(schema.metadata.create_all)
        await native.create_run(owner, ResearchSnapshot(run_id=run, config_fingerprint="fixed"), limits={"cost_micro_usd": 100})
        lease = await native.acquire(run, owner)
        await native.begin_operation(lease, key, "gateway:model", "request", reserve={"cost_micro_usd": 40})
        receipt = {"status": "uncertain", "outcome": {"status": "uncertain", "usage": {}, "response_cost_usd": None}}
        await native.commit_operation(lease, key, receipt, actual={"cost_micro_usd": 40})
        identity = native._monthly_key(run, key)
        assert provisional_gateway_cost(await native.operation_record(lease, key)) == 40
        assert (await quota.runway(owner))["settled"] == 0
        await quota.settle(identity, 40)  # Reproduce the earlier gateway's provisional settlement.
        for _ in range(2):
            await reconcile_spend(native, lease, [])
        account = await quota.runway(owner)
        assert (account["settled"], account["reserved"], account["available"]) == (0, 40, 60)
        bill = {"request_id": "verified", "request_tags": [f"run:{run}", "operation:review"],
                "prompt_tokens": 5, "completion_tokens": 1, "spend": "0.000040"}
        for _ in range(2):
            await reconcile_spend(native, lease, [bill])
        await quota.mark_unknown(identity, provisional_settlement=40)  # A delayed observer cannot undo a bill.
        account = await quota.runway(owner)
        assert (account["settled"], account["reserved"], account["available"]) == (40, 0, 60)
        op = await native.operation_record(lease, key)
        assert provisional_gateway_cost(op) is None and op["result"]["outcome"]["status"] == "uncertain"
        async with native.engine.connect() as conn:
            assert await conn.scalar(select(schema.charges.c.status).where(schema.charges.c.operation_id == identity)) == "settled"
    finally:
        await native.aclose()


def test_plan_heading_keeps_a_contiguous_source_slice_without_sibling_price():
    professional = "## Starter — USD 59 / month, or USD 590 / year\nFor independent teams.\n- One workspace"
    team = "## Team — USD 159 / month\nFor organizations.\n- Three workspaces"
    raw = "# Price list\n\n" + professional + "\n\n" + team
    quotes = [value for value in passages(raw) if "USD 59 " in value]
    assert quotes == [professional]
    assert source_excerpt(raw, quotes[0]) == professional
    assert all(value in raw for value in passages(raw))
    adjacent = professional + "\n" + team
    assert [p for p in passages(adjacent) if "USD 59 " in p] == [professional]
    assert not passages("## [Starter — USD 59 / month](/prices)\n- [Next](/team)")


@pytest.mark.asyncio
async def test_zero_admitted_findings_never_schedule_paid_outline():
    from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
    config = {"configurable": {"enable_human_in_loop": True}, "metadata": {"run_config_schema_version": 25}}
    writer = SimpleNamespace(outline=AsyncMock(side_effect=AssertionError("paid outline")))
    stage = NativeResearchStages(None, None, lambda: config, report_writer=writer)
    state = SimpleNamespace(completion_outcome={"action": "terminate", "reason": "insufficient_evidence"},
                            findings=[{"evidence_registry": [{"claim": "unverified entry"}],
                                       "assessment": {"handoff": {"accepted": False}}}])
    await stage.outline_approval(state)
    assert "证据" in state.outline
    writer.outline.assert_not_awaited()


def test_budget_wire_preserves_scope_and_real_remaining_amount():
    from open_deep_research.budgets import BudgetDimension, BudgetExhausted
    error = BudgetExhausted(BudgetDimension.COST_MICRO_USD, scope="account_month", available=24, required=30)
    restored = BudgetExhausted.from_wire(error.to_wire())
    assert (restored.scope, restored.available, restored.required) == ("account_month", 24, 30)
    assert BudgetExhausted.from_wire("budget_exhausted:cost_micro_usd").scope == "unknown"


@pytest.mark.parametrize("version", [24, 25, 26, 27, 28, 29])
def test_old_snapshot_keeps_its_original_fingerprint(version):
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.configuration import run_config_fingerprint
    saved = RunConfig.compile().snapshot()
    contract = saved["contract"]
    contract["metadata"]["run_config_schema_version"] = version
    contract["metadata"]["run_config_fingerprint"] = run_config_fingerprint(contract)
    assert RunConfig.restore(saved).snapshot() == saved


@pytest.mark.asyncio
async def test_skill_version_reuse_does_not_advance_evidence_progress(cache):
    from open_deep_research.agentscope_runtime.efficiency import progress_signature
    from open_deep_research.skills.runtime import skill_tools, ReadSkillInput
    from open_deep_research.tools.base import ToolContext
    cfg = {"metadata": {"run_config_schema_version": 25}}
    tool, = skill_tools(cfg, cache, task_id="main-research")
    before = progress_signature(await cache.progress())
    versions = []
    for index, expected in enumerate((False, True)):
        context = ToolContext(config=cfg, role="researcher", operation_id=str(index), tool_call_id=str(index))
        result = await tool.call(ReadSkillInput(key="comparative_research"), context, None)
        assert result.output["reused"] is expected and result.output["instructions"]
        versions.append(result.output["version"])
    after = await cache.progress()
    assert versions[0] == versions[1] == after["loaded_skills"]["main-research:comparative_research"]["version"]
    assert progress_signature(after) == before


def test_official_directory_requires_domain_grant_and_actual_field():
    from open_deep_research.web.pricing_navigation import discovery_links, has_field
    page = SimpleNamespace(markdown="Overview", final_url="https://plans.example/start", alternate_urls=[])
    def scoped(source):
        return {"metadata": {"source_selection": {"mode": "specific", "sources": [source]}}}
    domain = scoped({"type": "domain", "domain": "plans.example"})
    assert discovery_links(page, domain, allow_index=True) == ["https://plans.example/llms.txt"]
    assert discovery_links(page, domain, allow_index=False) == []
    assert discovery_links(page, scoped({"type": "url", "url": page.final_url}), allow_index=True) == []
    page.alternate_urls = ["https://plans.example/published.md", "https://unrelated.example/pricing"]
    assert discovery_links(page, domain, allow_index=True) == ["https://plans.example/published.md"]
    assert not has_field(["Starter $59 / month"], "currency")
    assert has_field(["Starter USD 59 / month"], "currency")


@pytest.mark.asyncio
async def test_stagnant_main_can_use_one_unread_directory_link_without_crediting_evidence():
    from open_deep_research.agentscope_runtime.adaptive_research import AdaptiveResearch
    from open_deep_research.agentscope_runtime.efficiency import progress_signature
    config = {"configurable": {"max_fetches_per_run": 12, "max_fetches_per_researcher": 12},
              "metadata": {"run_config_schema_version": 27, "source_selection": {"mode": "specific",
                           "sources": [{"type": "domain", "domain": "docs.example"}]}}}
    used = {"fetch_calls": 11}
    budget = AsyncMock(return_value={"limits": {"fetch_calls": 12}, "used": used, "reserved": {}})
    recovery = SimpleNamespace(lease=SimpleNamespace(run_id="run", user_id="owner"), store=SimpleNamespace(budget=budget))
    flow = AdaptiveResearch(SimpleNamespace(models=SimpleNamespace(recovery=recovery), config_provider=lambda: config, run_id="run"))
    page = {"url": "https://docs.example/", "markdown":
        "[团队计费](/billing) [已读计费说明](/visited) [外部计费](https://other.example/bill) [主页](/)"}
    gaps = [{"kind": "factual", "reason": "缺少团队计费主体的直接资料", "next_query": "团队计费说明"}]
    progress = {"web_reads": {"first": {"task_id": "main-research", "physical_fetches": 11,
                 "reads": [{"url": "https://docs.example/visited"}]}}}
    signature = progress_signature(progress)
    middle = {}
    assert await flow.offer_directory_followup(middle, page, gaps, progress)
    assert middle["research_context_authority"]["directory_followup"]["links"] == ["https://docs.example/billing"]
    assert not await flow.offer_directory_followup(middle, page, gaps, progress)
    assert progress_signature(progress) == signature and "admitted" not in progress
    used["fetch_calls"] = 12
    assert not await flow.offer_directory_followup({}, page, gaps, progress)
    used["fetch_calls"] = 11
    config["metadata"]["source_selection"]["sources"] = [{"type": "url", "url": page["url"]}]
    assert not await flow.offer_directory_followup({}, page, gaps, progress)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [25, 30])
async def test_fetch_tool_discovers_target_and_reuses_failed_method(cache, monkeypatch, version):
    import json
    from test_web_upgrade import Models, config
    from open_deep_research.agentscope_runtime.web_tools import FetchUrlInput, WebFetchLedger, fetch_url_tool
    from open_deep_research.tools.base import ToolContext
    from open_deep_research.web import pipeline
    from open_deep_research.web.models import FetchResult
    base = "https://docs.example"
    sheet = "## Starter — USD 59 / month, or USD 590 / year\nFor independent teams.\n- One workspace"
    pages = {base + "/start": ("text/html", "<main><p>Our entry plan is $59 per month per workspace.</p></main>"),
             base + "/llms.txt": ("text/plain", "# Directory\n\n[Published pricing](/published.md)"),
             base + "/published.md": ("text/markdown", sheet)}
    reads = []
    async def local(source, settings, **kwargs):
        url = source.canonical_url
        mime, body = pages[url]
        reads.append(url)
        return pipeline.RawFetch(FetchResult(candidate_id=source.candidate_id, requested_url=url, final_url=url,
            success=True, content_type=mime), body.encode())
    monkeypatch.setattr(pipeline, "fetch_local", local)
    cfg = config(fetch_backend_order=["local"], research_efficiency_mode="bounded", max_fetches_per_run=3,
                 max_fetches_per_researcher=3)
    cfg["metadata"].update(run_id=f"competitor-price-v{version}", run_config_schema_version=version, quality_policy_version="quality-gate-v6",
        source_selection={"mode": "specific", "sources": [{"type": "domain", "domain": "docs.example"}]})
    models = Models()
    models.research_cache = cache
    tool = fetch_url_tool(lambda: cfg, models, WebFetchLedger())
    async def read(url, call, mode="evidence"):
        context = ToolContext(config=cfg, role="researcher", tool_call_id=call, operation_id=call)
        return await tool.call(FetchUrlInput(url=url, objective="Check pricing currency", mode=mode), context)
    landing = await read(base + "/start", "first")
    payload = json.loads(landing.output)
    assert payload.get("pricing_navigation", {}).get("links") == [base + "/llms.txt"], (reads, payload)
    repeated = await read(base + "/start", "repeat")
    assert json.loads(repeated.output)["kind"] == "source_method_reused" and repeated.metadata["physical_fetches"] == 0
    index = await read(base + "/llms.txt", "index", "markdown")
    assert not json.loads(index.output)["evidence_eligible"] and json.loads(index.output)["pricing_navigation"] == [base + "/published.md"]
    result = await read(base + "/published.md", "sheet")
    evidence = await cache.get(result.metadata["evidence_ref"])
    assert any(row["supporting_excerpt"] == sheet for row in evidence)
    assert all(row["source_url"] == base + "/published.md" for row in evidence)
    if version >= 30:
        assert all(row["_source_read_receipt"]["read_at"] for row in evidence)
    else:
        assert all("_source_read_receipt" not in row for row in evidence)
    assert reads == list(pages)


def test_format_gate_uses_visible_text_and_rendered_table():
    from open_deep_research.report.format_checks import format_review
    request = [{"role": "user", "content": "写一张对比表，正文不超过 60 字。"}]
    body = "# 套餐\n\n| 产品 | 月费 |\n| --- | --- |\n| A | [59 美元](https://example.com/" + "a" * 200 + ") |\n"
    assert format_review(body + "\n## 参考资料\n\n" + "x" * 400, request, 1) is None
    invalid = format_review("- 产品 | 月费\n\n" + "过长" * 80, request, 1)
    assert invalid.decision == "revise" and len(invalid.issues) == 2


def test_review_character_budget_preserves_whole_required_and_conflicting_records():
    import json
    from copy import deepcopy
    from open_deep_research.report.reviewer import _fit_reviewer_projection
    from open_deep_research.report.writing import ReportInputBudgetExceeded, evidence_bindings
    records = [{"evidence_id": key, "source_url": "https://docs.example/" + key, "claim": key,
                "supporting_excerpt": key * 200, "requirement_ids": ["price"]} for key in ("a", "b", "c", "d", "e", "f")]
    ledger = {"price": {"status": "supported", "evidence_ids": ["a"]}}
    payload = {"draft_markdown": "the plan [ev-reference]", "evidence_registry": records,
        "citation_inventory": {"body_source_urls": [records[1]["source_url"]]}, "required_requirement_ids": ["price"],
        "requirement_to_evidence": evidence_bindings(records, ledger, prefer_reviewed=True)}
    assert payload["requirement_to_evidence"] == {"price": ["a"]}
    original = deepcopy(payload)
    _fit_reviewer_projection(payload, ledger, 1600, pinned_ids={"f"})
    assert len(json.dumps(payload, ensure_ascii=False)) <= 1600 and payload["input_truncated"]
    selected = {row["evidence_id"] for row in payload["evidence_registry"]}
    assert {"a", "b", "f"} <= selected and len(selected) < len(records)
    assert all(row in records for row in payload["evidence_registry"])
    with pytest.raises(ReportInputBudgetExceeded):
        _fit_reviewer_projection(original, ledger, 500, pinned_ids={"f"})


@pytest.mark.asyncio
async def test_admitted_supplement_uses_one_revision_before_semantic_review(monkeypatch):
    from open_deep_research.report import orchestrator, reviewer
    from open_deep_research.report.models import ReportReview
    semantic = AsyncMock(return_value=ReportReview(decision="pass"))
    monkeypatch.setattr(reviewer, "review_report", semantic)
    state = {"archived_review_draft": {"markdown": "old"}, "verified_supplement": [
        {"requirement_id": "price", "subject": "Acme", "text": "每工作区价格", "evidence_ids": ["verified"]}]}
    cfg = {"metadata": {"run_config_schema_version": 27}, "configurable": {"report_review_max_revisions": 1}}
    update = await orchestrator.review_report("old", state, cfg, attempt=1)
    assert update.decision == "revise" and update.issues[0].evidence_ids == ["verified"]
    semantic.assert_not_awaited()
    await orchestrator.review_report("revised", state, cfg, attempt=2)
    semantic.assert_awaited_once()


def test_process_receipts_require_a_completed_read_and_its_own_run():
    from open_deep_research.agentscope_runtime.execution_receipts import summarize_receipts
    request = (2, {"type": "evaluation.tool_requested", "name": "ReadSkill", "args": {"key": "compare"},
                   "operation_key": "operation", "task_id": "main"})
    complete = (3, {"type": "evaluation.tool_completed", "operation_key": "operation"})
    task = (1, {"type": "research.public", "public_type": "research.task.created",
                "public_payload": {"task_id": "main", "mode": "adaptive-main"}})
    web = (4, {"type": "evaluation.tool_requested", "name": "fetch_url"})
    progress = {"tasks": {"main": {}}, "web_reads": {"call": {"physical_fetches": 1, "reads": [
        {"success": True, "read_at": "2026-11-18T05:00:00Z", "cached": False}]}}}
    budget = {"used": {"fetch_calls": 1}}
    ok = summarize_receipts("parent", progress, budget, [task, request, complete, web])
    assert ok["status"] == "verified" and ok["skills"][0]["sequence"] < ok["first_web_sequence"]
    public = (2, {**request[1], "id": "logical-call"})
    inner = (3, {**public[1], "operation_key": "sql-reservation"})
    both = summarize_receipts("parent", progress, budget, [task, public, inner, complete, web])
    assert both["skills"] == ok["skills"] and both["status"] == "verified"
    absent = summarize_receipts("parent", progress, budget, [task, public, inner, web])
    assert not absent["skills"] and "skill_completion" in absent["unknown"]
    missing = summarize_receipts("child", {}, {"used": {}}, [request])
    assert missing["run_id"] == "child" and missing["skills"] == [] and missing["physical_fetches"] is None


def test_legacy_page_limit_is_verified_against_the_original_run():
    from test_research_migration import contract, evidence
    from open_deep_research.quality.contract import research_page_limit
    from open_deep_research.report.models import ReportDraft
    from open_deep_research.report.reviewer import build_reviewer_payload
    text = "最多读取 12 个相关页面"
    assert research_page_limit(text) == research_page_limit("Read up to 12 official pages.") == 12
    assert research_page_limit("核实 Acme 套餐最多可读取 12 页文档吗？") is None
    assert research_page_limit("正文最多输出 12 页") is None
    scope = contract(text)
    row = scope["requirements"][0]
    row.update(kind="deliverable", text=text)
    state = {"coverage_contract": scope, "evidence_registry": [evidence()],
             "execution_receipts": {"run_id": "child", "physical_fetches": 0},
             "source_run_receipts": [{"run_id": "supplier", "relationship": "supplement", "physical_fetches": 3},
                                     {"run_id": "original", "relationship": "original_research", "physical_fetches": 9}]}
    cfg = {"metadata": {"run_config_schema_version": 28}}
    payload = build_reviewer_payload(ReportDraft(markdown="报告"), state, cfg)
    assert payload["coverage_contract"]["requirements"][0]["kind"] == "process"
    assert payload["research_read_limits"] == [{"requirement_id": row["requirement_id"], "maximum": 12,
                                               "run_id": "original", "observed": 9}]
    assert row["kind"] == "deliverable" and payload["execution_receipts"]["run_id"] == "child"
    from open_deep_research.report.reviewer import _normalize_candidate
    raw = {"decision": "pass", "dimensions": {key: 1 for key in ("coverage", "redundancy", "unsupported_claims",
           "contradictions", "executive_readability", "citation_correctness")}, "issues": [], "issue_resolutions": [],
           "coverage": [{"requirement_id": row["requirement_id"], "status": "covered", "explanation": "within limit"}]}
    state["source_run_receipts"][-1]["physical_fetches"] = 13
    exceeded = build_reviewer_payload(ReportDraft(markdown="报告"), state, cfg)
    verdict = _normalize_candidate(raw, draft=ReportDraft(markdown="报告"), state=state, config=cfg,
                                   payload=exceeded, model_name="fixture", attempt=1)
    assert verdict.decision != "pass" and "research_read_limit_exceeded" in verdict.hard_failures
    state["source_run_receipts"] = []
    assert build_reviewer_payload(ReportDraft(markdown="报告"), state, cfg)["research_read_limits"][0]["observed"] is None


@pytest.mark.parametrize("version", [28, 29])
def test_writer_and_revisor_receive_versioned_read_dates_separately_from_publication(version):
    from test_research_migration import contract, evidence
    from open_deep_research.quality.context import context_payload
    from open_deep_research.report.assembly import ReportContext
    from open_deep_research.report.models import ReportDraft, ReportReview
    from open_deep_research.report.profiles import get_profile
    from open_deep_research.report.reviewer import _revision_prompt
    cfg = {"metadata": {"run_config_schema_version": version}}
    receipt = {"run_id": "run", "documents": [{"url": "https://example.test/source", "read_at": "2026-11-18T09:14:00Z",
               "published_at": "2024-01-01", "cached": False, "success": True}]}
    state = {"coverage_contract": contract("核实入门订阅价格"), "evidence_registry": [evidence()],
             "execution_receipts": receipt, "research_brief": "核实原文在何日读取"}
    context = ReportContext.from_state(state, cfg, get_profile("default"))
    writer = context.stage_messages("Write a comparison", {})
    revisor = _revision_prompt(ReportDraft(markdown="初稿"), ReportReview(decision="revise"), state, cfg)
    for messages in (writer, revisor):
        current = next(context_payload(str(m.content)) for m in messages if m.name == "report_context")
        if version == 29:
            assert current["execution_receipts"] == receipt and "read_at" in messages[0].content
        else:
            assert "execution_receipts" not in current


def test_read_timestamp_requires_matching_content_version_and_successful_physical_read():
    from open_deep_research.agentscope_runtime.execution_receipts import bind_verified_reads
    from open_deep_research.report.writing import project_evidence
    uri = "https://example.test/price"
    source = {"source_url": uri, "document_id": "version-one", "evidence_id": "ev_price", "claim": "Unit price"}
    doc = {"url": uri, "document_id": "version-one", "success": True, "cached": False,
           "read_at": "2026-11-18T09:14:00Z", "published_at": None}
    bind = lambda rows, reads: project_evidence(bind_verified_reads(rows, [{"run_id": "origin", "documents": reads}]))
    assert bind([source], [doc])[0]["source_read_receipt"]["read_at"] == doc["read_at"]
    assert "source_read_receipt" not in bind([source], [{**doc, "document_id": "version-two"}])[0]
    assert "source_read_receipt" not in bind([source], [{**doc, "cached": True}])[0]
    assert "_source_read_receipt" not in source


def test_research_judge_receives_dates_only_for_submitted_sql_candidates():
    from open_deep_research.agentscope_runtime.execution_receipts import submitted_read_receipts
    uri = "https://supplier.example/pricing"
    record = {"evidence_id": "ev_abc", "source_url": uri, "document_id": "doc-old"}
    progress = {"documents": {"doc-old": {"url": uri, "content_hash": "v1"}},
        "candidates": {"ev_abc": record},
        "web_reads": {"call": {"reads": [{"url": uri, "read_at": "2026-11-18T09:14:00Z", "content_hash": "v1",
                                        "published_at": None, "success": True, "cached": False}]}}}
    selected = {"source_url": uri, "evidence_id": "ev_abc", "read_at": "invented"}
    assert submitted_read_receipts([selected], progress)[0]["read_at"] == "2026-11-18T09:14:00Z"
    assert submitted_read_receipts([{**selected, "source_url": "https://other.example"}], progress) == []
    assert submitted_read_receipts([{**selected, "evidence_id": "ev_other"}], progress) == []
    progress["web_reads"]["call"]["reads"][0]["content_hash"] = "v2"
    assert submitted_read_receipts([selected], progress) == []


def test_chinese_handoff_url_annotation_never_expands_exact_source_grant():
    from open_deep_research.quality.gate import _handoff_cited_urls
    url = "https://example.test/guide"
    evidence = [{"source_url": url, "evidence_id": "ev_123"}]
    assert _handoff_cited_urls(url + "：ev_123原文“Search modes”", evidence) == {url}
    for text in (f"[link]({url}：ev_123)", f"<{url}：ev_123>", f"{url}：ev_unknown", f"{url}：ev_123/private"):
        assert url not in _handoff_cited_urls(text, evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_stop,expected_calls", [(None, 2), ("report_budget_reserved", 1)])
async def test_localizable_main_claim_rechecks_only_original_excerpts(cache, monkeypatch, budget_stop, expected_calls):
    from test_research_migration import contract, evidence
    from open_deep_research.agentscope_runtime import efficiency, research_quality
    from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff
    from open_deep_research.quality.gate import HandoffAssessment
    coverage = contract("核实入门云套餐价格")
    rid = coverage["requirements"][0]["requirement_id"]
    row = {**evidence(), "claim": "The plan costs USD 59 per workspace.", "supporting_excerpt": "USD 59 per workspace per month."}
    outcome = ResearchHandoff(task_id="main-research", research_topic="pricing", requirement_ids=[rid],
        compressed_research="USD 59 includes access to the unverified login address.", evidence_registry=[row],
        termination="research_complete")
    first = HandoffAssessment(accepted=False, relevance=5, source_quality=5, evidence_coverage=5, groundedness=4,
        reason="unsupported login link", deterministic_checks={"passed": True}, unsupported_claims=["login address"],
        hard_rejection_reasons=["unsupported_claims"], requirement_coverage=[{"requirement_id": rid,
            "status": "supported", "evidence_ids": [row["evidence_id"]], "explanation": "explicit price"}])
    final = HandoffAssessment.model_validate({**first.model_dump(), "accepted": True, "admission_status": "accepted",
        "unsupported_claims": [], "hard_rejection_reasons": []})
    replay = outcome.model_copy(deep=True)
    evaluate = AsyncMock(side_effect=[first, final])
    monkeypatch.setattr(research_quality, "evaluate_subagent_handoff", evaluate)
    monkeypatch.setattr(efficiency, "research_budget_reason", AsyncMock(return_value=budget_stop))
    config = {"metadata": {"run_config_schema_version": 25}}
    quality = research_quality.NativeResearchQuality(SimpleNamespace(recovery=SimpleNamespace(research_cache=cache)), lambda: config)
    result = await quality.handoff(outcome, coverage)
    assert evaluate.await_count == expected_calls
    if budget_stop:
        assert not result.accepted and "summary_repair" not in outcome.assessment
    else:
        assert result.accepted and "login" not in outcome.compressed_research
        assert "login" in outcome.assessment["summary_repair"]["original_summary"]
        assert row["supporting_excerpt"] in outcome.compressed_research
        assert evaluate.await_args.args[1]["evidence_registry"] == [row]
        monkeypatch.setattr(efficiency, "research_budget_reason", AsyncMock(return_value="report_budget_reserved"))
        restored = await quality._repair_main_handoff(replay, coverage, first, config)
        assert restored.accepted and replay.compressed_research == outcome.compressed_research
        assert evaluate.await_count == expected_calls


@pytest.mark.asyncio
async def test_monthly_reservation_rollback_and_late_bill_keep_semantic_isolation(pg_url, monkeypatch):
    import asyncio
    from uuid import uuid4
    from sqlalchemy import select
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore, UnknownOperation
    from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
    from open_deep_research.agentscope_runtime.spend_reconciliation import reconcile_spend
    from open_deep_research.platform import quota, runtime, schema
    from open_deep_research.platform.store import PlatformStore
    native = RecoveryStore(pg_url)
    platform = PlatformStore(native.engine)
    monkeypatch.setattr(platform, "limits", AsyncMock(return_value={"monthly_cost_micro_usd": 26}))
    monkeypatch.setattr(quota, "get_store", lambda: platform)
    monkeypatch.setattr(runtime, "enabled", lambda: True)
    owner, run = str(uuid4()), uuid4().hex
    key = "gateway:model:price"
    try:
        await native.create_tables()
        async with native.engine.begin() as conn:
            await conn.run_sync(schema.metadata.create_all)
        await native.create_run(owner, ResearchSnapshot(run_id=run, config_fingerprint="frozen"),
                                limits={"cost_micro_usd": 200})
        lease = await native.acquire(run, owner)
        async def start(amount):
            return await native.begin_operation(lease, key, "gateway:model", "request", reserve={"cost_micro_usd": amount})
        with pytest.raises(quota.QuotaExceeded) as refused:
            await start(40)
        assert (refused.value.available, refused.value.required) == (26, 40)
        with monkeypatch.context() as patch:
            patch.setattr(native, "_begin_operation", AsyncMock(side_effect=asyncio.CancelledError))
            with pytest.raises(asyncio.CancelledError):
                await start(20)
        assert (await quota.runway(owner))["reserved"] == 0
        await start(20)
        await native.release(lease)
        lease = await native.acquire(run, owner)
        await quota.reconcile_run_reservations(native, lease)
        assert (await quota.runway(owner))["reserved"] == 20
        with pytest.raises(UnknownOperation):
            await start(20)
        bill = {"request_id": "paid", "request_tags": [f"run:{run}", "operation:price"],
                "prompt_tokens": 5, "completion_tokens": 4, "spend": "0.000009"}
        assert (await reconcile_spend(native, lease, [bill, {**bill, "request_id": "other"}]))["unresolved"] == [key]
        for _ in range(2):
            assert (await reconcile_spend(native, lease, [bill]))["corrected"] == [key]
        result = await quota.runway(owner)
        assert (result["settled"], result["reserved"]) == (9, 0)
        assert (await native.operation_record(lease, key))["state"] == "quarantined"
        with pytest.raises(UnknownOperation):
            await start(20)
        async with native.engine.connect() as conn:
            assert await conn.scalar(select(schema.charges.c.actual).where(schema.charges.c.owner_id == owner)) == 9
    finally:
        await native.aclose()

"""Source-quality failures reproduced without paid calls or live services."""
import pytest

from open_deep_research.agentscope_runtime.source_planning import SourceVerification, verified_entry, website_entrypoint
from open_deep_research.evidence import classify_evidence_source


def source_contract():
    text = "Acme maintains this website. [Documentation](https://acme.example/docs) and [source repository](https://github.com/acme/engine)."
    entry = verified_entry("Acme", "https://acme.example/", text, SourceVerification(
        entity_quote="Acme maintains this website.", ownership_quote="Acme maintains this website.",
        documentation_urls=["https://acme.example/docs"], related_websites=[{
            "url": "https://github.com/acme/engine", "relationship_quote": text}]))
    return {"requirements": [{"requirement_id": "sources", "kind": "process", "text": "Only use official sources."}],
        "source_plan": {"status": "confirmed", "intent": "official_only", "entries": [entry]}}


def test_search_provider_is_not_a_subject_unless_the_user_also_researches_it():
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime.source_planning import research_entities
    entities = [SimpleNamespace(name=name) for name in ("Acme", "DeepSeek")]
    rule = "使用DeepSeek检索官方资料。"
    requirements = [{"kind": "process", "text": rule}]
    assert [e.name for e in research_entities("研究Acme。" + rule, entities, requirements, ["deepseek"])] == ["Acme"]
    assert [e.name for e in research_entities("对比Acme与DeepSeek。" + rule, entities, requirements, ["deepseek"])] == ["Acme", "DeepSeek"]
    entities += [SimpleNamespace(name="Beta"), SimpleNamespace(name="Kafka")]
    assert [e.name for e in research_entities("对Acme与Beta进行竞品研究。场景是Kafka持续写入。" + rule,
        entities, requirements, ["deepseek"])] == ["Acme", "Beta"]


def test_confirmed_website_and_linked_repository_do_not_admit_unrelated_github_content():
    assert website_entrypoint("https://acme.example/docs/latest") == "https://acme.example/"
    assert website_entrypoint("https://github.com/acme/engine") == "https://github.com/acme/engine"
    contract = source_contract()
    for url in ("https://acme.example/licensing", "https://github.com/acme/engine/blob/main/LICENSE"):
        assert classify_evidence_source({"source_url": url}, contract).source_scope_status.value == "in_scope"
    for url in ("https://github.com/other/engine", "https://github.com/acme/engine/issues/1"):
        assert classify_evidence_source({"source_url": url}, contract).source_scope_status.value == "out_of_scope"
    contract["source_selection"] = {"mode": "specific", "sources": [{"type": "url", "url": "https://acme.example/docs/start"}]}
    assert classify_evidence_source({"source_url": "https://acme.example/licensing"}, contract).source_scope_status.value == "out_of_scope"


@pytest.mark.asyncio
async def test_deepseek_uses_confirmed_site_and_bounded_retry_without_promoting_filtered_sources(monkeypatch):
    from open_deep_research.agentscope_runtime.search_providers import SearchResources, SearchService
    from open_deep_research.web.models import SearchRequest
    service = SearchService({"configurable": {"search_providers": ["deepseek"]},
        "metadata": {"coverage_contract": source_contract()}}, None, SearchResources())
    calls = []
    async def search(query, request):
        calls.append(query)
        if "github.com" in query:
            return [{"url": "https://github.com/unrelated/project"}], ""
        if "too many unnecessary words" in query:
            return [{"url": "https://third-party.example/post"}], ""
        return [{"url": "https://acme.example/docs/counting", "content": "Exact counting."}], ""
    monkeypatch.setattr(service, "_deepseek", search)
    result = await service.discover(SearchRequest(objective="Acme counting", queries=["Acme counting defaults with too many unnecessary words"]))
    assert {r.canonical_url for r in result.candidates} == {"https://acme.example/docs/counting"}
    assert all("site:" in q for q in calls)
    assert len(calls) <= 4
    assert result.search_calls == len(calls)
    assert sum(r.filtered_result_count for r in result.provider_results) > 0


def test_raw_read_keeps_navigation_but_evidence_extraction_does_not():
    from open_deep_research.agentscope_runtime.search_providers import candidate
    from open_deep_research.web.extraction import extract_html, text_document, needs_extraction_fallback
    from open_deep_research.web.fetching import RawFetch
    from open_deep_research.web.models import FetchResult
    source = candidate("direct", "https://acme.example/", "Acme", "", 1, "")
    raw = RawFetch(FetchResult(candidate_id=source.candidate_id, requested_url=source.canonical_url,
        final_url=source.canonical_url, success=True, content_type="text/html"),
        b'<body><nav><a href="https://github.com/acme/engine">Source code</a></nav><main>Acme official product documentation.</main></body>')
    assert "github.com/acme/engine" in extract_html(source, raw, include_navigation=True).markdown
    assert "github.com/acme/engine" not in extract_html(source, raw, evidence_fidelity=True).markdown
    index = text_document(source, "# Settings\n\n## Browse session settings\n\n/settings", extractor="deepseek")
    assert needs_extraction_fallback(index, evidence=True)
    assert not needs_extraction_fallback(index, evidence=False)
    fact = text_document(source, "By default, exact counting is enabled.", extractor="deepseek")
    assert not needs_extraction_fallback(fact, evidence=True)


def test_unassessed_task_ownership_and_default_qualifications_survive_report_selection():
    from open_deep_research.agentscope_runtime.report import task_evidence
    from open_deep_research.report.evidence_synthesis import _select_evidence_for_budget
    from open_deep_research.report.writing import project_evidence
    background = [{"evidence_id": f"intro-{i}", "source_url": "https://acme.example/delete",
        "claim": "Deletion", "supporting_excerpt": "The engine supports deleting records from tables."} for i in range(60)]
    fact = {"evidence_id": "visibility", "source_url": "https://acme.example/delete", "supporting_excerpt":
        "By default DELETE waits until marking is completed; asynchronous requests may return before rows disappear."}
    records = task_evidence([{"requirement_ids": ["delete"], "evidence_registry": [*background, fact]}])
    selected = _select_evidence_for_budget(project_evidence(records), token_budget=1200, max_records=5,
        requirements=[{"requirement_id": "delete", "text": "删除的查询可见性与默认行为"}])
    assert "visibility" in {r["evidence_id"] for r in selected}
    assert "requirement_ids" not in fact  # Ownership projection does not mutate accepted source records.


@pytest.mark.asyncio
async def test_missing_url_returns_observed_search_links_without_fetching_or_admitting_them(monkeypatch, tmp_path):
    import json
    from test_web_upgrade import Models, config, context
    from open_deep_research.agentscope_runtime.web_tools import FetchUrlInput, WebFetchLedger, fetch_url_tool
    from open_deep_research.agentscope_runtime.search_providers import SearchService, candidate
    from open_deep_research.web import pipeline
    from open_deep_research.web.models import FetchResult, SearchBatch
    calls = []
    async def missing(source, settings, **kwargs):
        calls.append(source.canonical_url)
        return pipeline.RawFetch(FetchResult(candidate_id=source.candidate_id,
            requested_url=source.canonical_url, failure_class="http_404"))
    async def discover(self, request):
        assert request.allowed_domains == ["acme.example"]
        return SearchBatch(candidates=[candidate("deepseek", "https://acme.example/docs/current", "Current guide", "", 1, "query")])
    monkeypatch.setattr(pipeline, "fetch_local", missing)
    monkeypatch.setattr(SearchService, "discover", discover)
    cfg = config(fetch_backend_order=["local"], search_providers=["deepseek"], runs_dir=str(tmp_path))
    cfg["metadata"]["coverage_contract"] = source_contract()
    result = await fetch_url_tool(lambda: cfg, Models(), WebFetchLedger()).call(
        FetchUrlInput(url="https://acme.example/docs/old.html", objective="Acme configuration"), context(cfg))
    payload = json.loads(result.output)
    assert payload["navigation"]["links"] == ["https://acme.example/docs/current"]
    assert payload["navigation"]["evidence_eligible"] is False
    assert not payload.get("evidence")
    assert calls == ["https://acme.example/docs/old.html"]
    assert result.metadata["physical_fetches"] == 1

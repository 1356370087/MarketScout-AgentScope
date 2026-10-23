"""DeepSeek Search/Fetch contracts and opt-in real API integration.

Run the live test with DEEPSEEK_WEB_LIVE=1 and DEEPSEEK_API_KEY in the environment.
"""

import asyncio
import json
import os
import re
from datetime import UTC, datetime

import httpx
import pytest

from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.agentscope_runtime.search import search_provider_tools
from open_deep_research.agentscope_runtime.search_providers import (
    SearchProviderError,
    SearchResources,
    SearchService,
    deepseek_request,
)
from open_deep_research.agentscope_runtime.web_fetch_backends import configured_fetch_backends
from open_deep_research.agentscope_runtime.web_tools import FetchUrlInput, WebFetchLedger, fetch_url_tool
from open_deep_research.configuration import Configuration, SearchAPI
from open_deep_research.sandbox.egress_context import egress_authorizer
from open_deep_research.sandbox.gateway_client import split_gateway_registration
from open_deep_research.sandbox.policy import allowed_domains
from open_deep_research.tools.base import ToolContext
from open_deep_research.web.models import SearchRequest
from open_deep_research.web.pipeline import clear_run_web_cache
from open_deep_research.web.settings import WebPipelineSettings
from open_deep_research.web.sources import PROVIDER_URLS


def config(**values):
    return {
        "configurable": {
            "search_api": "deepseek",
            "fetch_backend_order": ["deepseek"],
            "external_extract_backends": ["deepseek"],
            "respect_robots_txt": False,
            "event_log_enabled": False,
            "apiKeys": {"DEEPSEEK_API_KEY": "fixture-deepseek-key"},
            **values,
        },
        "metadata": {
            "run_id": "deepseek-web-test",
            "task_id": "task",
            "sandbox_gateway_physical": True,
        },
    }


def resources_for(handler):
    return SearchResources({
        "deepseek": lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    })


@pytest.fixture(autouse=True)
def clear_cache():
    clear_run_web_cache("deepseek-web-test")
    yield
    clear_run_web_cache("deepseek-web-test")


def test_configuration_freezes_selection_without_credentials(monkeypatch):
    cfg = config(search_providers=["tavily", "deepseek"])
    frozen = RunConfig.compile(cfg)
    assert "fixture-deepseek-key" not in json.dumps(frozen.snapshot())
    monkeypatch.setenv("SEARCH_PROVIDERS", '["bing"]')
    monkeypatch.setenv("FETCH_BACKEND_ORDER", '["local"]')
    restored = RunConfig.restore(frozen.snapshot())
    assert restored.get("search_providers") == ["tavily", "deepseek"]
    assert restored.get("fetch_backend_order") == ["deepseek"]
    assert restored.get("external_extract_backends") == ["deepseek"]
    assert Configuration(search_api="deepseek").resolved_search_providers == [SearchAPI.DEEPSEEK]
    assert "api.deepseek.com" in allowed_domains(Configuration(
        search_api="none", external_extract_backends=["deepseek"],
    ))


@pytest.mark.parametrize("isolation", [False, True])
def test_gateway_transfers_deepseek_credentials_only_to_the_vault(monkeypatch, isolation):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", str(isolation).lower())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-deepseek-key")
    cfg = config(apiKeys={"DEEPSEEK_API_KEY": "per-run-key"} if isolation else {})
    frozen, credentials = split_gateway_registration(cfg)
    assert credentials["DEEPSEEK_API_KEY"] == ("per-run-key" if isolation else "environment-deepseek-key")
    assert "apiKeys" not in frozen["configurable"]
    assert credentials["DEEPSEEK_API_KEY"] not in json.dumps(frozen)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_search_maps_results_and_enforces_domain_filters(legacy):
    async def http(request):
        assert str(request.url) == PROVIDER_URLS["deepseek"]
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer fixture-deepseek-key"
        assert json.loads(request.content) == {
            "query": json.loads(request.content)["query"],
            "ori_question": "API contract",
            "count": 10,
            "need_content": legacy,
        }
        assert json.loads(request.content)["query"] in {
            "API site:docs.example -site:bad.docs.example after:2026-10-01",
            "API site:other.example -site:bad.docs.example after:2026-10-01",
        }
        return httpx.Response(200, json={"results": [
            {"url": "https://docs.example/api", "title": "Docs", "summary": "Long summary", "snippet": "Short", "content": "Page body"},
            {"url": "https://other.example/api", "title": "Other", "snippet": "Short"},
            {"url": "https://outside.example/api"},
            {"url": "https://bad.docs.example/api"},
            {"url": "file:///private"},
        ]})

    resources = resources_for(http)
    try:
        batch = await SearchService(config(), None, resources, legacy=legacy).discover(SearchRequest(
            objective="API contract", queries=["API"],
            allowed_domains=["docs.example", "other.example"],
            blocked_domains=["bad.docs.example"], published_after=datetime(2026, 10, 1, tzinfo=UTC),
        ))
        assert not batch.errors
        assert [c.domain for c in batch.candidates] == ["docs.example", "other.example"]
        assert batch.candidates[0].snippet == "Long summary"
        assert batch.candidates[1].snippet == "Short"
        assert batch.candidates[0].content_hint == ("Page body" if legacy else None)
        assert batch.search_calls == 2
        assert batch.provider_results[0].raw_result_count == 10
        assert batch.provider_results[0].filtered_result_count == 6
        assert all(c.discoveries[0].provider == "deepseek" for c in batch.candidates)
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_deepseek_and_tavily_merge_shared_provenance():
    class Tavily:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://docs.example/shared", "title": "Docs"}]}

    async def http(request):
        return httpx.Response(200, json={"results": [{"url": "https://docs.example/shared"}]})

    resources = resources_for(http)
    resources.factories["tavily"] = lambda _: Tavily()
    try:
        batch = await SearchService(config(search_providers=["tavily", "deepseek"]), None, resources).discover(
            SearchRequest(objective="API", queries=["API"]))
        assert not batch.errors and len(batch.candidates) == 1
        assert {d.provider for d in batch.candidates[0].discoveries} == {"tavily", "deepseek"}
        assert batch.search_calls == 2
    finally:
        await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [" ", "长" * 129])
async def test_invalid_search_query_does_not_send_a_request(query):
    resources = SearchResources()
    batch = await SearchService(config(), None, resources).discover(SearchRequest(objective="API", queries=[query]))
    assert batch.errors == ["deepseek:deepseek_invalid_query"]
    assert batch.search_calls == 0 and not resources.clients


@pytest.mark.asyncio
async def test_multi_vendor_search_splits_filters_and_keeps_provenance():
    domains = ["elastic.co", "aws.amazon.com", "docs.aws.amazon.com", "learn.microsoft.com", "azure.microsoft.com"]
    queries = ["RAG vector document permissions and pricing", "multi tenant hybrid search capacity"]
    calls, progress = [], []

    async def http(request):
        payload = json.loads(request.content)
        calls.append(payload["query"])
        assert len(payload["query"]) <= 128
        assert "-site:blocked.elastic.co after:2026-10-01" in payload["query"]
        sites = re.findall(r"(?<!-)site:([\w.]+)", payload["query"])
        return httpx.Response(200, json={"results": [
            *[{"url": f"https://{site}/docs", "summary": "Official"} for site in sites],
            {"url": "https://outside.example/docs"},
            {"url": "https://blocked.elastic.co/docs"},
        ]})

    async def observe(event, **payload):
        progress.append((event, payload))

    resources = resources_for(http)
    try:
        cfg = config()
        cfg["metadata"]["source_selection"] = {"mode": "specific", "sources": [
            {"type": "domain", "domain": domain} for domain in domains]}
        service = SearchService(cfg, None, resources, progress=observe)
        batch = await service.discover(SearchRequest(objective="RAG", queries=queries,
            allowed_domains=["unapproved.example"], blocked_domains=["blocked.elastic.co"],
            published_after=datetime(2026, 10, 1, tzinfo=UTC)))
        assert not batch.errors
        assert {c.domain for c in batch.candidates} == set(domains)
        assert batch.search_calls == len(calls) > len(queries)
        assert batch.provider_results[0].query_count == 2
        assert batch.provider_results[0].provider_requests == len(calls)
        assert batch.provider_results[0].status == "completed"
        for query in queries:
            scoped = [call for call in calls if call.startswith(query + " ")]
            assert {site for call in scoped for site in re.findall(r"(?<!-)site:([\w.]+)", call)} == set(domains)
        assert {d.query for c in batch.candidates for d in c.discoveries} == set(queries)
        completed = [payload for event, payload in progress if event == "query_completed"]
        assert {p["effective_query"] for p in completed} == set(calls)
        assert all(p["attempted_queries"] == [p["effective_query"]] for p in completed)
        assert len([payload for event, payload in progress if event == "query_started"]) == len(calls)
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_invalid_provider_query_is_not_counted_as_a_physical_pipeline_search():
    from open_deep_research.web.pipeline import WebResearchPipeline
    from open_deep_research.web.models import SearchBatch

    async def search(request):
        return SearchBatch(errors=["deepseek:deepseek_invalid_query"], search_calls=0)

    engine = WebResearchPipeline(search=search, settings=WebPipelineSettings())
    result = await engine.run(SearchRequest(objective="RAG", queries=["x" * 129]))
    assert result.errors == ["deepseek:deepseek_invalid_query"]
    assert result.gap_analysis.budget.search_calls == 0


@pytest.mark.asyncio
async def test_physical_query_progress_is_distinct_and_replay_idempotent(monkeypatch):
    from unittest.mock import AsyncMock
    from open_deep_research.agentscope_runtime import web_progress

    publish = AsyncMock()
    monkeypatch.setattr(web_progress, "publish_task_activity", publish)
    progress = web_progress.WebProgress(config(), task_id="task", tool_call_id="tool", operation_id="operation")
    common = {"provider":"deepseek", "query_index":0, "query":"RAG", "result_status":"usable"}
    for effective in ("RAG site:elastic.co", "RAG site:learn.microsoft.com", "RAG site:elastic.co"):
        await progress("query_completed", **common, effective_query=effective)
    keys = [call.kwargs["dedupe_key"] for call in publish.await_args_list]
    assert keys[0] != keys[1] and keys[0] == keys[2]


@pytest.mark.asyncio
async def test_explicit_vendor_queries_are_not_broadened_and_later_vendors_remain_visible():
    queries = ["Elastic RRF site:elastic.co", "AWS FGAC site:docs.aws.amazon.com", "Azure ACL site:learn.microsoft.com"]
    calls = []

    async def http(request):
        query = json.loads(request.content)["query"]
        calls.append(query)
        host = re.search(r"site:([\w.]+)",query)[1]
        return httpx.Response(200,json={"results":[{"url":f"https://{host}/doc/{i}"} for i in range(10)]})

    resources = resources_for(http)
    try:
        batch = await SearchService(config(),None,resources).discover(SearchRequest(objective="RAG",queries=queries,
            candidate_limit=3,allowed_domains=["elastic.co","aws.amazon.com","docs.aws.amazon.com","learn.microsoft.com","azure.microsoft.com"]))
        assert set(calls) == set(queries)
        assert batch.search_calls == 3 and not batch.errors
        assert [c.domain for c in batch.candidates] == ["elastic.co","docs.aws.amazon.com","learn.microsoft.com"]
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_split_search_retains_success_when_one_batch_fails():
    calls = []

    async def http(request):
        query = json.loads(request.content)["query"]
        calls.append(query)
        if "site:learn.microsoft.com" in query:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"results": [{"url": "https://elastic.co/docs"}]})

    resources = resources_for(http)
    try:
        batch = await SearchService(config(), None, resources).discover(SearchRequest(
            objective="RAG", queries=["vector search document security and tenant isolation architecture"],
            allowed_domains=["elastic.co", "learn.microsoft.com", "azure.microsoft.com", "docs.aws.amazon.com"]))
        assert batch.errors == ["deepseek:rate_limited"]
        assert [c.domain for c in batch.candidates] == ["elastic.co"]
        assert batch.provider_results[0].status == "partial"
        assert batch.search_calls == len(calls) > 1
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_split_search_bounds_physical_requests_and_checks_egress_each_time():
    calls, approvals = [], []

    async def authorize(url, capability, consume=False):
        approvals.append((url, capability, consume))
        return "allow"

    async def http(request):
        calls.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": []})

    resources = resources_for(http)
    token = egress_authorizer.set(authorize)
    try:
        batch = await SearchService(config(), None, resources).discover(SearchRequest(
            objective="RAG", queries=["hybrid vector security" + " architecture" * 5, "hybrid vector capacity" + " architecture" * 5],
            allowed_domains=[f"vendor-{i:02}.example" for i in range(30)]))
        assert batch.search_calls == len(calls) == len(approvals) == 24
        assert batch.provider_results[0].status == "partial"
        assert batch.errors == ["deepseek_domain_query_limit_exceeded:60:24"]
        assert all(item == (PROVIDER_URLS["deepseek"], "search.provider", True) for item in approvals)
        assert {query[:22] for query in calls} == {"hybrid vector security", "hybrid vector capacity"}
    finally:
        egress_authorizer.reset(token)
        await resources.aclose()


@pytest.mark.asyncio
async def test_missing_run_key_cannot_fall_back_in_isolation_mode(monkeypatch):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-used")
    cfg = config(apiKeys={})
    cfg["metadata"].pop("sandbox_gateway_physical")
    resources = SearchResources()
    batch = await SearchService(cfg, None, resources).discover(SearchRequest(objective="API", queries=["API"]))
    assert batch.errors == ["deepseek:missing_credential"]
    assert batch.search_calls == 0 and not resources.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (402, "http_402"), (429, "rate_limited"), (504, "http_504")])
async def test_search_errors_are_sanitized_without_hidden_retries(status, code):
    calls = []

    async def http(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "fixture-deepseek-key PRIVATE_BODY"}})

    resources = resources_for(http)
    try:
        batch = await SearchService(config(), None, resources).discover(SearchRequest(objective="API", queries=["API"]))
        assert batch.errors == [f"deepseek:{code}"]
        assert len(calls) == batch.search_calls == 1
        assert "PRIVATE_BODY" not in batch.model_dump_json()
        assert "fixture-deepseek-key" not in batch.model_dump_json()
    finally:
        await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"results": None}, {"results": ["invalid"]}, []])
async def test_invalid_search_responses_are_provider_failures(data):
    resources = resources_for(lambda _: httpx.Response(200, json=data))
    try:
        batch = await SearchService(config(), None, resources).discover(SearchRequest(objective="API", queries=["API"]))
        assert batch.errors == ["deepseek:invalid_provider_response"]
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_empty_search_is_success_and_long_objective_is_omitted():
    async def http(request):
        assert "ori_question" not in json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    resources = resources_for(http)
    try:
        batch = await SearchService(config(), None, resources).discover(SearchRequest(objective="x" * 129, queries=["API"]))
        assert not batch.errors and not batch.candidates
        assert batch.provider_results[0].result_status == "empty"
    finally:
        await resources.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("response,code", [
    (httpx.Response(200, text="x" * 33), "provider_response_too_large"),
    (httpx.Response(200, text="{broken"), "invalid_provider_response"),
    (httpx.Response(302, headers={"Location": "https://outside.example"}), "provider_redirect_refused"),
])
async def test_shared_transport_bounds_bodies_and_refuses_redirects(response, code):
    resources = resources_for(lambda _: response)
    try:
        with pytest.raises(SearchProviderError, match=code):
            await deepseek_request(PROVIDER_URLS["deepseek"], {"query": "API"}, config(), resources, max_bytes=32)
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_search_preserves_cancellation():
    async def http(request):
        raise asyncio.CancelledError()

    resources = resources_for(http)
    try:
        with pytest.raises(asyncio.CancelledError):
            await SearchService(config(), None, resources).discover(SearchRequest(objective="API", queries=["API"]))
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_fetch_url_uses_deepseek_markdown_and_the_shared_client(monkeypatch):
    from open_deep_research.agentscope_runtime import web_fetch_backends as backends

    async def public(url):
        return url

    monkeypatch.setattr(backends, "validate_public_http_url", public)
    calls = []
    markdown = "# API\n\n```python\nprint('example')\n```\n\nMarkdown from the official source."

    async def http(request):
        assert request.headers["Authorization"] == "Bearer fixture-deepseek-key"
        assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://docs.example/api"}
        calls.append(request)
        return httpx.Response(200, json={"title": "API", "content": markdown})

    cfg = config()
    resources = resources_for(http)
    try:
        client = resources.client("deepseek", cfg)
        tool = fetch_url_tool(lambda: cfg, None, WebFetchLedger(), resources=resources)
        result = await tool.call(FetchUrlInput(url="https://docs.example/api", mode="markdown"), ToolContext(
            config=cfg, role="researcher", tool_call_id="fetch", operation_id="fetch"))
        output = json.loads(result.output)
        assert output["markdown"] == markdown
        assert output["evidence_eligible"] is False
        assert result.metadata["physical_fetches"] == 1
        assert resources.client("deepseek", cfg) is client and len(calls) == 1
    finally:
        await resources.aclose()
    assert client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["search.provider", "external.extract"])
async def test_provider_service_egress_denial_prevents_the_api_call(monkeypatch, capability):
    from open_deep_research.agentscope_runtime import web_fetch_backends as backends

    async def public(url):
        return url

    async def authorize(url, name, consume=False):
        return "deny" if name == capability else "allow"

    monkeypatch.setattr(backends, "validate_public_http_url", public)
    resources = SearchResources()
    token = egress_authorizer.set(authorize)
    try:
        if capability == "search.provider":
            batch = await SearchService(config(), None, resources).discover(SearchRequest(objective="API", queries=["API"]))
            assert batch.errors == ["deepseek:provider_egress_not_allowed"] and batch.search_calls == 0
        else:
            fetch = configured_fetch_backends(config(), WebPipelineSettings(respect_robots_txt=False), resources)["deepseek"]
            with pytest.raises(PermissionError, match="external_service_not_allowed"):
                await fetch("https://docs.example/api")
        assert not resources.clients
    finally:
        egress_authorizer.reset(token)


@pytest.mark.asyncio
async def test_disabled_fetch_backend_does_not_make_target_requests():
    resources = SearchResources()
    fetch = configured_fetch_backends(config(external_extract_backends=[]), WebPipelineSettings(), resources)["deepseek"]
    with pytest.raises(SearchProviderError, match="extractor_disabled"):
        await fetch("https://docs.example/api")
    assert not resources.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [False, True])
async def test_fetch_rejects_source_scope_and_private_targets_before_service_requests(unsafe):
    resources = SearchResources()
    cfg = config()
    if not unsafe:
        cfg["metadata"]["source_selection"] = {
            "mode": "specific", "sources": [{"type": "url", "url": "https://docs.example/api"}],
        }
    fetch = configured_fetch_backends(cfg, WebPipelineSettings(), resources)["deepseek"]
    with pytest.raises(PermissionError, match="source_scope_denied"):
        await fetch("http://127.0.0.1/private" if unsafe else "https://outside.example/api")
    assert not resources.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", None, ["invalid"]])
async def test_fetch_distinguishes_empty_content_from_invalid_responses(monkeypatch, content):
    from open_deep_research.agentscope_runtime import web_fetch_backends as backends

    async def public(url):
        return url

    monkeypatch.setattr(backends, "validate_public_http_url", public)
    resources = resources_for(lambda _: httpx.Response(200, json={"content": content}))
    try:
        fetch = configured_fetch_backends(config(), WebPipelineSettings(respect_robots_txt=False), resources)["deepseek"]
        if content == "":
            assert await fetch("https://docs.example/api") is None
        else:
            with pytest.raises(SearchProviderError, match="invalid_provider_response"):
                await fetch("https://docs.example/api")
    finally:
        await resources.aclose()


@pytest.mark.skipif(
    os.environ.get("DEEPSEEK_WEB_LIVE") != "1" or not os.environ.get("DEEPSEEK_API_KEY"),
    reason="需要显式设置 DEEPSEEK_WEB_LIVE=1 和 DEEPSEEK_API_KEY，才执行付费真实 API 集成测试",
)
@pytest.mark.asyncio
async def test_live_deepseek_search_and_fetch(monkeypatch):
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    cfg = config(apiKeys={}, respect_robots_txt=True)
    resources = SearchResources()
    try:
        batch = await SearchService(cfg, None, resources).discover(SearchRequest(
            objective="DeepSeek Search and Fetch API documentation", queries=["DeepSeek Search Fetch API"],
            allowed_domains=["api-docs.deepseek.com"], candidate_limit=3,
        ))
        assert not batch.errors, batch.errors
        assert batch.candidates and batch.search_calls == 1
        assert all(c.provider == "deepseek" and c.domain == "api-docs.deepseek.com" for c in batch.candidates)
        assert [t.name for t in search_provider_tools(lambda: cfg, None)] == ["web_search"]
        tool = fetch_url_tool(lambda: cfg, None, WebFetchLedger(), resources=resources)
        result = await tool.call(FetchUrlInput(
            url="https://api-docs.deepseek.com/zh-cn/guides/search", mode="markdown",
        ), ToolContext(config=cfg, role="researcher", tool_call_id="live-fetch", operation_id="live-fetch"))
        output = json.loads(result.output)
        assert len(output.get("markdown", "")) > 200, output
        assert "Search" in output["markdown"] and "Fetch" in output["markdown"]
        assert result.metadata["physical_fetches"] == 1
        print(json.dumps({"provider": "deepseek", "search_results": len(batch.candidates),
                          "search_requests": batch.search_calls, "fetch_requests": result.metadata["physical_fetches"],
                          "markdown_characters": len(output["markdown"])}))
    finally:
        await resources.aclose()

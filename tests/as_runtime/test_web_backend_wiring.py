"""Production adapter composition and evidence/budget boundaries."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_web_upgrade import Models, config, context

from open_deep_research.agentscope_runtime.search_providers import SearchResources
from open_deep_research.agentscope_runtime.web_tools import (
    FetchUrlInput,
    WebFetchLedger,
    fetch_url_tool,
)
from open_deep_research.web.pipeline import clear_run_web_cache


@pytest.fixture(autouse=True)
def cache():
    clear_run_web_cache("web-upgrade-test")
    yield
    clear_run_web_cache("web-upgrade-test")


@pytest.mark.asyncio
async def test_firecrawl_receives_run_key_and_validates_final_url(monkeypatch):
    from open_deep_research.agentscope_runtime import web_fetch_backends as backends

    async def public(url):
        return url

    monkeypatch.setattr(backends, "validate_public_http_url", public)
    seen = []

    async def http(request):
        assert request.headers["Authorization"] == "Bearer run-fire-key"
        seen.append(json.loads(request.content)["url"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "markdown": "This is a complete, verifiable fact from the fetched official documentation. "
                    * 8,
                    "metadata": {
                        "sourceURL": "https://docs.example/page",
                        "title": "Docs",
                    },
                }
            },
        )

    cfg = config(
        fetch_backend_order=["firecrawl"], apiKeys={"FIRECRAWL_API_KEY": "run-fire-key"}
    )
    cfg["metadata"]["sandbox_gateway_physical"] = True
    resources = SearchResources(
        {"firecrawl": lambda _: httpx.AsyncClient(transport=httpx.MockTransport(http))}
    )
    try:
        tool = fetch_url_tool(
            lambda: cfg, Models(), WebFetchLedger(), resources=resources
        )
        result = await tool.call(
            FetchUrlInput(url="https://docs.example/page"), context(cfg)
        )
        assert json.loads(result.output)["documents"][0]["extractor"] == "firecrawl"
        assert seen == ["https://docs.example/page"]
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_browser_navigation_and_snapshot_are_serialized_on_one_session(
    monkeypatch,
):
    from open_deep_research.agentscope_runtime import web_fetch_backends as backends
    from open_deep_research.tools import governance
    from open_deep_research.tools.base import ToolEffect, ToolResult

    current = ""
    calls = []
    session = object()
    tools = [
        SimpleNamespace(name=name, effect=ToolEffect.READ_ONLY, server=session)
        for name in ("browser_navigate", "browser_snapshot")
    ]

    async def public(url):
        return url

    async def governed(call, registry, role, config, **kwargs):
        nonlocal current
        assert (
            registry["browser_navigate"].server is registry["browser_snapshot"].server
        )
        calls.append(call["name"])
        if call["name"] == "browser_navigate":
            current = call["args"]["url"]
        await asyncio.sleep(0.005)
        return SimpleNamespace(
            error=None,
            result=ToolResult(
                output=f"### Page state\n- Page URL: {current}\n- Page Title: Documentation\n- paragraph: Documented behavior."
            ),
        )

    monkeypatch.setattr(backends, "validate_public_http_url", public)
    monkeypatch.setattr(governance, "execute_governed_tool_call_native", governed)
    renderer = backends.BrowserRenderer(tools, config())
    first, second = await asyncio.gather(
        renderer("https://docs.example/a"), renderer("https://docs.example/b")
    )
    assert first.final_url.endswith("/a") and second.final_url.endswith("/b")
    assert calls == [
        "browser_navigate",
        "browser_snapshot",
        "browser_navigate",
        "browser_snapshot",
    ]
    assert first.extractor == "playwright_mcp" and first.content_type == "text/plain"


def test_raw_reading_cannot_satisfy_the_evidence_gate():
    from open_deep_research.quality.gate import deterministic_tool_checks
    from open_deep_research.web.models import MarkdownReadResult

    payload = MarkdownReadResult(
        url="https://docs.example/page",
        markdown="A fact with a URL https://docs.example/page",
    )
    checks = deterministic_tool_checks(
        [{"name": "fetch_url", "content": payload.model_dump_json()}], min_sources=1
    )
    assert (
        not checks["passed"]
        and checks["structured_evidence_count"] == 0
        and checks["source_count"] == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True])
async def test_markdown_reading_renders_js_shells_but_keeps_short_static_pages(
    monkeypatch, dynamic
):
    from test_web_upgrade import document

    from open_deep_research.agentscope_runtime import web_tools
    from open_deep_research.web import pipeline
    from open_deep_research.web.models import FetchResult
    from open_deep_research.web.pipeline import RawFetch

    rendered = []

    async def local(source, settings, **kwargs):
        html = (
            b'<div id="root">Please enable JavaScript.</div>'
            if dynamic
            else b"<article><h1>Short documentation</h1><p>One useful sentence.</p></article>"
        )
        return RawFetch(
            FetchResult(
                candidate_id=source.candidate_id,
                requested_url=source.canonical_url,
                final_url=source.canonical_url,
                success=True,
                content_type="text/html",
            ),
            html,
        )

    async def render(url):
        rendered.append(url)
        return document(
            url,
            "Rendered documentation with an actual, complete description of the page.",
        )

    monkeypatch.setattr(pipeline, "fetch_local", local)
    monkeypatch.setattr(
        web_tools, "configured_fetch_backends", lambda *args: {"playwright": render}
    )
    cfg = config(fetch_backend_order=["local", "playwright"])
    result = await fetch_url_tool(lambda: cfg, Models(), WebFetchLedger()).call(
        FetchUrlInput(url="https://docs.example/page", mode="markdown"), context(cfg)
    )
    payload = json.loads(result.output)
    assert len(rendered) == int(dynamic)
    assert ("Rendered documentation" if dynamic else "Short documentation") in payload[
        "markdown"
    ]


def test_transport_refunds_are_bounded_per_task_and_grants_reset_announcements():
    cfg = config(max_fetches_per_run=10, max_fetches_per_researcher=2)
    ledger = WebFetchLedger()
    assert ledger.reserve("r", "t", 2, cfg)[0] == 2
    ledger.record_transport_failure("r", "t", 2)
    assert ledger.transport_failure_allowance("r", "t", cfg) == 0
    assert ledger.transport_failure_allowance("r", "other", cfg) == 2
    cfg = config(max_fetches_per_run=1, max_fetches_per_researcher=10)
    ledger = WebFetchLedger()
    ledger.reserve("r", "t", 1, cfg)
    assert [ledger.reserve("r", "t", 1, cfg)[2] for _ in range(3)] == [
        True,
        True,
        False,
    ]
    cfg["metadata"]["fetch_budget_extension"] = {"extra_fetches": 1}
    assert ledger.reserve("r", "t", 1, cfg)[0] == 1
    ledger.record_fetch("r", "t")
    assert ledger.reserve("r", "t", 1, cfg)[2] is True

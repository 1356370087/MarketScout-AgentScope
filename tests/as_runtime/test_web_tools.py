"""AS-A030: 原生 Web 证据流水线——额度、缓存、来源限制、robots、证据锚点。"""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from open_deep_research.agentscope_runtime.models import ModelFactory
from open_deep_research.agentscope_runtime.web_tools import (
    NativeEvidenceExtractor,
    NativeWebReranker,
    WebFetchLedger,
    compact_web_result,
    fetch_url_tool,
    web_research_tool,
)
from open_deep_research.tools.base import ToolContext
from open_deep_research.web.models import (
    CandidateSource,
    DocumentChunk,
    ExtractedDocument,
)

pytestmark = pytest.mark.asyncio


def _config(**configurable):
    return {
        "configurable": {
            "event_log_enabled": False,
            "search_api": "none",
            "web_pipeline_mode": "enforced",
            **configurable,
        },
        "metadata": {"run_id": "run-1", "task_id": "task-1"},
    }


def _ctx(cfg):
    return ToolContext(config=cfg, role="researcher", tool_call_id="t", operation_id="op")


class _StubFactory(ModelFactory):
    def __init__(self, rerank=None, evidence=None):
        pass


# monkeypatch 重排/证据模型调用（离线；确定性引擎自身不依赖网络）。
@pytest.fixture
def no_models(monkeypatch):
    async def failing_rerank(self, objective, candidates):
        return {}

    async def failing_evidence(self, objective, documents, chunks):
        return []

    monkeypatch.setattr(NativeWebReranker, "__call__", failing_rerank)
    monkeypatch.setattr(NativeEvidenceExtractor, "__call__", failing_evidence)


def _search_batch(sources):
    async def discover(request):
        from open_deep_research.web.models import SearchBatch

        return SearchBatch(candidates=sources)

    return discover


def _patch_engine(monkeypatch, *, documents_by_url=None, runs=None):
    """把引擎的物理抓取/提取换成离线确定性假实现。"""
    import open_deep_research.web.pipeline as engine

    documents_by_url = documents_by_url or {}

    async def fake_fetch_local(candidate, settings, *, redirect_allowed=None):
        from datetime import datetime

        from open_deep_research.web.models import FetchResult

        body = documents_by_url[candidate.canonical_url]
        encoded = body.encode("utf-8")
        return engine.RawFetch(
            result=FetchResult(
                candidate_id=candidate.candidate_id,
                requested_url=candidate.canonical_url,
                final_url=candidate.canonical_url,
                status_code=200,
                content_type="text/html",
                byte_count=len(encoded),
                content_hash=str(hash(body)),
                fetched_at=datetime.now(UTC),
                adapter="local",
                success=True,
            ),
            body=encoded,
        )

    def fake_extract(candidate, raw, settings):
        from open_deep_research.web.pipeline import canonicalize_url, stable_id

        text = raw.body.decode("utf-8")
        canonical = canonicalize_url(candidate.canonical_url)
        return ExtractedDocument(
            document_id=stable_id("doc", canonical),
            candidate_id=candidate.candidate_id,
            requested_url=candidate.original_url,
            final_url=raw.result.final_url or canonical,
            canonical_url=canonical,
            title=canonical,
            content_type="text",
            markdown=text,
            extractor="local",
            content_hash=str(hash(text)),
        )

    monkeypatch.setattr(engine, "fetch_local", fake_fetch_local)
    monkeypatch.setattr(engine, "extract_document", fake_extract)
    if runs is not None:
        monkeypatch.setattr(engine.WebResearchPipeline, "run", runs)


def _document(url, text, *, extractor="local"):
    from open_deep_research.web.pipeline import canonicalize_url, stable_id

    canonical = canonicalize_url(url)
    return ExtractedDocument(
        document_id=stable_id("doc", canonical),
        candidate_id=stable_id("src", canonical),
        requested_url=url,
        final_url=url,
        canonical_url=canonical,
        title=url,
        content_type="text",
        markdown=text,
        extractor=extractor,
        content_hash=str(hash(text)),
    )


def _candidate(url, provider="direct"):
    from open_deep_research.web.pipeline import canonicalize_url, stable_id

    canonical = canonicalize_url(url)
    return CandidateSource(
        candidate_id=stable_id("src", canonical),
        provider=provider,
        original_url=url,
        canonical_url=canonical,
        domain=canonical.split("/")[2],
    )


# ------------------------------------------------------------------- 额度


async def test_fetch_ledger_run_and_task_caps_with_release():
    ledger = WebFetchLedger()
    cfg = _config(max_fetches_per_run=3, max_fetches_per_researcher=2)
    granted, scope, announced = ledger.reserve("r", "t1", 2, cfg)
    assert (granted, scope, announced) == (2, "none", False)
    granted, scope, _ = ledger.reserve("r", "t1", 2, cfg)
    assert (granted, scope) == (0, "task")  # 任务上限先到
    # 另一任务仍可拿到 run 余量。
    granted, scope, _ = ledger.reserve("r", "t2", 2, cfg)
    assert (granted, scope) == (1, "none")
    granted, scope, _ = ledger.reserve("r", "t3", 1, cfg)
    assert (granted, scope) == (0, "run")
    # 归还后可复用。
    ledger.release("r", "t1", 2)
    granted, _, _ = ledger.reserve("r", "t1", 1, cfg)
    assert granted == 1
    # 零分配只播报两次。
    ledger.release("r", "t1", 1)
    ledger.reserve("r", "t1", 2, cfg)
    assert ledger.reserve("r", "t1", 1, cfg)[2] is True
    assert ledger.reserve("r", "t1", 1, cfg)[2] is False
    # transport 失败退款并计入失败配额。
    ledger.record_transport_failure("r", "t1", 1)
    assert ledger.transport_failure_allowance("r", "t1", cfg) >= 0


async def test_zero_allocation_skips_provider_search(monkeypatch, no_models):
    """零分配不烧搜索配额：前两次返回引擎的确定性耗尽结果，之后跳过播报。"""
    cfg = _config(max_fetches_per_run=1, max_fetches_per_researcher=1)
    ledger = WebFetchLedger()
    # 预先耗尽 run 上限，使工具调用进入零分配路径。
    assert ledger.reserve("run-1", "task-1", 1, cfg)[0] == 1
    tool = web_research_tool(
        lambda: cfg, _StubFactory(), ledger, tavily_client_factory=lambda _: None
    )
    args = tool.input_schema.model_validate({"objective": "o", "queries": ["q"]})
    first = await tool.call(args, _ctx(cfg))
    payload = json.loads(first.output)
    assert payload["gap_analysis"]["budget"]["exhausted"] is True
    assert payload["gap_analysis"]["budget"]["search_calls"] == 0
    assert payload["gap_analysis"]["decision"] == "budget_exhausted"
    await tool.call(args, _ctx(cfg))
    third = await tool.call(args, _ctx(cfg))
    assert "Fetch skipped" in third.output
    assert "zero-allocation" in third.output


class _TavilyWithResults:
    def __init__(self, results):
        self.results = results

    async def search(self, query, **kwargs):
        return {"query": query, "results": self.results}


async def test_web_research_budget_releases_unused_and_counts_physical(monkeypatch, no_models):
    cfg = _config(
        search_api="tavily",
        fetch_top_k=3,
        max_fetches_per_run=2,
        max_fetches_per_researcher=2,
        web_min_source_authority=0.0,
    )
    _patch_engine(
        monkeypatch, documents_by_url={"https://one.example/a": "First body. " * 40}
    )
    tavily = _TavilyWithResults(
        [{"url": "https://one.example/a", "title": "A", "content": "c"}]
    )
    ledger = WebFetchLedger()
    tool = web_research_tool(
        lambda: cfg,
        _StubFactory(),
        ledger,
        tavily_client_factory=lambda _: tavily,
    )
    result = await tool.call(
        tool.input_schema.model_validate({"objective": "o", "queries": ["q"]}),
        _ctx(cfg),
    )
    payload = json.loads(result.output)
    assert payload["gap_analysis"]["budget"]["fetch_attempts"] == 1
    assert payload["gap_analysis"]["budget"]["reserved_fetches"] == 2  # 悲观预留
    # 只发生 1 次物理抓取、预留 1：全部归还，额度可继续使用。
    granted, _, _ = ledger.reserve("run-1", "task-1", 2, cfg)
    assert granted == 1


# ------------------------------------------------------------------- 缓存


async def test_run_cache_avoids_second_physical_fetch(monkeypatch, no_models):
    cfg = _config(
        search_api="tavily",
        fetch_top_k=3,
        max_fetches_per_run=4,
        max_fetches_per_researcher=4,
        web_min_source_authority=0.0,
    )
    text = "Cached body sentence. " * 30
    _patch_engine(monkeypatch, documents_by_url={"https://cache.example/x": text})
    import open_deep_research.web.pipeline as engine

    original_fetch = engine.fetch_local
    fetches = []

    async def counting_fetch(candidate, settings, *, redirect_allowed=None):
        fetches.append(candidate.canonical_url)
        return await original_fetch(candidate, settings, redirect_allowed=redirect_allowed)

    monkeypatch.setattr(engine, "fetch_local", counting_fetch)

    tavily = _TavilyWithResults(
        [{"url": "https://cache.example/x", "title": "X", "content": "c"}]
    )
    ledger = WebFetchLedger()
    tool = web_research_tool(
        lambda: cfg, _StubFactory(), ledger, tavily_client_factory=lambda _: tavily
    )
    args = tool.input_schema.model_validate({"objective": "o", "queries": ["q"]})
    first = await tool.call(args, _ctx(cfg))
    second = await tool.call(args, _ctx(cfg))
    assert fetches == ["https://cache.example/x"]  # 第二次命中 run 缓存
    assert "run_cache" in second.output
    assert json.loads(first.output)["documents"]


# --------------------------------------------------------------- 来源限制


async def test_specific_source_boundary_filters_discovery(monkeypatch, no_models):
    cfg = _config(
        fetch_top_k=3,
        max_fetches_per_run=4,
        max_fetches_per_researcher=4,
        web_min_source_authority=0.0,
    )
    cfg["metadata"]["source_selection"] = {
        "mode": "specific",
        "sources": [
            {"type": "url", "url": "https://only.example/doc"},
            {"type": "domain", "domain": "only.example"},
        ],
    }
    _patch_engine(
        monkeypatch,
        documents_by_url={"https://only.example/doc": "Allowed body sentence here. " * 20},
    )
    ledger = WebFetchLedger()
    tool = web_research_tool(
        lambda: cfg, _StubFactory(), ledger, tavily_client_factory=lambda _: None
    )
    result = await tool.call(
        tool.input_schema.model_validate({"objective": "o", "queries": ["q"]}),
        _ctx(cfg),
    )
    payload = json.loads(result.output)
    # SPECIFIC 模式：只保留显式 URL/允许域候选。
    assert payload["candidates"]
    assert all(
        c["canonical_url"].startswith("https://only.example/")
        for c in payload["candidates"]
    )


async def test_fetch_url_rejects_out_of_boundary_url(monkeypatch, no_models):
    cfg = _config(
        fetch_top_k=3,
        max_fetches_per_run=2,
        max_fetches_per_researcher=2,
        web_min_source_authority=0.0,
    )
    cfg["metadata"]["source_selection"] = {
        "mode": "specific",
        "sources": [
            {"type": "url", "url": "https://inside.example/a"},
            {"type": "domain", "domain": "inside.example"},
        ],
    }
    _patch_engine(
        monkeypatch,
        documents_by_url={"https://sub.inside.example/b": "Inside sentence. " * 20},
    )
    tool = fetch_url_tool(lambda: cfg, _StubFactory(), WebFetchLedger())
    inside = await tool.call(
        tool.input_schema.model_validate(
            {"url": "https://sub.inside.example/b", "objective": "o"}
        ),
        _ctx(cfg),
    )
    assert json.loads(inside.output)["request"]
    with pytest.raises(ValueError, match="specific-source boundary"):
        await tool.call(
            tool.input_schema.model_validate(
                {"url": "https://outside.example/x", "objective": "o"}
            ),
            _ctx(cfg),
        )


# ------------------------------------------------------------------ robots


class _FakeRobotsResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self.charset = None
        self._body = body
        self.content = self

    async def read(self, limit=-1):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRobotsSession:
    def __init__(self, body=b"User-agent: *\nDisallow: /private\n"):
        self.body = body
        self.requested = []

    def get(self, url, allow_redirects=False):

        self.requested.append(url)
        return _FakeRobotsResponse(200, self.body)


async def test_robots_rules_are_path_sensitive(monkeypatch):
    import open_deep_research.web.pipeline as engine

    engine._ROBOTS_CACHE.clear()
    settings = engine.WebPipelineSettings(cache_namespace="robots-test")
    session = _FakeRobotsSession()
    async def ok(url):
        return None

    monkeypatch.setattr(engine, "validate_public_http_url", ok)
    monkeypatch.setattr(engine, "validate_response_peer", lambda response: None)
    allowed_open = await engine._robots_allowed(
        session, "https://site.example/open", settings
    )
    allowed_private = await engine._robots_allowed(
        session, "https://site.example/private/page", settings
    )
    assert allowed_open is True
    assert allowed_private is False
    # 路径敏感缓存：两条路径独立判定，互不共享（防止先允许后禁止的路径串扰）。
    assert len(session.requested) == 2
    engine._ROBOTS_CACHE.clear()


async def test_robots_unavailable_means_allowed(monkeypatch):
    import open_deep_research.web.pipeline as engine

    engine._ROBOTS_CACHE.clear()
    settings = engine.WebPipelineSettings(cache_namespace="robots-test-2")

    class Gone:
        def get(self, url, allow_redirects=False):
            return _FakeRobotsResponse(404)

    async def ok(url):
        return None

    monkeypatch.setattr(engine, "validate_public_http_url", ok)
    monkeypatch.setattr(engine, "validate_response_peer", lambda response: None)
    assert await engine._robots_allowed(
        Gone(), "https://missing.example/x", settings
    ) is True
    engine._ROBOTS_CACHE.clear()


# -------------------------------------------------------------- 证据锚点


async def test_evidence_anchor_validation():
    doc = _document("https://anchor.example/x", "unused")
    chunk_text = (
        "The verified launch happened in 2019 according to official records. "
        "The program continued afterward without interruption."
    )
    import hashlib

    chunk = DocumentChunk(
        chunk_id="chunk-1",
        document_id=doc.document_id,
        text=chunk_text,
        start_offset=0,
        end_offset=len(chunk_text),
        content_hash=hashlib.sha256(chunk_text.encode()).hexdigest(),
    )

    from agentscope.model import StructuredResponse

    class FakeFactory(ModelFactory):
        def __init__(self, items):
            self.items = items

        def policy_middleware(self, role, candidates=None):
            factory = self

            class M:
                class P:
                    async def invoke(self_, handler, input_kwargs, state):
                        return await handler(current_model=FakeModel(factory.items), messages=[])

                policy = P()

            return M()

    class FakeModel:
        def __init__(self, items):
            self.items = items

        async def generate_structured_output(self, messages, schema):
            return StructuredResponse(content={"items": self.items})

    good = {
        "chunk_id": "chunk-1",
        "claim": "Launch year",
        "supporting_excerpt": (
            "The verified launch happened in 2019 according to official records."
        ),
        "confidence": 0.9,
    }
    wrong_chunk = {**good, "chunk_id": "missing"}
    fragment = {
        **good,
        "supporting_excerpt": "launch happened",  # 非完整句
    }
    not_in_chunk = {
        **good,
        "supporting_excerpt": (
            "This sentence exists nowhere in the verified chunk whatsoever."
        ),
    }
    extractor = NativeEvidenceExtractor(
        FakeFactory([good, wrong_chunk, fragment, not_in_chunk])
    )
    records = await extractor("objective", {doc.document_id: doc}, [chunk])
    assert len(records) == 1
    record = records[0]
    assert record.chunk_id == "chunk-1"
    assert record.source_url == "https://anchor.example/x"
    assert record.locator.startswith("chars ")
    assert "2019" in record.supporting_excerpt


async def test_reranker_scores_only_known_candidates():
    from agentscope.model import StructuredResponse

    class FakeModel:
        async def generate_structured_output(self, messages, schema):
            return StructuredResponse(
                content={
                    "items": [
                        {
                            "candidate_id": "src-known",
                            "relevance": 0.8,
                            "authority": 0.9,
                            "information_gain": 0.5,
                        },
                        {
                            "candidate_id": "src-unknown",
                            "relevance": 1.0,
                            "authority": 1.0,
                            "information_gain": 1.0,
                        },
                    ]
                }
            )

    class FakeFactory(ModelFactory):
        def __init__(self):
            pass

        def policy_middleware(self, role, candidates=None):
            class M:
                class P:
                    async def invoke(self_, handler, input_kwargs, state):
                        return await handler(current_model=FakeModel(), messages=[])

                policy = P()

            return M()

    reranker = NativeWebReranker(FakeFactory())
    known = _candidate("https://known.example/a")
    known.candidate_id = "src-known"
    scores = await reranker("objective", [known])
    assert scores == {"src-known": (0.8, 0.9, 0.5)}


# ---------------------------------------------------------------- 压缩


async def test_compaction_sheds_audit_before_evidence():
    from open_deep_research.web.models import (
        BudgetSnapshot,
        GapAnalysis,
        SearchRequest,
        WebResearchResult,
    )

    cfg = _config(max_mcp_output_chars=2000)
    request = SearchRequest(objective="o", queries=["q"])
    long_text = "x" * 500
    result = WebResearchResult(
        request=request,
        gap_analysis=GapAnalysis(
            decision="continue",
            reason="continue",
            covered_dimensions=[],
            missing_dimensions=[],
            budget=BudgetSnapshot(search_calls=1),
        ),
        candidates=[_candidate(f"https://c.example/{i}") for i in range(10)],
        provider_syntheses=[
            __import__(
                "open_deep_research.web.models", fromlist=["ProviderSynthesis"]
            ).ProviderSynthesis(provider="openai", text=long_text)
            for _ in range(5)
        ],
        documents=[
            _document(f"https://d.example/{i}", long_text) for i in range(5)
        ],
        evidence=[
            __import__(
                "open_deep_research.web.models", fromlist=["EvidenceRecord"]
            ).EvidenceRecord(
                evidence_id=f"ev_{i}",
                claim="claim " + long_text[:50],
                supporting_excerpt="excerpt. " + long_text[:60],
                document_id="doc",
                chunk_id="chunk",
                locator="chars 0-1",
                source_url=f"https://e.example/{i}",
                source_title="t",
            )
            for i in range(5)
        ],
    )
    text = compact_web_result(result, cfg)
    payload = json.loads(text)
    assert len(text) <= 2000 - 256 + 300  # 压缩在预算附近
    # evidence 比 audit 列表活得久。
    assert len(payload["evidence"]) >= len(payload.get("provider_syntheses", []))


# ------------------------------------------------------ 外部异常提取回退


async def test_tavily_external_extractor(monkeypatch):
    from open_deep_research.agentscope_runtime.web_tools import _tavily_extract

    class FakeTavily:
        async def extract(self, urls, format):
            assert format == "markdown"
            return {
                "results": [
                    {"url": urls[0], "title": "T", "raw_content": "# Extracted"}
                ]
            }

    document = await _tavily_extract(
        "https://extract.example/a", lambda _: FakeTavily()
    )
    assert document is not None
    assert document.markdown == "# Extracted"
    assert document.extractor == "tavily_extract"

    class Empty:
        async def extract(self, urls, format):
            return {"results": []}

    assert await _tavily_extract("https://x.example", lambda _: Empty()) is None


# ------------------------------------------------------------- 工具装配


async def test_native_web_tools_pair_and_enabled_gates():
    from open_deep_research.agentscope_runtime.web_tools import native_web_tools

    cfg = _config()
    tools = native_web_tools(lambda: cfg, _StubFactory())
    assert [tool.name for tool in tools] == ["web_research", "fetch_url"]
    for tool in tools:
        assert tool.origin.value == "search"
        assert tool.is_enabled(cfg) is True
    legacy = _config(web_pipeline_mode="legacy")
    assert tools[0].is_enabled(legacy) is False
    # fetch_url 声明出网 URL。
    assert tools[1].egress_urls({"url": "https://egress.example/x"}) == [
        "https://egress.example/x"
    ]

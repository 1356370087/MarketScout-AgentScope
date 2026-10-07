"""Durable progress, cache safety and bounded native research regressions."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from open_deep_research.agentscope_runtime.efficiency import (
    ResearchCache,
    finish_requirements,
    merge_progress,
    progress_signature,
    stop_reason,
)
from open_deep_research.agentscope_runtime.recovery import RecoverySession
from open_deep_research.agentscope_runtime.recovery_store import (
    FenceLost,
    RecoveryStore,
    UnknownOperation,
)
from open_deep_research.agentscope_runtime.research_pipeline import ResearchSnapshot
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.agentscope_runtime.runtime_limits import attributed, limited
from open_deep_research.configuration import (
    RUN_CONFIG_FROZEN_FIELDS_V16,
    freeze_run_config,
    run_config_fingerprint,
)


@pytest_asyncio.fixture
async def cache(tmp_path):
    store = RecoveryStore(
        "sqlite+aiosqlite:///" + (tmp_path / "research.db").as_posix()
    )
    await store.create_tables()
    await store.create_run(
        "owner", ResearchSnapshot(run_id="efficiency", config_fingerprint="test")
    )
    session = await RecoverySession.open(store, "efficiency", "owner")
    value = ResearchCache(session, tmp_path)
    session.research_cache = value
    yield value
    await session.close()
    await store.aclose()


def test_v16_fingerprint_and_behavior_survive_new_defaults(monkeypatch):
    frozen = freeze_run_config({"configurable": {"enable_memory": False}})
    frozen["metadata"]["run_config_schema_version"] = 16
    frozen["configurable"] = {
        k: v
        for k, v in frozen["configurable"].items()
        if k in RUN_CONFIG_FROZEN_FIELDS_V16
    }
    frozen["metadata"]["run_config_fingerprint"] = run_config_fingerprint(frozen)
    monkeypatch.setenv("RESEARCH_EFFICIENCY_MODE", "bounded")
    restored = RunConfig.restore(
        {
            "schema": "insightforge.run-config.v1",
            "engine": "agentscope",
            "contract": frozen,
        }
    )
    assert restored.get("research_efficiency_mode") == "baseline"
    assert (
        restored.snapshot()["contract"]["metadata"]["run_config_fingerprint"]
        == frozen["metadata"]["run_config_fingerprint"]
    )
    assert RunConfig.compile().get("research_efficiency_mode") == "bounded"


@pytest.mark.asyncio
async def test_cache_coalesces_and_survives_reconstruction(cache):
    calls = []

    async def calculate():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"evidence": ["stable"]}

    values = await asyncio.gather(
        *(cache.compute("extraction", {"hash": "content"}, calculate) for _ in range(4))
    )
    assert values == [{"evidence": ["stable"]}] * 4
    assert len(calls) == 1
    restored = ResearchCache(cache.recovery, cache.directory.parent.parent)
    assert (
        await restored.compute("extraction", {"hash": "content"}, calculate)
        == values[0]
    )
    assert len(calls) == 1
    await restored.compute("extraction", {"hash": "changed"}, calculate)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_cache_computation_is_not_repeated(cache):
    calls = []

    async def interrupted():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cache.compute("extraction", {"hash": "unknown"}, interrupted)
    with pytest.raises(UnknownOperation):
        await cache.compute("extraction", {"hash": "unknown"}, interrupted)
    assert calls == [1]


@pytest.mark.asyncio
async def test_cache_read_checks_current_fence(cache):
    await cache.compute(
        "assessment", {}, lambda: asyncio.sleep(0, result={"accepted": True})
    )
    await cache.recovery.store.release(cache.recovery.lease)
    with pytest.raises(FenceLost):
        await cache.compute("assessment", {}, lambda: asyncio.sleep(0, result={}))


@pytest.mark.asyncio
async def test_production_cache_route_replays_and_rejects_lost_authority(cache):
    import base64

    import httpx
    from fastapi import FastAPI

    from open_deep_research.agentscope_runtime.efficiency import RemoteResearchCache
    from open_deep_research.agentscope_runtime.gateway_ledger import SQLGatewayLedger
    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.gateway import GatewayRuntime
    from open_deep_research.sandbox.internal_api import build_internal_sandbox_router

    root = base64.b64encode(b"x" * 32).decode()
    gateway = GatewayRuntime(Configuration(sandbox_root_signing_key=root))
    ledger = SQLGatewayLedger(cache.recovery, {})
    ledger.research_cache = cache

    async def resolve(run_id):
        return ledger if run_id == cache.recovery.lease.run_id else None

    app = FastAPI()
    app.include_router(
        build_internal_sandbox_router(
            lambda _: None, native_ledger=resolve, native_root_key=lambda: root
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://host"
    ) as client:

        async def post(path, request):
            response = await client.post(path, json=request.model_dump(mode="json"))
            response.raise_for_status()
            return response.json()

        gateway.internal.post = post
        remote = RemoteResearchCache(
            gateway.internal,
            cache.recovery.lease.run_id,
            cache.recovery.lease.fence,
            {},
        )
        calls = []

        async def compute():
            calls.append(1)
            await asyncio.sleep(0.01)
            return {"evidence": ["validated"]}

        values = await asyncio.gather(
            *(remote.compute("extraction", {}, compute) for _ in range(3))
        )
        assert values == [{"evidence": ["validated"]}] * 3
        assert len(calls) == 1
        await remote.begin("pending")
        with pytest.raises(UnknownOperation):
            await remote.get("pending")
        await cache.recovery.store.release(cache.recovery.lease)
        with pytest.raises(FenceLost):
            await remote.get("pending")


def test_progress_is_query_independent_and_task_replay_does_not_increment():
    config = {"configurable": {"max_no_progress_rounds": 2, "max_supplement_rounds": 2}}
    state = {}
    for task in ("first", "second"):
        patch = finish_requirements(
            state,
            ["COV-1"],
            {
                "accepted": False,
                "gaps": [
                    {
                        "requirement_id": "COV-1",
                        "next_query": task + " different wording",
                    }
                ],
            },
            progress_signature(state, ["COV-1"]),
            task,
        )
        merge_progress(state, {"requirements": patch})
        merge_progress(state, {"requirements": patch})
    assert state["requirements"]["COV-1"]["rounds"] == 2
    assert stop_reason(state, ["COV-1"], config) == "research_no_progress"


def test_downloaded_or_blocked_document_is_not_exhausted():
    config = {
        "metadata": {
            "source_selection": {
                "mode": "specific",
                "sources": [{"type": "url", "url": "https://example.org/doc"}],
            }
        }
    }
    state = {
        "documents": {
            "doc": {
                "url": "https://example.org/doc",
                "content_hash": "a",
                "total_chunks": 2,
                "visited_chunks": ["one", "two"],
                "processed_chunks": ["one"],
                "blocked_chunks": ["two"],
            }
        }
    }
    merge_progress(state, {"documents": state["documents"]})
    assert not state["documents"]["doc"]["inspection_complete"]
    assert stop_reason(state, ["COV-1"], config) is None
    state["documents"]["doc"].update(
        processed_chunks=["one", "two"], inspection_complete=True
    )
    assert stop_reason(state, ["COV-1"], config) == "selected_sources_exhausted"


@pytest.mark.asyncio
async def test_parent_deadline_bounds_child_without_retries():
    import time

    with attributed(deadline_at=time.time() + 0.02), pytest.raises(TimeoutError):
        await limited(lambda: asyncio.sleep(1), 30)


@pytest.mark.asyncio
async def test_empty_evidence_does_not_call_judge():
    from open_deep_research.agentscope_runtime.research_quality import (
        NativeResearchQuality,
    )

    class Models:
        async def structured(self, *args, **kwargs):
            pytest.fail("No evidence should not consume a semantic evaluation")

    quality = NativeResearchQuality(
        Models(), lambda: {"configurable": {"research_efficiency_mode": "bounded"}}
    )
    result = await quality.batch(
        SimpleNamespace(requirement_ids=["COV-1"], research_topic="topic"),
        {},
        [{"name": "fetch_url", "error": {"error_type": "validation_error"}}],
        [],
    )
    assert not result["accepted"]
    assert result["evaluation_source"] == "deterministic"


@pytest.mark.asyncio
async def test_evidence_reader_is_constructible_and_preserves_candidate_status(cache):
    from open_deep_research.agentscope_runtime.efficiency import evidence_read_tools
    from open_deep_research.tools.base import ToolContext, ToolExecutionZone

    await cache.begin("evidence:read-test")
    await cache.commit(
        "evidence:read-test",
        [{"source_url": "https://example.org/page", "evidence_id": "record"}],
    )
    tool = evidence_read_tools(cache, {})[0]
    assert tool.execution_zone is ToolExecutionZone.HOST_CONTROL
    result = await tool.call(
        tool.input_schema(reference="evidence:read-test"),
        ToolContext(config={}, role="researcher", tool_call_id="read"),
    )
    import json

    payload = json.loads(result.output)
    assert payload["admission"] == "candidate"
    assert payload["evidence"][0]["evidence_id"] == "record"


def test_chinese_url_list_is_not_one_source_target():
    from open_deep_research.agentscope_runtime.research_agents import _CITATION_URL

    assert _CITATION_URL.findall("https://example.org/a、https://example.org/b。") == [
        "https://example.org/a",
        "https://example.org/b",
    ]


@pytest.mark.asyncio
async def test_no_admitted_evidence_produces_explicit_partial_without_model():
    from open_deep_research.agentscope_runtime.report import NativeReportWriter

    state = ResearchSnapshot(
        run_id="partial",
        config_fingerprint="f",
        completion_outcome={
            "action": "complete_partial",
            "reason": "selected_sources_exhausted",
            "gaps": ["COV-1"],
        },
    )
    report = await NativeReportWriter(SimpleNamespace())(
        state, {"configurable": {"enable_memory": False}}
    )
    assert report
    assert state.report_product["completion_status"] == "partial"
    assert state.report_product["canonical_report"]["completion_status"] == "partial"


@pytest.mark.asyncio
async def test_finite_corpus_batches_same_domain_and_advances_chunks(cache):
    from open_deep_research.agentscope_runtime.search_providers import candidate
    from open_deep_research.web.extraction import text_document
    from open_deep_research.web.models import EvidenceRecord, SearchBatch, SearchRequest
    from open_deep_research.web.pipeline import (
        WebPipelineSettings,
        WebResearchPipeline,
        clear_run_web_cache,
    )

    urls = [f"https://example.org/{i}" for i in range(3)]
    items = [candidate("specific_url", url, "Facts", "", 1, "fixed") for url in urls]
    docs = {
        item.canonical_url: text_document(
            item,
            "Publication "
            + item.canonical_url
            + ".\n\n"
            + "\n\n".join(
                f"Section {i}. This complete documented fact describes the specific behavior and its applicable conditions. "
                * 3
                for i in range(6)
            ),
        )
        for item in items
    }
    fetches, extracts = [], []

    async def discover(request):
        return SearchBatch(candidates=items)

    async def fetch(url):
        fetches.append(url)
        return docs[url]

    async def extract(objective, documents, chunks):
        extracts.append([chunk.chunk_id for chunk in chunks])
        return [
            EvidenceRecord(
                evidence_id=chunk.chunk_id,
                claim="Documented behavior.",
                supporting_excerpt=chunk.text[:100],
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                locator=f"chars {chunk.start_offset}-{chunk.end_offset}",
                source_url=documents[chunk.document_id].final_url,
                source_title="Facts",
                confidence=1,
            )
            for chunk in chunks
        ]

    pipeline = WebResearchPipeline(
        search=discover,
        settings=WebPipelineSettings(
            cache_namespace="efficiency",
            finite_corpus=True,
            fetch_top_k=3,
            min_source_authority=0,
            chunk_chars=350,
            chunk_overlap_chars=20,
            max_chunks_per_document=1,
            respect_robots_txt=False,
        ),
        backend_order=["fixture"],
        fetch_backends={"fixture": fetch},
        evidence_extractor=extract,
        result_cache=cache,
        allow_url=lambda url: url in urls,
    )
    try:
        first = await pipeline.run(
            SearchRequest(objective="documented fact", queries=[])
        )
        second = await pipeline.run(
            SearchRequest(objective="documented fact", queries=[])
        )
        assert len(first.documents) == 3
        assert len(fetches) == 3
        assert set(extracts[0]).isdisjoint(extracts[1])
        assert len(second.evidence) >= len(first.evidence)
        state = await cache.progress()
        assert len(state["documents"]) == 3
        assert all(
            len(row["processed_chunks"]) == 2 for row in state["documents"].values()
        )
    finally:
        clear_run_web_cache("efficiency")


@pytest.mark.parametrize("budget", [256, 512, 1800])
def test_projection_pages_remain_valid_and_never_modify_authoritative_records(budget):
    import json
    from copy import deepcopy

    from open_deep_research.agentscope_runtime.efficiency import evidence_page

    records = [
        {
            "evidence_id": str(i),
            "source_url": "https://example.org/a",
            "claim": "事实" * 120,
        }
        for i in range(7)
    ]
    original = deepcopy(records)
    page = evidence_page(records, 0, len(records), budget)
    serialized = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= budget
    assert json.loads(serialized)["next_offset"] == len(page["evidence"])
    assert records == original
    assert all(row in original for row in page["evidence"])


def test_finished_old_requirement_does_not_exhaust_new_requirement():
    config = {
        "metadata": {
            "source_selection": {
                "mode": "specific",
                "sources": [{"type": "url", "url": "https://example.org/a"}],
            }
        }
    }
    state = {
        "documents": {
            "d": {
                "url": "https://example.org/a",
                "content_hash": "h",
                "inspection_complete": True,
                "total_chunks": 1,
                "processed_chunks": ["c"],
                "inspections": {"A": {"processed_chunks": ["c"]}},
            }
        }
    }
    assert stop_reason(state, ["A"], config) == "selected_sources_exhausted"
    assert stop_reason(state, ["B"], config) is None


@pytest.mark.asyncio
async def test_handoff_rejects_fixed_data_that_cannot_fit_token_target():
    from open_deep_research.agentscope_runtime.efficiency import handoff_prompt

    class Counter:
        context_size = 64000

        async def count_tokens(self, messages, tools):
            return sum(len(m.get_text_content()) for m in messages)

    with pytest.raises(ValueError, match="fixed_input"):
        await handoff_prompt(
            SimpleNamespace(agent_model=lambda *_: Counter()),
            SimpleNamespace(task_id="t", requirement_ids=["A"]),
            {"requirements": [{"requirement_id": "A", "text": "x" * 10000}]},
            [],
            [],
            {},
        )


@pytest.mark.asyncio
async def test_quality_cache_invalidates_on_evidence_and_policy(cache):
    from pydantic import BaseModel

    from open_deep_research.agentscope_runtime.research_quality import (
        NativeResearchQuality,
    )

    class Verdict(BaseModel):
        accepted: bool
        protocol_repair_count: int = 0
        protocol_errors: list[str] = []

    class Models:
        recovery = cache.recovery
        calls = 0

        async def structured(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(0.01)
            return Verdict(accepted=True)

    models = Models()
    quality = NativeResearchQuality(models, dict)

    async def evaluate(evidence="A", rigor="standard"):
        return await quality.evaluate(
            Verdict,
            "rules",
            {
                "cumulative_evidence": [evidence],
                "approval_thresholds": {"rigor": rigor},
            },
            {},
            span_name="batch",
        )

    await asyncio.gather(evaluate(), evaluate())
    assert models.calls == 1
    await evaluate("B")
    await evaluate("B", "strict")
    assert models.calls == 3


@pytest.mark.asyncio
async def test_shared_output_attempt_budget_exhausts_before_extra_dispatch():
    from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
    from open_deep_research.agentscope_runtime.runtime_limits import (
        consume_structured_attempt,
        structured_attempt_budget,
    )

    token = structured_attempt_budget.set([2])
    try:
        consume_structured_attempt()
        consume_structured_attempt()
        with pytest.raises(ModelOutputProtocolError):
            consume_structured_attempt()
    finally:
        structured_attempt_budget.reset(token)


@pytest.mark.asyncio
async def test_sql_retains_content_free_attribution(cache):
    store, lease = cache.recovery.store, cache.recovery.lease
    await store.begin_operation(
        lease,
        "test-attribution",
        "model_attempt",
        {},
        observation={
            "task_id": "t",
            "agent_role": "quality_evaluation",
            "purpose": "handoff",
            "logical_call_id": "logic",
            "parent_tool_call_id": "tool",
            "prompt": "must not persist",
        },
    )
    events = await store.events(lease.run_id, lease.user_id)
    event = next(
        item["payload"]
        for item in events
        if item["payload"].get("operation_key") == "test-attribution"
    )
    assert event["purpose"] == "handoff"
    assert event["logical_call_id"] == "logic"
    assert "prompt" not in event


@pytest.mark.asyncio
async def test_research_progress_has_a_durable_public_sse_projection(cache):
    from open_deep_research.agentscope_runtime.recovery_events import public_event
    from open_deep_research.events.public import sanitize_public_payload

    await cache.progress({"counters": {"extraction_cache_hits": 1}})
    events = await cache.recovery.store.events(
        cache.recovery.lease.run_id, cache.recovery.lease.user_id
    )
    mapped = [
        public_event(event)
        for event in events
        if event["payload"]["type"] == "research.progress"
    ]
    assert len(mapped) == 1
    kind, _, body = mapped[0]
    assert kind == "research.progress.updated"
    assert (
        sanitize_public_payload(kind, body)["progress"]["counters"][
            "extraction_cache_hits"
        ]
        == 1
    )


def test_candidate_requirement_membership_is_monotonic():
    state = {}
    for rid in ("A", "B", "A"):
        merge_progress(state, {"candidates": {"E": {"requirement_ids": [rid]}}})
    assert state["candidates"]["E"]["requirement_ids"] == ["A", "B"]


@pytest.mark.asyncio
async def test_oversized_evidence_text_is_readable_by_reference(cache):
    import json

    from open_deep_research.agentscope_runtime.efficiency import evidence_read_tools
    from open_deep_research.tools.base import ToolContext

    text = "Evidence quotation. " * 100
    await cache.begin("evidence:large")
    await cache.commit(
        "evidence:large",
        [
            {
                "evidence_id": "E1",
                "source_url": "https://example.org/a",
                "supporting_excerpt": text,
            }
        ],
    )
    config = {"configurable": {"max_mcp_output_chars": 512}}
    tool = evidence_read_tools(cache, config)[0]
    context = ToolContext(config=config, role="researcher", tool_call_id="read")
    offset, recovered = 0, ""
    while offset is not None:
        result = await tool.call(
            tool.input_schema(
                reference="evidence:large",
                field="supporting_excerpt",
                char_offset=offset,
            ),
            context,
        )
        assert len(result.output) <= 512
        page = json.loads(result.output)
        assert page["admission"] == "candidate_fragment"
        assert "evidence" not in page
        recovered += page["text"]
        assert page["next_char_offset"] is None or page["next_char_offset"] > offset
        offset = page["next_char_offset"]
    assert recovered == text


@pytest.mark.asyncio
async def test_report_recovery_partial_status_is_not_overwritten(monkeypatch):
    from open_deep_research.agentscope_runtime.report import NativeReportWriter
    from open_deep_research.report import orchestrator

    async def report(state, config):
        return {
            "final_report": "Partial report",
            "completion_decision": {
                "action": "complete_partial",
                "reason": "report_evidence_recovery",
                "gaps": ["COV-1"],
            },
        }

    monkeypatch.setattr(orchestrator, "build_report", report)
    state = ResearchSnapshot(
        run_id="partial-final",
        config_fingerprint="f",
        completion_outcome={"action": "complete", "reason": "explicit_completion"},
    )
    await NativeReportWriter(SimpleNamespace())(state, {})
    assert state.report_product["completion_status"] == "partial"
    assert state.report_product["stop_reason"] == "report_evidence_recovery"
    assert state.report_product["research_gaps"] == ["COV-1"]


@pytest.mark.asyncio
async def test_model_stream_obeys_parent_deadline_and_closes():
    import time

    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse

    from open_deep_research.agentscope_runtime.model_policy import ModelCallPolicy

    closed = []

    async def stream():
        try:
            yield ChatResponse(content=[TextBlock(text="first")], is_last=False)
            await asyncio.sleep(1)
        finally:
            closed.append(True)

    async def handler(**kwargs):
        return stream()

    policy = ModelCallPolicy([object()], total_timeout=10, circuit_enabled=False)
    with attributed(deadline_at=time.time() + 0.02):
        output = await policy.invoke(handler, {"messages": []}, {})
        with pytest.raises(TimeoutError):
            async for _ in output:
                pass
    assert closed == [True]


def test_baseline_web_tool_keeps_required_query_and_original_guidance():
    from open_deep_research.agentscope_runtime.web_tools import (
        WebFetchLedger,
        web_research_tool,
    )

    config = {"configurable": {"research_efficiency_mode": "baseline"}}
    tool = web_research_tool(lambda: config, SimpleNamespace(), WebFetchLedger())
    schema = tool.input_schema.model_json_schema()
    assert schema["title"] == "WebResearchInput"
    assert schema["required"] == ["objective", "queries"]
    assert schema["properties"]["queries"]["minItems"] == 1
    assert (
        tool.prompt(config)
        == "Provide an objective and up to three queries. Only fetched, source-checked evidence can support report claims."
    )

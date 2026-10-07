"""Four source modes: long accepted handoffs to reviewed reports and HTTP download.

Upstream research and model responses are deterministic fixtures. SQL, report
budgeting, citation/review gates, approvals and publication execute real code.
"""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse, StructuredResponse
from fastapi import FastAPI
from test_report_native import Factory

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.report import NativeReportWriter
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_pipeline import ResearchPipeline
from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.research_router import build_research_router
from open_deep_research.documents.contracts import SourceSelection
from open_deep_research.report.publication_store import PublisherSettings
from open_deep_research.report.publisher_worker import PublisherWorker
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["web", "documents", "hybrid", "specific"])
async def test_long_handoff_approved_report_and_download(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    local = "/documents/doc?chunk=chunk"
    web = "https://example.com/source"
    urls = [local] if mode == "documents" else [local, web] if mode == "hybrid" else [web]
    selection = {"mode": mode, "sources": (
        [{"type": "document", "id": "doc"}] if mode in {"documents", "hybrid"}
        else [{"type": "url", "url": web}] if mode == "specific" else [])}
    records = [{"evidence_id": f"ev-{i}", "claim": "Supported finding",
                "supporting_excerpt": "Evidence text " * 200 + f"TAIL-{i}",
                "source_url": urls[i % len(urls)], "source_title": "Source",
                "source_type": "local_document" if urls[i % len(urls)] == local else "web",
                "security_status": "accepted"} for i in range(100)]
    original = copy.deepcopy(records)
    body = "# Report\n\n## Findings\n\n" + "\n\n".join(
        f"Supported finding [Source]({url})." for url in urls)
    factory = Factory()
    observed = []

    def check(messages):
        assert sum(len(m.get_text_content().encode("utf8")) + 16 for m in messages) <= 24000 - 1024 - 1200
        assert "UNBOUNDED_HANDOFF" not in str(messages)
        evidence = json.loads(messages[-1].get_text_content())
        assert 0 < len(evidence["records"]) < len(records)
        assert len(evidence["records"]) + evidence["omitted_record_count"] == len(records)
        assert all(r["supporting_excerpt"].endswith("TAIL-" + r["evidence_id"].split("-")[1])
                   for r in evidence["records"])
        observed.append(len(evidence["records"]))

    async def complete(role, messages, **kwargs):
        check(messages)
        return ChatResponse(content=[TextBlock(text=body)], is_last=True)

    async def review(messages, schema):
        check(messages)
        return StructuredResponse(content={"decision": "pass", "dimensions": {
            key: 1.0 for key in ("coverage", "citation_correctness", "contradictions",
                                "unsupported_claims", "redundancy", "executive_readability")},
            "citation_audit": [{"claim": "Supported finding", "citation_target": url,
                                "supported": True, "evidence_ids": [f"ev-{i}"]}
                               for i, url in enumerate(urls)]})

    factory.complete_with_recovery = complete
    factory.generate_structured_output = review
    store = RecoveryStore("sqlite+aiosqlite:///" + (tmp_path / "delivery.db").as_posix())
    await store.create_tables()

    @asynccontextmanager
    async def pipeline(snapshot, frozen, recovery):
        cfg = {"configurable": snapshot.application["request_configurable"],
               "metadata": {"source_selection": snapshot.application["source_selection"]}}
        assert cfg["metadata"]["source_selection"] == SourceSelection.model_validate(selection).model_dump(mode="json")
        models = ResearchModels(factory, recovery=recovery)

        class Stages(NativeResearchStages):
            async def write_research_brief(self, state):
                state.research_brief = "Research the finding"

            async def research_supervisor(self, state):
                state.findings = [{"research_topic": "finding", "evidence_registry": records,
                                   "compressed_research": "UNBOUNDED_HANDOFF" * 30000}]
                state.completion_outcome = {"action": "complete"}

        stages = Stages(models, None, lambda: cfg, report_writer=NativeReportWriter(models))
        yield ResearchPipeline(snapshot, stages, recovery.save,
                               config_fingerprint=snapshot.config_fingerprint, recovery=recovery)

    async def prepare(request, principal):
        return {"configurable": request.configurable}

    service = NativeRuns(store, pipeline, prepare, runs_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal("alice")

    async def settle():
        for task in list(service.tasks.values()):
            await asyncio.wait_for(asyncio.shield(task), 30)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/runs", json={
                "messages": [{"role": "user", "content": "Research the finding"}],
                "source_selection": selection, "configurable": {
                    "allow_clarification": False,
                    "enable_memory": False, "enable_human_in_loop": True,
                    "web_pipeline_mode": "enforced", "quality_evaluation_enabled": True,
                    "report_review_enabled": True, "report_review_fail_open": False,
                    "model_context_window_overrides": {"openai:gpt-4.1": 24000}}})
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            for stage in ("plan_approval", "outline_approval"):
                await settle()
                snapshot, _ = await store.load(run_id, "alice")
                assert snapshot.status == "waiting" and snapshot.pending.stage == stage, snapshot.error
                approved = await client.post(f"/runs/{run_id}/human-actions/{snapshot.pending.id}",
                                             json={"action": "approve"})
                assert approved.status_code == 200, approved.text
            await settle()
            snapshot, _ = await store.load(run_id, "alice")
            assert snapshot.status == "completed", snapshot.error
            review_result = snapshot.report_product["report_review"]
            assert review_result["decision"] == "pass" and not review_result["degraded"]
            assert not review_result["skipped"]
            assert snapshot.report_product["canonical_report"]["completion_status"] == "success"
            assert all(url in snapshot.final_report for url in urls)
            assert records == original and len(observed) >= 3
            for fmt in ("markdown", "pdf"):
                publication = await client.post(f"/runs/{run_id}/publications", json={"format": fmt})
                assert publication.status_code == 202, publication.text
                worker = PublisherWorker(PublisherSettings(runs_dir=tmp_path))
                assert await worker.run_once() == 1
                download = await client.get(publication.json()["download_url"])
                assert download.status_code == 200, download.text
                if fmt == "pdf":
                    assert download.content.startswith(b"%PDF")
                else:
                    assert "Supported finding" in download.text
    finally:
        await service.aclose()
        await store.aclose()


@pytest.mark.asyncio
async def test_finalization_uses_revised_markdown_and_preserves_partial_status():
    from test_report_native import state

    from open_deep_research.agentscope_runtime.report import _ReportRun
    from open_deep_research.report.models import ReportDraft, SourceRef
    from open_deep_research.report.orchestrator import finalize_report
    from open_deep_research.report.runtime import native_report

    revised = "# Revised report\n\nSupported finding [Source](https://example.com/source)."
    draft = ReportDraft(markdown=revised, sources=[SourceRef(url="https://example.com/source")],
                        finalization={"completion_decision": {"action": "complete_partial"}})
    token = native_report.set(_ReportRun(ResearchModels(Factory()), state()))
    try:
        update = await finalize_report(draft, {"final_report": "OLD DRAFT",
                                                "completion_decision": {"action": "complete"}},
                                       {"metadata": {"run_id": "reviewed-run"}})
    finally:
        native_report.reset(token)
    canonical = update["canonical_report"]
    assert canonical["run_id"] == "reviewed-run"
    assert canonical["completion_status"] == "partial"
    assert canonical["source_markdown_sha256"] == hashlib.sha256(revised.encode()).hexdigest()
    assert "OLD DRAFT" not in json.dumps(canonical)

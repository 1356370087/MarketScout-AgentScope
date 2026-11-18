"""The archived Web draft, child SQL identity and Worker export use the same bytes."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from agentscope.message import UserMsg
from fastapi import FastAPI
from test_research_migration import contract, evidence
from test_report_native import Factory

from open_deep_research.agentscope_runtime.recovery import RecoveryStages
from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.report import NativeReportWriter
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_pipeline import ResearchPipeline, ResearchSnapshot, STAGES
from open_deep_research.agentscope_runtime.research_stages import NativeResearchStages
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.research_router import build_research_router
from open_deep_research.report import orchestrator
from open_deep_research.report.models import ReportReview
from open_deep_research.report.publication_store import PublisherSettings
from open_deep_research.report.publisher_worker import PublisherWorker
from security.rbac.dependencies import get_current_principal
from security.rbac.permissions import RESEARCH_RUN_READ_OWN
from tests.auth_helpers import research_principal


@pytest.mark.asyncio
@pytest.mark.parametrize(("supplement", "proxy_down"), [(False, False), (True, False), (True, True)])
async def test_derived_native_http_and_worker(pg_url, tmp_path, monkeypatch, supplement, proxy_down):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    owner, parent_id, key = uuid4().hex, uuid4().hex, uuid4().hex
    principal = research_principal(owner)
    scope = {"mode": "specific", "sources": [{"type": "domain", "domain": "example.test"}]}
    body = "# 云套餐\n\n入门版按月收费。[官网](https://example.test/source)\n"
    sha = sha256(body.encode()).hexdigest()
    base = tmp_path / parent_id / "report-reviews"
    base.mkdir(parents=True)
    (base / (sha + ".md")).write_bytes(body.encode())
    facts = contract("核实云套餐的计费单位")
    base_requirement = facts["requirements"][0]
    rid = base_requirement["requirement_id"]
    facts["requirements"] = [{**base_requirement, "subject": "Acme"},
                             {**base_requirement, "requirement_id": "other-plan-unit", "subject": "Contoso"}]
    admitted = {**evidence(), "source_type": "web"}
    parent = ResearchSnapshot(run_id=parent_id, config_fingerprint="old", status="completed",
        completed=list(STAGES), messages=[UserMsg("user", "核实云套餐的计费单位")], research_brief="核实计费单位",
        coverage_contract={**facts, "source_selection": scope}, report_product={"completion_status": "partial"},
        findings=[{"task_id": "main", "requirement_ids": [rid], "evidence_registry": [admitted],
                   "assessment": {"handoff": {"accepted": True}}},
                  {"task_id": "rejected", "evidence_registry": [{**admitted, "evidence_id": "bad"}],
                   "assessment": {"handoff": {"accepted": False}}}],
        application={"source_selection": scope, "report_review_artifacts": {sha: {"attempt": 1}}})
    native = RecoveryStore(pg_url)
    await native.create_tables()
    await native.create_run(owner, parent)
    from unittest.mock import AsyncMock
    renew = AsyncMock(wraps=native.renew)
    monkeypatch.setattr(native, "renew", renew)
    calls = []
    extra_url = "https://example.test/cloud-plans"
    extra = {**admitted, "evidence_id": "ev-extra", "source_url": extra_url}
    class Supplement:
        async def run(self, brief, coverage, feedback):
            assert supplement and coverage["source_selection"]["sources"] == [{"type": "url", "url": extra_url}]
            assert [r["requirement_id"] for r in coverage["requirements"]] == [rid]
            assert "Acme · " + base_requirement["text"] in brief and "Contoso" not in brief
            outcome = {"task_id": "main-research", "requirement_ids": [rid],
                       "assessment": {"handoff": {"accepted": True}}, "evidence_registry": [extra]}
            return [outcome], {"middle_context": {"coverage_ledger": {rid: {"status": "supported",
                "evidence_ids": ["ev-extra"]}}, "business_completion": {"action": "complete"}}}

    async def review(draft, state, config, *, attempt):
        assert {r["evidence_id"] for r in state["evidence_registry"]} == ({"ev1", "ev-extra"} if supplement else {"ev1"})
        assert state["execution_receipts"]["run_id"] != parent_id
        assert state["execution_receipts"]["physical_fetches"] is None
        if supplement:
            prior = renew.await_count
            await asyncio.sleep(1.3)
            assert renew.await_count > prior
        calls.append(draft.markdown)
        return ReportReview(decision="pass", status="passed", draft_sha256=sha, attempt=attempt)

    if proxy_down:
        from open_deep_research.report import reviewer
        monkeypatch.setattr(orchestrator, "revise_report", AsyncMock(return_value=body))
        unavailable = AsyncMock(side_effect=httpx.ProxyError("fixture proxy unavailable"))
        monkeypatch.setattr(reviewer, "_invoke_reviewer", unavailable)
    else:
        monkeypatch.setattr(orchestrator, "review_report", review)
    @asynccontextmanager
    async def factory(state, frozen, recovery):
        if supplement and not proxy_down:
            recovery.ttl = 1
            await native.renew(recovery.lease, recovery.ttl)
        cfg = frozen.compatibility_projection()
        models = ResearchModels(Factory(), recovery=recovery)
        stages = NativeResearchStages(models, Supplement(), lambda: cfg, report_writer=NativeReportWriter(models))
        yield ResearchPipeline(state, RecoveryStages(stages, recovery), recovery.save,
                               recovery=recovery, config_fingerprint=state.config_fingerprint)

    async def prepare(request, user):
        return {"configurable": request.configurable}

    service = NativeRuns(native, factory, prepare, runs_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_research_router(service))
    actor = principal
    app.dependency_overrides[get_current_principal] = lambda: actor
    endpoint = f"/runs/{parent_id}/draft-reviews"
    args = {"base_sha256": sha, "max_cost_micro_usd": 200000}
    if supplement:
        args.update(urls=[extra_url], requirement_ids=[rid])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            actor = replace(principal, permissions=frozenset({RESEARCH_RUN_READ_OWN.code}))
            assert (await client.post(endpoint, json=args)).status_code == 403
            actor = research_principal(uuid4().hex)
            assert (await client.post(endpoint + "/preview", json=args)).status_code == 404
            actor = principal
            available = (await client.get(f"/runs/{parent_id}/review-versions")).json()["requirements"]
            assert [(r["id"], r["subject"], r["text"]) for r in available] == [
                (rid, "Acme", base_requirement["text"]), ("other-plan-unit", "Contoso", base_requirement["text"])]
            assert (await client.post(endpoint + "/preview", json={**args, "urls": ["https://other.test/"],
                                                                "requirement_ids": [rid]})).status_code == 422
            assert (await client.post(endpoint + "/preview", json={**args, "evidence": [admitted]})).status_code == 422
            preview = await client.post(endpoint + "/preview", json=args)
            assert preview.status_code == 200 and preview.json()["inherited_count"] == 1
            async def create(data):
                return await client.post(endpoint, headers={"Idempotency-Key": key}, json=data)
            first, replay = await asyncio.gather(create(args), create(args))
            assert first.status_code == replay.status_code == 202, (first.text, replay.text)
            child = first.json()["run_id"]
            assert child == replay.json()["run_id"] and child != parent_id
            assert (await create({**args, "max_cost_micro_usd": 300000})).status_code == 409
            if child in service.tasks:
                await asyncio.wait_for(asyncio.shield(service.tasks[child]), 30)
            if supplement:
                pending, _ = await native.load(child, owner)
                assert pending.pending.stage == "plan_approval"
                confirm = f"/runs/{child}/human-actions/{pending.pending.id}"
                rejected = await client.post(confirm, json={"action": "approve", "source_selection": scope})
                assert rejected.status_code == 409
                accepted = await client.post(confirm, json={"action": "approve"})
                assert accepted.status_code == 200
                if child in service.tasks:
                    await asyncio.wait_for(asyncio.shield(service.tasks[child]), 30)
            result, _ = await native.load(child, owner)
            if proxy_down:
                assert unavailable.await_count and result.status == "failed"
                assert result.report_product.get("report_review", {}).get("decision") != "pass"
                retry = await create(args)
                assert retry.json()["run_id"] == child and child not in service.tasks
                assert (await native.load(parent_id, owner))[0].model_dump() == parent.model_dump()
                return
            assert result.status == "completed", result.error
            assert result.report_product["completion_status"] == "success"
            assert result.application["delivered_sha256"] == sha == result.report_product["report_review"]["draft_sha256"]
            assert calls == [body] and result.final_report == body
            assert (await native.budget(child, owner))["limits"]["cost_micro_usd"] == 200000
            versions = (await client.get(f"/runs/{parent_id}/review-versions")).json()
            assert {"run_id": child, "status": "completed", "completion_status": "success", "review_passed": True} in versions["children"]
            publish = await client.post(f"/runs/{child}/publications", json={"format": "markdown"})
            assert publish.status_code == 202
            assert await PublisherWorker(PublisherSettings(runs_dir=tmp_path)).run_once() == 1
            download = await client.get(publish.json()["download_url"])
            assert download.content == body.encode()
            assert (await native.load(parent_id, owner))[0].model_dump() == parent.model_dump()
    finally:
        await service.aclose()
        await native.aclose()

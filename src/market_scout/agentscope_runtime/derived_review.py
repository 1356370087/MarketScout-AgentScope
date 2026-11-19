"""Create and execute an independently billed review of an immutable Web draft."""

import hashlib
import math

from market_scout.documents.contracts import SourceSelection
from market_scout.evidence import source_scoped_evidence_records
from .recovery_store import RecoveryConflict, digest
from .storage import runtime_data_dir


def read_draft(parent, sha):
    if sha not in parent.application.get("report_review_artifacts", {}):
        raise KeyError("draft not found")
    path = runtime_data_dir() / parent.run_id / "report-reviews" / (sha + ".md")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise RecoveryConflict("archived_draft_unavailable") from None
    if hashlib.sha256(raw).hexdigest() != sha:
        raise RecoveryConflict("report_draft_hash_changed")
    return raw.decode("utf-8")


def parent_selection(parent):
    scope = (parent.application.get("approved_source_selection") or parent.coverage_contract.get("source_selection")
             or parent.application.get("source_selection") or {})
    selection = SourceSelection.model_validate(scope)
    if not selection.web_enabled or selection.documents_enabled:
        raise ValueError("derived_review_requires_web_sources")
    return selection


def admitted_parent_findings(parent, base_sha):
    from .search_providers import source_allowed
    scope = parent_selection(parent).model_dump(mode="json")
    config = {"metadata": {"source_selection": scope, "coverage_contract": parent.coverage_contract}}
    artifact = parent.application["report_review_artifacts"][base_sha]
    archived_ids = set(artifact["evidence_ids"]) if "evidence_ids" in artifact else None
    versions = artifact.get("evidence_versions", {})
    findings = []
    for row in parent.findings:
        if row.get("assessment", {}).get("handoff", {}).get("accepted") is not True:
            continue
        records = [r for r in source_scoped_evidence_records(row.get("evidence_registry", []), parent.coverage_contract)
                   if r.get("source_type", "web") == "web" and source_allowed(r["source_url"], config)]
        if archived_ids is not None:
            records = [r for r in records if r["evidence_id"] in archived_ids]
        selected = {r["evidence_id"] for r in records}
        if any(r["evidence_id"] in selected and r["evidence_id"] in versions and versions[r["evidence_id"]] != digest(r)
               for r in row.get("evidence_registry", [])):
            raise RecoveryConflict("archived_evidence_version_changed")
        if records:
            findings.append({"task_id": f"inherited:{parent.run_id}:{row['task_id']}",
                "research_topic": row.get("research_topic", ""), "requirement_ids": row.get("requirement_ids", []),
                "compressed_research": "", "evidence_registry": records, "assessment": {"handoff": {"accepted": True}},
                "origin_run_id": parent.run_id})
    return findings


def review_inputs(parent, request):
    from .search_providers import source_allowed
    from market_scout.api.contracts import RunRequest
    if parent.status not in {"completed", "cancelled", "failed"}:
        raise RecoveryConflict("review_requires_archived_parent")
    markdown = read_draft(parent, request.base_sha256)
    scope = parent_selection(parent)
    allowed = {"metadata": {"source_selection": scope.model_dump(mode="json"), "coverage_contract": parent.coverage_contract}}
    supplement = SourceSelection.model_validate({"mode": "specific", "sources": [
        {"type": "url", "url": url} for url in request.urls]}) if request.urls else None
    if supplement and not all(source_allowed(url, allowed) for url in supplement.urls):
        raise ValueError("supplement_outside_parent_scope")
    facts = {r["requirement_id"] for r in parent.coverage_contract.get("requirements", []) if r.get("kind", "factual") == "factual"}
    if bool(supplement) != bool(request.requirement_ids) or not set(request.requirement_ids) <= facts:
        raise ValueError("choose_parent_requirements_for_supplement")
    inherited = admitted_parent_findings(parent, request.base_sha256)
    if not inherited and not supplement:
        raise ValueError("review_requires_admitted_evidence")
    options = {"enable_human_in_loop": True, "report_review_enabled": True, "report_review_fail_open": False,
               "quality_evaluation_enabled": True, "quality_evaluation_fail_open": False}
    child = RunRequest(messages=[{"role": "user", "content": m.get_text_content()} for m in parent.messages
                                if m.role == "user"], source_selection=supplement or scope,
        title=("草稿复核 · " + parent.application.get("title", parent.run_id))[:160], configurable=options)
    refs = [(r["evidence_id"], digest(r)) for task in inherited for r in task["evidence_registry"]]
    version = {"parent_run_id": parent.run_id, "base_sha256": request.base_sha256,
        "inherited_evidence_sha256": digest(sorted(refs)), "inherited_count": len(refs),
        "urls": supplement.urls if supplement else [], "requirement_ids": sorted(set(request.requirement_ids)),
        "max_cost_micro_usd": request.max_cost_micro_usd}
    prior = parent.application.get("review_derivation") or {}
    if (not supplement and prior.get("base_sha256") == request.base_sha256
            and parent.application.get("review_evidence_manifest") and not parent.application.get("supplement_incomplete")):
        version["inherited_update_ids"] = sorted(set(prior.get("requirement_ids") or prior.get("inherited_update_ids", [])))
    frozen = parent.application.get("configuration", {}).get("contract", {}).get("configurable", {})
    model = frozen.get("report_review_model") or frozen.get("research_model")
    entry = frozen.get("model_catalog_snapshot", {}).get(model)
    estimate = None
    if entry:
        incoming = max(len(markdown.encode()) // 2, frozen.get("research_context_target_tokens", 24000))
        estimate = math.ceil(1_000_000 * (incoming * entry["input_cost_per_token"]
            + frozen.get("report_review_model_max_tokens", 4096) * entry["output_cost_per_token"]))
    return child, version, estimate


async def execute_stage(stages, stage, state, version):
    """Use the normal pipeline lease, approvals, retrieval, admission and writer."""
    if stage == "final_report_generation":
        return await stages.final_report_generation(state)
    if stage == "plan_approval" and version["urls"]:
        from .research_pipeline import PendingDecision
        return PendingDecision(stage=stage, question=state.research_brief + "\n" + "\n".join(version["urls"]))
    if stage not in {"write_research_brief", "research_supervisor"}:
        return
    recovery = stages.models.recovery
    parent, _ = await recovery.store.load(version["parent_run_id"], recovery.lease.user_id)
    if stage == "write_research_brief":
        state.research_brief = parent.research_brief
        state.coverage_contract = dict(parent.coverage_contract)
        if version["urls"]:
            ids = set(version["requirement_ids"])
            goals = [r for r in state.coverage_contract.get("requirements", []) if r["requirement_id"] in ids]
            state.research_brief = "仅核实下列补充来源中的已选事实。\n" + "\n".join(
                f"{r['subject']} · {r['text']}" if r.get("subject") else r["text"] for r in goals)
            state.coverage_contract.update(requirements=goals, single_research_task=True,
                source_plan=None, source_selection=state.application["source_selection"])
    elif stage == "research_supervisor":
        from .research_quality import admitted_findings
        if version["urls"]:
            state.findings = [f for f in state.findings if not f.get("origin_run_id")]
            await stages.research_supervisor(state)
        supplements = [f for f in admitted_findings(state.findings, stages.config_provider()) if not f.get("origin_run_id")]
        inherited = admitted_parent_findings(parent, version["base_sha256"])
        current_refs = [(r["evidence_id"], digest(r)) for task in inherited for r in task["evidence_registry"]]
        if digest(sorted(current_refs)) != version["inherited_evidence_sha256"]:
            raise RecoveryConflict("parent_evidence_version_changed")
        state.findings = [*inherited, *supplements]
        state.research_brief = parent.research_brief
        state.coverage_contract = dict(parent.coverage_contract)
        ids = {r["evidence_id"] for f in state.findings for r in f.get("evidence_registry", [])}
        ledger = {key: row for key, row in parent.coverage_ledger.items()
                  if row.get("evidence_ids") and set(row["evidence_ids"]) <= ids}
        ledger.update({key: row for key, row in state.coverage_ledger.items() if row.get("evidence_ids")
                       and set(row["evidence_ids"]) <= ids})
        if version["urls"]:
            from market_scout.documents.contracts import source_url_identity
            new_records = [r for finding in supplements for r in finding.get("evidence_registry", [])]
            new_ids = {r["evidence_id"] for r in new_records}
            supported_urls = {source_url_identity(r["source_url"]) for r in new_records}
            covered = all(state.coverage_ledger.get(rid, {}).get("status") == "supported"
                and new_ids.intersection(state.coverage_ledger[rid].get("evidence_ids", [])) for rid in version["requirement_ids"])
            state.application["supplement_incomplete"] = not (covered and all(
                source_url_identity(url) in supported_urls for url in version["urls"]))
        state.coverage_ledger = ledger
        state.completion_outcome = {"action": "complete" if ids else "complete_partial",
            "reason": "derived_draft_review" if ids else "insufficient_evidence", "gaps": []}
        state.application["review_evidence_manifest"] = sorted((r["evidence_id"], digest(r)) for f in state.findings
            for r in f.get("evidence_registry", []))
    return None

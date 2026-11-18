"""Human review bridges observations into the existing published-source contract."""

import hashlib
import tempfile
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from open_deep_research.knowledge import authz
from .authorization import principal_for_grant
from .contracts import IntelligenceError
from .observations import get_snapshot, read_document, artifact, assert_job
from .store import Store, record, encode, enqueue, previous_command, save_command


async def review_item(actor, item_id, command, *, store=None):
    store = store or Store()
    item, plan = await store.item(item_id, actor)
    await authz.require_kb_capability(actor, plan["knowledge_base_id"], authz.CAP_REVIEW)
    pool = await store.pool()
    key, body = f"review:{item_id}:{command.command_id}", command.model_dump(mode="json")
    async with pool.acquire() as c, c.transaction():
        prior = await previous_command(c, actor, key, body)
        if prior is not None:
            return prior
        if item["version"] != command.expected_version:
            raise IntelligenceError("version_conflict", current_version=item["version"])
        if item["status"] not in {"awaiting_review", "blocked"}:
            raise IntelligenceError("item_not_reviewable")
        if command.action != "reject" and item["quality_status"] != "passed":
            raise IntelligenceError("quality_not_passed")
        from datetime import datetime, UTC
        patch = {"actor_id": actor, "reason": command.reason, "action": command.action, "reviewed_at": datetime.now(UTC).isoformat()}
        status = item["status"]
        if command.action == "reject":
            if not command.reason.strip():
                raise IntelligenceError("review_reason_required", 422)
            status = "dismissed"
        elif command.action == "complete":
            if not command.published_fact_ids and not command.published_wiki_revision_ids:
                raise IntelligenceError("published_knowledge_required", 422)
            generations = [entry.get("generation_id") for entry in item["review"].get("sources", {}).values() if entry.get("generation_id")]
            for fid in command.published_fact_ids:
                valid = await c.fetchval("""SELECT EXISTS(SELECT 1 FROM knowledge_fact_assertions a
                    WHERE a.id=$1::uuid AND a.knowledge_base_id=$2::uuid AND a.status='published'
                    AND EXISTS(SELECT 1 FROM knowledge_fact_evidence e WHERE e.assertion_id=a.id AND e.generation_id=ANY($3::uuid[])))""",
                    str(fid), plan["knowledge_base_id"], generations)
                if not valid:
                    raise IntelligenceError("published_fact_outside_review_scope")
            for rid in command.published_wiki_revision_ids:
                valid = await c.fetchval("""SELECT EXISTS(SELECT 1 FROM knowledge_page_revisions r
                    JOIN knowledge_pages p ON p.id=r.page_id WHERE r.id=$1::uuid AND p.id=$2::uuid
                    AND p.knowledge_base_id=$3::uuid AND r.status='published' AND p.published_revision_id=r.id)""",
                    str(rid), item["review"].get("wiki_page_id"), plan["knowledge_base_id"])
                if not valid:
                    raise IntelligenceError("published_wiki_outside_review_scope")
            patch.update(fact_ids=[str(value) for value in command.published_fact_ids],
                         wiki_revision_ids=[str(value) for value in command.published_wiki_revision_ids])
            status = "completed"
        changed = await c.fetchval("""UPDATE intelligence_items SET review=review || $3::jsonb,status=$4,
            version=version+1,updated_at=$5::uuid,update_time=now() WHERE id=$1::uuid AND version=$2 RETURNING version""",
            item_id, command.expected_version, encode(patch), status, actor)
        if changed is None:
            raise IntelligenceError("version_conflict")
        if command.action in {"prepare_sources", "prepare_knowledge"}:
            await enqueue(c, plan["id"], plan["owner_id"], command.action,
                key, {"item_id": item_id, "reviewer_id": actor, "run_sequence": item["run_sequence"]})
        if status == "completed":
            await c.execute("UPDATE intelligence_plans SET baseline=$2::jsonb,version=version+1,update_time=now() WHERE id=$1::uuid",
                plan["id"], encode({"work_item_id": item_id, "run_id": item["run_id"], **patch}))
        return await save_command(c, actor, key, body, {"status": status, "version": changed})


async def prepare_sources(job, *, store=None):
    store = store or Store()
    item, plan = await store.item(job["payload"]["item_id"])
    if job["payload"].get("run_sequence") != item["run_sequence"]:
        raise IntelligenceError("review_item_run_changed")
    reviewer = job["payload"]["reviewer_id"]
    await authz.require_kb_capability(reviewer, plan["knowledge_base_id"], authz.CAP_REVIEW)
    _, grant, plan = await principal_for_grant(plan["authorization_id"], plan_id=plan["id"], store=store)
    if item["quality_status"] != "passed" or item["status"] != "awaiting_review":
        raise IntelligenceError("item_no_longer_reviewable")
    from open_deep_research.documents.settings import get_document_settings
    from open_deep_research.documents.storage import StagedUpload, commit_upload
    from open_deep_research.documents.repository import create_document
    settings = get_document_settings()
    from open_deep_research.platform import runtime
    if runtime.enabled():
        from dataclasses import replace
        from open_deep_research.platform.store import get_store
        limits = await get_store().limits(plan["owner_id"])
        settings = replace(settings, **{name: limits[name] for name in ("max_documents_per_user", "max_bytes_per_user") if limits.get(name) is not None})
    pool = await store.pool()
    sources = dict(item["review"].get("sources", {}))
    referenced = {row.get("observation_snapshot_id") for row in item["result"].get("evidence", [])}
    for entry in item["manifest"].get("snapshots", []):
        if entry["id"] not in referenced and not entry.get("generation_id"):
            continue
        snapshot = await get_snapshot(entry["id"], reviewer, store=store)
        if snapshot["id"] in sources:
            continue
        if snapshot["generation_id"]:
            sources[snapshot["id"]] = {"document_id": snapshot["document_id"], "generation_id": snapshot["generation_id"]}
            continue
        document = read_document(snapshot)
        body = (artifact(snapshot) / "raw.bin").read_bytes()
        extension = ".pdf" if document.content_type == "application/pdf" else ".html" if "html" in document.content_type else ".txt"
        staging = settings.storage_dir / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=extension, dir=staging) as temporary:
            temporary.write(body)
            path = Path(temporary.name)
        try:
            staged = StagedUpload(path, "观察资料-" + snapshot["id"][:8] + extension, document.content_type, len(body), snapshot["raw_sha256"])
            storage_key, blob_created = commit_upload(staged, plan["owner_id"], settings)
            created, duplicate = await create_document(plan["owner_id"], staged, storage_key, settings,
                knowledge_base_id=plan["knowledge_base_id"], intelligence_binding={"plan_id": plan["id"],
                    "authorization_id": grant["id"], "billing_owner_id": plan["owner_id"]})
            if duplicate and blob_created:
                from open_deep_research.documents.repository import get_document
                from open_deep_research.documents.storage import delete_storage_key
                existing = await get_document(plan["owner_id"], str(created.id))
                if existing and existing["storage_key"] != storage_key:
                    delete_storage_key(storage_key, settings)
            sources[snapshot["id"]] = {"document_id": str(created.id), "raw_sha256": snapshot["raw_sha256"]}
        finally:
            path.unlink(missing_ok=True)
    async with pool.acquire() as c, c.transaction():
        await assert_job(c, job)
        updated = await c.fetchval("""UPDATE intelligence_items SET review=review || $2::jsonb,version=version+1
            WHERE id=$1::uuid AND status='awaiting_review' AND run_sequence=$3 RETURNING id""",
            item["id"], encode({"sources": sources, "source_review_status": "awaiting_publication"}), item["run_sequence"])
        if not updated:
            raise IntelligenceError("review_item_run_changed")
    return {"sources": sources}


async def prepare_knowledge(job, *, store=None):
    store = store or Store()
    item, plan = await store.item(job["payload"]["item_id"])
    if job["payload"].get("run_sequence") != item["run_sequence"]:
        raise IntelligenceError("review_item_run_changed")
    actor = job["payload"]["reviewer_id"]
    await authz.require_kb_capability(actor, plan["knowledge_base_id"], authz.CAP_REVIEW)
    await principal_for_grant(plan["authorization_id"], plan_id=plan["id"], store=store)
    if item["quality_status"] != "passed" or item["status"] != "awaiting_review":
        raise IntelligenceError("item_no_longer_reviewable")
    pool = await store.pool()
    sources = dict(item["review"].get("sources", {}))
    citations = []
    for evidence in item["result"].get("evidence", []):
        sid = evidence.get("observation_snapshot_id")
        link = sources.get(sid) if sid else {"document_id": evidence.get("document_id"), "generation_id": evidence.get("generation_id")}
        if not link or not link.get("document_id"):
            raise IntelligenceError("evidence_requires_published_observation")
        row = await pool.fetchrow("""SELECT d.current_generation_id,g.version_id,v.sha256
            FROM research_documents d JOIN research_document_generations g ON g.id=coalesce($3::uuid,d.current_generation_id)
            JOIN research_document_versions v ON v.id=g.version_id
            WHERE d.id=$1::uuid AND d.home_knowledge_base_id=$2::uuid AND g.document_id=d.id
            AND d.deleted_at IS NULL AND g.status='published'""", link["document_id"], plan["knowledge_base_id"], link.get("generation_id"))
        if not row:
            raise IntelligenceError("observation_source_not_published")
        if link.get("raw_sha256") and row["sha256"] != link["raw_sha256"]:
            raise IntelligenceError("published_source_differs_from_observation")
        generation = link.get("generation_id") or str(row["current_generation_id"])
        units = await pool.fetch("SELECT id,coalesce(revised_text,index_text) AS index_text FROM research_document_units WHERE generation_id=$1::uuid AND NOT excluded ORDER BY ordinal", generation)
        quote = " ".join(str(evidence.get("supporting_excerpt", "")).split())
        match = next((unit for unit in units if quote and quote in " ".join(str(unit["index_text"]).split())), None)
        if match is None:
            raise IntelligenceError("published_evidence_excerpt_mismatch")
        citations.append({"type": "document", "document_id": link["document_id"], "generation_id": generation,
                          "unit_id": str(match["id"]), "excerpt": quote})
        if sid:
            sources[sid] = {**link, "generation_id": generation}
    if not citations:
        raise IntelligenceError("published_evidence_required")
    from open_deep_research.knowledge.wiki import create_page, get_page, save_draft
    page_id = str(uuid5(NAMESPACE_URL, f"intelligence-wiki:{item['id']}:{item['run_sequence']}"))
    await create_page(actor, plan["knowledge_base_id"], title=plan["name"] + " · 变化简报", page_id=page_id)
    page = await get_page(actor, page_id)
    blocks = [{"id": str(uuid5(NAMESPACE_URL, "intelligence-block:" + item["id"])), "ordinal": 0,
               "block_type": "analysis", "content": item["result"]["report"], "requires_source": True,
               "citations": citations, "generated_by": "intelligence:" + item["id"]}]
    if page["current_revision"]["blocks"] != blocks:
        if page["current_revision"]["number"] != 1:
            raise IntelligenceError("wiki_draft_changed_by_reviewer")
        await save_draft(actor, page_id, base_revision=1, blocks=blocks, generated=True)
    async with pool.acquire() as c, c.transaction():
        await assert_job(c, job)
        updated = await c.fetchval("""UPDATE intelligence_items SET review=review || $2::jsonb,version=version+1
            WHERE id=$1::uuid AND status='awaiting_review' AND run_sequence=$3 RETURNING id""",
            item["id"], encode({"sources": sources, "wiki_page_id": page_id, "source_review_status": "published"}), item["run_sequence"])
        if not updated:
            raise IntelligenceError("review_item_run_changed")
    return {"wiki_page_id": page_id}

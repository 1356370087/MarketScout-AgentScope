"""Immutable public-web observations, separate from published knowledge."""

import hashlib
import json
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from open_deep_research.documents.settings import get_document_settings
from open_deep_research.documents.contracts import source_url_identity
from open_deep_research.knowledge.network import PublicSyncTransport, sync_authorizer
from open_deep_research.knowledge.sync import WebAdapter
from open_deep_research.web.models import CandidateSource, FetchResult, ExtractedDocument
from open_deep_research.web.fetching import RawFetch
from open_deep_research.web.extraction import extract_document, chunk_document
from open_deep_research.web.settings import WebPipelineSettings
from .authorization import principal_for_grant
from .contracts import IntelligenceError, next_check
from .store import Store, record, encode


def root():
    return Path(get_document_settings().storage_dir) / "observations"


def artifact(snapshot):
    return root() / snapshot["artifact"]


def read_document(snapshot):
    directory = artifact(snapshot)
    raw = (directory / "raw.bin").read_bytes()
    if hashlib.sha256(raw).hexdigest() != snapshot["raw_sha256"]:
        raise IntelligenceError("observation_integrity_mismatch")
    document = ExtractedDocument.model_validate_json((directory / "document.json").read_text(encoding="utf-8"))
    if hashlib.sha256(document.markdown.encode()).hexdigest() != snapshot["content_sha256"]:
        raise IntelligenceError("observation_integrity_mismatch")
    return document


def extract(url, body, content_type, *, final_url=None, observed_at=None):
    candidate = CandidateSource(candidate_id="observation", provider="watch", original_url=url,
        canonical_url=url, domain=urlsplit(url).hostname or "")
    raw = RawFetch(FetchResult(candidate_id=candidate.candidate_id, requested_url=url, final_url=final_url or url,
        content_type=content_type, byte_count=len(body), fetched_at=observed_at or datetime.now(UTC), success=True), body)
    document = extract_document(candidate, raw, WebPipelineSettings(evidence_fidelity=True))
    if not document.markdown.strip() or "dynamic_render_recommended" in document.quality_flags or "needs_external_extraction" in document.quality_flags:
        raise IntelligenceError("observation_requires_supported_extraction")
    # Canonical HTML tags are untrusted. They cannot change the source's authority.
    document.canonical_url = source_url_identity(final_url or url)
    return document


async def assert_job(c, job):
    changed = await c.fetchval("""UPDATE intelligence_jobs SET update_time=now()
        WHERE id=$1::uuid AND attempts=$2 AND status='running' AND lease_until>now() RETURNING id""", job["id"], job["attempts"])
    if not changed:
        raise IntelligenceError("intelligence_job_lease_lost")


async def capture(job, *, store=None):
    store = store or Store()
    plan = await store.plan(job["plan_id"])
    if plan["status"] != "active":
        return {"status": "paused"}
    principal, grant, plan = await principal_for_grant(job["payload"].get("authorization_id") or plan["authorization_id"], plan_id=job["plan_id"], store=store)
    pool = await store.pool()
    source = record(await pool.fetchrow("SELECT * FROM intelligence_sources WHERE id=$1::uuid AND plan_id=$2::uuid AND NOT is_deleted", job["payload"]["source_id"], plan["id"]))
    if source is None:
        return {"status": "removed"}
    if source["definition"]["kind"] == "knowledge":
        return await capture_knowledge(job, plan, source, store)
    url = source["definition"]["url"]
    permitted = {source_url_identity(row["url"]) for row in grant["scope"]["sources"] if row["kind"] == "web"}
    network = sync_authorizer(principal.roles)

    async def authorize(target):
        await principal_for_grant(grant["id"], store=store)
        if source_url_identity(target) not in permitted:
            raise IntelligenceError("observation_source_expansion_requires_authorization", 403)
        await network(target)

    from open_deep_research.configuration import Configuration
    cfg = Configuration.from_runnable_config(None)
    if cfg.respect_robots_txt:
        import aiohttp
        from open_deep_research.web.fetching import _robots_allowed
        from open_deep_research.security.network import PublicWebResolver
        await network(url)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicWebResolver(), use_dns_cache=False),
                                        timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
            if not await _robots_allowed(session, url, WebPipelineSettings(cache_namespace="intelligence:" + source["id"])):
                raise IntelligenceError("observation_robots_denied", 403)
    headers = {}
    for field, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
        if source["checkpoint"].get(field):
            headers[header] = source["checkpoint"][field]
    async with httpx.AsyncClient(transport=PublicSyncTransport(max_bytes=max(cfg.html_max_bytes, cfg.pdf_max_bytes),
        text_max_bytes=cfg.html_max_bytes), timeout=30, trust_env=False) as client:
        response = await WebAdapter(authorize)._get(client, url, headers)
    if response.status_code == 429:
        raise IntelligenceError("observation_rate_limited:" + response.headers.get("Retry-After", "60"), 429)
    checkpoint = {"etag": response.headers.get("etag", source["checkpoint"].get("etag")),
                  "last_modified": response.headers.get("last-modified", source["checkpoint"].get("last_modified"))}
    if response.status_code == 304:
        await checked(job, plan, source, checkpoint, store)
        return {"status": "unchanged"}
    response.raise_for_status()
    content_type = response.headers.get("content-type", "text/html").split(";", 1)[0]
    document = extract(url, response.content, content_type, final_url=str(response.url))
    if source["snapshot_id"]:
        previous = await pool.fetchrow("SELECT content_sha256 FROM intelligence_snapshots WHERE id=$1::uuid", source["snapshot_id"])
        if previous and previous["content_sha256"] == document.content_hash:
            await checked(job, plan, source, checkpoint, store)
            return {"status": "unchanged"}
    return await save_capture(job, plan, source, document, response.content, checkpoint, store)


async def checked(job, plan, source, checkpoint, store):
    pool = await store.pool()
    async with pool.acquire() as c, c.transaction():
        await assert_job(c, job)
        await c.execute("""UPDATE intelligence_sources SET checkpoint=$3::jsonb,last_checked_at=now(),
            next_run_at=$4,failures=0,error=NULL,update_time=now() WHERE id=$1::uuid AND sequence=$2""",
            source["id"], source["sequence"], encode(checkpoint), next_check(plan["definition"]["cadence"]))


async def save_capture(job, plan, source, document, body, checkpoint, store, *, generation_id=None, document_id=None, origin_item_id=None):
    snapshot_id, now = str(uuid4()), datetime.now(UTC)
    relative = str(Path(snapshot_id[:2]) / snapshot_id)
    directory = root() / relative
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "raw.bin").write_bytes(body)
    (directory / "document.json").write_text(document.model_dump_json(), encoding="utf-8")
    metadata = {"url": document.requested_url, "final_url": document.final_url, "title": document.title,
        "content_type": document.content_type, "published_at": document.published_at,
        "extractor": document.extractor, "extractor_version": document.extractor_version,
        "quality_flags": document.quality_flags}
    pool = await store.pool()
    async with pool.acquire() as c, c.transaction():
        await assert_job(c, job)
        updated = await c.fetchval("""UPDATE intelligence_sources SET sequence=sequence+1,snapshot_id=$3::uuid,
            checkpoint=$4::jsonb,last_checked_at=now(),next_run_at=$5,failures=0,error=NULL,update_time=now()
            WHERE id=$1::uuid AND sequence=$2 RETURNING sequence""", source["id"], source["sequence"], snapshot_id,
            encode(checkpoint), next_check(plan["definition"]["cadence"]))
        if updated is None:
            raise IntelligenceError("observation_source_version_conflict")
        await c.execute("""INSERT INTO intelligence_snapshots(id,plan_id,source_id,sequence,raw_sha256,content_sha256,
            artifact,metadata,observed_at,document_id,generation_id,created_at)
            VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8::jsonb,$9,$10::uuid,$11::uuid,$12::uuid)""",
            snapshot_id, plan["id"], source["id"], updated, hashlib.sha256(body).hexdigest(), document.content_hash,
            relative, encode(metadata), now, document_id, generation_id, plan["owner_id"])
        self_published = origin_item_id and await c.fetchval("SELECT plan_id=$2::uuid FROM intelligence_items WHERE id=$1::uuid", origin_item_id, plan["id"])
        await c.execute("""INSERT INTO intelligence_events(plan_id,source_id,sequence,previous_snapshot_id,snapshot_id,kind,available_at,created_at,origin_item_id,status)
            VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,$6,now()+interval '5 seconds',$7::uuid,$8::uuid,$9)""",
            plan["id"], source["id"], updated, source["snapshot_id"], snapshot_id,
            "knowledge_published" if generation_id else "baseline" if not source["snapshot_id"] else "content_changed", plan["owner_id"],
            origin_item_id, "self_published" if self_published else "pending")
        if generation_id and checkpoint.get("publication_sequence") is not None:
            await c.execute("""UPDATE intelligence_sources SET next_run_at=now() WHERE id=$1::uuid AND EXISTS(
                SELECT 1 FROM intelligence_publications WHERE document_id=$2::uuid AND sequence>$3)""",
                source["id"], document_id, checkpoint["publication_sequence"])
    return {"status": "changed", "snapshot_id": snapshot_id}


async def capture_knowledge(job, plan, source, store):
    pool = await store.pool()
    publication = await pool.fetchrow("""SELECT * FROM intelligence_publications WHERE document_id=$1::uuid
        AND sequence>$2 ORDER BY sequence LIMIT 1""", source["definition"]["document_id"], source["checkpoint"].get("publication_sequence", 0))
    initial_cursor = None
    if source["snapshot_id"] is None:
        initial_cursor = await pool.fetchval("SELECT coalesce(max(sequence),0) FROM intelligence_publications WHERE document_id=$1::uuid", source["definition"]["document_id"])
        publication = None
    row = await pool.fetchrow("""SELECT d.id,d.filename,d.current_generation_id,g.metadata_snapshot
        ,g.id AS selected_generation_id FROM research_documents d JOIN research_document_generations g ON g.id=coalesce($3::uuid,d.current_generation_id)
        WHERE d.id=$1::uuid AND d.home_knowledge_base_id=$2::uuid AND d.deleted_at IS NULL AND g.status='published'""",
        source["definition"]["document_id"], plan["knowledge_base_id"], str(publication["generation_id"]) if publication else None)
    if not row:
        raise IntelligenceError("knowledge_source_unavailable", 403)
    generation = str(row["selected_generation_id"])
    if source["checkpoint"].get("generation_id") == generation:
        checkpoint = {**source["checkpoint"], **({"publication_sequence": publication["sequence"]} if publication else {})}
        await checked(job, plan, source, checkpoint, store)
        return {"status": "unchanged"}
    units = await pool.fetch("SELECT index_text FROM research_document_units WHERE generation_id=$1::uuid AND NOT excluded ORDER BY ordinal", generation)
    text = "\n\n".join(str(unit["index_text"]) for unit in units)
    if not text.strip():
        raise IntelligenceError("knowledge_source_has_no_usable_content")
    url = "/documents/" + str(row["id"])
    document = ExtractedDocument(document_id=str(row["id"]), candidate_id=source["id"], requested_url=url, final_url=url,
        canonical_url=url, title=row["filename"], content_type="text/plain", markdown=text,
        extractor="published-generation", extractor_version="1", content_hash=hashlib.sha256(text.encode()).hexdigest())
    return await save_capture(job, plan, source, document, text.encode(),
        {"generation_id": generation, "publication_sequence": publication["sequence"] if publication else initial_cursor if initial_cursor is not None else source["checkpoint"].get("publication_sequence", 0)}, store,
        generation_id=generation, document_id=str(row["id"]), origin_item_id=str(publication["origin_item_id"]) if publication and publication["origin_item_id"] else None)


async def get_snapshot(snapshot_id, actor, *, store=None):
    store = store or Store()
    pool = await store.pool()
    snapshot = record(await pool.fetchrow("SELECT * FROM intelligence_snapshots WHERE id=$1::uuid AND NOT is_deleted", snapshot_id))
    if not snapshot:
        raise IntelligenceError("observation_not_found", 404)
    plan = await store.plan(snapshot["plan_id"], actor)
    if snapshot.get("generation_id"):
        from open_deep_research.knowledge import authz
        await authz.authorize_document_version(actor, snapshot["document_id"], snapshot["generation_id"])
        if not await pool.fetchval("""SELECT EXISTS(SELECT 1 FROM research_document_generations g
            JOIN research_documents d ON d.id=g.document_id WHERE g.id=$1::uuid AND d.id=$2::uuid
            AND g.status='published' AND d.deleted_at IS NULL AND d.home_knowledge_base_id=$3::uuid)""",
            snapshot["generation_id"], snapshot["document_id"], plan["knowledge_base_id"]):
            raise IntelligenceError("observation_knowledge_source_unavailable", 403)
    return snapshot

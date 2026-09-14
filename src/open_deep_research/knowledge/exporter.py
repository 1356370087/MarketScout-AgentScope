"""Checksummed knowledge archives and transactional, ID-remapped import."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id
from open_deep_research.documents.settings import get_document_settings
from open_deep_research.documents.storage import resolve_storage_key

from . import authz

MANIFEST_FORMAT = "kb-export-v2"
TABLES = (
    "knowledge_collections",
    "research_documents",
    "research_document_versions",
    "research_document_generations",
    "research_document_units",
    "research_document_segments",
    "research_document_chunks",
    "knowledge_document_links",
    "knowledge_fact_keys",
    "knowledge_fact_assertions",
    "knowledge_fact_evidence",
    "knowledge_pages",
    "knowledge_page_revisions",
    "knowledge_page_citations",
    "knowledge_source_relations",
    "research_document_operations",
    "knowledge_sync_sources",
    "knowledge_content_fingerprints",
    "knowledge_health_targets",
)
POINTERS = {
    "current_generation_id",
    "supersedes_version_id",
    "supersedes_generation_id",
    "parent_id",
    "supersedes_id",
    "current_revision_id",
    "published_revision_id",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


async def export_to_path(actor, kb, path):
    """Stream original files and a consistent metadata manifest into a ZIP archive."""
    await authz.require_kb_capability(actor, kb, authz.CAP_MANAGE)
    pool = await get_document_pool()
    data = {}
    async with pool.acquire() as c, c.transaction(isolation="repeatable_read"):
        await c.execute("LOCK TABLE research_documents IN SHARE MODE")
        base = await c.fetchrow("SELECT * FROM knowledge_bases WHERE id=$1::uuid", kb)

        async def rows(table, where, args):
            records = await c.fetch(
                f"SELECT row_to_json(t)::text AS record FROM {table} t WHERE {where}",
                *args,
            )
            data[table] = [json.loads(r["record"]) for r in records]

        await rows("knowledge_collections", "knowledge_base_id=$1::uuid", [kb])
        await rows("research_documents", "home_knowledge_base_id=$1::uuid", [kb])
        docs = [r["id"] for r in data["research_documents"]]
        for table in (
            "research_document_versions",
            "research_document_generations",
            "research_document_chunks",
            "knowledge_document_links",
            "research_document_operations",
        ):
            await rows(table, "document_id=ANY($1::uuid[])", [docs])
        gens = [r["id"] for r in data["research_document_generations"]]
        for table in ("research_document_units", "research_document_segments"):
            await rows(table, "generation_id=ANY($1::uuid[])", [gens])
        await rows("knowledge_fact_assertions", "knowledge_base_id=$1::uuid", [kb])
        await rows(
            "knowledge_fact_keys",
            "id=ANY($1::uuid[])",
            [[r["fact_key_id"] for r in data["knowledge_fact_assertions"]]],
        )
        await rows(
            "knowledge_fact_evidence",
            "assertion_id=ANY($1::uuid[])",
            [[r["id"] for r in data["knowledge_fact_assertions"]]],
        )
        await rows("knowledge_pages", "knowledge_base_id=$1::uuid", [kb])
        await rows(
            "knowledge_page_revisions",
            "page_id=ANY($1::uuid[])",
            [[r["id"] for r in data["knowledge_pages"]]],
        )
        await rows(
            "knowledge_page_citations",
            "revision_id=ANY($1::uuid[])",
            [[r["id"] for r in data["knowledge_page_revisions"]]],
        )
        await rows(
            "knowledge_source_relations",
            "left_document_id=ANY($1::uuid[]) AND right_document_id=ANY($1::uuid[])",
            [docs],
        )
        for table in ("knowledge_sync_sources", "knowledge_content_fingerprints"):
            await rows(table, "document_id=ANY($1::uuid[])", [docs])
        await rows('knowledge_health_targets', 'knowledge_base_id=$1::uuid', [kb])
        manifest = {
            "format": MANIFEST_FORMAT,
            "knowledge_base_name": base["name"],
            "tables": data,
            "files": {},
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        try:
            with zipfile.ZipFile(
                temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for version in data["research_document_versions"]:
                    original = resolve_storage_key(
                        version["storage_key"], get_document_settings()
                    )
                    name = f"originals/{version['id']}"
                    digest = _hash(original)
                    if digest != version["sha256"]:
                        raise ValueError("original_hash_mismatch")
                    archive.write(original, name)
                    manifest["files"][name] = {
                        "sha256": digest,
                        "size": original.stat().st_size,
                    }
                    version["archive_path"] = name
                    preview = (
                        get_document_settings().storage_dir
                        / "previews"
                        / f"{version['id']}.pdf"
                    )
                    if preview.is_file():
                        p = f"previews/{version['id']}.pdf"
                        archive.write(preview, p)
                        manifest["files"][p] = {
                            "sha256": _hash(preview),
                            "size": preview.stat().st_size,
                        }
                archive.writestr("manifest.json", _json(manifest))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


async def export_knowledge_base(actor_id, knowledge_base_id):
    """Return an archive for the synchronous compatibility endpoint."""
    with tempfile.TemporaryDirectory() as tmp:
        path = await export_to_path(
            actor_id, knowledge_base_id, Path(tmp) / "export.zip"
        )
        return path.read_bytes(), f"kb-export-{knowledge_base_id[:8]}.zip"


def _read_archive(source):
    archive = zipfile.ZipFile(
        io.BytesIO(source) if isinstance(source, bytes) else source
    )
    try:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        maximum = int(os.getenv("KNOWLEDGE_IMPORT_MAX_BYTES", str(10 * 1024**3)))
        if (
            len(infos) > 100000
            or len(set(names)) != len(names)
            or sum(i.file_size for i in infos) > maximum
        ):
            raise ValueError("archive_size_or_duplicate_entries")
        for entry in infos:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or ":" in entry.filename
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("archive_unsafe_path")
        if archive.getinfo("manifest.json").file_size > 128 * 1024**2:
            raise ValueError("manifest_too_large")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict):
            raise ValueError("archive_manifest_invalid")
        if manifest.get("format") != MANIFEST_FORMAT:
            raise ValueError("unsupported_archive_format")
        tables = manifest.get("tables")
        # Older v2 archives predate KB-16 coverage targets.
        if isinstance(tables, dict):
            tables.setdefault('knowledge_health_targets', [])
        if not isinstance(tables, dict) or set(tables) != set(TABLES):
            raise ValueError("archive_table_set_invalid")
        ids = {}
        for table, records in tables.items():
            if not isinstance(records, list) or any(
                not isinstance(r, dict) for r in records
            ):
                raise ValueError("archive_records_invalid")
            ids[table] = {r["id"] for r in records}
            if len(ids[table]) != len(records):
                raise ValueError("archive_duplicate_id")
            for identifier in ids[table]:
                uuid.UUID(identifier)
        fk = {
            "document_id": "research_documents",
            "generation_id": "research_document_generations",
            "version_id": "research_document_versions",
            "unit_id": "research_document_units",
            "segment_id": "research_document_segments",
            "page_id": "knowledge_pages",
            "revision_id": "knowledge_page_revisions",
            "assertion_id": "knowledge_fact_assertions",
            "fact_assertion_id": "knowledge_fact_assertions",
            "fact_key_id": "knowledge_fact_keys",
            "collection_id": "knowledge_collections",
            "current_generation_id": "research_document_generations",
            "current_revision_id": "knowledge_page_revisions",
            "published_revision_id": "knowledge_page_revisions",
            "supersedes_id": "knowledge_fact_assertions",
            "supersedes_version_id": "research_document_versions",
            "supersedes_generation_id": "research_document_generations",
            "left_document_id": "research_documents",
            "right_document_id": "research_documents",
            "parent_id": "research_document_units",
        }
        for records in tables.values():
            for record in records:
                for field, target in fk.items():
                    if record.get(field) and record[field] not in ids[target]:
                        raise ValueError("archive_broken_reference:" + field)
        for item in tables["knowledge_fact_evidence"]:
            generation = next(
                r
                for r in tables["research_document_generations"]
                if r["id"] == item["generation_id"]
            )
            if generation["document_id"] != item["document_id"]:
                raise ValueError("archive_evidence_chain_invalid")
        files = manifest["files"]
        if not isinstance(files, dict) or any(
            not isinstance(v, dict) for v in files.values()
        ):
            raise ValueError("archive_files_invalid")
        if set(names) != {"manifest.json", *files}:
            raise ValueError("archive_file_set_invalid")
        for name, meta in files.items():
            with archive.open(name) as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if (
                digest != meta["sha256"]
                or archive.getinfo(name).file_size != meta["size"]
            ):
                raise ValueError("archive_checksum_mismatch")
        for version in tables["research_document_versions"]:
            if files[version["archive_path"]]["sha256"] != version["sha256"]:
                raise ValueError("archive_version_hash_mismatch")
        return archive, manifest
    except Exception:
        archive.close()
        raise


def precheck_import(source):
    """Validate archive checksums and references before scheduling import."""
    try:
        archive, manifest = _read_archive(source)
        archive.close()
        data = manifest["tables"]
        return {
            "ok": True,
            "errors": [],
            "document_count": len(data["research_documents"]),
            "fact_count": len(data["knowledge_fact_assertions"]),
            "page_count": len(data["knowledge_pages"]),
            "collection_count": len(data["knowledge_collections"]),
            "version_count": len(data["research_document_versions"]),
        }
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}


async def import_archive(actor, target_kb, path, import_id):
    """Import reviewed archive data into a new restricted knowledge base."""
    context = await authz.require_kb_capability(actor, target_kb, authz.CAP_MANAGE)
    archive, manifest = _read_archive(path)
    actor = document_owner_id(actor)
    pool = await get_document_pool()
    namespace = uuid.UUID(import_id)
    new_kb = str(uuid.uuid5(namespace, "knowledge-base"))
    data = manifest["tables"]
    mapping = {
        r["id"]: str(uuid.uuid5(namespace, t + ":" + r["id"]))
        for t, records in data.items()
        for r in records
    }
    settings = get_document_settings()
    created = []

    def remap(value):
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    try:
        async with pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))", import_id)
            if await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE id=$1::uuid)", new_kb
            ):
                return {"knowledge_base_id": new_kb, "already_imported": True}
            reused_keys = set()
            for key in data["knowledge_fact_keys"]:
                existing = await c.fetchval(
                    """SELECT id FROM knowledge_fact_keys WHERE workspace_id=$1
                    AND entity_name=$2 AND metric=$3 AND region=$4 AND period_label=$5 AND condition_text=$6""",
                    context["knowledge_base"]["workspace_id"],
                    key["entity_name"],
                    key["metric"],
                    key["region"],
                    key["period_label"],
                    key["condition_text"],
                )
                if existing:
                    mapping[key["id"]] = str(existing)
                    reused_keys.add(key["id"])
            await c.execute(
                """INSERT INTO knowledge_bases(id,owner_id,name,description,workspace_id,visibility,created_by)
                VALUES($1::uuid,$2::uuid,$3,'Imported archive; review required',$4,'restricted',$2::uuid)""",
                new_kb,
                actor,
                manifest["knowledge_base_name"][:150] + " · 导入 " + import_id[:8],
                context["knowledge_base"]["workspace_id"],
            )
            await c.execute(
                "INSERT INTO knowledge_base_members(knowledge_base_id,user_id,role) VALUES($1::uuid,$2::uuid,'manager')",
                new_kb,
                actor,
            )
            deferred = []
            version_paths = {}
            for v in data["research_document_versions"]:
                key = f"imports/{import_id}/{mapping[v['id']]}"
                original = resolve_storage_key(key, settings)
                original.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(v["archive_path"]) as src, original.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                created.append(original)
                version_paths[v["id"]] = key
                preview = f"previews/{v['id']}.pdf"
                if preview in manifest["files"]:
                    dest = settings.storage_dir / "previews" / f"{mapping[v['id']]}.pdf"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(preview) as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    created.append(dest)
            for table in TABLES:
                columns = await c.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 AND is_generated='NEVER'",
                    table,
                )
                allowed = {r["column_name"] for r in columns}
                for original in data[table]:
                    if table == "knowledge_fact_keys" and original["id"] in reused_keys:
                        continue
                    row = {k: v for k, v in remap(original).items() if k in allowed}
                    for key in (
                        "owner_id",
                        "created_by",
                        "reviewed_by",
                        "actor_id",
                        "confirmed_by",
                    ):
                        if key in row:
                            row[key] = (
                                actor
                                if key in {"owner_id", "created_by", "actor_id"}
                                else None
                            )
                    if "workspace_id" in row:
                        row["workspace_id"] = str(
                            context["knowledge_base"]["workspace_id"]
                        )
                    for key in ("knowledge_base_id", "home_knowledge_base_id"):
                        if key in row:
                            row[key] = new_kb
                    for key in POINTERS & row.keys():
                        if row[key] and key not in {
                            "current_generation_id",
                            "published_revision_id",
                        }:
                            deferred.append((table, row["id"], key, row[key]))
                        row[key] = None
                    if table == "research_documents":
                        versions = [
                            v
                            for v in data["research_document_versions"]
                            if v["document_id"] == original["id"]
                        ]
                        v = max(versions, key=lambda v: v["version_no"])
                        row.update(
                            storage_key=version_paths[v["id"]],
                            status="ready",
                            deleted_at=None,
                            deleted_by=None,
                            purge_after=None,
                            current_generation_id=None,
                        )
                    if table == "research_document_versions":
                        row["storage_key"] = version_paths[original["id"]]
                    if table == "research_document_generations":
                        row.update(status="pending_review", published_at=None)
                        row["parse_config"] = {
                            **(row.get("parse_config") or {}),
                            "imported_status": original["status"],
                        }
                    if table == "knowledge_fact_assertions":
                        row.update(
                            status="draft",
                            published_at=None,
                            adopted=False,
                            extraction_key=None,
                        )
                    if table == "knowledge_page_revisions":
                        row.update(status="draft", published_at=None)
                    if table == "knowledge_page_citations":
                        row["citation_status"] = "needs_review"
                    if table == "knowledge_sync_sources":
                        row.update(paused=True, next_run_at=None)
                    names = ",".join('"' + name + '"' for name in row)
                    await c.execute(
                        f"INSERT INTO {table} ({names}) SELECT {names} FROM jsonb_populate_record(NULL::{table},$1::jsonb)",
                        _json(row),
                    )
            for table, identifier, key, value in deferred:
                await c.execute(
                    f"UPDATE {table} SET {key}=$2::uuid WHERE id=$1::uuid",
                    identifier,
                    value,
                )
        return {"knowledge_base_id": new_kb, "status": "review_required"}
    except Exception:
        for file in created:
            file.unlink(missing_ok=True)
        raise
    finally:
        archive.close()

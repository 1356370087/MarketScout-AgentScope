"""Freeze published knowledge inputs once, before research execution."""

from open_deep_research.documents.repository import validate_selection
from open_deep_research.documents.settings import get_document_settings

from .search_service import SearchScopeError, load_profile


async def prepare_knowledge_sources(owner_id, selection):
    """Return source snapshots and their non-secret retrieval manifest."""
    rows = await validate_selection(owner_id, selection)
    profile = get_document_settings().index_profile
    if any(row["index_profile"] != profile for row in rows):
        raise SearchScopeError("knowledge_index_profile_mismatch")
    snapshots = [{key: str(row[key]) for key in ("id", "filename", "sha256", "current_generation_id")}
                 for row in rows]
    manifest = {
        "schema": "knowledge-run.v1",
        "documents": [{"document_id": str(row["id"]), "generation_id": str(row["current_generation_id"]),
                       "index_profile": row["index_profile"]} for row in rows],
        "parameters": await load_profile(selection.retrieval.profile_version),
        "filters": selection.retrieval.filters,
    }
    from .research_assets import freeze_assets

    manifest["assets"] = await freeze_assets(owner_id, selection, manifest["documents"])
    return {"selected_source_snapshots": snapshots, "knowledge_manifest": manifest}


async def legacy_knowledge_manifest(owner_id, snapshots):
    """Keep pre-v15 runs on their existing generations and fixed RRF-only policy."""
    from open_deep_research.documents.database import get_document_pool
    from open_deep_research.documents.identity import document_owner_id
    from open_deep_research.documents.retrieval import locator_dict

    from .authz import document_read_sql
    from .search_service import DEFAULT_PARAMETERS

    generations = [row["current_generation_id"] for row in snapshots]
    pool = await get_document_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            f"""SELECT g.document_id,g.id,g.index_profile FROM research_document_generations g
            JOIN research_documents d ON d.id=g.document_id
            WHERE g.id=ANY($1::uuid[]) AND {document_read_sql('$2')}
              AND d.deleted_at IS NULL AND g.published_at IS NOT NULL""",
            generations, document_owner_id(owner_id))
    if {str(row["id"]) for row in rows} != set(generations):
        raise SearchScopeError("legacy_run_knowledge_sources_unavailable")
    return {"schema": "knowledge-run.v1", "documents": [
        {"document_id": str(row["document_id"]), "generation_id": str(row["id"]),
         "index_profile": locator_dict(row["index_profile"])} for row in rows],
        "parameters": {**DEFAULT_PARAMETERS, "version": "legacy-rrf-v1",
                       "rerank_enabled": False, "parent_context": False,
                       "context_neighbors": 0, "per_document_quota": 40},
        "filters": {}, "assets": {"facts": [], "wiki": []}}

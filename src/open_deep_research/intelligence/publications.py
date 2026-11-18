"""Published generations enter a durable journal in the publishing transaction."""

from .settings import settings
from .store import encode


async def record_publication(c, document_id, generation_id, previous, actor):
    if not settings().enabled:
        return
    origin = await c.fetchval("""WITH reviews AS (
        SELECT id,review,update_time FROM intelligence_items UNION ALL
        SELECT item_id,result->'review',update_time FROM intelligence_item_runs)
        SELECT i.id FROM reviews i, LATERAL jsonb_each(coalesce(i.review->'sources','{}'::jsonb)) source
        WHERE source.value->>'document_id'=$1 AND (source.value->>'generation_id'=$2 OR source.value->>'raw_sha256'=(
            SELECT v.sha256 FROM research_document_generations g JOIN research_document_versions v ON v.id=g.version_id WHERE g.id=$2::uuid))
        ORDER BY i.update_time DESC LIMIT 1""", document_id, generation_id)
    await c.execute("""INSERT INTO intelligence_publications(document_id,generation_id,previous_generation_id,origin_item_id,created_at)
        VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5)""", document_id, generation_id, previous, origin, actor)
    await c.execute("""UPDATE intelligence_sources s SET next_run_at=now() FROM intelligence_plans p
        WHERE s.plan_id=p.id AND p.status='active' AND p.definition->>'cadence'<>'manual'
        AND NOT s.is_deleted AND s.definition->>'document_id'=$1""", document_id)


async def record_asset_publication(c, base_id, kind, asset_id, revision_id, content, actor, *, previous_id=None):
    """Published knowledge is context for every authorized plan in its own KB.

    The internal source carries publication operations, never a new web or
    document permission. Models still have to obtain admitted source evidence.
    """
    if not settings().enabled:
        return
    origin = await c.fetchval("""WITH reviews AS (
        SELECT id,review,update_time FROM intelligence_items UNION ALL
        SELECT item_id,result->'review',update_time FROM intelligence_item_runs)
        SELECT id FROM reviews WHERE review->>'wiki_page_id'=$1
        OR review->'fact_ids' @> $2::jsonb ORDER BY update_time DESC LIMIT 1""", str(asset_id), encode([str(asset_id)]))
    plans = await c.fetch("SELECT id FROM intelligence_plans WHERE knowledge_base_id=$1::uuid AND status='active'", str(base_id))
    payload = {"kind": kind, "asset_id": str(asset_id), "revision_id": str(revision_id),
        "previous_id": str(previous_id) if previous_id else None, "content": content[:16000], "truncated": len(content) > 16000}
    for plan in plans:
        source = await c.fetchrow("""INSERT INTO intelligence_sources(plan_id,definition,identity,sequence,created_at)
            VALUES($1,'{"kind":"knowledge_events","label":"知识库发布事件"}'::jsonb,'knowledge-publications',1,$2)
            ON CONFLICT(plan_id,identity) DO UPDATE SET sequence=intelligence_sources.sequence+1,is_deleted=false
            RETURNING id,sequence""", plan["id"], actor)
        own = origin and await c.fetchval("SELECT plan_id=$2 FROM intelligence_items WHERE id=$1", origin, plan["id"])
        await c.execute("""INSERT INTO intelligence_events(plan_id,source_id,sequence,kind,payload,origin_item_id,status,created_at,available_at)
            VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,now()+interval '5 seconds')""",
            plan["id"], source["id"], source["sequence"], kind, encode(payload), origin, "self_published" if own else "pending", actor)

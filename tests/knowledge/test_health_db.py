"""Database health aggregation, pagination and private-query isolation."""

import json
import os
import uuid

import pytest

from open_deep_research.documents.database import close_document_pool, get_document_pool
from open_deep_research.knowledge import authz, health
from open_deep_research.knowledge.repository import create_knowledge_base

DSN = os.getenv("IAM_TEST_DATABASE_URL", "")


@pytest.mark.asyncio
@pytest.mark.skipif(not DSN, reason="IAM_TEST_DATABASE_URL not configured")
async def test_health_is_scoped_and_zero_results_are_business_events(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RESEARCH_ENABLED", "true")
    monkeypatch.setenv(
        "DOCUMENT_DATABASE_URL", DSN.replace("postgresql+asyncpg://", "postgresql://")
    )
    owner, outsider = str(uuid.uuid4()), str(uuid.uuid4())
    kb = await create_knowledge_base(owner, "健康看板测试 " + owner[:8], "KB-16 集成测试")
    pool = await get_document_pool()
    try:
        async with pool.acquire() as c:
            await c.execute(
                """INSERT INTO knowledge_health_targets(knowledge_base_id,company,period,topic,created_by)
                VALUES($1::uuid,'竞品甲','2025年','财报',$2::uuid)""",
                kb.id,
                owner,
            )
            for actor, hits, rerank, age in [
                (owner, 0, True, 0),
                (owner, 2, True, 0),
                (owner, 0, False, 0),
                (outsider, 0, True, 0),
                (owner, 0, True, 100),
            ]:
                await c.execute(
                    """INSERT INTO knowledge_queries(id,owner_id,query_text,scope,result_digest,created_at)
                    VALUES($1::uuid,$2::uuid,'测试检索',$3::jsonb,$4::jsonb,now()-$5*interval '1 day')""",
                    str(uuid.uuid4()),
                    actor,
                    json.dumps({"kb_ids": [kb.id]}),
                    json.dumps({"hits": hits, "rerank_completed": rerank}),
                    age,
                )
        result = await health.dashboard(owner, kb.id, limit=1)
        assert result["counts"]["missing_topic"] == 1
        assert result["counts"]["no_results"] == 1
        assert result["total"] == 2 and len(result["items"]) == 1
        assert (await health.dashboard(owner, kb.id, company="竞品甲"))["counts"][
            "no_results"
        ] == 0
        assert (await health.dashboard(owner, kb.id, kind="no_results"))["total"] == 1
        with pytest.raises(authz.AuthorizationError):
            await health.dashboard(outsider, kb.id)
    finally:
        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM knowledge_queries WHERE owner_id=ANY($1::uuid[])",
                [owner, outsider],
            )
            await c.execute("DELETE FROM knowledge_bases WHERE id=$1::uuid", kb.id)
        await close_document_pool()

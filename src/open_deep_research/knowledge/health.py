"""Business health signals scoped to one knowledge base and current permissions."""

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from open_deep_research.documents.database import get_document_pool
from open_deep_research.documents.identity import document_owner_id

from . import authz

KINDS = ("expired", "missing_topic", "pending_review", "sync_failed", "no_results")


def _object(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def _text(value):
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    if isinstance(value, dict):
        return _text(
            value.get("value")
            or value.get("name")
            or value.get("name_zh")
            or value.get("name_en")
        )
    return str(value or "").strip()


def metadata(raw):
    """Use confirmed metadata only; unconfirmed attribution is not business coverage."""
    raw = _object(raw)
    confirmed = raw.get("confirmed") or {}
    result = {key: _text(value) for key, value in confirmed.items()}
    validity = confirmed.get("validity")
    if isinstance(validity, dict):
        result["valid_until"] = _text(validity.get("end"))
        result["valid_from"] = _text(validity.get("start"))
    return result


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def calculate(documents, assertions, pages, syncs, queries, targets, today):
    """Calculate health rows without conflating historical periods with expired sources."""
    items = []
    coverage = []
    groups = set()
    docs = {str(d["id"]): d for d in documents}

    def add(kind, company, period, title, source_id, url, reason, **extra):
        company, period = company or "未标注竞品", period or "未标注期间"
        groups.add((company, period))
        items.append(
            dict(
                kind=kind,
                company=company,
                period=period,
                title=title,
                source_id=str(source_id),
                url=url,
                reason=reason,
                **extra,
            )
        )

    for d in documents:
        m = metadata(d["metadata_snapshot"])
        company, period = (
            m.get("company", ""),
            m.get("period") or m.get("data_period", ""),
        )
        d["_meta"] = m
        groups.add((company or "未标注竞品", period or "未标注期间"))
        published = bool(d["published_id"])
        expiry = _day(m.get("valid_until") or m.get("valid_to"))
        expired = published and expiry is not None and expiry <= today
        reason = "明确有效期已结束" if expired else ""
        applicable = [
            t["max_age_days"]
            for t in targets
            if t["company"] == company
            and t["period"] == period
            and t["topic"] == (m.get("topic") or m.get("doc_type"))
            and t["max_age_days"]
        ]
        published_date = _day(m.get("publish_date")) or _day(d.get("published_at"))
        if (
            published
            and applicable
            and published_date
            and (today - published_date).days > min(applicable)
        ):
            expired, reason = True, "超过覆盖目标设定的更新周期"
        d["_expired"] = expired
        start = _day(m.get("valid_from"))
        d["_active"] = not start or start <= today
        if expired:
            add(
                "expired",
                company,
                period,
                d["filename"],
                d["id"],
                f"/documents/{d['id']}",
                reason,
            )
        if d["pending_id"] or not company or not period:
            pending = metadata(d.get("pending_metadata")) if d["pending_id"] else m
            add(
                "pending_review",
                pending.get("company"),
                pending.get("period") or pending.get("data_period"),
                d["filename"],
                d["id"],
                f"/documents/{d['id']}",
                "存在未发布代次" if d["pending_id"] else "竞品或数据期间尚未确认",
                source_type="document",
            )
    for t in targets:
        matching = [
            d
            for d in documents
            if d["published_id"]
            and not d["_expired"]
            and d["_active"]
            and d["_meta"].get("company") == t["company"]
            and (d["_meta"].get("period") or d["_meta"].get("data_period"))
            == t["period"]
            and (d["_meta"].get("topic") or d["_meta"].get("doc_type")) == t["topic"]
        ]
        valid_count = len(matching)
        groups.add((t["company"], t["period"]))
        coverage.append(
            {
                **dict(t),
                "id": str(t["id"]),
                "available_documents": valid_count,
                "missing_documents": max(0, t["min_documents"] - valid_count),
            }
        )
        if valid_count < t["min_documents"]:
            add(
                "missing_topic",
                t["company"],
                t["period"],
                t["topic"],
                t["id"],
                "/documents",
                f"需要 {t['min_documents']} 份有效已发布资料，当前 {valid_count} 份",
                missing_documents=t["min_documents"] - valid_count,
            )
    for a in assertions:
        if a["status"] in ("draft", "pending_review") or (
            a["status"] == "published" and a["verification"] != "verified"
        ):
            add(
                "pending_review",
                a["entity_name"],
                a["data_period"] or a["period_label"],
                a["metric"],
                a["id"],
                "/knowledge/ledger",
                "事实草稿待审核"
                if a["status"] != "published"
                else "事实尚未核验或存在争议",
                source_type="fact",
            )
    for p in pages:
        if p["draft"] or p["stale_count"]:
            add(
                "pending_review",
                p["entity_name"],
                "",
                p["title"],
                p["id"],
                "/knowledge/ledger",
                "Wiki 来源变化或需核验" if p["stale_count"] else "Wiki 草稿待发布",
                source_type="wiki",
            )
    for s in syncs:
        d = docs.get(str(s["document_id"]))
        if not d:
            continue
        m = d["_meta"]
        add(
            "sync_failed",
            m.get("company"),
            m.get("period") or m.get("data_period"),
            d["filename"],
            s["id"],
            f"/documents/{d['id']}",
            f"连续失败 {s['consecutive_failures']} 次"
            + ("，同步已暂停" if s["paused"] else ""),
            consecutive_failures=s["consecutive_failures"],
            last_success_at=s["last_success_at"],
        )
    for q in queries:
        scope = _object(q["scope"])
        selected = scope.get("resolved_document_ids") or scope.get("document_ids") or []
        dimensions = {
            (
                docs[str(i)]["_meta"].get("company", ""),
                docs[str(i)]["_meta"].get("period")
                or docs[str(i)]["_meta"].get("data_period", ""),
            )
            for i in selected
            if str(i) in docs
        }
        unambiguous = (
            len(dimensions) == 1
            and len(scope.get("kb_ids") or []) <= 1
            and all(str(i) in docs for i in selected)
        )
        company, period = next(iter(dimensions)) if unambiguous else ("", "")
        add(
            "no_results",
            company,
            period,
            q["query_text"],
            q["id"],
            "/knowledge",
            "当前用户检索返回 0 条结果；多竞品范围不推断归属",
            created_at=q["created_at"],
        )
    return items, coverage, groups


async def dashboard(
    actor, kb, company="", period="", kind="", days=30, offset=0, limit=50
):
    """Read authoritative health inputs; reviewers can inspect governance work items."""
    await authz.require_kb_capability(actor, kb, authz.CAP_REVIEW)
    pool = await get_document_pool()
    async with (
        pool.acquire() as c,
        c.transaction(isolation="repeatable_read", readonly=True),
    ):
        targets = await c.fetch(
            "SELECT * FROM knowledge_health_targets WHERE knowledge_base_id=$1::uuid ORDER BY company,period,topic",
            kb,
        )
        documents = await c.fetch(
            """SELECT d.id,d.filename,pg.id AS published_id,
            pg.metadata_snapshot,pg.published_at,pending.id AS pending_id,
            pending.metadata_snapshot AS pending_metadata FROM research_documents d
            LEFT JOIN research_document_generations pg ON pg.id=d.current_generation_id AND pg.status='published'
            LEFT JOIN LATERAL (SELECT id,metadata_snapshot FROM research_document_generations
                WHERE document_id=d.id AND status IN ('draft','pending_review') ORDER BY created_at DESC LIMIT 1) pending ON true
            WHERE d.home_knowledge_base_id=$1::uuid AND d.deleted_at IS NULL ORDER BY d.id""",
            kb,
        )
        documents = [dict(d) for d in documents]
        for d in documents:
            if not d["published_id"]:
                d["metadata_snapshot"] = d["pending_metadata"]
        assertions = await c.fetch(
            """SELECT a.id,a.status,a.verification,a.data_period,k.entity_name,k.period_label,k.metric
            FROM knowledge_fact_assertions a JOIN knowledge_fact_keys k ON k.id=a.fact_key_id
            WHERE a.knowledge_base_id=$1::uuid AND a.status IN ('draft','pending_review','published')""",
            kb,
        )
        pages = await c.fetch(
            """SELECT p.id,p.title,p.entity_name,r.status='draft' AS draft,
            (SELECT count(*) FROM knowledge_page_citations c WHERE (c.revision_id=p.current_revision_id OR c.revision_id=p.published_revision_id)
             AND c.citation_status<>'current') AS stale_count
            FROM knowledge_pages p LEFT JOIN knowledge_page_revisions r ON r.id=p.current_revision_id WHERE p.knowledge_base_id=$1::uuid""",
            kb,
        )
        syncs = await c.fetch(
            "SELECT * FROM knowledge_sync_sources WHERE knowledge_base_id=$1::uuid AND consecutive_failures>0 ORDER BY consecutive_failures DESC",
            kb,
        )
        queries = await c.fetch(
            """SELECT q.id,q.query_text,q.scope,q.created_at FROM knowledge_queries q
            WHERE q.owner_id=$2::uuid AND q.created_at >= $3 AND q.result_digest->>'hits'='0'
              AND COALESCE(q.result_digest->>'rerank_completed','true')='true'
              AND ((q.scope->'kb_ids') ? $1 OR EXISTS(SELECT 1 FROM research_documents d
                WHERE d.home_knowledge_base_id=$1::uuid AND d.deleted_at IS NULL
                AND ((q.scope->'resolved_document_ids') ? d.id::text OR (q.scope->'document_ids') ? d.id::text)))
            ORDER BY q.created_at DESC""",
            kb,
            document_owner_id(actor),
            datetime.now(timezone.utc) - timedelta(days=days),
        )
    items, coverage, groups = calculate(
        documents, assertions, pages, syncs, queries, targets, date.today()
    )
    for item in items:
        if item.get("source_type") in {"wiki", "fact"}:
            key = "page_id" if item["source_type"] == "wiki" else "fact_id"
            item["url"] = "/knowledge/ledger?" + urlencode(
                {"kb_id": kb, key: item["source_id"]}
            )
        elif item["kind"] == "no_results":
            item["url"] = "/knowledge?" + urlencode(
                {"kb_id": kb, "query": item["title"]}
            )
    selected = [
        i
        for i in items
        if (not company or i["company"] == company)
        and (not period or i["period"] == period)
    ]
    counts = Counter(i["kind"] for i in selected)
    group_rows = []
    for co, pe in sorted(groups):
        if (company and co != company) or (period and pe != period):
            continue
        totals = Counter(
            i["kind"] for i in selected if i["company"] == co and i["period"] == pe
        )
        group_rows.append(
            {"company": co, "period": pe, **{k: totals[k] for k in KINDS}}
        )
    details = [i for i in selected if not kind or i["kind"] == kind]
    return {
        "knowledge_base_id": kb,
        "generated_at": datetime.now(timezone.utc),
        "query_window_days": days,
        "query_visibility": "current_user",
        "counts": {k: counts[k] for k in KINDS},
        "groups": group_rows,
        "companies": sorted({g[0] for g in groups}),
        "periods": sorted({g[1] for g in groups}),
        "targets": coverage,
        "items": details[offset : offset + limit],
        "total": len(details),
        "offset": offset,
        "limit": limit,
    }

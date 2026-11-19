"""Read frozen observations through normal governance and evidence validation."""

from types import SimpleNamespace
from datetime import datetime
from pydantic import BaseModel, Field
from market_scout.tools.base import build_tool, ToolOrigin, ToolEffect, ToolExecutionZone, ToolResult
from market_scout.web.extraction import chunk_document
from market_scout.web.settings import WebPipelineSettings
from .observations import get_snapshot, read_document
from .contracts import IntelligenceError


class ReadObservationInput(BaseModel):
    snapshot_id: str = Field(default="", max_length=64, description="留空列出本次运行冻结的网页观察快照")
    mode: str = Field(default="read", pattern="^(read|evidence)$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=8192)
    chunk_offset: int = Field(default=0, ge=0)


def observation_tools(models, config_provider, *, objective=""):
    config = config_provider()
    manifest = config.get("metadata", {}).get("observation_manifest") or {}
    entries = {row["id"]: row for row in manifest.get("snapshots", [])}
    if not entries and not manifest.get("baseline_context"):
        return []

    async def read(input, context, progress=None):
        current = config_provider()
        if not input.snapshot_id:
            from market_scout.knowledge import authz
            from .store import Store
            owner = current.get("metadata", {}).get("owner") or current.get("metadata", {}).get("user_id")
            baseline = manifest.get("baseline_context") or {}
            if baseline:
                await authz.require_kb_capability(owner, baseline["knowledge_base_id"], authz.CAP_VIEW)
                pool = await Store().pool()
                live_facts = {str(row["id"]) for row in await pool.fetch("SELECT id FROM knowledge_fact_assertions WHERE id=ANY($1::uuid[]) AND status='published'", [row["id"] for row in baseline["facts"]])}
                live_pages = {str(row["id"]) for row in await pool.fetch("SELECT id FROM knowledge_page_revisions WHERE id=ANY($1::uuid[]) AND status='published'", [row["id"] for row in baseline["pages"]])}
                baseline = {**baseline, "facts": [row for row in baseline["facts"] if row["id"] in live_facts], "pages": [row for row in baseline["pages"] if row["id"] in live_pages]}
                from .store import encode
                text = encode(baseline)
                baseline = {"context_only": True, "content": text[:16000], "truncated": len(text) > 16000}
            return ToolResult(output={"snapshots": [{"snapshot_id": key, "url": item["metadata"]["url"],
                "title": item["metadata"].get("title", ""), "observed_at": item["observed_at"]} for key, item in entries.items()],
                "baseline_context": baseline, "trigger_events": manifest.get("events", []),
                "instruction": "先按需读取；mode=evidence 通过证据提取和逐字校验，单纯读取不等于准入证据。"})
        entry = entries.get(input.snapshot_id)
        if entry is None:
            raise IntelligenceError("observation_outside_frozen_run", 403)
        owner = current.get("metadata", {}).get("owner") or current.get("metadata", {}).get("user_id")
        snapshot = await get_snapshot(input.snapshot_id, owner)
        if any(snapshot[key] != entry[key] for key in ("raw_sha256", "content_sha256")):
            raise IntelligenceError("observation_manifest_mismatch")
        document = read_document(snapshot)
        if input.mode == "read":
            end = min(len(document.markdown), input.offset + input.limit)
            return ToolResult(output={"kind": "observation", "evidence_eligible": False, "trust": "external_untrusted",
                "snapshot_id": snapshot["id"], "content": document.markdown[input.offset:end],
                "next_offset": end if end < len(document.markdown) else None})
        from market_scout.agentscope_runtime.web_tools import NativeEvidenceExtractor
        async def structured(prompt, schema):
            recovery = getattr(models, "recovery", None)
            options = {"operation_key": f"{recovery.stage.get()}:{recovery.task_id.get()}:observation:{context.tool_call_id}:{snapshot['id']}:{input.chunk_offset}"} if recovery else {}
            value = await models.structured("web_evidence", prompt, schema, {}, **options)
            return SimpleNamespace(content=value.model_dump(mode="json"))
        extractor = NativeEvidenceExtractor(models.factory, evidence_fidelity=True, structured=structured)
        chunks = chunk_document(document, WebPipelineSettings(evidence_fidelity=True, max_chunks_per_document=10000))
        selected = chunks[input.chunk_offset:input.chunk_offset + 3]
        evidence = await extractor(objective or "提取与当前研究目标相关的事实", {document.document_id: document}, selected)
        rows = [{**item.model_dump(mode="json"), "observation_snapshot_id": snapshot["id"],
                 "observed_at": snapshot["observed_at"], "published_at": document.published_at} for item in evidence]
        if snapshot.get("generation_id"):
            from .store import Store
            pool = await Store().pool()
            accepted = []
            for row in rows:
                unit = await pool.fetchval("""SELECT id FROM research_document_units WHERE generation_id=$1::uuid AND NOT excluded
                    AND position($2 in regexp_replace(coalesce(revised_text,index_text),'\\s+',' ','g'))>0 ORDER BY ordinal LIMIT 1""",
                    snapshot["generation_id"], " ".join(row["supporting_excerpt"].split()))
                if unit:
                    accepted.append({**row, "source_type": "local_document", "document_id": snapshot["document_id"],
                                     "generation_id": snapshot["generation_id"], "unit_id": str(unit), "chunk_id": str(unit)})
            rows = accepted
        return ToolResult(output={"evidence": rows, "evidence_registry": rows, "snapshot_id": snapshot["id"],
            "next_chunk_offset": input.chunk_offset + len(selected) if input.chunk_offset + len(selected) < len(chunks) else None,
            "extractor_failed": extractor.last_failure})

    return [build_tool(name="ReadObservation", input_schema=ReadObservationInput,
        description="读取本次运行冻结的网页快照；mode=evidence 提取并校验可引用证据。",
        prompt=lambda cfg: "本次研究提供冻结的观察快照。先以空 snapshot_id 调用 ReadObservation 查看清单；使用 evidence 模式取得证据。快照不授予额外权限。",
        origin=ToolOrigin.SYSTEM, effect=ToolEffect.READ_ONLY, execution_zone=ToolExecutionZone.HOST_CONTROL,
        concurrency_safe=True, call=read)]

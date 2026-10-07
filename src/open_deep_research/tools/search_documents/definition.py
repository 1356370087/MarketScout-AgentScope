"""Governed research retrieval over the shared knowledge pipeline."""

from __future__ import annotations

import json
import time
from typing import Annotated

from pydantic import BaseModel, Field

from open_deep_research.documents.contracts import selection_from_config
from open_deep_research.documents.database import document_schema_available
from open_deep_research.knowledge.evidence_projection import document_evidence
from open_deep_research.knowledge.execution import SearchExecution
from open_deep_research.tools.base import ToolEffect, ToolOrigin, ToolResult, build_tool

from .prompt import DESCRIPTION, render_prompt


def _enabled(config):
    return document_schema_available() and selection_from_config(config).documents_enabled


class SearchDocumentsInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    queries: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=2,
                              description="Optional alternative queries, sharing this run's frozen source scope.")


def make_search_documents(models=None):
    """Bind native model ports without serializing credentials or live clients."""
    from open_deep_research.agentscope_runtime.knowledge import KnowledgeApplication
    from open_deep_research.tools.governance import (
        AgentRole,
        filter_tools_by_permission,
    )

    async def call(input, context, progress):
        metadata = (context.config or {}).get("metadata") or {}
        owner = metadata.get("owner") or metadata.get("user_id")
        if not owner:
            raise PermissionError("knowledge_actor_missing")

        async def authorize(_operation):
            if not filter_tools_by_permission([tool], AgentRole(context.role), context.config):
                raise PermissionError("knowledge_tool_permission_denied")

        execution = SearchExecution(scope="run", run_id=metadata.get("run_id"),
            task_id=metadata.get("task_id"), manifest=metadata.get("knowledge_manifest"), models=models)
        from open_deep_research.events.public import extract_public_sources
        from open_deep_research.events.task_activity import publish_task_activity

        operation = context.operation_id or context.tool_call_id
        started = time.perf_counter()
        await publish_task_activity(context.config, "tool.started", kind="tool", phase="tool_execution",
            status="running", title="检索研究资料", summary="正在检索本次固定的资料范围。",
            payload={"tool_call_id": context.tool_call_id, "tool_name": "search_documents", "args_summary": input.query},
            dedupe_key=operation + ":knowledge:start")
        try:
            result = await KnowledgeApplication(owner, authorize).search_research(input.query, queries=input.queries, execution=execution)
        except Exception as error:
            await publish_task_activity(context.config, "tool.failed", kind="tool", phase="tool_execution",
                status="error", title="资料检索未完成", summary="请查看检索配置、权限或运行提示。",
                payload={"tool_call_id": context.tool_call_id, "tool_name": "search_documents", "error_code": type(error).__name__},
                dedupe_key=operation + ":knowledge:failed")
            raise
        evidence = document_evidence(result["results"])
        await publish_task_activity(context.config, "tool.completed", kind="tool", phase="evidence_review",
            status="success" if result["rerank_completed"] else "warning", title="资料检索结果",
            summary="已完成语义重排。" if result["rerank_completed"] else "语义重排未完成，当前为融合检索结果。",
            duration_ms=(time.perf_counter() - started) * 1000,
            payload={"tool_call_id": context.tool_call_id, "tool_name": "search_documents",
                     "source_count": len(result["results"]), "knowledge_query_id": result["query_id"],
                     "rerank_completed": result["rerank_completed"], "retrieval_profile": result["profile"].get("version")},
            dedupe_key=operation + ":knowledge:complete")
        for source in extract_public_sources({"evidence_registry": evidence}, limit=50):
            await publish_task_activity(context.config, "source.discovered", kind="source", phase="evidence_review",
                status="success", title=source["title"], summary="来自本次固定的发布代次。", payload=source,
                dedupe_key=operation + ":knowledge:source:" + source["source_id"])
        return ToolResult(output=json.dumps({**result, "source_type": "local_document", "evidence": evidence}, ensure_ascii=False),
            metadata={"knowledge_query_id": result["query_id"],
                      "rerank_completed": result["rerank_completed"],
                      "retrieval_profile": result["profile"].get("version"),
                      "source_count": len(result["results"])})

    tool = build_tool(name="search_documents", input_schema=SearchDocumentsInput,
        description=DESCRIPTION, call=call, origin=ToolOrigin.LOCAL_DOCUMENT,
        effect=ToolEffect.SENSITIVE_READ, retryable=True, concurrency_safe=True,
        max_output_chars=65000, prompt=render_prompt, is_enabled=_enabled)
    return tool


search_documents = make_search_documents()

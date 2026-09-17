"""Definition of the owner-filtered local-document hybrid search tool."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from open_deep_research.config_types import RuntimeConfig
from open_deep_research.documents.contracts import selection_from_config
from open_deep_research.documents.database import document_schema_available
from open_deep_research.documents.repository import run_source_document_ids
from open_deep_research.documents.retrieval import search_document_chunks
from open_deep_research.tools.base import ToolEffect, ToolOrigin, ToolResult, build_tool

from .prompt import DESCRIPTION, render_prompt


def _enabled(config: RuntimeConfig) -> bool:
    return (
        document_schema_available() and selection_from_config(config).documents_enabled
    )


async def _search_documents_call(query: str, config: RuntimeConfig = None) -> str:
    selection = selection_from_config(config)
    metadata = dict((config or {}).get("metadata") or {})
    owner_id = str(metadata.get("owner") or "")
    document_ids = selection.document_ids
    if not document_ids and (selection.knowledge_base_ids or selection.collection_ids):
        # KB/collection selections were expanded and frozen at run creation.
        document_ids = await run_source_document_ids(str(metadata.get("run_id") or ""))
    if not owner_id or not document_ids:
        raise ValueError("No owner-scoped documents are selected for this run")
    results = await search_document_chunks(
        owner_id=owner_id,
        document_ids=document_ids,
        query=query,
        run_id=str(metadata.get("run_id") or "") or None,
    )
    documents = {
        item["document_id"]: {
            "document_id": item["document_id"],
            "filename": item["filename"],
            "source_type": "local_document",
            "source_uri": f"/documents/{item['document_id']}",
        }
        for item in results
    }
    evidence = []
    for item in results:
        excerpt = str(item["text"]).strip()[:1600]
        evidence.append(
            {
                "evidence_id": "ev-local-"
                + hashlib.sha256(f"{item['chunk_id']}:{excerpt}".encode()).hexdigest()[
                    :20
                ],
                "claim": (str(item.get("heading") or excerpt).strip()[:600]),
                "supporting_excerpt": excerpt,
                "document_id": item["document_id"],
                "chunk_id": item["chunk_id"],
                "locator": item["locator"],
                "source_url": item["source_uri"],
                "source_uri": item["source_uri"],
                "source_title": item["filename"],
                "source_type": "local_document",
                "source_authority": 0.0,
                "confidence": min(0.95, 0.55 + float(item["score"]) * 8),
                "security_status": "accepted",
            }
        )
    return json.dumps(
        {
            "source_type": "local_document",
            "query": query,
            "results": results,
            "documents": list(documents.values()),
            "evidence": evidence,
        },
        ensure_ascii=False,
    )


class SearchDocumentsInput(BaseModel):
    query: str


async def _call(input, context, progress):
    return ToolResult(output=await _search_documents_call(input.query, context.config))


search_documents = build_tool(
    name="search_documents",
    input_schema=SearchDocumentsInput,
    description=DESCRIPTION,
    call=_call,
    origin=ToolOrigin.LOCAL_DOCUMENT,
    effect=ToolEffect.SENSITIVE_READ,
    retryable=True,
    concurrency_safe=True,
    max_output_chars=50_000,
    prompt=render_prompt,
    is_enabled=_enabled,
)

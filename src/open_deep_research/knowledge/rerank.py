"""Discrete semantic reranking through the LiteLLM text-model route.

Scores are relevance grades, not probabilities (plan §KB-05): 0 unrelated,
1 topical, 2 partially supporting, 3 directly supporting. Failures surface
as :class:`RerankUnavailableError` so search can degrade honestly to fused
results marked "未完成重排" instead of pretending the corpus is empty.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .credentials import (
    KnowledgeBudgetExceeded,
    KnowledgeCredentialError,
    knowledge_service_key,
    rerank_model,
)
from .evidence_projection import source_excerpt
from .execution import SearchExecution

VALID_GRADES = (0, 1, 2, 3)

_SYSTEM_PROMPT = (
    "你是知识库检索重排器。对每个候选片段给出 0-3 的相关性等级："
    "0=无关；1=主题相关但不能支持问题；2=部分支持；3=直接支持。"
    "等级表示相关性，不是事实置信概率。仅输出 JSON。"
)


class RerankUnavailableError(RuntimeError):
    """Raised when reranking cannot run (credential, model or upstream)."""


def _batch_prompt(question: str, candidates: list[dict[str, Any]]) -> str:
    lines = [
        f"问题：{question}",
        "候选片段（id | 文本）：",
    ]
    for index, candidate in enumerate(candidates, 1):
        text = source_excerpt(re.sub(r"\s+", " ", str(candidate.get("text") or "")), 1200, question)
        lines.append(f"{index} | {candidate.get('id')} | {text}")
    lines.append(
        '输出格式：{"scores": [{"id": "<片段id>", "score": <0-3>, "reason": "<短语>"}]}'
    )
    return "\n".join(lines)


def parse_rerank_response(
    content: str, candidates: list[dict[str, Any]]
) -> dict[str, tuple[int, str | None]]:
    """Map the model's JSON answer onto candidate ids, ignoring unknowns."""
    try:
        payload = json.loads(content)
    except ValueError:
        raise RerankUnavailableError("rerank_invalid_response") from None
    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, list):
        raise RerankUnavailableError("rerank_invalid_response")
    known = {str(candidate.get("id")) for candidate in candidates}
    mapped: dict[str, tuple[int, str | None]] = {}
    for item in scores:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        if identifier not in known:
            continue
        try:
            grade = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        if grade not in VALID_GRADES:
            continue
        mapped[identifier] = (grade, str(item.get("reason") or "") or None)
    if not mapped:
        raise RerankUnavailableError("rerank_no_valid_scores")
    return mapped


async def rerank_segments(
    question: str, candidates: list[dict[str, Any]], *, execution: SearchExecution | None = None
) -> dict[str, tuple[int, str | None]]:
    """Grade candidates in one structured batch call."""
    if execution is not None and execution.scope == "run":
        if execution.models is None:
            raise RerankUnavailableError("research_knowledge_model_port_missing")
        from open_deep_research.agentscope_runtime.research_models import ResearchModels
        from open_deep_research.agentscope_runtime.service_models import policy_text

        if isinstance(execution.models, ResearchModels):
            content = await execution.models.text("knowledge_rerank", _SYSTEM_PROMPT + "\n" + _batch_prompt(question, candidates), {"task_id": execution.task_id})
        else:
            content = await policy_text(execution.models, "knowledge_rerank", system=_SYSTEM_PROMPT,
                                        prompt=_batch_prompt(question, candidates))
        return parse_rerank_response(content, candidates)
    model = rerank_model()
    if not model:
        raise RerankUnavailableError("rerank_model_unconfigured")
    if not candidates:
        return {}
    try:
        service_key = knowledge_service_key()
    except KnowledgeCredentialError as exc:
        raise RerankUnavailableError(str(exc)) from exc
    base_url = (os.getenv("LITELLM_BASE_URL") or "http://litellm-proxy:4000/v1").rstrip("/")
    try:
        from open_deep_research.agentscope_runtime.service_models import service_text

        content = await service_text(model=model, api_key=service_key, base_url=base_url,
            system=_SYSTEM_PROMPT, prompt=_batch_prompt(question, candidates), timeout=60, operation="rerank")
    except KnowledgeBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - upstream failures degrade, never crash
        raise RerankUnavailableError(f"rerank_upstream_unavailable:{type(exc).__name__}") from exc
    return parse_rerank_response(content, candidates)

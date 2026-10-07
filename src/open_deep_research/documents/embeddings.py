"""LiteLLM-routed embeddings with strict dimension validation."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from collections.abc import Sequence
from threading import RLock

from openai import AsyncOpenAI

from open_deep_research.configuration import Configuration
from open_deep_research.models.credentials_context import (
    current_gateway_key,
    current_run_key,
)
from open_deep_research.observability.telemetry import get_prometheus_metrics

from .settings import DocumentSettings


class EmbeddingError(RuntimeError):
    """Raised when the configured embedding route violates its contract."""


_EmbeddingClientKey = tuple[str, str]
_embedding_clients: OrderedDict[_EmbeddingClientKey, AsyncOpenAI] = OrderedDict()
_embedding_clients_lock = RLock()
_MAX_EMBEDDING_CLIENTS = 32


def _get_embedding_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """Return a process-level client while isolating clients by credential."""
    key: _EmbeddingClientKey = (base_url, api_key)
    evicted: list[AsyncOpenAI] = []
    with _embedding_clients_lock:
        client = _embedding_clients.pop(key, None)
        if client is None:
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                timeout=120,
            )
        _embedding_clients[key] = client
        while len(_embedding_clients) > _MAX_EMBEDDING_CLIENTS:
            _, old = _embedding_clients.popitem(last=False)
            evicted.append(old)
    if evicted:
        # Eviction only removes the client from future lookups. Closing is
        # scheduled outside the lock so an in-flight request can finish using
        # its existing client; the bounded pool makes this overlap transient.
        loop = asyncio.get_running_loop()
        for old in evicted:
            loop.create_task(old.close())
    return client


async def close_embedding_clients() -> None:
    """Close all pooled clients during process shutdown or test teardown."""
    with _embedding_clients_lock:
        clients = list(_embedding_clients.values())
        _embedding_clients.clear()
    if clients:
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


async def embed_texts(
    texts: Sequence[str],
    settings: DocumentSettings,
    *,
    api_key: str | None = None,
    operation: str = "ingest",
) -> list[list[float]]:
    """Embed texts through the configured LiteLLM alias in bounded batches."""
    if not texts:
        return []
    base_url = (os.getenv("LITELLM_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise EmbeddingError("document_embedding_gateway_unconfigured")
    query_operation = operation != "ingest"
    key = api_key
    if key is None:
        try:
            key = (
                current_run_key()
                if query_operation
                else current_gateway_key()
            )
        except RuntimeError as exc:
            error_code = (
                "document_query_run_key_unavailable"
                if query_operation
                else "document_embedding_key_unavailable"
            )
            raise EmbeddingError(error_code) from exc
    client = _get_embedding_client(base_url, key)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), settings.embedding_batch_size):
        batch = list(texts[start : start + settings.embedding_batch_size])
        from open_deep_research.knowledge.accounting import (
            reserve_attempt,
            settle_attempt,
        )

        attempt = await reserve_attempt("embedding", settings.embedding_model) if operation == "knowledge_query" else None
        try:
            response = await client.embeddings.create(model=settings.embedding_model, input=batch)
        except BaseException as error:
            await settle_attempt(attempt, error=error)
            raise
        usage = getattr(response, "usage", None)
        await settle_attempt(attempt, usage={"input_tokens": usage.prompt_tokens, "output_tokens": 0}
                             if usage is not None else None)
        ordered = sorted(response.data, key=lambda item: item.index)
        for item in ordered:
            vector = list(item.embedding)
            if len(vector) != settings.embedding_dimensions:
                raise EmbeddingError(
                    f"document_embedding_dimension_mismatch:{len(vector)}:{settings.embedding_dimensions}"
                )
            vectors.append(vector)
    if len(vectors) != len(texts):
        raise EmbeddingError("document_embedding_result_count_mismatch")
    metrics = get_prometheus_metrics(Configuration.from_runnable_config(None))
    if metrics is not None:
        metrics.observe_document_embedding(operation, len(texts))
    return vectors


def vector_literal(vector: Sequence[float]) -> str:
    """Serialize a validated float vector for PostgreSQL's vector input type."""
    return "[" + ",".join(f"{float(value):.9g}" for value in vector) + "]"

"""Service provisioning must authorize the configured consumers and their budget."""

import json

import httpx
import pytest

from open_deep_research.models import mint_service_key
from open_deep_research.models.credentials import LiteLLMKeyAdminClient


@pytest.fixture
def provisioned_key(monkeypatch):
    for name in (
        "EVALUATION_MODEL", "QUALITY_EVALUATION_MODEL", "DOCUMENT_EMBEDDING_MODEL",
        "DOCUMENT_METADATA_SUGGESTION_MODEL", "KNOWLEDGE_ANSWER_MODEL",
        "KNOWLEDGE_RERANK_MODEL", "LITELLM_SERVICE_BUDGET_MICRO_USD",
        "LITELLM_SERVICE_BUDGET_DURATION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://proxy.example/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-admin-placeholder")
    captured = {}

    def handler(request):
        assert request.url.path == "/key/generate"
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"key": "test-service-placeholder"})

    def client_factory(settings):
        client = LiteLLMKeyAdminClient(
            settings,
            client=httpx.AsyncClient(
                base_url="http://proxy.example",
                transport=httpx.MockTransport(handler),
            ),
        )
        client._owns_client = True
        return client

    monkeypatch.setattr(mint_service_key, "LiteLLMKeyAdminClient", client_factory)
    return captured


@pytest.mark.asyncio
async def test_default_key_covers_configured_knowledge_and_evaluation(monkeypatch, provisioned_key):
    for name, value in {
        "EVALUATION_MODEL": "if-evaluation-v1",
        "DOCUMENT_EMBEDDING_MODEL": "if-embedding-v1",
        "DOCUMENT_METADATA_SUGGESTION_MODEL": "if-summarization-v1",
        "KNOWLEDGE_ANSWER_MODEL": "if-summarization-v1",
        "KNOWLEDGE_RERANK_MODEL": "if-web-rerank-v1",
    }.items():
        monkeypatch.setenv(name, value)

    assert await mint_service_key.mint([]) == "test-service-placeholder"
    assert provisioned_key["models"] == [
        "if-embedding-v1", "if-evaluation-v1", "if-summarization-v1", "if-web-rerank-v1",
    ]
    assert provisioned_key["max_budget"] == 20.0
    assert provisioned_key["budget_duration"] == "30d"


@pytest.mark.asyncio
async def test_default_key_tracks_custom_aliases_and_budget(monkeypatch, provisioned_key):
    for name, value in {
        "QUALITY_EVALUATION_MODEL": "custom-judge",
        "DOCUMENT_EMBEDDING_MODEL": "custom-embedding",
        "DOCUMENT_METADATA_SUGGESTION_MODEL": "custom-summary",
        "KNOWLEDGE_ANSWER_MODEL": "custom-answer",
        "KNOWLEDGE_RERANK_MODEL": "custom-rerank",
        "LITELLM_SERVICE_BUDGET_MICRO_USD": "3000000",
        "LITELLM_SERVICE_BUDGET_DURATION": "7d",
    }.items():
        monkeypatch.setenv(name, value)

    await mint_service_key.mint([])
    assert provisioned_key["models"] == [
        "custom-answer", "custom-embedding", "custom-judge", "custom-rerank", "custom-summary",
    ]
    assert provisioned_key["max_budget"] == 3.0
    assert provisioned_key["budget_duration"] == "7d"


@pytest.mark.asyncio
async def test_explicit_models_keep_evaluation_only_key_restricted(monkeypatch, provisioned_key):
    monkeypatch.setenv("KNOWLEDGE_ANSWER_MODEL", "custom-answer")
    await mint_service_key.mint([
        "--model", "dedicated-judge", "--budget-micro-usd", "1000000",
        "--budget-duration", "1d",
    ])
    assert provisioned_key["models"] == ["dedicated-judge"]
    assert provisioned_key["max_budget"] == 1.0
    assert provisioned_key["budget_duration"] == "1d"


@pytest.mark.asyncio
async def test_unconfigured_optional_models_do_not_grant_extra_access(provisioned_key):
    await mint_service_key.mint([])
    assert provisioned_key["models"] == ["if-embedding-v1", "if-evaluation-v1"]

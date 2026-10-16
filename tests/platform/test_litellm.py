"""真实 LiteLLM 控制接口验证；上游固定为不可用的本机端口，不调用模型。"""

import os
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest

from open_deep_research.platform.components import _litellm_apply, model_aliases

URL = os.getenv("PLATFORM_TEST_PROXY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="需要专用 PLATFORM_TEST_PROXY_URL")


@pytest.mark.asyncio
async def test_versioned_model_deployment_is_idempotent_and_catalog_is_verified(monkeypatch, tmp_path):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_BASE_URL", URL + "/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-platform-test-master")
    monkeypatch.setenv("PLATFORM_CONFIG_DIR", str(tmp_path))
    name = "platform-contract-" + uuid4().hex[:12]
    document = {"litellm": {"model_list": [{"model_name": name,
        "litellm_params": {"model": "openai/test-only-model", "api_base": "http://127.0.0.1:9/v1", "api_key": "credential:fixture"},
        "model_info": {"max_input_tokens": 4096, "max_output_tokens": 128, "input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002}}]}}
    async def secret(_identity, _owner):
        return "test-only-provider-placeholder"
    row = {"id": str(uuid4()), "owner_id": None, "fingerprint": "fixture", "document": document}
    store = SimpleNamespace(secret=secret)
    deployed = model_aliases(document)[name]
    try:
        state, detail = await _litellm_apply(store, row, restart=False)
        assert state == "applied" and detail["models"] == [deployed]
        state, _ = await _litellm_apply(store, row, restart=False)
        assert state == "applied"
        state, _ = await _litellm_apply(store, row, restart=True)
        assert state == "pending"
    finally:
        async with httpx.AsyncClient(base_url=URL, headers={"Authorization": "Bearer sk-platform-test-master"}, timeout=20) as client:
            response = await client.get("/model/info")
            for item in response.json().get("data", []):
                if item.get("model_name") == deployed:
                    removed = await client.post("/model/delete", json={"id": item["model_info"]["id"]})
                    removed.raise_for_status()

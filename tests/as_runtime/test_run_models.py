"""T018/T019 原生配置与模型凭据契约验证（不访问模型网络）。"""

import csv
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from agentscope.model import ChatModelBase, OpenAIChatModel
from pydantic import SecretStr

from open_deep_research.agentscope_runtime.models import (
    ROLES,
    CredentialBinding,
    ModelFactory,
    bind_role,
)
from open_deep_research.agentscope_runtime.run_config import FIELD_MAP, RunConfig
from open_deep_research.configuration import Configuration, freeze_run_config


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            monkeypatch.delenv(key, raising=False)
        if (
            key in {name.upper() for name in Configuration.model_fields}
            or key.endswith("_API_KEY")
            or key.endswith("_BASE_URL")
            or key
            in {"GET_API_KEYS_FROM_CONFIG", "MEM0_PROVIDER", "MEM0_MEMORY_PROJECT_ID"}
        ):
            monkeypatch.delenv(key, raising=False)


def test_all_244_fields_mapped_and_equivalent():
    run = RunConfig.compile()
    assert len(FIELD_MAP) == 244
    rows = list(
        csv.DictReader(
            Path("docs/agentscope-migration/evidence/configuration-map.csv").open(
                encoding="utf-8-sig"
            )
        )
    )
    assert (
        set(FIELD_MAP)
        == {row["field"] for row in rows}
        == set(Configuration.model_fields)
    )
    values = {
        key: value
        for group in {item["group"] for item in FIELD_MAP.values()}
        for key, value in run.group(group).items()
    }
    old = Configuration.from_runnable_config(freeze_run_config({})).model_dump(
        mode="json"
    )
    assert values == old
    for row in rows:
        assert FIELD_MAP[row["field"]]["handling"] == row["handling"]


def test_priority_and_frozen_restore(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL", "openai:environment")
    run = RunConfig.compile({"configurable": {"research_model": "openai:request"}})
    assert run.get("research_model") == "openai:environment"
    monkeypatch.setenv("RESEARCH_MODEL", "openai:changed")
    assert (
        RunConfig.restore(run.snapshot()).get("research_model") == "openai:environment"
    )
    with pytest.raises(ValueError, match="conflict"):
        RunConfig.restore(run.snapshot(), overrides={"research_model": "openai:other"})


def test_snapshot_no_credentials_and_copies_independent():
    run = RunConfig.compile(
        {
            "configurable": {
                "apiKeys": {"OPENAI_API_KEY": "fixture-secret"},
                "langfuse_secret_key": "fixture-secret",
            },
            "metadata": {"authorization": "fixture-secret"},
        }
    )
    assert "fixture-secret" not in json.dumps(run.snapshot()) + repr(run)
    copy = run.snapshot()
    copy["contract"]["configurable"]["research_model"] = "tampered"
    with pytest.raises(ValueError, match="fingerprint"):
        RunConfig.restore(copy)
    assert run.snapshot() != copy


@pytest.mark.parametrize(
    "mutation", ["engine", "schema", "fingerprint", "version", "frozen"]
)
def test_restore_rejects_invalid_contract(mutation):
    data = RunConfig.compile().snapshot()
    if mutation in {"engine", "schema"}:
        data[mutation] = "legacy"
    elif mutation == "fingerprint":
        data["contract"]["metadata"].pop("run_config_fingerprint")
    elif mutation == "version":
        data["contract"]["metadata"]["run_config_schema_version"] = 999
    else:
        data["contract"]["metadata"]["runtime_config_frozen"] = False
    with pytest.raises(ValueError):
        RunConfig.restore(data)


@pytest.mark.parametrize("role", list(ROLES))
@pytest.mark.asyncio
async def test_native_role_models_and_secret_free_descriptors(role):
    run = RunConfig.compile(
        {
            "configurable": {
                field: "openai:fixture-model" for field, _, _ in ROLES.values()
            }
        }
    )
    binding = CredentialBinding(
        "ref-role",
        "run",
        "run-1",
        ("openai:fixture-model",),
        SecretStr("fixture-secret"),
        "https://example.invalid/v1",
    )
    factory = ModelFactory(run, scope="run", owner="run-1", bindings={role: binding})
    try:
        model = factory.build(role)
        assert isinstance(model, OpenAIChatModel)
        assert model.max_retries == 0 and model.client.max_retries == 0
        assert factory.agent_model_config().max_retries == 0
        assert factory.build(role) is model
        assert model.credential.api_key.get_secret_value() == "fixture-secret"
        assert "fixture-secret" not in json.dumps(factory.descriptor(role)) + repr(
            binding
        ) + json.dumps(run.snapshot())
    finally:
        await factory.aclose()
    assert not factory._models


def test_role_key_and_config_isolation(monkeypatch):
    run = RunConfig.compile({"configurable": {"research_model": "openai:fixture"}})
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("RESEARCHER_API_KEY", "role-key")
    kwargs = dict(reference="ref", scope="run", owner="run-1")
    assert bind_role(run, "researcher", **kwargs).key.get_secret_value() == "role-key"
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    with pytest.raises(ValueError, match="missing credential"):
        bind_role(run, "researcher", **kwargs)
    binding = bind_role(
        run,
        "researcher",
        source={"configurable": {"apiKeys": {"OPENAI_API_KEY": "config-key"}}},
        **kwargs,
    )
    assert binding.key.get_secret_value() == "config-key"


@pytest.mark.parametrize(
    "field,value",
    [("owner", "another-run"), ("scope", "service"), ("allowed_models", ("other",))],
)
def test_binding_scope_and_allowlist_rejected(field, value):
    run = RunConfig.compile()
    binding = CredentialBinding(
        "ref", "run", "run-1", (run.get("research_model"),), SecretStr("fixture")
    )
    factory = ModelFactory(
        run,
        scope="run",
        owner="run-1",
        bindings={"researcher": replace(binding, **{field: value})},
    )
    with pytest.raises(ValueError):
        factory.build("researcher")


@pytest.mark.asyncio
async def test_frozen_catalog_gateway_binding_and_limits():
    spec = "openai:fixture"
    catalog = {
        spec: {
            "model_name": spec,
            "context_window": 4096,
            "max_output_tokens": 512,
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
            "api_key": "untrusted-secret",
        }
    }
    run = RunConfig.compile(
        {
            "configurable": {
                "model_backend": "litellm",
                "research_model": spec,
                "model_catalog_snapshot": catalog,
            }
        }
    )
    assert "untrusted-secret" not in json.dumps(run.snapshot())
    binding = CredentialBinding(
        "run-key-ref",
        "run",
        "run-1",
        (spec,),
        SecretStr("fixture"),
        "https://example.invalid/v1",
        gateway=True,
    )
    factory = ModelFactory(
        run, scope="run", owner="run-1", bindings={"researcher": binding}
    )
    try:
        model = factory.build("researcher")
        assert model.model == spec and model.context_size == 4096
        assert model.parameters.max_tokens == 512
    finally:
        await factory.aclose()
    catalog[spec]["context_window"] = 100
    assert (
        RunConfig.restore(run.snapshot()).get("model_catalog_snapshot")[spec][
            "context_window"
        ]
        == 4096
    )
    factory = ModelFactory(
        run,
        scope="run",
        owner="run-1",
        bindings={"researcher": replace(binding, gateway=False)},
    )
    with pytest.raises(ValueError, match="gateway binding"):
        factory.build("researcher")


def test_no_langchain_imported_by_native_config_and_factory():
    code = 'import sys; from open_deep_research.agentscope_runtime.run_config import RunConfig; from open_deep_research.agentscope_runtime.models import ModelFactory; RunConfig.compile(); assert not any(k.startswith("langchain") for k in sys.modules)'
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("provider", ["anthropic", "google_genai", "deepseek"])
@pytest.mark.asyncio
async def test_native_provider_constructs_and_closes(provider):
    spec = f"{provider}:fixture-model"
    run = RunConfig.compile({"configurable": {"research_model": spec}})
    binding = CredentialBinding(
        "ref", "service", "report-worker", (spec,), SecretStr("fixture")
    )
    factory = ModelFactory(
        run, scope="service", owner="report-worker", bindings={"researcher": binding}
    )
    try:
        model = factory.build("researcher")
        assert isinstance(model, ChatModelBase)
        assert model.max_retries == 0
        if provider == "google_genai":
            # 检查安装 SDK 的实际选项与连接池，防止仅验证工厂传参造成假通过。
            transport = model.client._api_client
            assert transport._http_options.retry_options.attempts == 1
            assert not transport._httpx_client.is_closed
            assert not transport._async_httpx_client.is_closed
    finally:
        await factory.aclose()
    if provider == "google_genai":
        assert transport._httpx_client.is_closed
        assert transport._async_httpx_client.is_closed


@pytest.mark.parametrize("role", list(ROLES)[:7])
def test_each_core_role_credential_override(role, monkeypatch):
    run = RunConfig.compile(
        {"configurable": {field: "openai:fixture" for field, _, _ in ROLES.values()}}
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fallback-key")
    monkeypatch.setenv(role.upper() + "_API_KEY", "role-key")
    binding = bind_role(run, role, reference="ref", scope="service", owner="worker")
    assert binding.key.get_secret_value() == "role-key"


@pytest.mark.asyncio
async def test_runtime_owns_model_factory_close():
    from open_deep_research.agentscope_runtime.app import ASRuntime
    from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings

    runtime = ASRuntime(ASRuntimeSettings(None, "unused", True, "test_"), None, None)
    factory = runtime.create_model_factory(
        RunConfig.compile(), scope="run", owner="run-1", bindings={}
    )
    # 实际工厂关闭钩子已注册；独立验证关闭栈消费一次。
    await runtime.aclose()
    assert "models:run:run-1" in runtime.shutdown_stack.order
    with pytest.raises(RuntimeError, match="shutting_down"):
        runtime.create_model_factory(
            RunConfig.compile(), scope="run", owner="run-1", bindings={}
        )


def test_mem0_alias_priority_preserved(monkeypatch):
    monkeypatch.setenv("MEM0_MEMORY_PROJECT_ID", "env-project")
    assert RunConfig.compile().get("memory_project_id") == "env-project"
    assert (
        RunConfig.compile(
            {"configurable": {"memory_project_id": "request-project"}}
        ).get("memory_project_id")
        == "request-project"
    )


def test_secret_in_frozen_endpoint_is_rejected():
    with pytest.raises(ValueError, match="endpoint"):
        RunConfig.compile(
            {
                "configurable": {
                    "quality_evaluation_base_url": "https://user:secret@example.invalid/v1"
                }
            }
        )


def test_legacy_catalog_environment_shape_is_rejected(monkeypatch):
    monkeypatch.setenv(
        "MODEL_CATALOG_SNAPSHOT",
        json.dumps(
            {
                "openai:test": {
                    "model_name": "openai:test",
                    "context_window": 4096,
                    "max_output_tokens": 512,
                    "input_cost_per_token": 0.1,
                    "output_cost_per_token": 0.2,
                    "api_key": "fixture-secret",
                }
            }
        ),
    )
    # 旧 Configuration 未将该环境变量 JSON 解码；保持原有拒绝行为。
    with pytest.raises(ValueError):
        RunConfig.compile()


def test_auxiliary_model_fallbacks_match_old_roles():
    run = RunConfig.compile(
        {
            "configurable": {
                "quality_evaluation_model": "openai:judge",
                "final_report_model": "openai:writer",
            }
        }
    )
    binding = CredentialBinding(
        "ref", "run", "run-1", ("openai:judge",), SecretStr("fixture")
    )
    for role in ("report_review", "egress_classifier"):
        factory = ModelFactory(
            run, scope="run", owner="run-1", bindings={role: binding}
        )
        assert factory.descriptor(role)["model"] == "openai:judge"
    writer_binding = CredentialBinding(
        "writer", "run", "run-1", ("openai:writer",), SecretStr("fixture")
    )
    factory = ModelFactory(
        run, scope="run", owner="run-1", bindings={"report_revisor": writer_binding}
    )
    assert factory.descriptor("report_revisor")["model"] == "openai:writer"

"""现有配置的完整目录，以及部署配置和秘密的边界。"""

import json
import re
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

BOOTSTRAP_NAMES = frozenset({
    "PATH", "PYTHONPATH", "HOME", "USERPROFILE", "TMP", "TEMP", "SYSTEMROOT", "COMSPEC",
    "SANDBOX_API_INTERNAL_URL", "SANDBOX_GATEWAY_PORT", "SANDBOX_GATEWAY_PROXY_PORT",
    "SANDBOX_TASK_PAYLOAD_PATH", "SANDBOX_RESULT_PATH", "SANDBOX_LOG_PATH", "SANDBOX_POLICY_PATH",
    "SANDBOX_GATEWAY_PHYSICAL_PROCESS", "SANDBOX_GATEWAY_FAKE_PROVIDER", "SANDBOX_GATEWAY_CONTAINER",
    "SANDBOX_TASK_TOKEN", "SANDBOX_CONTROL_SUBNET",
    "TRACE_STORE_PATH", "DOCUMENT_SOFFICE_PATH", "MEM0_CONFIG_PATH",
    "APP_ENV", "RESEARCH_ENGINE", "AS_NATIVE_RESOURCES", "AS_DATABASE_URL", "AS_DATABASE_SCHEMA",
    "AS_STORAGE_AUTO_CREATE", "AS_BUS_TABLE_PREFIX", "AS_RECOVERY_DATABASE_URL",
    "IAM_DATABASE_URL", "DOCUMENT_DATABASE_URL", "COORDINATION_DATABASE_URL", "DATABASE_URL",
    "RUNS_DIR", "DOCUMENT_STORAGE_DIR", "SANDBOX_CONTROLLER_SOCKET", "SANDBOX_GATEWAY_URL",
    "SANDBOX_ROOT_SIGNING_KEY", "SANDBOX_CONTROLLER_ID", "SANDBOX_RUNTIME_ROOT",
    "LITELLM_BASE_URL", "LITELLM_MASTER_KEY", "LITELLM_DATABASE_URL", "LITELLM_DATABASE_PASSWORD",
    "LITELLM_RUN_KEY_ENCRYPTION_KEY", "LITELLM_REDIS_HOST", "LITELLM_REDIS_PORT",
    "PLATFORM_SECRET_ENCRYPTION_KEY", "PLATFORM_SETUP_TOKEN", "PLATFORM_CONFIG_DIR",
    "PLATFORM_CONFIGURATION_MODE", "LOCAL_DEV_AUTH_BYPASS", "NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS",
    "FRONTEND_ALLOWED_ORIGINS", "RESEARCH_API_ORIGIN", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "IAM_JWT_ACCESS_SIGNING_KEY", "IAM_JWT_REFRESH_SIGNING_KEY", "IAM_JWT_ACCESS_KID",
    "IAM_JWT_REFRESH_KID", "IAM_JWT_ACCESS_VERIFY_KEYS", "IAM_JWT_REFRESH_VERIFY_KEYS",
    "IAM_TOKEN_DIGEST_SECRET", "IAM_BOOTSTRAP_ADMIN_EMAIL", "IAM_BOOTSTRAP_ADMIN_PASSWORD",
    "AS_TEAM_WORKER_ENV_FILE", "AS_TEAM_WORKER_NETWORK", "AS_TEAM_WORKER_MOUNTS",
    "AS_SANDBOX_NETWORK", "AS_SANDBOX_MOUNTS", "PROMETHEUS_METRICS_PATH",
    "DOCUMENT_WORKER_METRICS_PORT", "KNOWLEDGE_WORKER_METRICS_PORT", "OBSERVABILITY_DB_PATH",
})
GROUP_NAMES = {"models": "模型", "budgets": "预算", "context": "上下文", "coordination": "团队协作",
    "hitl": "人工审批", "integrations": "外部接入", "knowledge": "知识库", "memory": "记忆",
    "observability": "观测", "quality": "质量", "report": "报告", "research": "研究",
    "runtime": "运行", "sandbox": "沙箱", "tools": "工具", "web": "网页证据"}
DERIVED_RUNTIME_FIELDS = frozenset({"model_catalog_snapshot", "sandbox_policy_digest", "sandbox_runtime_digest", "gateway_protocol_version"})


def is_bootstrap(name: str) -> bool:
    return name in BOOTSTRAP_NAMES or name.startswith(("IAM_JWT_", "ROCKETMQ_", "AS_ROCKETMQ_", "AS_TEAM_", "AS_SANDBOX_", "OTEL_EXPORTER_", "KNOWLEDGE_BACKUP_", "KNOWLEDGE_RESTORE_")) or name.endswith(("_DATABASE_URL", "_STORAGE_DIR", "_SIGNING_KEY", "_METRICS_PORT", "_HOST_PORT", "_CALLBACK_PORT"))


def is_secret(name: str) -> bool:
    return bool(re.search(r"(?:API_KEYS?|SECRET(?:_KEY)?|PASSWORD|ACCESS_KEY|PRIVATE_KEY|MASTER_KEY|SALT_KEY|SIGNING_KEY|ACCESS_TOKEN|SERVICE_KEY|AUTH_TOKEN|BEARER_TOKEN|API_TOKEN|CREDENTIALS)$", name.upper()))


@lru_cache(maxsize=1)
def environment_catalog() -> dict[str, dict[str, Any]]:
    packaged = Path(__file__).with_name("environment_fields.json")
    if packaged.exists():
        result = json.loads(packaged.read_text(encoding="utf-8"))
        return {name: {**entry, "bootstrap": is_bootstrap(name), "secret": is_secret(name)} for name, entry in result.items()}
    # Used only when regenerating the bundled directory from checked-in examples.
    root = Path(__file__).resolve().parents[3]
    result, comments = {}, []
    for line in (root / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("#"):
            comments.append(line.lstrip("# "))
        elif match := re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line):
            name = match[1]
            result[name] = {"description": " ".join(comments[-5:]), "bootstrap": is_bootstrap(name), "secret": is_secret(name)}
            comments = []
        elif not line.strip():
            comments = []
    return result


def runtime_catalog() -> dict[str, dict[str, Any]]:
    from open_deep_research.configuration import Configuration
    root = Path(__file__).resolve().parents[1]
    mapping = json.loads((root / "agentscope_runtime" / "config_fields.json").read_text(encoding="utf-8"))
    schema = Configuration.model_json_schema()
    env = environment_catalog()
    result = {}
    for name, field in Configuration.model_fields.items():
        group = mapping[name]["group"]
        entry = env.get(name.upper(), {})
        bootstrap, secret = is_bootstrap(name.upper()), is_secret(name.upper())
        unit = "秒" if name.endswith("_seconds") else "字节" if name.endswith("_bytes") else "Token" if "tokens" in name else "微美元" if "micro_usd" in name else ""
        result[name] = {"name": name, "group": group, "group_label": GROUP_NAMES[group],
            "label": entry.get("description", "").split("；")[0][:90] or f"{GROUP_NAMES[group]}配置 · {name}",
            "description": entry.get("description") or f"设置{GROUP_NAMES[group]}行为。{field.description or ''}",
            "unit": unit, "bootstrap": bootstrap, "secret": secret,
            "derived": name in DERIVED_RUNTIME_FIELDS,
            "activation": "restart" if group in {"integrations", "sandbox"} or name.endswith(("_enabled", "_provider")) else "new_task",
            "schema": schema["properties"][name]}
    return result


def managed_runtime_keys() -> set[str]:
    from open_deep_research.configuration import Configuration
    return {name for name in Configuration.model_fields if not is_bootstrap(name.upper()) and not is_secret(name) and name not in DERIVED_RUNTIME_FIELDS}


def normalize_document(document, *, scope):
    """草稿即保存完整默认值，让差异预览与最终发布内容一致。"""
    cfg = validate_document(document)
    if scope == "global":
        from open_deep_research.configuration import Configuration
        from pydantic import TypeAdapter
        sandbox_enabled = TypeAdapter(bool).validate_python(document.runtime.get("sandbox_enabled", False))
        document.runtime = {key: value for key, value in cfg.model_dump(mode="json").items() if key in managed_runtime_keys()}
        document.runtime["sandbox_enabled"] = sandbox_enabled
        defaults = {key: entry["default"] for key, entry in environment_catalog().items()
                    if "default" in entry and not entry["bootstrap"] and not entry["secret"] and key.lower() not in Configuration.model_fields}
        document.services = {**defaults, **document.services}
    return document


def validate_document(document, *, publishing: bool = False):
    json.dumps(document.model_dump(mode="json"), allow_nan=False)
    from open_deep_research.configuration import Configuration
    from open_deep_research.sandbox.schema import SandboxPolicyBundle
    allowed = managed_runtime_keys()
    invalid = set(document.runtime) - allowed
    if invalid:
        raise ValueError("不能由业务配置修改的字段：" + ", ".join(sorted(invalid)))
    for name, value in document.services.items():
        if name not in environment_catalog() or is_bootstrap(name) or is_secret(name):
            raise ValueError(f"服务配置字段不允许：{name}")
        if name.lower() in Configuration.model_fields:
            raise ValueError(f"请在研究参数中维护此字段：{name.lower()}")
        kind = environment_catalog()[name].get("type")
        if value is not None and value != "" and kind:
            if kind == "boolean":
                if str(value).lower() not in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
                    raise ValueError(f"{name} 必须是布尔值")
                document.services[name] = str(value).lower() in {"true", "1", "on", "yes"}
            else:
                number = float(value)
                if isinstance(value, bool) or not math.isfinite(number) or number < 0 or kind == "integer" and not number.is_integer():
                    raise ValueError(f"{name} 必须是非负{'整数' if kind == 'integer' else '数值'}")
                document.services[name] = int(number) if kind == "integer" else number
    for name in document.credential_refs:
        if not is_secret(name) or is_bootstrap(name):
            raise ValueError(f"凭据绑定字段不允许：{name}")
    if set(document.policy) - allowed:
        raise ValueError("字段策略包含未知或部署字段")
    if any(rule.editable for key, rule in document.policy.items() if key in {"max_concurrent_runs_per_user", "api_run_create_per_minute", "max_concurrent_sse_connections"}):
        raise ValueError("准入限制必须通过平台策略或用户配额管理")
    # Secret and deployment prerequisites are checked when components apply the revision.
    values = dict(document.runtime)
    from pydantic import TypeAdapter
    sandbox_enabled = TypeAdapter(bool).validate_python(values.pop("sandbox_enabled", False))
    cfg = Configuration.model_validate({**values, "sandbox_enabled": False})
    if document.sandbox is not None:
        SandboxPolicyBundle.model_validate(document.sandbox)
    if document.litellm.get("model_list"):
        from urllib.parse import urlsplit
        from open_deep_research.models.catalog import parse_model_info, validate_model_catalog, ModelCatalogError
        names = []
        for entry in document.litellm["model_list"]:
            name = entry.get("model_name")
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("模型别名必须非空且不可重复")
            names.append(name)
            endpoint = entry.get("litellm_params", {}).get("api_base")
            if endpoint:
                if isinstance(endpoint, str) and endpoint.startswith("os.environ/") and not publishing:
                    continue
                url = urlsplit(endpoint)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError("模型端点必须是无凭据和查询参数的 HTTP(S) 地址")
        try:
            validate_model_catalog(parse_model_info(document.litellm), names, budget_enabled=False)
        except ModelCatalogError as exc:
            raise ValueError(str(exc)) from exc
    if publishing and sandbox_enabled and document.sandbox is None:
        raise ValueError("启用沙箱前必须配置沙箱策略")
    if publishing and cfg.enable_memory and cfg.memory_provider == "oss" and not document.memory:
        raise ValueError("启用 OSS 记忆前必须填写数据库中的记忆后端配置")
    if publishing and document.quotas.monthly_cost_micro_usd is None:
        raise ValueError("发布前必须设置月度费用配额（微美元，0 表示禁止付费调用）")
    if publishing and document.quotas.max_run_cost_micro_usd is None:
        raise ValueError("发布前必须设置单次研究费用上限（微美元）")
    if publishing and document.quotas.monthly_cost_micro_usd and not document.litellm.get("model_list") and not cfg.model_costs_per_million:
        raise ValueError("启用付费调用前必须配置模型价格")
    from open_deep_research.platform.importing import assert_secret_free
    assert_secret_free(document.model_dump(exclude={"credential_refs"}))
    return cfg

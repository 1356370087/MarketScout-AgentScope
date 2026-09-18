"""原生运行配置：复用既有校验/冻结算法，内部不传播 RunnableConfig。"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from open_deep_research.configuration import (
    Configuration,
    RUN_CONFIG_SCHEMA_VERSION,
    freeze_run_config,
    frozen_run_config_values,
    run_config_fingerprint,
)

FIELD_MAP = json.loads(
    Path(__file__).with_name("config_fields.json").read_text(encoding="utf-8")
)
_METADATA = {
    "runtime_config_frozen",
    "run_config_schema_version",
    "quality_policy_version",
    "quality_rigor_policy",
    "quality_evaluation_epoch",
    "run_config_fingerprint",
    "quality_configuration_warnings",
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    """不可变解析结果；持久化只使用 snapshot，运行值不进入 repr。"""

    _values_json: str = field(repr=False)
    _contract_json: str = field(repr=False)

    @classmethod
    def compile(cls, external: dict[str, Any] | None = None) -> RunConfig:
        external = external or {}
        # 目录只保存已定义的能力/价格字段，不接受管理 API 回传的凭据扩展。
        configurable = dict(external.get("configurable", {}))
        if configurable.get("model_catalog_snapshot"):
            from open_deep_research.models.catalog import ModelCatalogEntry

            configurable["model_catalog_snapshot"] = {
                k: ModelCatalogEntry.model_validate(v).model_dump(mode="json")
                for k, v in configurable["model_catalog_snapshot"].items()
            }
        external = {**external, "configurable": configurable}
        contract = freeze_run_config(external)
        if contract["configurable"].get("model_catalog_snapshot"):
            from open_deep_research.models.catalog import ModelCatalogEntry

            contract["configurable"]["model_catalog_snapshot"] = {
                k: ModelCatalogEntry.model_validate(v).model_dump(mode="json")
                for k, v in contract["configurable"]["model_catalog_snapshot"].items()
            }
            contract["metadata"]["run_config_fingerprint"] = run_config_fingerprint(
                contract
            )
        values = Configuration.from_runnable_config(contract).model_dump(mode="json")
        for key, value in frozen_run_config_values(contract).items():
            if key.endswith("base_url") and value:
                endpoint = urlsplit(value)
                if (
                    endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError(
                        "frozen model endpoint must not contain credentials or query parameters"
                    )
        if set(values) != set(FIELD_MAP):
            raise ValueError("configuration field map is out of date")
        safe = {
            "configurable": frozen_run_config_values(contract),
            "metadata": {
                k: v for k, v in contract["metadata"].items() if k in _METADATA
            },
        }
        return cls(
            json.dumps(values, ensure_ascii=False), json.dumps(safe, ensure_ascii=False)
        )

    def get(self, name: str) -> Any:
        return json.loads(self._values_json)[name]

    def group(self, name: str) -> dict[str, Any]:
        values = json.loads(self._values_json)
        return {k: values[k] for k, spec in FIELD_MAP.items() if spec["group"] == name}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "insightforge.run-config.v1",
            "engine": "agentscope",
            "contract": json.loads(self._contract_json),
        }

    def compatibility_projection(self) -> dict[str, Any]:
        """仅供旧 HTTP/冻结契约边界使用，不携带 apiKeys/身份/回调。"""
        return json.loads(self._contract_json)

    @classmethod
    def restore(
        cls, snapshot: dict[str, Any], *, overrides: dict[str, Any] | None = None
    ) -> RunConfig:
        if (
            snapshot.get("schema") != "insightforge.run-config.v1"
            or snapshot.get("engine") != "agentscope"
        ):
            raise ValueError("unsupported native run configuration schema")
        contract = snapshot["contract"]
        if (
            contract.get("metadata", {}).get("run_config_schema_version")
            not in {13, RUN_CONFIG_SCHEMA_VERSION}
        ):
            raise ValueError("unsupported native frozen contract version")
        if not contract["metadata"].get("run_config_fingerprint"):
            raise ValueError("missing native frozen fingerprint")
        if contract["metadata"].get("runtime_config_frozen") is not True:
            raise ValueError("native configuration must be frozen")
        for key, value in (overrides or {}).items():
            if contract["metadata"]["run_config_schema_version"] == 13 and key in {
                "async_research_mode", "team_execution_mode"
            } and value != {"async_research_mode": "collaborator", "team_execution_mode": "direct"}[key]:
                raise ValueError(f"frozen configuration conflict: {key}")
            if (
                key in contract["configurable"]
                and value != contract["configurable"][key]
            ):
                raise ValueError(f"frozen configuration conflict: {key}")
        return cls.compile(
            {
                "configurable": {"async_research_mode": "collaborator", "team_execution_mode": "direct", **(overrides or {}), **contract["configurable"]},
                "metadata": contract["metadata"],
            }
        )

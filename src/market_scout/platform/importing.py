"""受限格式导入；只解析数据，不执行环境变量插值或文件中命令。"""

import io
import json
import os
import tomllib
from copy import deepcopy
from pathlib import Path
from dotenv import dotenv_values
import yaml

from .catalog import environment_catalog, is_bootstrap, is_secret, managed_runtime_keys
from .contracts import ConfigurationDocument


def credential_ids(value):
    found = set()
    if isinstance(value, dict):
        found.update(str(v).removeprefix("credential:").split("@", 1)[0] for v in value.get("credential_refs", {}).values())
        for child in value.values():
            found.update(credential_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(credential_ids(child))
    elif isinstance(value, str) and value.startswith("credential:"):
        found.add(value.removeprefix("credential:").split("@", 1)[0])
    return found


def credential_references(value):
    if isinstance(value, dict):
        found = {item if item.startswith("credential:") else "credential:" + item
                 for item in value.get("credential_refs", {}).values()}
        for child in value.values():
            found.update(credential_references(child))
        return found
    if isinstance(value, list):
        return set().union(*(credential_references(child) for child in value)) if value else set()
    return {value} if isinstance(value, str) and value.startswith("credential:") else set()


def assert_secret_free(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if (is_secret(key) or key.lower() in {"authorization", "apikey", "apikeys", "api_key", "token"}) and child:
                if not isinstance(child, str) or not child.startswith(("credential:", "os.environ/")):
                    raise ValueError(f"秘密必须通过凭据引用保存：{key}")
            assert_secret_free(child)
    elif isinstance(value, list):
        for child in value:
            assert_secret_free(child)


def _value(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def parse_import(format, content):
    document = ConfigurationDocument().model_dump(mode="json")
    secrets, deployed, unsupported = {}, [], []
    known, runtime = environment_catalog(), managed_runtime_keys()
    if format == "env":
        values = dotenv_values(stream=io.StringIO(content), interpolate=False)
        for name, raw in values.items():
            if is_bootstrap(name):
                deployed.append(name)
            elif is_secret(name):
                if raw:
                    secrets[name] = raw
                    document["credential_refs"][name] = "pending:" + name
            elif name.lower() in runtime:
                if raw not in (None, ""):
                    document["runtime"][name.lower()] = _value(raw)
            elif name in known:
                document["services"][name] = _value(raw)
            else:
                unsupported.append(name)
    elif format == "yaml":
        value = yaml.safe_load(content)
        if not isinstance(value, dict) or not isinstance(value.get("model_list"), list):
            raise ValueError("LiteLLM 配置必须包含 model_list")
        document["litellm"] = value
        # Deployment settings never become editable business configuration.
        for key in ("master_key", "database_url"):
            if key in value.get("general_settings", {}):
                value["general_settings"].pop(key)
                deployed.append("litellm.general_settings." + key)
        def extract(node, path="litellm"):
            if isinstance(node, dict):
                for key, child in list(node.items()):
                    if (is_secret(key) or key == "api_key") and isinstance(child, str) and child:
                        if child.startswith(("credential:", "os.environ/")):
                            continue
                        name = path + "." + key
                        secrets[name] = child
                        node[key] = "pending:" + name
                    else:
                        extract(child, path + "." + key)
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    extract(child, path + "." + str(index))
        extract(value)
    elif format == "toml":
        document["sandbox"] = tomllib.loads(content)
    elif format == "json":
        raw = json.loads(content)
        if "document" in raw:
            raw = raw["document"]
        document = ConfigurationDocument.model_validate(raw).model_dump(mode="json")
        assert_secret_free(document)
    else:
        raise ValueError("unsupported_import_format")
    return document, secrets, {"deployment_fields": deployed, "unsupported_fields": unsupported,
                               "credential_count": len(secrets)}


def server_import():
    root = Path(__file__).resolve().parents[3]
    env_text = "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in os.environ.items()
                         if key in environment_catalog())
    document, secrets, report = parse_import("env", env_text)
    for path, format in ((root / "config/litellm.yaml", "yaml"), (root / "config/sandbox-policy.toml", "toml")):
        if path.exists():
            imported, extra, note = parse_import(format, path.read_text(encoding="utf-8-sig"))
            document["litellm" if format == "yaml" else "sandbox"] = imported["litellm" if format == "yaml" else "sandbox"]
            secrets.update(extra)
            report["deployment_fields"].extend(note["deployment_fields"])
    memory_path = os.environ.get("MEM0_CONFIG_PATH")
    if memory_path:
        document["memory"] = json.loads(Path(memory_path).read_text(encoding="utf-8-sig"))
        def protect(node, path="memory"):
            if isinstance(node, dict):
                for key, value in list(node.items()):
                    if is_secret(key) and isinstance(value, str) and value and not value.startswith(("credential:", "os.environ/")):
                        name = path + "." + key
                        secrets[name] = value
                        node[key] = "pending:" + name
                    else:
                        protect(value, path + "." + key)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    protect(value, path + "." + str(index))
        protect(document["memory"])
    report["credential_count"] = len(secrets)
    return resolve_references(document), secrets, report


def resolve_references(document, *, strict=False):
    """将模型参数中的文件引用固定为数据库字段或凭据引用。"""
    result = deepcopy(document)
    services = result.get("services", {})
    references = result.get("credential_refs", {})
    def memory_references(value):
        if isinstance(value, dict):
            return {key: memory_references(item) for key, item in value.items()}
        if isinstance(value, list):
            return [memory_references(item) for item in value]
        if isinstance(value, str) and value.startswith("os.environ/"):
            name = value.removeprefix("os.environ/")
            if name in references or name in services:
                return references.get(name, services.get(name))
            if strict:
                raise ValueError("请补齐记忆配置引用：" + name)
        return value
    result["memory"] = memory_references(result.get("memory", {}))
    for entry in result.get("litellm", {}).get("model_list", []):
        params = entry.get("litellm_params", {})
        for name, value in list(params.items()):
            if not isinstance(value, str) or not value.startswith("os.environ/"):
                continue
            reference = value.removeprefix("os.environ/")
            replacement = references.get(reference) if is_secret(name) else services.get(reference)
            if replacement is not None:
                params[name] = replacement
            elif strict:
                raise ValueError("请在服务参数或凭据绑定中补齐：" + reference)
    return result


def replace_pending(value, references):
    if isinstance(value, dict):
        return {key: replace_pending(child, references) for key, child in value.items()}
    if isinstance(value, list):
        return [replace_pending(child, references) for child in value]
    if isinstance(value, str) and value.startswith("pending:"):
        return "credential:" + references[value.removeprefix("pending:")]
    return value

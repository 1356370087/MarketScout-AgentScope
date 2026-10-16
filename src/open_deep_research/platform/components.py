"""版本化组件产物及可验证的应用状态。"""

from copy import deepcopy
import json
import base64
import os
from pathlib import Path
from uuid import uuid4
import httpx
from sqlalchemy import select

from . import schema as s
from .store import PlatformError, fingerprint


def config_directory():
    return Path(os.getenv("PLATFORM_CONFIG_DIR", str(Path(os.getenv("RUNS_DIR", ".runs")) / "platform" / "config"))).resolve()


def policy_path(revision):
    return config_directory() / revision / "sandbox-policy.json"


def required_components(document, previous):
    required = {"api": "online"}
    if document.get("litellm", {}).get("model_list"):
        global_settings = lambda d: {k: v for k, v in (d or {}).get("litellm", {}).items() if k != "model_list"}
        required["litellm"] = "restart" if global_settings(document) != global_settings(previous) else "online"
    if document.get("sandbox") and document.get("runtime", {}).get("sandbox_enabled"):
        required["sandbox"] = "restart"
    services, old_services = document.get("services", {}), (previous or {}).get("services", {})
    if services != old_services:
        if services.get("DOCUMENT_RESEARCH_ENABLED") in (True, "true", "1"):
            required.update({"document-worker": "restart", "knowledge-worker": "restart"})
        if services.get("PUBLISHER_ENABLED") in (True, "true", "1"):
            required["publisher-worker"] = "restart"
    return required


def model_aliases(document):
    return {entry["model_name"]: entry["model_name"] + "--" + fingerprint(entry)[:12]
            for entry in document.get("litellm", {}).get("model_list", [])}


def execution_document(document):
    value = deepcopy(document)
    aliases = model_aliases(document)
    def translate(node):
        if isinstance(node, str):
            return aliases.get(node, node)
        if isinstance(node, list):
            return [translate(item) for item in node]
        if isinstance(node, dict):
            return {aliases.get(k, k): translate(v) for k, v in node.items()}
        return node
    value["runtime"] = translate(value["runtime"])
    value["services"] = translate(value["services"])
    return value


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


async def materialize(store, row):
    directory = config_directory() / row["id"]
    document = row["document"]
    _atomic_json(directory / "configuration.json", {"revision_id": row["id"], "fingerprint": row["fingerprint"],
        "document": execution_document(document), "policy_path": f"{row['id']}/sandbox-policy.json"})
    if document.get("sandbox"):
        _atomic_json(policy_path(row["id"]), document["sandbox"])
    # Offline publisher sees only its settings, never model credentials or private templates.
    _atomic_json(directory / "publisher.json", {"revision_id": row["id"], "services": {
        k: v for k, v in document.get("services", {}).items() if k.startswith("PUBLISHER_")}})
    if row["scope"] == "global":
        await materialize_proxy(store, row)
        _atomic_json(config_directory() / "desired.json", {"revision_id": row["id"]})
    return directory


async def materialize_proxy(store, row):
    """保留历史物理部署；配置文件中的秘密仍是加密数据或进程环境引用。"""
    async with store.engine.connect() as conn:
        retained = [dict(item) for item in (await conn.execute(select(s.revisions).where(s.revisions.c.status != "draft")
            .order_by(s.revisions.c.created_at))).mappings()]
    routes, envelopes, fallback_routes, context_fallbacks = {}, {}, [], []
    for revision in retained:
        doc = revision["document"]
        aliases = model_aliases(doc)
        for source in doc.get("litellm", {}).get("model_list", []):
            entry = deepcopy(source)
            policy = entry.pop("_platform_policy", {})
            settings = policy.get("litellm_settings", {})
            params = entry["litellm_params"]
            for target, origin in (("timeout", "request_timeout"), ("num_retries", "num_retries")):
                if origin in settings:
                    params.setdefault(target, settings[origin])
            entry["model_name"] = aliases[source["model_name"]]
            reference = params.get("api_key", "")
            if isinstance(reference, str) and reference.startswith("credential:"):
                identity, _, version_text = reference.removeprefix("credential:").partition("@")
                credential = await store.credential(identity)
                version = int(version_text) if version_text else credential["version"]
                async with store.engine.connect() as conn:
                    secret = (await conn.execute(select(s.credential_versions).where(s.credential_versions.c.credential_id == identity,
                        s.credential_versions.c.version == version))).mappings().one()
                variable = "IF_CREDENTIAL_" + fingerprint([identity, version])[:20].upper()
                envelopes[variable] = {"aad": f"{identity}:{version}:{credential['owner_id'] or 'global'}",
                    "ciphertext": base64.b64encode(secret["ciphertext"]).decode(), "key_id": secret["key_id"]}
                params["api_key"] = "os.environ/" + variable
            entry.setdefault("model_info", {})["id"] = fingerprint(source)
            routes[entry["model_name"]] = entry
        settings = doc.get("litellm", {}).get("litellm_settings", {})
        for field, target in (("fallbacks", fallback_routes), ("context_window_fallbacks", context_fallbacks)):
            for mapping in settings.get(field, []):
                target.append({aliases.get(key, key): [aliases.get(value, value) for value in values] for key, values in mapping.items()})
    result = deepcopy(row["document"].get("litellm", {}))
    result["model_list"] = list(routes.values())
    result.setdefault("general_settings", {}).update(master_key="os.environ/LITELLM_MASTER_KEY", database_url="os.environ/LITELLM_DATABASE_URL")
    result.setdefault("litellm_settings", {}).update(fallbacks=fallback_routes, context_window_fallbacks=context_fallbacks)
    async def protect(value):
        if isinstance(value, dict):
            return {key: await protect(child) for key, child in value.items()}
        if isinstance(value, list):
            return [await protect(child) for child in value]
        if isinstance(value, str) and value.startswith("credential:"):
            identity, _, version_text = value.removeprefix("credential:").partition("@")
            credential = await store.credential(identity)
            version = int(version_text) if version_text else credential["version"]
            variable = "IF_CREDENTIAL_" + fingerprint([identity, version])[:20].upper()
            if variable not in envelopes:
                async with store.engine.connect() as conn:
                    secret = (await conn.execute(select(s.credential_versions).where(s.credential_versions.c.credential_id == identity,
                        s.credential_versions.c.version == version))).mappings().one()
                envelopes[variable] = {"aad": f"{identity}:{version}:{credential['owner_id'] or 'global'}",
                    "ciphertext": base64.b64encode(secret["ciphertext"]).decode(), "key_id": secret["key_id"]}
            return "os.environ/" + variable
        return value
    result = await protect(result)
    _atomic_json(config_directory() / row["id"] / "proxy-bundle.json", {"revision_id": row["id"],
        "fingerprint": row["fingerprint"], "configuration": result, "credentials": envelopes})


async def _litellm_apply(store, row, *, restart):
    base = os.getenv("LITELLM_BASE_URL", "").rstrip("/").removesuffix("/v1")
    master = os.getenv("LITELLM_MASTER_KEY", "")
    if not base or not master:
        raise PlatformError("litellm_control_plane_not_configured", 503)
    document, owner = row["document"], row["owner_id"]
    aliases = model_aliases(document)
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer " + master}, timeout=30) as client:
        response = await client.get("/model/info")
        response.raise_for_status()
        existing = {item.get("model_name") for item in response.json().get("data", [])}
        for source in document.get("litellm", {}).get("model_list", []):
            entry = deepcopy(source)
            policy = entry.pop("_platform_policy", {})
            for target, origin in (("timeout", "request_timeout"), ("num_retries", "num_retries")):
                if origin in policy.get("litellm_settings", {}):
                    entry["litellm_params"].setdefault(target, policy["litellm_settings"][origin])
            entry["model_name"] = aliases[source["model_name"]]
            if entry["model_name"] in existing:
                continue
            key = entry.get("litellm_params", {}).get("api_key", "") or ""
            if key.startswith("credential:"):
                entry["litellm_params"]["api_key"] = await store.secret(key.removeprefix("credential:"), owner)
            elif key.startswith("os.environ/"):
                ref = document.get("credential_refs", {}).get(key.removeprefix("os.environ/"))
                if not ref:
                    raise PlatformError("model_credential_reference_missing")
                entry["litellm_params"]["api_key"] = await store.secret(ref.removeprefix("credential:"), owner)
            async def resolve_credentials(value):
                if isinstance(value, dict):
                    return {key: await resolve_credentials(child) for key, child in value.items()}
                if isinstance(value, list):
                    return [await resolve_credentials(child) for child in value]
                if isinstance(value, str) and value.startswith("credential:"):
                    return await store.secret(value.removeprefix("credential:"), owner)
                return value
            entry["litellm_params"] = await resolve_credentials(entry.get("litellm_params", {}))
            entry.setdefault("model_info", {})["id"] = fingerprint(source)
            result = await client.post("/model/new", json=entry)
            result.raise_for_status()
        response = await client.get("/model/info")
        response.raise_for_status()
        from open_deep_research.models.catalog import parse_model_info, validate_model_catalog
        catalog = parse_model_info(response.json())
        validate_model_catalog(catalog, aliases.values(), budget_enabled=False)
    if restart:
        # Router-wide changes need the managed launcher receipt, not an operator checkbox.
        receipt = config_directory() / row["id"] / "litellm.applied.json"
        from datetime import datetime, timezone
        observed = json.loads(receipt.read_text()) if receipt.exists() else {}
        fresh = bool(observed.get("time")) and (datetime.now(timezone.utc) - datetime.fromisoformat(observed["time"])).total_seconds() < 90
        if not fresh or observed.get("fingerprint") != row["fingerprint"]:
            return "pending", {"mode": "restart", "instruction": "使用平台组件启动器重启 LiteLLM，然后重新检查应用状态。"}
    return "applied", {"models": sorted(aliases.values())}


async def apply_revision(store, row):
    await materialize(store, row)
    async with store.engine.connect() as conn:
        applications = (await conn.execute(select(s.applications).where(s.applications.c.revision_id == row["id"]))).mappings().all()
    for item in applications:
        component = item["component"]
        try:
            if component == "api":
                status, detail = "applied", {"fingerprint": row["fingerprint"]}
            elif component == "litellm":
                status, detail = await _litellm_apply(store, row, restart=item["detail"].get("mode") == "restart")
            elif component == "sandbox":
                from open_deep_research.sandbox.schema import SandboxPolicyBundle, policy_digest
                expected = policy_digest(SandboxPolicyBundle.model_validate(row["document"]["sandbox"]))
                socket = os.environ.get("SANDBOX_CONTROLLER_SOCKET", "/run/insightforge/sandbox-controller.sock")
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket), base_url="http://controller", timeout=5) as client:
                    response = await client.get("/healthz")
                    response.raise_for_status()
                    controller = response.json()
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get(os.environ.get("SANDBOX_GATEWAY_URL", "http://sandbox-gateway:8081").rstrip("/") + "/healthz")
                    response.raise_for_status()
                    gateway = response.json()
                matched = controller.get("status") == "ok" and controller.get("policy_digest") == expected and gateway.get("policy_digest") == expected
                status, detail = ("applied", {"policy_digest": expected}) if matched else ("pending", {"mode": "restart", "instruction": "重启沙箱 Controller 与 Gateway，使两个组件加载同一策略版本。"})
            else:
                receipt = config_directory() / row["id"] / f"{component}.applied.json"
                from datetime import datetime, timezone
                observed = json.loads(receipt.read_text()) if receipt.exists() else {}
                fresh = bool(observed.get("time")) and (datetime.now(timezone.utc) - datetime.fromisoformat(observed["time"])).total_seconds() < 90
                applied = fresh and observed.get("fingerprint") == row["fingerprint"]
                status, detail = ("applied", {"fingerprint": row["fingerprint"]}) if applied else (
                    "pending", {"mode": "restart", "instruction": "使用平台组件启动器启动受影响组件，完成就绪检查后重新检查。"})
            await store.application(row["id"], component, status, {"mode": item["detail"].get("mode", "online"), **detail})
        except Exception as exc:
            # HTTP response bodies may contain credentials; expose only the error class.
            await store.application(row["id"], component, "failed", {"mode": item["detail"].get("mode", "online"), "error_code": type(exc).__name__})
    async with store.engine.connect() as conn:
        return [dict(item) for item in (await conn.execute(select(s.applications).where(s.applications.c.revision_id == row["id"]))).mappings()]

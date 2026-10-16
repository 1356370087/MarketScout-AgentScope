"""平台策略与个人模板的唯一解析边界。"""

from copy import deepcopy
from urllib.parse import urlsplit
from .store import PlatformError, get_store, fingerprint


def validate_overrides(baseline, overrides):
    from open_deep_research.configuration import Configuration
    from .components import model_aliases
    defaults = {**Configuration().model_dump(mode="json"), **baseline.get("runtime", {})}
    policies = baseline.get("policy", {})
    aliases = model_aliases(baseline)
    logical = {value: key for key, value in aliases.items()}
    for key, value in overrides.items():
        compared = logical.get(value, value) if isinstance(value, str) else value
        if key in defaults and defaults[key] == compared:
            continue
        rule = policies.get(key, {})
        if not rule.get("editable"):
            raise PlatformError("configuration_field_locked:" + key, 403)
        if rule.get("allowed") is not None and compared not in rule["allowed"]:
            raise PlatformError("configuration_value_not_allowed:" + key)
        for bound, test in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b)):
            limit = rule.get(bound)
            if limit is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or test(value, limit)):
                raise PlatformError("configuration_bound_violated:" + key)


def validate_personal_models(baseline, config):
    from .importing import assert_secret_free
    assert_secret_free(config)
    if set(config) - {"model_list"}:
        raise PlatformError("个人模型不能修改代理全局设置", 403)
    for model in config.get("model_list", []):
        if model.get("model_name") in {entry.get("model_name") for entry in baseline.get("litellm", {}).get("model_list", [])}:
            raise PlatformError("personal_model_alias_conflicts_with_platform", 409)
        params = model.get("litellm_params", {})
        if set(params) - {"model", "api_base", "api_key", "max_tokens", "temperature", "timeout"}:
            raise PlatformError("personal_model_parameter_not_allowed", 403)
        provider = str(params.get("model", "")).split("/", 1)[0]
        if provider not in baseline.get("allowed_providers", []):
            raise PlatformError("provider_not_allowed", 403)
        endpoint = params.get("api_base")
        if endpoint:
            approved_endpoints = set(baseline.get("allowed_model_endpoints", [])) | {
                item.get("litellm_params", {}).get("api_base") for item in baseline.get("litellm", {}).get("model_list", [])}
            if endpoint not in approved_endpoints:
                raise PlatformError("model_endpoint_not_allowed", 403)
            url = urlsplit(endpoint)
            if url.username or url.password or url.query or url.fragment:
                raise PlatformError("model_endpoint_contains_credentials")


async def resolve(principal, request):
    store = get_store()
    active = await store.active()
    if request.expected_platform_revision and request.expected_platform_revision != active["id"]:
        raise PlatformError("platform_revision_conflict", 409)
    baseline = active["document"]
    from .components import execution_document, model_aliases
    document = execution_document(baseline)
    profile = active
    if request.profile_revision_id and request.profile_revision_id != active["id"]:
        profile = await store.revision(request.profile_revision_id, principal)
        if profile["status"] != "active":
            raise PlatformError("configuration_not_active", 409)
        if profile["scope"] == "personal":
            validate_overrides(baseline, profile["document"]["runtime"])
            validate_personal_models(baseline, profile["document"]["litellm"])
            document["runtime"].update(execution_document(profile["document"])["runtime"])
            document["credential_refs"].update(profile["document"]["credential_refs"])
            document["litellm"].setdefault("model_list", []).extend(profile["document"]["litellm"].get("model_list", []))
        else:
            # Old global templates contribute only the user-editable research preferences.
            chosen = {k: v for k, v in profile["document"]["runtime"].items() if baseline.get("policy", {}).get(k, {}).get("editable")}
            validate_overrides(baseline, chosen)
            translated = execution_document(profile["document"])["runtime"]
            document["runtime"].update({key: translated[key] for key in chosen})
            available = {entry["model_name"] for entry in baseline.get("litellm", {}).get("model_list", [])}
            document["litellm"].setdefault("model_list", []).extend(
                deepcopy(entry) for entry in profile["document"].get("litellm", {}).get("model_list", [])
                if entry["model_name"] in available)
    validate_overrides(baseline, request.configurable)
    document["runtime"].update(request.configurable)
    # The Proxy catalog contains other users' private deployments. Only approved
    # global routes and this owner's active private routes may reach a Run Key.
    if document["runtime"].get("model_backend") == "litellm":
        from .components import model_aliases
        permitted = {}
        for entry in baseline.get("litellm", {}).get("model_list", []):
            permitted[entry["model_name"]] = entry
        permitted.update({alias: permitted[name] for name, alias in model_aliases(baseline).items()})
        for summary in await store.list_revisions(principal):
            if summary["scope"] != "personal" or summary["status"] != "active":
                continue
            own = await store.revision(summary["id"], principal)
            aliases = model_aliases(own["document"])
            for entry in own["document"].get("litellm", {}).get("model_list", []):
                permitted.setdefault(entry["model_name"], entry)
                permitted[aliases[entry["model_name"]]] = entry
        for entry in document.get("litellm", {}).get("model_list", []):
            permitted[entry["model_name"]] = entry
            own_alias = model_aliases({"litellm": {"model_list": [entry]}})[entry["model_name"]]
            permitted[own_alias] = entry
        selections = {value for key, value in document["runtime"].items()
                      if key.endswith("_model") and isinstance(value, str) and value}
        selections.update(value for chain in document["runtime"].get("model_fallbacks", {}).values() for value in chain)
        for model in selections:
            if model not in permitted:
                raise PlatformError("model_not_available_for_user:" + model, 403)
            entry = permitted[model]
            entries = document["litellm"].setdefault("model_list", [])
            if entry not in entries:
                entries.append(deepcopy(entry))
        def physical(model):
            entry = permitted[model]
            return model_aliases({"litellm": {"model_list": [entry]}})[entry["model_name"]]
        for key, value in list(document["runtime"].items()):
            if key.endswith("_model") and isinstance(value, str) and value:
                document["runtime"][key] = physical(value)
        document["runtime"]["model_fallbacks"] = {role: [physical(model) for model in chain]
            for role, chain in document["runtime"].get("model_fallbacks", {}).items()}
    limits = await store.limits(principal.user_id)
    document["quotas"] = limits
    for name in ("max_run_cost_micro_usd", "max_concurrent_runs_per_user", "api_run_create_per_minute"):
        limit = limits.get(name)
        if limit is not None:
            requested = document["runtime"].get(name)
            if name in request.configurable and (requested is None or requested > limit):
                raise PlatformError("user_quota_exceeded:" + name)
            document["runtime"][name] = min(requested, limit) if name == "max_run_cost_micro_usd" and isinstance(requested, (int, float)) and requested > 0 else limit
        elif name != "max_run_cost_micro_usd":
            document["runtime"][name] = baseline.get("runtime", {}).get(name)
    from .components import policy_path
    from .components import execution_document
    document = execution_document(document)
    value = {"revision_id": active["id"], "profile_revision_id": profile["id"], "document": document,
             "policy_path": f"{active['id']}/sandbox-policy.json"}
    from . import runtime
    from open_deep_research.configuration import Configuration
    with runtime.scope(value, principal.user_id):
        cfg = Configuration.from_runnable_config({"configurable": document["runtime"], "metadata": {"platform_snapshot": value}})
    document["runtime"].update({k: v for k, v in cfg.model_dump(mode="json").items() if k in document["runtime"]})
    return {"platform_revision_id": active["id"], "profile_revision_id": profile["id"],
            "fingerprint": fingerprint(document), "snapshot": value, "configurable": document["runtime"], "limits": limits}

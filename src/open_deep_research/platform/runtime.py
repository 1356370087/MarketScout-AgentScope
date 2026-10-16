"""只读运行配置快照。环境变量不再覆盖已纳管业务字段。"""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import os

_current = ContextVar("platform_configuration", default=None)
_secrets = ContextVar("platform_credentials", default=None)
_owner = ContextVar("platform_owner", default=None)
_limits = ContextVar("platform_user_limits", default=None)
_source = ContextVar("platform_operation_source", default=None)
_billing_owner = ContextVar("platform_billing_owner", default=None)
_active = None


def enabled():
    mode = os.getenv("PLATFORM_CONFIGURATION_MODE", "database")
    if mode not in {"database", "explicit"}:
        raise ValueError("PLATFORM_CONFIGURATION_MODE 必须为 database 或 explicit")
    return mode == "database"


def snapshot():
    return _current.get() or _active


def owner():
    return _owner.get()


def bind_request(user_id, limits=None):
    _owner.set(user_id)
    _limits.set(limits)


def user_limits():
    return _limits.get() or {}


def source():
    return _source.get()


def billing_owner():
    return _billing_owner.get()


@contextmanager
def billing_as(user_id):
    token = _billing_owner.set(user_id)
    try:
        yield
    finally:
        _billing_owner.reset(token)


def resolve_secret_references(value):
    if isinstance(value, dict):
        return {key: resolve_secret_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_secret_references(item) for item in value]
    if isinstance(value, str) and value.startswith("credential:"):
        result = (_secrets.get() or {}).get(value)
        if result is None:
            raise ValueError("credential_reference_unavailable")
        return result
    return value


def database_configuration(function):
    """真实模型 CLI 在同一事件循环内装配配置和服务凭据。"""
    from functools import wraps
    @wraps(function)
    async def wrapped(*args, **kwargs):
        if not enabled():
            return await function(*args, **kwargs)
        value = await refresh()
        secrets = await credential_values(value, None)
        with scope(value, secrets=secrets), billing_as("platform"):
            return await function(*args, **kwargs)
    return wrapped


def install(value):
    global _active
    _active = deepcopy(value)


def reset():
    global _active
    _active = None


@contextmanager
def scope(value, user_id=None, secrets=None, source=None):
    token, owner_token, secret_token = _current.set(value), _owner.set(user_id), _secrets.set(secrets)
    source_token = _source.set(source)
    try:
        yield
    finally:
        _current.reset(token)
        _owner.reset(owner_token)
        _secrets.reset(secret_token)
        _source.reset(source_token)


def getenv(name, default=None):
    from .catalog import is_bootstrap, is_secret, environment_catalog
    if not enabled() or is_bootstrap(name):
        return os.getenv(name, default)
    value = snapshot()
    if is_secret(name):
        return (_secrets.get() or {}).get(name, default)
    if value:
        document = value.get("document", value)
        if name in {"LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD", "LITELLM_RUN_BUDGET_MAX_MICRO_USD"}:
            maximum = document.get("quotas", {}).get("max_run_cost_micro_usd")
            result = maximum if name.endswith("MAX_MICRO_USD") else document.get("runtime", {}).get("max_run_cost_micro_usd") or maximum
        elif name.lower() in document.get("runtime", {}):
            result = document["runtime"][name.lower()]
        elif name in document.get("services", {}):
            result = document["services"][name]
        elif name == "SANDBOX_POLICY_PATH" and value.get("policy_path"):
            result = value["policy_path"]
        else:
            result = None
        if result is not None:
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if name in environment_catalog():
        return default
    return os.getenv(name, default)


class Environment(Mapping):
    def __getitem__(self, key):
        value = getenv(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self):
        from .catalog import environment_catalog
        return iter(set(os.environ) | set(environment_catalog()))

    def __len__(self):
        return len(set(iter(self)))


environment = Environment()


async def refresh():
    from .store import get_store
    row = await get_store().active()
    from .components import policy_path, execution_document
    value = {"revision_id": row["id"], "document": execution_document(row["document"]), "policy_path": f"{row['id']}/sandbox-policy.json"}
    install(value)
    return value


async def credential_values(value, user_id, *, allowed_names=None):
    from .store import get_store
    document = value.get("document", value)
    result = {}
    for name, identity in document.get("credential_refs", {}).items():
        if allowed_names is not None and name not in allowed_names:
            continue
        from .store import PlatformError
        try:
            result[name] = await get_store().secret(identity.removeprefix("credential:"), user_id)
        except PlatformError as exc:
            if exc.detail != "credential_not_available":
                raise
    if allowed_names is None:
        def references(node):
            if isinstance(node, dict):
                return set().union(*(references(item) for item in node.values())) if node else set()
            if isinstance(node, list):
                return set().union(*(references(item) for item in node)) if node else set()
            return {node} if isinstance(node, str) and node.startswith("credential:") else set()
        for reference in references(document.get("memory", {})):
            result[reference] = await get_store().secret(reference.removeprefix("credential:"), user_id)
    return result


async def initialize_worker():
    if enabled():
        return await refresh()
    return None

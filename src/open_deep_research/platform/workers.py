"""后台任务绑定不可变配置；组件以实际启动后的回执确认生效。"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from sqlalchemy import select, func
from . import schema as s, runtime
from .components import config_directory, policy_path, execution_document, _atomic_json
from .store import get_store, now, PlatformError
from .accounting import UncertainServiceCall


async def initialize(component):
    if not runtime.enabled():
        return
    while True:
        desired = config_directory() / "desired.json"
        if desired.exists():
            record = json.loads(desired.read_text(encoding="utf-8"))
            path = config_directory() / record["revision_id"] / ("publisher.json" if component == "publisher-worker" else "configuration.json")
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                if component == "publisher-worker":
                    value = {"revision_id": record["revision_id"], "document": {"runtime": {}, "services": value["services"]}}
                else:
                    value["document"]["runtime"]["sandbox_enabled"] = False
                runtime.install(value)
                return
        if component == "publisher-worker":
            await asyncio.sleep(3)
            continue
        try:
            await runtime.refresh()
            return
        except PlatformError as exc:
            if exc.detail != "platform_configuration_required":
                raise
        await asyncio.sleep(3)


def acknowledge(component):
    value = runtime.snapshot()
    if not runtime.enabled() or not value:
        return
    path = config_directory() / value["revision_id"] / "configuration.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    _atomic_json(config_directory() / value["revision_id"] / f"{component}.applied.json",
        {"revision_id": value["revision_id"], "fingerprint": source["fingerprint"], "pid": os.getpid(), "time": now()})


async def active_ready():
    if not runtime.enabled():
        return True
    try:
        value = await runtime.refresh()
        value["document"]["runtime"]["sandbox_enabled"] = False
        runtime.install(value)
        return True
    except PlatformError as exc:
        if exc.detail != "platform_configuration_required":
            raise
        return False


def publisher_ready():
    if not runtime.enabled():
        return True
    pointer = config_directory() / "active.json"
    if not pointer.exists():
        return False
    revision = json.loads(pointer.read_text(encoding="utf-8"))["revision_id"]
    payload = json.loads((config_directory() / revision / "publisher.json").read_text(encoding="utf-8"))
    runtime.install({"revision_id": revision, "document": {"runtime": {}, "services": payload["services"]}})
    return True


@asynccontextmanager
async def job_context(component, identity, owner):
    if not runtime.enabled():
        yield
        return
    store = get_store()
    key = f"{component}:{identity}"
    async with store.engine.begin() as conn:
        active = await store.active(conn)
        await conn.execute(store.upsert(s.job_bindings).values(job_id=key, owner_id=owner,
            revision_id=active["id"], created_at=now()).on_conflict_do_nothing())
        revision = await conn.scalar(select(s.job_bindings.c.revision_id).where(s.job_bindings.c.job_id == key))
        pending = await conn.scalar(select(func.count()).select_from(s.charges).where(s.charges.c.source == key,
            s.charges.c.status.in_(["unknown", "reserved"])))
        if pending:
            raise UncertainServiceCall("job_has_unresolved_model_calls")
        row = await store.revision(revision, conn=conn)
    value = {"revision_id": revision, "document": execution_document(row["document"]), "policy_path": f"{revision}/sandbox-policy.json"}
    value["document"]["runtime"]["sandbox_enabled"] = False
    secrets = await runtime.credential_values(value, owner, allowed_names={"LITELLM_SERVICE_KEY", "DOCUMENT_OCR_API_KEY", "DOCLING_SERVE_API_KEY"})
    with runtime.scope(value, owner, secrets, source=key):
        yield

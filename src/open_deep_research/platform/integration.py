"""原生研究入口与数据库配置的接线。"""

from . import runtime
from .contracts import PreviewRequest
from .resolution import resolve


async def prepare_run(prepare, request, principal):
    from open_deep_research.configuration import Configuration
    result = await resolve(principal, PreviewRequest(profile_revision_id=request.profile_revision_id,
        expected_platform_revision=request.expected_platform_revision, configurable=request.configurable))
    snapshot = result["snapshot"]
    secrets = await runtime.credential_values(snapshot, principal.user_id)
    internal_request = request.model_copy(update={"configurable": result["configurable"]})
    with runtime.scope(snapshot, principal.user_id, secrets):
        prepared = await prepare(internal_request, principal)
        cfg = Configuration.from_runnable_config({"configurable": result["configurable"], "metadata": {"platform_snapshot": snapshot}})
    prepared.setdefault("metadata", {}).update(platform_revision_id=result["platform_revision_id"],
        profile_revision_id=result["profile_revision_id"], platform_snapshot=snapshot)
    return cfg, prepared


async def check_credentials(snapshot, owner):
    from .importing import credential_ids
    from .store import get_store, PlatformError
    store = get_store()
    for identity in credential_ids(snapshot):
        row = await store.credential(identity)
        if row["disabled"] or (row["owner_id"] is not None and row["owner_id"] != owner):
            raise PlatformError("credential_not_available", 403)

"""Translate the legacy development identity at the document SQL boundary."""

from uuid import NAMESPACE_URL, uuid5

_DEV_DOCUMENT_OWNER = str(uuid5(NAMESPACE_URL, "insightforge:documents:local-dev-user"))


def document_owner_id(owner_id: str) -> str:
    """Keep IAM UUIDs intact while giving the synthetic owner a stable SQL ID."""
    return _DEV_DOCUMENT_OWNER if owner_id == "local-dev-user" else owner_id

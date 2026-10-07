"""Trusted runtime inputs for the shared retrieval pipeline."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SearchExecution:
    """Keep credentials and model ports outside public/persisted search inputs."""

    scope: Literal["service", "run"] = "service"
    run_id: str | None = None
    task_id: str | None = None
    manifest: dict[str, Any] | None = None
    models: Any = field(default=None, repr=False)

    def embedding_key(self):
        """A research request never falls back to service credentials."""
        if self.scope == "run":
            from open_deep_research.models.credentials_context import current_run_key

            return current_run_key()
        from .credentials import knowledge_service_key

        return knowledge_service_key()

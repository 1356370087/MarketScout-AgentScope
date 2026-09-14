"""Run-scoped egress mode override and classification ledger stores.

Both stores live on the API authority side (``.runs`` directory), mirroring
``SecurityApprovalStore``: the Gateway reaches them only through the
HMAC-authenticated internal control plane because the sandbox network has
no shared ``.runs`` volume.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

import portalocker
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.tasks.mailbox import (
    atomic_write_json,
    read_json_file,
    validate_component,
)

EgressRuntimeMode = Literal["manual", "auto", "open"]

_MODE_VALUES: frozenset[str] = frozenset({"manual", "auto", "open"})


class EgressModeOverride(BaseModel):
    """One durable runtime mode switch for a single run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    mode: EgressRuntimeMode
    actor: str = ""
    origin: str = "api"
    fence_token: int = Field(ge=1)
    updated_at: float = Field(default_factory=time.time)


class RunEgressModeStore:
    """File-backed runtime egress mode override for one run."""

    def __init__(self, run_id: str, *, runs_dir: str = ".runs") -> None:
        """Bind the override document to one validated run directory."""
        self.run_id = validate_component(run_id, "run_id")
        self.root = Path(runs_dir).resolve() / self.run_id / "sandbox" / "egress"
        self.mode_path = self.root / "mode.json"
        self.lock_path = self.root / "mode.lock"

    def get(self) -> EgressModeOverride | None:
        """Return the stored override, or ``None`` when never switched."""
        if not self.mode_path.exists():
            return None
        return EgressModeOverride.model_validate(read_json_file(self.mode_path))

    def set(
        self,
        *,
        mode: str,
        actor: str,
        fence_token: int,
        origin: str = "api",
    ) -> EgressModeOverride:
        """Atomically replace the runtime override for this run."""
        if mode not in _MODE_VALUES:
            raise ValueError(f"sandbox_egress_mode_invalid:{mode}")
        override = EgressModeOverride(
            run_id=self.run_id,
            mode=mode,  # type: ignore[arg-type]
            actor=actor,
            origin=origin,
            fence_token=fence_token,
            updated_at=time.time(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            atomic_write_json(self.mode_path, override.model_dump(mode="json"))
        return override

    def clear(self, *, fence_token: int) -> None:
        """Drop a stale override from an earlier ownership epoch."""
        if not self.mode_path.exists():
            return
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            current = self.get()
            if current is not None and current.fence_token == fence_token:
                self.mode_path.unlink(missing_ok=True)


class EgressClassificationStore:
    """File-backed classification ledger keyed by domain fingerprint."""

    _MAX_ENTRIES = 2000

    def __init__(self, run_id: str, *, runs_dir: str = ".runs") -> None:
        """Bind the ledger to one validated run directory."""
        self.run_id = validate_component(run_id, "run_id")
        self.root = Path(runs_dir).resolve() / self.run_id / "sandbox" / "egress"
        self.ledger_path = self.root / "classifications.json"
        self.lock_path = self.root / "classifications.lock"

    def _load(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"schema_version": 1, "entries": {}}
        return read_json_file(self.ledger_path)

    def load(self) -> dict[str, dict[str, Any]]:
        """Return every stored entry payload keyed by fingerprint."""
        data = self._load()
        entries = data.get("entries") or {}
        return dict(entries) if isinstance(entries, dict) else {}

    def record(self, entry: dict[str, Any]) -> Literal["recorded", "duplicate"]:
        """Insert one entry unless its fingerprint already exists.

        Raises:
            ValueError: If the entry payload fails ledger validation.
        """
        from open_deep_research.sandbox.egress_classifier import (
            EgressClassificationEntry,
        )

        parsed = EgressClassificationEntry.from_payload(entry)
        from open_deep_research.sandbox.egress_classifier import (
            classification_fingerprint,
        )
        expected = classification_fingerprint(parsed.host, parsed.port, parsed.capability,
                                               parsed.intent_hash)
        if parsed.fingerprint not in {expected, f"egress:{parsed.registered_domain}"}:
            raise ValueError("egress_classification_fingerprint_mismatch")

        def insert(data: dict[str, Any]) -> Literal["recorded", "duplicate"]:
            entries: dict[str, Any] = data.setdefault("entries", {})
            previous = entries.get(parsed.fingerprint)
            if previous and float(previous.get("classified_at", 0)) >= parsed.classified_at:
                return "duplicate"
            if parsed.fingerprint not in entries and len(entries) >= self._MAX_ENTRIES:
                raise ValueError("egress_classification_ledger_full")
            entries[parsed.fingerprint] = parsed.to_payload()
            return "recorded"

        self.root.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            data = self._load()
            result = insert(data)
            atomic_write_json(self.ledger_path, data)
        return result

    def classifier_state(self) -> dict[str, Any]:
        """Read durable counters, without discarding errors as zero usage."""
        return dict(self._load().get("classifier_state", {}))

    def save_classifier_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist monotonically ordered classifier health snapshots."""
        self.root.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+b", timeout=30):
            data = self._load()
            previous = data.get("classifier_state", {})
            if state.get("revision", 0) <= previous.get("revision", -1):
                return previous
            data["classifier_state"] = state
            atomic_write_json(self.ledger_path, data)
        return state

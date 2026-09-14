"""LiteLLM Virtual Key control plane and encrypted per-run Secret Store."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field


class LiteLLMKeyConfigurationError(ValueError):
    """Raised when administrator-owned Run Key policy is incomplete."""


class RunKeyMetadata(BaseModel):
    """Secret-free Run Key identity safe for the run Manifest."""

    model_config = ConfigDict(extra="forbid")

    key_hash: str
    key_alias: str
    budget_micro_usd: int = Field(ge=1)
    expires_at: float
    allowed_models: list[str] = Field(default_factory=list)
    policy_revision: str
    team_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunKeyLease:
    """Decrypted in-memory key and its persistable metadata."""

    key: str
    metadata: RunKeyMetadata


@dataclass(frozen=True, slots=True)
class RunKeySettings:
    """Protected administrator configuration for per-run Virtual Keys."""

    base_url: str
    master_key: str
    team_id: str
    encryption_key: bytes
    default_budget_micro_usd: int
    maximum_budget_micro_usd: int
    ttl_seconds: int = 86_400
    policy_revision: str = "v1"

    @classmethod
    def from_env(cls) -> RunKeySettings:
        """Load and strictly validate protected Run Key settings."""
        base_url = (os.getenv("LITELLM_BASE_URL") or "").strip()
        master_key = (os.getenv("LITELLM_MASTER_KEY") or "").strip()
        team_id = (os.getenv("LITELLM_RUN_TEAM_ID") or "").strip()
        encoded_key = (os.getenv("LITELLM_RUN_KEY_ENCRYPTION_KEY") or "").strip()
        default_raw = (os.getenv("LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD") or "").strip()
        maximum_raw = (os.getenv("LITELLM_RUN_BUDGET_MAX_MICRO_USD") or "").strip()
        missing = [
            name
            for name, value in (
                ("LITELLM_BASE_URL", base_url),
                ("LITELLM_MASTER_KEY", master_key),
                ("LITELLM_RUN_TEAM_ID", team_id),
                ("LITELLM_RUN_KEY_ENCRYPTION_KEY", encoded_key),
                ("LITELLM_RUN_BUDGET_DEFAULT_MICRO_USD", default_raw),
                ("LITELLM_RUN_BUDGET_MAX_MICRO_USD", maximum_raw),
            )
            if not value
        ]
        if missing:
            raise LiteLLMKeyConfigurationError(
                "missing protected LiteLLM settings: " + ", ".join(missing)
            )
        try:
            encryption_key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
        except (ValueError, TypeError) as exc:
            raise LiteLLMKeyConfigurationError(
                "LITELLM_RUN_KEY_ENCRYPTION_KEY must be base64 encoded"
            ) from exc
        if len(encryption_key) != 32:
            raise LiteLLMKeyConfigurationError(
                "LITELLM_RUN_KEY_ENCRYPTION_KEY must decode to exactly 32 bytes"
            )
        try:
            default_budget = int(default_raw)
            maximum_budget = int(maximum_raw)
            ttl_seconds = int(os.getenv("LITELLM_RUN_KEY_TTL_SECONDS", "86400"))
        except ValueError as exc:
            raise LiteLLMKeyConfigurationError("LiteLLM numeric policy values are invalid") from exc
        if default_budget < 1 or maximum_budget < 1 or ttl_seconds < 60:
            raise LiteLLMKeyConfigurationError("LiteLLM budget and TTL values must be positive")
        if default_budget > maximum_budget:
            raise LiteLLMKeyConfigurationError(
                "LiteLLM default Run budget must not exceed the administrator maximum"
            )
        return cls(
            base_url=base_url,
            master_key=master_key,
            team_id=team_id,
            encryption_key=encryption_key,
            default_budget_micro_usd=default_budget,
            maximum_budget_micro_usd=maximum_budget,
            ttl_seconds=ttl_seconds,
            policy_revision=os.getenv("LITELLM_POLICY_REVISION", "v1"),
        )

    def resolve_budget(self, requested_micro_usd: int | None) -> int:
        """Apply the default and immutable administrator ceiling."""
        if requested_micro_usd is not None and requested_micro_usd < 1:
            raise LiteLLMKeyConfigurationError("requested Run budget must be positive")
        requested = requested_micro_usd or self.default_budget_micro_usd
        return min(requested, self.maximum_budget_micro_usd)


def micro_usd_to_usd(value: int) -> Decimal:
    """Convert integral micro-USD to exact decimal USD."""
    if value < 0:
        raise ValueError("micro-USD value cannot be negative")
    return Decimal(value) / Decimal(1_000_000)


def key_digest(key: str) -> str:
    """Return the non-reversible key identity stored in the Manifest."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def spend_micro_usd_from_key_info(payload: dict[str, Any]) -> int:
    """Extract LiteLLM's authoritative cumulative spend using exact USD units."""
    candidates: list[Any] = [payload.get("spend")]
    info = payload.get("info")
    if isinstance(info, dict):
        candidates.extend((info.get("spend"), info.get("total_spend")))
    candidates.append(payload.get("total_spend"))
    for value in candidates:
        if value is None:
            continue
        try:
            amount = Decimal(str(value))
        except (ValueError, TypeError):
            continue
        if amount < 0:
            continue
        return int((amount * Decimal(1_000_000)).to_integral_value())
    raise RuntimeError("litellm_key_info_spend_missing")


class RunKeySecretStore:
    """AES-GCM encrypted, atomically written per-run key storage."""

    _AAD = b"insightforge-litellm-run-key-v1"

    def __init__(self, runs_dir: str | Path, encryption_key: bytes) -> None:
        """Bind an encrypted store to an administrator-owned runs directory."""
        if len(encryption_key) != 32:
            raise ValueError("Run Key encryption key must contain exactly 32 bytes")
        self._root = Path(runs_dir).resolve()
        self._cipher = AESGCM(encryption_key)

    def _path(self, run_id: str) -> Path:
        if not run_id or any(part in {"", ".", ".."} for part in Path(run_id).parts):
            raise ValueError("invalid run_id")
        path = (self._root / run_id / "secrets" / "litellm-run-key.enc").resolve()
        if self._root not in path.parents:
            raise ValueError("run secret path escaped runs directory")
        return path

    def save(self, run_id: str, key: str) -> None:
        """Encrypt and atomically replace a Run Key."""
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, key.encode("utf-8"), self._AAD)
        payload = b"IFRK1" + nonce + ciphertext
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, run_id: str) -> str | None:
        """Decrypt a Run Key, returning None when no local secret exists."""
        path = self._path(run_id)
        if not path.exists():
            return None
        payload = path.read_bytes()
        if len(payload) < 34 or not payload.startswith(b"IFRK1"):
            raise ValueError("invalid encrypted Run Key envelope")
        return self._cipher.decrypt(payload[5:17], payload[17:], self._AAD).decode("utf-8")

    def delete(self, run_id: str) -> None:
        """Delete the local encrypted key after remote blocking succeeds."""
        path = self._path(run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def run_ids(self) -> list[str]:
        """List runs that still contain encrypted keys for reconciliation."""
        return [
            path.parent.parent.name
            for path in self._root.glob("*/secrets/litellm-run-key.enc")
            if path.is_file()
        ]


class LiteLLMKeyAdminClient:
    """Minimal Master-Key client for LiteLLM Virtual Key lifecycle APIs."""

    def __init__(self, settings: RunKeySettings, *, client: httpx.AsyncClient | None = None) -> None:
        """Create a control client without exposing its Master Key in diagnostics."""
        self.settings = settings
        management_url = settings.base_url.rstrip("/")
        if management_url.endswith("/v1"):
            management_url = management_url[:-3]
        self._client = client or httpx.AsyncClient(
            base_url=management_url,
            headers={"Authorization": f"Bearer {settings.master_key}"},
            timeout=30,
        )
        self._owns_client = client is None

    async def generate(
        self,
        *,
        run_id: str,
        budget_micro_usd: int,
        allowed_models: list[str],
        team_id: str | None = None,
    ) -> RunKeyLease:
        """Create one non-resetting, model-restricted Run Key."""
        alias = f"run-{run_id}-{secrets.token_hex(4)}"
        response = await self._client.post(
            "/key/generate",
            json={
                "team_id": team_id or self.settings.team_id,
                "key_alias": alias,
                "models": sorted(set(allowed_models)),
                "max_budget": float(micro_usd_to_usd(budget_micro_usd)),
                "duration": f"{self.settings.ttl_seconds}s",
                "metadata": {"run_id": run_id, "policy_revision": self.settings.policy_revision},
            },
        )
        response.raise_for_status()
        payload = response.json()
        key = str(payload.get("key") or payload.get("token") or "")
        if not key:
            raise RuntimeError("litellm_key_generate_missing_key")
        expires_at = _expiry_from_payload(payload, self.settings.ttl_seconds)
        return RunKeyLease(
            key=key,
            metadata=RunKeyMetadata(
                key_hash=key_digest(key),
                key_alias=alias,
                budget_micro_usd=budget_micro_usd,
                expires_at=expires_at,
                allowed_models=sorted(set(allowed_models)),
                policy_revision=self.settings.policy_revision,
                team_id=team_id,
            ),
        )

    async def renew(self, lease: RunKeyLease) -> RunKeyLease:
        """Extend a live key without changing its budget or permissions."""
        response = await self._client.post(
            "/key/update",
            json={"key": lease.key, "duration": f"{self.settings.ttl_seconds}s"},
        )
        response.raise_for_status()
        payload = response.json()
        metadata = lease.metadata.model_copy(
            update={"expires_at": _expiry_from_payload(payload, self.settings.ttl_seconds)}
        )
        return RunKeyLease(key=lease.key, metadata=metadata)

    async def generate_service_key(
        self,
        *,
        alias: str,
        models: list[str],
        max_budget_usd: float | None = None,
        budget_duration: str | None = None,
    ) -> str:
        """Create a long-lived, model-restricted Service Key for evaluation.

        Unlike Run Keys these never expire; the optional ``budget_duration``
        (e.g. ``"30d"``) makes LiteLLM reset the spend window periodically
        while ``max_budget_usd`` caps each window. The raw key is returned
        exactly once because the gateway only stores its hash.
        """
        body: dict[str, Any] = {
            "team_id": self.settings.team_id,
            "key_alias": alias,
            "models": sorted(set(models)),
            "metadata": {
                "purpose": "service",
                "policy_revision": self.settings.policy_revision,
            },
        }
        if max_budget_usd is not None:
            body["max_budget"] = float(max_budget_usd)
        if budget_duration:
            body["budget_duration"] = budget_duration
        response = await self._client.post("/key/generate", json=body)
        response.raise_for_status()
        payload = response.json()
        key = str(payload.get("key") or payload.get("token") or "")
        if not key:
            raise RuntimeError("litellm_service_key_generate_missing_key")
        return key

    async def block(self, key: str) -> None:
        """Immediately prevent further model use by a terminal Run."""
        response = await self._client.post("/key/block", json={"key": key})
        response.raise_for_status()

    async def info(self, key: str) -> dict[str, Any]:
        """Read authoritative budget/spend information for reconciliation."""
        response = await self._client.get("/key/info", params={"key": key})
        response.raise_for_status()
        payload = response.json()
        return dict(payload) if isinstance(payload, dict) else {}

    async def list_run_keys(self, *, page_size: int = 100, max_keys: int = 1000) -> list[dict[str, Any]]:
        """List Virtual Keys of the Run team for orphan reconciliation.

        LiteLLM list payloads vary across versions, so callers must read the
        entries defensively (``key``/``token``, ISO or epoch ``created_at``).
        """
        entries: list[dict[str, Any]] = []
        page = 1
        while len(entries) < max_keys:
            response = await self._client.get(
                "/key/list",
                params={"page": page, "size": page_size},
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                break
            entries.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < page_size:
                break
            page += 1
        return entries[:max_keys]

    async def block_entry(self, entry: Mapping[str, Any]) -> None:
        """Block one listed key using whichever identifier the payload exposes."""
        candidates = [
            {"key": entry["key"]} if entry.get("key") else None,
            {"key": entry["token"]} if entry.get("token") else None,
            {"key_hash": entry["key_hash"]} if entry.get("key_hash") else None,
        ]
        for body in candidates:
            if body is None:
                continue
            response = await self._client.post("/key/block", json=body)
            response.raise_for_status()
            return
        raise RuntimeError("litellm_key_entry_missing_identifier")

    async def aclose(self) -> None:
        """Close an internally owned HTTP pool."""
        if self._owns_client:
            await self._client.aclose()


_TEAM_ROLES = ("admin", "developer", "researcher")
_TEAM_BUDGET_DURATION_PATTERN = re.compile(r"^\d+(s|m|h|d|mo)$")


@dataclass(frozen=True, slots=True)
class TeamBudgetPolicy:
    """Enforced spend window for one IAM role's team on the gateway."""

    budget_micro_usd: int
    budget_duration: str = "30d"
    rpm_limit: int | None = None
    tpm_limit: int | None = None

    def body_fields(self) -> dict[str, Any]:
        """Project the policy into LiteLLM team create/update payloads."""
        body: dict[str, Any] = {
            "max_budget": float(micro_usd_to_usd(self.budget_micro_usd)),
            "budget_duration": self.budget_duration,
        }
        if self.rpm_limit is not None:
            body["rpm_limit"] = self.rpm_limit
        if self.tpm_limit is not None:
            body["tpm_limit"] = self.tpm_limit
        return body

    def matches_remote(self, team: Mapping[str, Any]) -> bool:
        """Compare against a /team/list entry; unknown remote fields read as None."""
        try:
            remote_budget = float(team.get("max_budget") or 0)
        except (TypeError, ValueError):
            remote_budget = 0.0
        if abs(remote_budget - float(micro_usd_to_usd(self.budget_micro_usd))) > 1e-9:
            return False
        if str(team.get("budget_duration") or "") != self.budget_duration:
            return False
        for field, limit in (("rpm_limit", self.rpm_limit), ("tpm_limit", self.tpm_limit)):
            remote = team.get(field)
            if limit is None:
                # Some versions project 0 instead of null; both mean "unset".
                if remote not in (None, 0, 0.0, ""):
                    return False
            elif remote != limit:
                return False
        return True


def team_budget_policies_from_env() -> dict[str, TeamBudgetPolicy]:
    """Load per-role team budgets with ``LITELLM_TEAM_DEFAULT_*`` fallbacks.

    A role joins the policy map when its own ``BUDGET_MICRO_USD`` or the
    default one is configured; roles absent from the map keep using the global
    Run team (pre-existing behavior).
    """
    policies: dict[str, TeamBudgetPolicy] = {}
    default_raw = os.getenv("LITELLM_TEAM_DEFAULT_BUDGET_MICRO_USD", "").strip()
    for role in _TEAM_ROLES:
        prefix_role = f"LITELLM_TEAM_{role.upper()}_"
        budget_raw = os.getenv(f"{prefix_role}BUDGET_MICRO_USD", "").strip()
        if not budget_raw:
            budget_raw = default_raw
        if not budget_raw:
            continue
        try:
            budget_micro_usd = int(budget_raw)
        except ValueError as exc:
            raise LiteLLMKeyConfigurationError(
                f"LITELLM_TEAM_{role.upper()}_BUDGET_MICRO_USD must be an integer"
            ) from exc
        if budget_micro_usd < 1:
            raise LiteLLMKeyConfigurationError(
                f"LITELLM_TEAM_{role.upper()}_BUDGET_MICRO_USD must be positive"
            )
        duration = (
            os.getenv(f"{prefix_role}BUDGET_DURATION", "").strip()
            or os.getenv("LITELLM_TEAM_DEFAULT_BUDGET_DURATION", "").strip()
            or "30d"
        )
        if not _TEAM_BUDGET_DURATION_PATTERN.match(duration):
            raise LiteLLMKeyConfigurationError(
                f"LITELLM_TEAM_{role.upper()}_BUDGET_DURATION must match "
                "'<n><s|m|h|d|mo>', e.g. '30d'"
            )

        def _limit(name: str) -> int | None:
            raw = (
                os.getenv(f"{prefix_role}{name}", "").strip()
                or os.getenv(f"LITELLM_TEAM_DEFAULT_{name}", "").strip()
            )
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError as exc:
                raise LiteLLMKeyConfigurationError(
                    f"LITELLM_TEAM_{role.upper()}_{name} must be an integer"
                ) from exc
            if value < 1:
                raise LiteLLMKeyConfigurationError(
                    f"LITELLM_TEAM_{role.upper()}_{name} must be positive"
                )
            return value

        policies[role] = TeamBudgetPolicy(
            budget_micro_usd=budget_micro_usd,
            budget_duration=duration,
            rpm_limit=_limit("RPM_LIMIT"),
            tpm_limit=_limit("TPM_LIMIT"),
        )
    return policies


def team_alias_for_role(role: str) -> str:
    """Deterministic LiteLLM team alias so every worker converges on one team."""
    return f"if-team-{role}"


SYNTHETIC_LOCAL_DEV_USER_ID = "local-dev-user"


def resolve_role_team_role(
    roles: list[str] | tuple[str, ...] | set[str],
    *,
    user_id: str | None,
    policies: dict[str, TeamBudgetPolicy],
) -> str | None:
    """Pick the highest-privileged role that has a configured team budget.

    The synthetic local-dev bypass identity always stays on the global Run
    team so demo deployments never sprout per-role teams.
    """
    if user_id == SYNTHETIC_LOCAL_DEV_USER_ID:
        return None
    for role in _TEAM_ROLES:
        if role in policies and role in roles:
            return role
    return None


class LiteLLMTeamAdminClient:
    """Master-Key client for the role-team budget lifecycle."""

    _team_id_cache: dict[str, str] = {}

    def __init__(self, settings: RunKeySettings, *, client: httpx.AsyncClient | None = None) -> None:
        """Create a control client without exposing its Master Key."""
        self.settings = settings
        management_url = settings.base_url.rstrip("/")
        if management_url.endswith("/v1"):
            management_url = management_url[:-3]
        self._client = client or httpx.AsyncClient(
            base_url=management_url,
            headers={"Authorization": f"Bearer {settings.master_key}"},
            timeout=30,
        )
        self._owns_client = client is None

    async def ensure_team(self, alias: str, policy: TeamBudgetPolicy) -> str:
        """Return the team id for ``alias``, creating or updating as needed.

        Convergence across workers is guaranteed by alias: any worker that
        finds an existing team updates it toward the configured policy, and a
        create race loses to the list-resolve retry.
        """
        cached = self._team_id_cache.get(alias)
        if cached is not None:
            return cached
        team = await self._find_team(alias)
        if team is None:
            body: dict[str, Any] = {"team_alias": alias, **policy.body_fields()}
            response = await self._client.post("/team/new", json=body)
            response.raise_for_status()
            payload = response.json()
            team_id = str(
                payload.get("team_id")
                or (payload.get("team") or {}).get("team_id")
                or ""
            )
            if team_id:
                self._team_id_cache[alias] = team_id
                return team_id
            # Some versions return the created team without an id payload;
            # fall through to the authoritative list lookup.
            team = await self._find_team(alias)
            if team is None:
                raise RuntimeError("litellm_team_create_unresolved")
        if not policy.matches_remote(team):
            # Budget changes reset the team's spend window on most versions;
            # operators should treat policy edits as a new accounting period.
            update = {"team_id": team["team_id"], **policy.body_fields()}
            response = await self._client.post("/team/update", json=update)
            response.raise_for_status()
        team_id = str(team["team_id"])
        self._team_id_cache[alias] = team_id
        return team_id

    async def _find_team(self, alias: str) -> dict[str, Any] | None:
        page = 1
        while page <= 20:
            response = await self._client.get(
                "/team/list",
                params={"page": page, "size": 100},
            )
            response.raise_for_status()
            payload = response.json()
            teams = payload.get("teams") if isinstance(payload, dict) else None
            if not isinstance(teams, list):
                return None
            for entry in teams:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("team_alias") or "") == alias and entry.get("team_id"):
                    return entry
            if len(teams) < 100:
                return None
            page += 1
        return None

    async def aclose(self) -> None:
        """Close an internally owned HTTP pool."""
        if self._owns_client:
            await self._client.aclose()


def _expiry_from_payload(payload: dict[str, Any], ttl_seconds: int) -> float:
    raw = payload.get("expires") or payload.get("expires_at")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + ttl_seconds


class RunKeyManager:
    """Coordinate creation, renewal, encrypted persistence and terminal blocking."""

    def __init__(
        self,
        settings: RunKeySettings,
        store: RunKeySecretStore,
        admin: LiteLLMKeyAdminClient,
    ) -> None:
        """Bind policy, encrypted storage and the remote control API."""
        self.settings = settings
        self.store = store
        self.admin = admin

    async def ensure(
        self,
        *,
        run_id: str,
        requested_budget_micro_usd: int | None,
        allowed_models: list[str],
        metadata: RunKeyMetadata | None = None,
        team_id: str | None = None,
    ) -> RunKeyLease:
        """Create, renew, or regenerate the key required by a new/resumed Run.

        ``team_id`` optionally pins the key to a role-budget team instead of
        the global Run team; a resumed Run keeps whatever team its stored key
        already belongs to because renewal never changes team membership.

        A stored key whose remote state can no longer be proven (secret lost,
        manifest metadata lost, or renewal rejected because reconciliation
        already blocked the key) is replaced by a freshly generated key instead
        of failing the resume: the orphaned remote key stays blocked/expired and
        is bounded by the administrator TTL.
        """
        existing = self.store.load(run_id)
        if existing is not None and metadata is not None:
            budget = metadata.budget_micro_usd
            if key_digest(existing) != metadata.key_hash:
                raise RuntimeError("litellm_run_key_hash_mismatch")
            if sorted(metadata.allowed_models) != sorted(set(allowed_models)):
                raise RuntimeError("litellm_run_model_allowlist_changed_during_resume")
            lease = RunKeyLease(key=existing, metadata=metadata)
            renewal_window = min(3600, max(60, self.settings.ttl_seconds // 4))
            if metadata.expires_at <= time.time() + renewal_window:
                try:
                    lease = await self.admin.renew(lease)
                except (httpx.HTTPError, RuntimeError):
                    lease = await self._replace_unusable_key(
                        run_id,
                        allowed_models=allowed_models,
                        budget_micro_usd=budget,
                        stale_key=existing,
                        team_id=team_id,
                    )
            self.store.save(run_id, lease.key)
            return lease
        if existing is not None:
            # Secret without manifest identity: the key can no longer be renewed
            # or attributed, so block it and continue with a fresh key.
            lease = await self._replace_unusable_key(
                run_id,
                allowed_models=allowed_models,
                budget_micro_usd=None,
                stale_key=existing,
                team_id=team_id,
            )
            self.store.save(run_id, lease.key)
            return lease
        budget = (
            metadata.budget_micro_usd
            if metadata is not None
            else self.settings.resolve_budget(requested_budget_micro_usd)
        )
        lease = await self.admin.generate(
            run_id=run_id,
            budget_micro_usd=budget,
            allowed_models=allowed_models,
            team_id=team_id,
        )
        self.store.save(run_id, lease.key)
        return lease

    async def _replace_unusable_key(
        self,
        run_id: str,
        *,
        allowed_models: list[str],
        budget_micro_usd: int | None,
        stale_key: str,
        team_id: str | None = None,
    ) -> RunKeyLease:
        """Block a stale key best-effort and mint its replacement."""
        try:
            await self.admin.block(stale_key)
        except (httpx.HTTPError, RuntimeError):
            # The remote TTL bounds the unblocked orphan; replacement continues.
            pass
        self.store.delete(run_id)
        budget = (
            budget_micro_usd
            if budget_micro_usd is not None
            else self.settings.resolve_budget(None)
        )
        return await self.admin.generate(
            run_id=run_id,
            budget_micro_usd=budget,
            allowed_models=allowed_models,
            team_id=team_id,
        )

    async def finalize(self, run_id: str) -> bool:
        """Block then erase the key; retain ciphertext when cleanup must be retried."""
        key = self.store.load(run_id)
        if key is None:
            return True
        try:
            await self.admin.block(key)
        except (httpx.HTTPError, RuntimeError):
            return False
        self.store.delete(run_id)
        return True

    async def authoritative_spend_micro_usd(self, run_id: str) -> int:
        """Read cumulative Run spend without persisting or exposing the key."""
        key = self.store.load(run_id)
        if key is None:
            raise RuntimeError("litellm_run_key_unavailable")
        return spend_micro_usd_from_key_info(await self.admin.info(key))

    async def aclose(self) -> None:
        """Close the control-plane HTTP pool owned by this manager."""
        await self.admin.aclose()


__all__ = [
    "LiteLLMKeyAdminClient",
    "LiteLLMKeyConfigurationError",
    "RunKeyLease",
    "RunKeyManager",
    "RunKeyMetadata",
    "RunKeySecretStore",
    "RunKeySettings",
    "key_digest",
    "micro_usd_to_usd",
    "spend_micro_usd_from_key_info",
]

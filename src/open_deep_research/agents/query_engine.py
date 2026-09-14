"""Outer session engines for Deep Research.

``QueryEngine`` owns the session/protocol layer. The lower-level model/tool loop
lives in :mod:`open_deep_research.agents.query`.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig

from open_deep_research.agents.model_recovery import (
    ModelCandidate,
    build_model_candidate_chain,
)
from open_deep_research.agents.query import (
    BeforeTurnHookResult,
    ContextPolicy,
    QueryParams,
    StopHookResult,
    ToolResultsHookResult,
    query,
)
from open_deep_research.agents.query_checkpoint import (
    CallbackQueryCheckpointSink,
    RunContextQueryCheckpointSink,
)
from open_deep_research.agents.query_state import (
    QualityRecoveryState,
    QueryCheckpointSink,
    QueryLoopState,
)
from open_deep_research.budgets import BudgetGate
from open_deep_research.completion import (
    CompletionDecision,
    ResearchCompletionPolicy,
    completion_policy_context,
)
from open_deep_research.configuration import (
    QUALITY_POLICY_VERSION,
    RUN_CONFIG_FROZEN_FIELDS,
    RUN_CONFIG_SCHEMA_VERSION,
    Configuration,
    freeze_run_config,
    run_config_fingerprint,
)
from open_deep_research.evaluation import build_evaluation_snapshot
from open_deep_research.events.public import (
    PUBLIC_STAGES,
    event_publisher_from_config,
    extract_public_sources,
)
from open_deep_research.events.task_activity import publish_task_activity
from open_deep_research.evidence import (
    eligible_evidence_records,
    source_scoped_evidence_records,
)
from open_deep_research.models.catalog import (
    LiteLLMModelCatalogClient,
    ModelCatalogEntry,
    freeze_catalog_snapshot,
    validate_model_catalog,
)
from open_deep_research.models.credentials import (
    LiteLLMKeyAdminClient,
    LiteLLMTeamAdminClient,
    RunKeyManager,
    RunKeyMetadata,
    RunKeySecretStore,
    RunKeySettings,
    resolve_role_team_role,
    team_alias_for_role,
    team_budget_policies_from_env,
)
from open_deep_research.models.gateway import bind_run_key, reset_run_key
from open_deep_research.models.invocation import close_run_gateway
from open_deep_research.models.limits import install_gateway_token_limits
from open_deep_research.observability import get_trace_recorder
from open_deep_research.quality.contract import (
    AdmissionStatus,
    ResearchRiskProfile,
    is_delegable_requirement,
    merge_coverage_ledger,
)
from open_deep_research.quality.gate import (
    HandoffAssessment,
    evaluate_subagent_handoff,
)
from open_deep_research.report.coverage import derive_state_coverage_checklist
from open_deep_research.report.evidence_synthesis import (
    build_evidence_limited_report,
)
from open_deep_research.run_context import (
    JournalCorruptedError,
    ResearchBriefPersistenceError,
    RunConfigurationError,
    RunContextStore,
)
from open_deep_research.runtime import (
    END,
    RuntimeCommand,
    apply_update_to_state,
    coerce_command,
    normalize_messages,
)
from open_deep_research.runtime_control import CancellationScope, RunCancelled
from open_deep_research.security.inputs import validate_client_messages
from open_deep_research.tasks.lease import (
    FenceLostError,
    LeaderLeaseManager,
)
from open_deep_research.tool_taxonomy import classify_tool_name
from open_deep_research.tools.governance import GovernedToolCallResult

logger = logging.getLogger(__name__)


def _ensure_config(config: RunnableConfig | None, fallback: RunnableConfig | None = None) -> RunnableConfig:
    merged: RunnableConfig = {"configurable": {}, "metadata": {}}
    for source in (fallback, config):
        if not source:
            continue
        merged["configurable"].update(source.get("configurable", {}))
        merged["metadata"].update(source.get("metadata", {}))
    merged["metadata"].setdefault("run_id", merged["configurable"].get("thread_id") or str(uuid.uuid4()))
    merged["configurable"].setdefault("thread_id", merged["metadata"]["run_id"])
    return merged

_PASSIVE_COORDINATION_TOOLS = frozenset({
    "WaitForResearchUpdates",
    "WaitForTeamEvents",
    "TaskGet",
    "TaskList",
    "CheckResearchTask",
    "ListResearchTasks",
    "ReadResearchArtifact",
})
_APPROVAL_PENDING_MESSAGE = (
    "[Approval Pending] One or more fetch targets are waiting for a human "
    "network approval. Re-issue the exact same fetch call: it will block until "
    "the decision arrives. Do not substitute new searches — they burn budget "
    "without producing admissible evidence."
)


async def _supervisor_turn_advance_policy(
    messages: list[BaseMessage],
    _state: QueryLoopState,
    config: RunnableConfig,
) -> int:
    """Keep passive coordination waits off the research-turn budget.

    A turn stays budget-free only when every tool call it issued is a
    read-only coordination call AND at least one async task is unfinished;
    waits with nothing in flight keep counting so the loop cannot spin
    forever on an empty mailbox.
    """
    last_ai = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    calls = list(getattr(last_ai, "tool_calls", None) or [])
    if not calls or any(
        str(call.get("name")) not in _PASSIVE_COORDINATION_TOOLS
        for call in calls
    ):
        return 1
    from open_deep_research.agents.deep_researcher import (
        has_unfinished_async_tasks,
    )

    try:
        return 0 if await has_unfinished_async_tasks(config) else 1
    except Exception:  # noqa: BLE001 - a broken task store must not stall turns
        logger.exception("supervisor turn policy failed; counting the turn")
        return 1


def _message_text(messages: list[Any]) -> str:
    normalized = [m for m in normalize_messages(messages) if isinstance(m, BaseMessage)]
    return get_buffer_string(normalized)


def _latest_handoff_assessments(
    assessments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the most recent assessment record per task id.

    A task accumulates records across waves and artifact re-reads; terminal
    verdicts (gate status, refs) must reflect each task's latest outcome
    instead of duplicating or resurrecting superseded ones.
    """
    latest: dict[str, dict[str, Any]] = {}
    for item in assessments:
        task_id = str(item.get("tool_call_id", ""))
        if task_id:
            latest[task_id] = item
    return latest


def _evidence_limited_coverage_map(
    *,
    authoritative_ledger: dict[str, Any],
    latest_assessments: dict[str, dict[str, Any]],
    verified_artifacts: list[dict[str, Any]],
    eligible_evidence: list[dict[str, Any]],
    contract_requirement_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Build a non-authoritative map used only by partial report synthesis."""
    eligible_ids = {
        str(record.get("evidence_id", ""))
        for record in eligible_evidence
        if record.get("evidence_id")
    }
    artifact_by_task = {
        str(artifact.get("task_id", "")): artifact
        for artifact in verified_artifacts
        if artifact.get("task_id")
    }
    result: dict[str, dict[str, Any]] = {}

    def merge_entry(
        requirement_id: str,
        *,
        status: str,
        evidence_ids: list[str],
        task_ids: list[str],
        caveats: list[str],
    ) -> None:
        if (
            requirement_id not in contract_requirement_ids
            or status not in {"supported", "partial"}
        ):
            return
        safe_evidence_ids = [
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id in eligible_ids
        ]
        if not safe_evidence_ids:
            return
        current = result.setdefault(
            requirement_id,
            {
                "status": "partial",
                "evidence_ids": [],
                "task_ids": [],
                "caveats": [],
            },
        )
        if status == "supported":
            current["status"] = "supported"
        current["evidence_ids"] = list(
            dict.fromkeys([*current["evidence_ids"], *safe_evidence_ids])
        )
        current["task_ids"] = list(
            dict.fromkeys([*current["task_ids"], *task_ids])
        )
        current["caveats"] = list(
            dict.fromkeys([*current["caveats"], *caveats])
        )

    for requirement_id, entry in authoritative_ledger.items():
        if not isinstance(entry, dict):
            continue
        merge_entry(
            str(requirement_id),
            status=str(entry.get("status", "")),
            evidence_ids=[
                str(item) for item in entry.get("evidence_ids", [])
            ],
            task_ids=[str(item) for item in entry.get("task_ids", [])],
            caveats=[str(item) for item in entry.get("caveats", [])],
        )

    for task_id, assessment in latest_assessments.items():
        artifact = artifact_by_task.get(task_id)
        if (
            not isinstance(artifact, dict)
            or int(artifact.get("schema_version", 1)) < 2
            or not artifact.get("coverage_contract")
        ):
            continue
        owned_requirement_ids = {
            str(item) for item in artifact.get("requirement_ids", [])
        }
        assessment_caveats = [
            str(item) for item in assessment.get("caveats", [])
        ]
        for coverage in assessment.get("requirement_coverage", []):
            if not isinstance(coverage, dict):
                continue
            requirement_id = str(coverage.get("requirement_id", ""))
            if requirement_id not in owned_requirement_ids:
                continue
            merge_entry(
                requirement_id,
                status=str(coverage.get("status", "")),
                evidence_ids=[
                    str(item) for item in coverage.get("evidence_ids", [])
                ],
                task_ids=[task_id],
                caveats=assessment_caveats,
            )
    return result


def _config_user_id(config: RunnableConfig) -> str | None:
    metadata = config.get("metadata", {})
    configurable = config.get("configurable", {})
    return metadata.get("user_id") or metadata.get("owner") or configurable.get("user_id")

class QueryEngine:
    """Conversation and protocol shell for the lead research agent."""

    def __init__(self, config: RunnableConfig | None = None):
        """Create a lead-agent session engine."""
        self.config = _ensure_config(config)
        self.run_id = self.config["metadata"]["run_id"]
        self.messages: list[Any] = []
        self.transcript: list[dict[str, Any]] = []
        self.total_usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "rate_429": 0.0,
            "total_llm_tool_calls": 0,
            "cache_hit_rate": 0.0,
            "cache_input_ratio": 0.0,
            "llm_output_tokens_per_second": 0.0,
            "tool_success_rate": 0.0,
        }
        self.permission_denials: list[dict[str, Any]] = []
        self.cancellation_scope = CancellationScope()
        self.cancelled = False
        self.status = "pending"
        self.started_at = time.time()
        self.pending_human_action: dict[str, Any] | None = None
        self.human_feedback: list[dict[str, Any]] = []
        self._feedback_cursor = 0
        self._pending_action_future: asyncio.Future[dict[str, Any]] | None = None
        self._pending_action_loop: asyncio.AbstractEventLoop | None = None
        self.final_state: dict[str, Any] | None = None
        self._restored_query_states: dict[str, dict[str, Any]] = {}
        self.context_store: RunContextStore | None = None
        self.persistence_degraded = False
        self.run_fence_token: int | None = None
        self._lease_heartbeat_task: asyncio.Task[None] | None = None
        self._litellm_key_manager: RunKeyManager | None = None
        self._litellm_run_key: str | None = None
        self._litellm_context_token: Any = None
        self._configure_run_lease()
        self._configure_context_store()

    def _configure_run_lease(self) -> None:
        """Bind the lease manager to the current run and persistence settings."""
        configurable = Configuration.from_runnable_config(self.config)
        expected_root = Path(configurable.runs_dir).resolve() / self.run_id / "coordination"
        current = getattr(self, "run_lease", None)
        matches = (
            current is not None
            and current.run_id == self.run_id
            and current.root == expected_root
            and current.lease_seconds == configurable.leader_lease_seconds
            and current.lock_timeout == configurable.mailbox_lock_timeout_seconds
        )
        if matches:
            return
        if self.run_fence_token is not None:
            raise RuntimeError("cannot_reconfigure_run_resources_while_lease_is_held")
        self.run_lease = LeaderLeaseManager(
            runs_dir=configurable.runs_dir,
            run_id=self.run_id,
            lease_seconds=configurable.leader_lease_seconds,
            lock_timeout=configurable.mailbox_lock_timeout_seconds,
            owner_id=f"query-{uuid.uuid4()}",
        )

    async def acquire_run_lease(self) -> int:
        """Acquire this engine's durable run ownership epoch once."""
        if self.run_fence_token is not None:
            if await self.run_lease.is_owner(
                expected_fence_token=self.run_fence_token
            ):
                return self.run_fence_token
            raise FenceLostError(f"Lost Lead lease for run {self.run_id}")
        lease = await self.run_lease.acquire()
        self.run_fence_token = lease.fence_token
        self.config.setdefault("metadata", {})["run_started_at"] = self.started_at
        self.config["metadata"]["run_fence_token"] = lease.fence_token
        self.config["metadata"]["run_lease_owner_id"] = self.run_lease.owner_id
        if self.context_store is not None:
            self.context_store.bind_fence_token(
                lease.fence_token,
                self.run_lease.owner_id,
            )
        self._lease_heartbeat_task = asyncio.create_task(self._lease_heartbeat())
        return lease.fence_token

    async def _register_sandbox_gateway(self) -> None:
        """Register the frozen run and ephemeral credentials before any model call."""
        configurable = Configuration.from_runnable_config(self.config)
        if not configurable.sandbox_enabled:
            return
        if self.run_fence_token is None:
            raise RuntimeError("sandbox_gateway_registration_requires_fence")
        from open_deep_research.sandbox.gateway_client import (
            SandboxGatewayControlClient,
            split_gateway_registration,
        )

        frozen, credentials = split_gateway_registration(dict(self.config))
        if configurable.model_backend == "litellm":
            if not self._litellm_run_key:
                raise RuntimeError("litellm_run_key_unavailable")
            credentials["LITELLM_RUN_KEY"] = self._litellm_run_key
        await SandboxGatewayControlClient(configurable).register_run(
            run_id=self.run_id,
            fence_token=self.run_fence_token,
            frozen_config=frozen,
            api_keys=credentials,
        )

    def _set_egress_intent(self, messages: list[Any]) -> None:
        """Anchor domain review to user messages, never retrieved content."""
        human_messages = [
            message for message in normalize_messages(messages)
            if message.type == "human"
        ]
        self.config.setdefault("metadata", {})["sandbox_egress_intent"] = (
            " ".join(_message_text(human_messages).split())[:500]
        )

    # litellm.yaml routes every model group's cross-group fallback to this
    # alias; Run Keys must whitelist it or the Router's fallback request is
    # rejected by the key's model allowlist (403).
    _LITELLM_FALLBACK_MODEL_ALIAS = "if-fallback-v1"

    @staticmethod
    def _litellm_allowed_models(configurable: Configuration) -> list[str]:
        """Return the exact versioned model groups available to this Run."""
        candidates = [
            configurable.supervisor_model or configurable.research_model,
            configurable.research_model,
            configurable.summarization_model,
            configurable.message_summary_model or configurable.summarization_model,
            configurable.compression_model,
            configurable.final_report_model,
            configurable.quality_evaluation_model,
            configurable.report_review_model or configurable.quality_evaluation_model,
            configurable.web_rerank_model,
            configurable.web_evidence_model,
            configurable.egress_classifier_model or configurable.quality_evaluation_model,
            QueryEngine._LITELLM_FALLBACK_MODEL_ALIAS,
        ]
        return sorted({str(model) for model in candidates if model})

    async def _resolve_role_team_id(self, settings: RunKeySettings) -> str | None:
        """Pin Run Keys to the creator's role-budget team when one is configured.

        Tier 1 policy is entirely env-driven: roles without a configured
        budget, the synthetic local-dev identity, and key-less offline engines
        keep the global Run team, byte-for-byte matching pre-team behavior.
        """
        policies = team_budget_policies_from_env()
        if not policies:
            return None
        user = self.config.get("configurable", {}).get("langgraph_auth_user") or {}
        role = resolve_role_team_role(
            list(user.get("roles") or []),
            user_id=str(user.get("id") or user.get("identity") or ""),
            policies=policies,
        )
        if role is None:
            return None
        client = LiteLLMTeamAdminClient(settings)
        try:
            return await client.ensure_team(
                team_alias_for_role(role), policies[role]
            )
        finally:
            await client.aclose()

    async def _ensure_litellm_run_key(self) -> None:
        """Create or renew the encrypted Virtual Key before any model call."""
        configurable = Configuration.from_runnable_config(self.config)
        if configurable.model_backend != "litellm":
            return
        settings = RunKeySettings.from_env()
        team_id = await self._resolve_role_team_id(settings)
        allowed_models = self._litellm_allowed_models(configurable)
        if configurable.model_catalog_snapshot:
            catalog = {
                name: ModelCatalogEntry.model_validate(value)
                for name, value in configurable.model_catalog_snapshot.items()
            }
        else:
            catalog_client = LiteLLMModelCatalogClient(
                base_url=settings.base_url,
                api_key=settings.master_key,
            )
            try:
                catalog = await catalog_client.load()
            finally:
                await catalog_client.aclose()
            validate_model_catalog(
                catalog,
                allowed_models,
                budget_enabled=True,
            )
            snapshot = freeze_catalog_snapshot(catalog, allowed_models)
            self.config.setdefault("configurable", {})[
                "model_catalog_snapshot"
            ] = snapshot
            self.config.setdefault("metadata", {})[
                "run_config_fingerprint"
            ] = run_config_fingerprint(self.config)
            configurable = Configuration.from_runnable_config(self.config)
        validate_model_catalog(
            catalog,
            allowed_models,
            budget_enabled=True,
        )
        snapshot = freeze_catalog_snapshot(catalog, allowed_models)
        install_gateway_token_limits(snapshot)
        self.config.setdefault("configurable", {})[
            "model_context_window_overrides"
        ] = {
            model: int(value["context_window"])
            for model, value in snapshot.items()
        }
        self.config.setdefault("configurable", {})[
            "model_max_output_tokens_overrides"
        ] = {
            model: int(value["max_output_tokens"])
            for model, value in snapshot.items()
        }
        self.config.setdefault("metadata", {})[
            "run_config_fingerprint"
        ] = run_config_fingerprint(self.config)
        store = RunKeySecretStore(configurable.runs_dir, settings.encryption_key)
        admin = LiteLLMKeyAdminClient(settings)
        manager = RunKeyManager(settings, store, admin)
        existing: RunKeyMetadata | None = None
        if self.context_store is not None and self.context_store.manifest_path.exists():
            existing = self.context_store.load_manifest().litellm_key
        else:
            cached_metadata = self.config.get("metadata", {}).get(
                "litellm_run_key_metadata"
            )
            if cached_metadata is not None:
                # Session persistence disabled: the in-memory session is the
                # only place the key identity survives between messages.
                existing = RunKeyMetadata.model_validate(cached_metadata)
        try:
            lease = await manager.ensure(
                run_id=self.run_id,
                requested_budget_micro_usd=configurable.max_run_cost_micro_usd,
                allowed_models=allowed_models,
                metadata=existing,
                team_id=team_id,
            )
        except Exception:
            await manager.aclose()
            raise
        self._litellm_key_manager = manager
        self._litellm_run_key = lease.key
        self.config.setdefault("metadata", {})["litellm_run_key_metadata"] = (
            lease.metadata.model_dump(mode="json")
        )
        if self._litellm_context_token is not None:
            reset_run_key(self._litellm_context_token)
        self._litellm_context_token = bind_run_key(lease.key)
        if self.context_store is not None and self.context_store.manifest_path.exists():
            self.context_store._update_manifest(  # noqa: SLF001
                litellm_key=lease.metadata,
                litellm_key_cleanup_pending=False,
            )

    async def _finalize_litellm_run_key_if_terminal(self) -> None:
        """Block terminal Run Keys while retaining failed cleanup for reconciliation."""
        manager = self._litellm_key_manager
        if manager is None:
            return
        status = self.status
        if self.context_store is not None and self.context_store.manifest_path.exists():
            status = self.context_store.load_manifest().status
        if status not in {"completed", "failed", "cancelled"}:
            return
        spend_micro_usd: int | None = None
        try:
            spend_micro_usd = await manager.authoritative_spend_micro_usd(self.run_id)
        except Exception:  # noqa: BLE001 - terminal key blocking must still proceed
            logger.warning("LiteLLM spend snapshot failed for run %s", self.run_id)
        cleaned = await manager.finalize(self.run_id)
        if self.context_store is not None and self.context_store.manifest_path.exists():
            updates: dict[str, Any] = {"litellm_key_cleanup_pending": not cleaned}
            if spend_micro_usd is not None:
                updates.update(
                    litellm_spend_micro_usd=spend_micro_usd,
                    litellm_spend_updated_at=time.time(),
                )
            self.context_store._update_manifest(**updates)  # noqa: SLF001

    def _unbind_litellm_run_key(self) -> None:
        """Remove plaintext key references from this execution context."""
        if self._litellm_context_token is not None:
            reset_run_key(self._litellm_context_token)
            self._litellm_context_token = None
        self._litellm_run_key = None

    async def _close_litellm_key_manager(self) -> None:
        manager = self._litellm_key_manager
        self._litellm_key_manager = None
        if manager is not None:
            await manager.aclose()

    async def _unregister_sandbox_gateway(self) -> None:
        """Best-effort erasure of the run's in-memory Gateway credential vault."""
        configurable = Configuration.from_runnable_config(self.config)
        if not configurable.sandbox_enabled or self.run_fence_token is None:
            return
        try:
            from open_deep_research.sandbox.gateway_client import (
                SandboxGatewayControlClient,
            )

            await SandboxGatewayControlClient(configurable).unregister_run(
                run_id=self.run_id,
                fence_token=self.run_fence_token,
            )
        except Exception:
            # Capability expiry and Controller termination remain authoritative
            # if Gateway is unavailable during terminal cleanup.
            return

    async def _shutdown_teammates_before_gateway_release(self) -> None:
        """Stop every task container before erasing its Gateway run context."""
        from open_deep_research.agents.deep_researcher import (
            finalize_interrupted_task_snapshots,
        )
        from open_deep_research.tasks.teammate_pool import shutdown_teammate_pool

        cleanup = asyncio.create_task(shutdown_teammate_pool(self.config))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A caller cancellation must not strand a Docker thread/container
            # or erase the Gateway context while that Worker is still alive.
            await cleanup
            raise
        finally:
            try:
                await finalize_interrupted_task_snapshots(
                    self.config,
                    reason="run_terminal",
                )
            except Exception:  # noqa: BLE001 - snapshot close is best-effort cleanup
                logger.exception("failed to finalize interrupted task snapshots")

    async def _lease_heartbeat(self) -> None:
        configurable = Configuration.from_runnable_config(self.config)
        while self.run_fence_token is not None:
            await asyncio.sleep(configurable.leader_heartbeat_seconds)
            token = self.run_fence_token
            if token is None:
                return
            try:
                await self.run_lease.renew(expected_fence_token=token)
            except Exception:  # noqa: BLE001 - ownership is unknown after any renew failure
                if self.cancellation_scope.request("lease_lost"):
                    self.cancelled = True
                    if self.status not in {"completed", "failed", "cancelled"}:
                        self.status = "cancelling"
                return

    async def release_run_lease(self) -> None:
        """Stop heartbeating and release only this engine's current epoch."""
        heartbeat = self._lease_heartbeat_task
        self._lease_heartbeat_task = None
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        from open_deep_research.tasks.team_runtime import team_runtime
        if team_runtime.transport is not None:
            team_runtime.transport.leases.pop(self.run_id, None)
            team_runtime.transport.publishers.pop(self.run_id, None)
        token = self.run_fence_token
        self.run_fence_token = None
        if token is None:
            return
        try:
            await self.run_lease.release(expected_fence_token=token)
        except Exception:  # noqa: BLE001 - the lease will expire after heartbeat stops
            self.persistence_degraded = True

    async def _publish_public(
        self,
        event_type: str,
        *,
        stage: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str,
    ) -> None:
        """Persist a sanitized event before it can be observed over SSE."""
        await event_publisher_from_config(self.config).publish(
            event_type,
            stage=stage,
            payload=payload,
            dedupe_key=dedupe_key,
        )

    async def _public_stage_event(self, stage: str, status: str) -> None:
        index = PUBLIC_STAGES.index(stage) + 1
        await self._publish_public(
            f"stage.{status}",
            stage=stage,
            payload={"stage_id": stage, "stage_index": index, "stage_count": len(PUBLIC_STAGES)},
            dedupe_key=f"stage:{stage}:{status}",
        )

    @staticmethod
    def _public_stage_for_internal(stage: str) -> str:
        if stage in {"summarize_messages", "memory_recall", "clarify_with_user", "write_research_brief"}:
            return "preparing"
        if stage == "plan_approval":
            return "planning"
        if stage.startswith("supervisor.") or stage.startswith("fetch_budget_"):
            return "researching"
        if stage == "outline_approval":
            return "synthesizing"
        if stage in {
            "final_report_generation",
            "report_draft_generated",
            "report_review",
            "report_reviewed",
            "report_revision",
            "report_revised",
            "report_finalize",
            "report_finalized",
        }:
            return "writing"
        return "finalizing"

    async def _publish_public_cancelled(self) -> None:
        termination_reason = (
            self.cancellation_scope.reason
            if self.cancellation_scope.is_cancelled
            else "cancel_requested"
        )
        await self._publish_public(
            "run.cancelled",
            payload={
                "status": "cancelled",
                "termination_reason": termination_reason,
                "result_status": "cancelled",
                "permission_denial_count": len(self.permission_denials),
            },
            dedupe_key="run:terminal",
        )

    def _configure_context_store(self) -> None:
        """Configure the optional run-scoped persistence store."""
        configurable = Configuration.from_runnable_config(self.config)
        if not configurable.query_session_persistence_enabled:
            self.context_store = None
            return
        self.context_store = RunContextStore(
            self.run_id,
            runs_dir=configurable.runs_dir,
            inline_content_max_chars=configurable.query_journal_inline_content_max_chars,
        )
        metadata = self.config.get("metadata", {})
        fence_token = metadata.get("run_fence_token")
        fence_owner_id = metadata.get("run_lease_owner_id")
        if self.run_fence_token is not None and fence_token is not None and fence_owner_id:
            self.context_store.bind_fence_token(
                int(fence_token),
                str(fence_owner_id),
                advance_manifest=False,
            )

    def _validate_resume_manifest(self, manifest: Any) -> None:
        """Reject unauthorized or terminal resumes before acquiring ownership."""
        expected_owner = manifest.owner_id
        current_owner = _config_user_id(self.config)
        if expected_owner and current_owner != expected_owner:
            raise PermissionError("run_owner_mismatch")
        if manifest.status == "completed":
            raise RuntimeError("run_already_completed")
        if manifest.status == "cancelled" or manifest.next_stage == "cancelled":
            raise RuntimeError("run_not_recoverable")

    def _clear_run_resources(self) -> None:
        """Durable V7 approvals require no process-local cleanup."""

    @classmethod
    def load(
        cls,
        run_id: str,
        *,
        runs_dir: str = ".runs",
        config: RunnableConfig | None = None,
        legacy_migration: bool = False,
    ) -> QueryEngine:
        """Load a persisted run shell for explicit recovery."""
        bootstrap_store = RunContextStore(run_id, runs_dir=runs_dir)
        manifest = bootstrap_store.load_manifest()
        if int(manifest.schema_version) < 2:
            raise RunConfigurationError(
                "run_schema_not_resumable:journal_schema_below_2"
            )
        if manifest.coordination_backend not in {"file_mailbox", "rocketmq"}:
            raise JournalCorruptedError("legacy_coordination_backend_not_resumable")
        persisted = manifest.config
        if manifest.coordination_backend == "file_mailbox" and persisted.get("configurable", {}).get("enable_async_research"):
            raise RunConfigurationError("legacy_async_coordination_not_resumable")
        persisted_configurable = {
            key: value
            for key, value in persisted.get("configurable", {}).items()
            if value != "[REDACTED]"
        }
        persisted_metadata = {
            key: value
            for key, value in persisted.get("metadata", {}).items()
            if value != "[REDACTED]"
        }
        supplied_configurable = dict((config or {}).get("configurable", {}))
        supplied_metadata = dict((config or {}).get("metadata", {}))
        frozen = persisted_metadata.get("runtime_config_frozen") is True
        if not frozen:
            del legacy_migration
            raise RunConfigurationError(
                "run_schema_not_resumable:run_config_not_frozen"
            )

        persisted_run_config: RunnableConfig = {
            "configurable": dict(persisted_configurable),
            "metadata": dict(persisted_metadata),
        }
        try:
            persisted_run_config = freeze_run_config(persisted_run_config)
        except ValueError as exc:
            if str(exc).startswith("run_schema_not_resumable:"):
                raise RunConfigurationError(str(exc)) from exc
            raise JournalCorruptedError(str(exc)) from exc
        if (
            manifest.config_fingerprint
            and manifest.config_fingerprint
            != persisted_run_config["metadata"].get("run_config_fingerprint")
        ):
            raise JournalCorruptedError("run_config_fingerprint_mismatch")

        explicitly_frozen = set(supplied_configurable).intersection(
            RUN_CONFIG_FROZEN_FIELDS
        )
        if explicitly_frozen:
            expected = Configuration.from_runnable_config(persisted_run_config)
            candidate_config: RunnableConfig = {
                "configurable": {
                    **persisted_run_config["configurable"],
                    **supplied_configurable,
                },
                "metadata": dict(persisted_run_config["metadata"]),
            }
            candidate = Configuration.from_runnable_config(candidate_config)
            conflicts = sorted(
                field_name
                for field_name in explicitly_frozen
                if getattr(expected, field_name) != getattr(candidate, field_name)
            )
            if conflicts:
                raise RunConfigurationError(
                    "run_config_mismatch:" + ",".join(conflicts)
                )

        protected_metadata = {
            "runtime_config_frozen",
            "run_config_schema_version",
            "run_config_fingerprint",
            "quality_policy_version",
            "quality_evaluation_epoch",
            "quality_rigor_policy",
            "quality_configuration_warnings",
        }
        supplied: RunnableConfig = {
            "configurable": {
                **persisted_configurable,
                **supplied_configurable,
                "thread_id": run_id,
                "runs_dir": runs_dir,
            },
            "metadata": {
                **persisted_metadata,
                **supplied_metadata,
                **{
                    key: value
                    for key, value in persisted_metadata.items()
                    if key in protected_metadata
                },
                "run_id": run_id,
            },
        }
        engine = cls(freeze_run_config(supplied))
        if engine.context_store is None:
            raise JournalCorruptedError("run_context_persistence_disabled")
        return engine

    async def resume(self) -> dict[str, Any]:
        """Resume a persisted run and return its final state."""
        async for _event in self.stream_resume():
            pass
        return self.final_state or {}

    async def _persist_update(
        self,
        *,
        channel: str,
        stage: str,
        update: dict[str, Any],
        scope: str = "main",
        record_type: str = "state_delta",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.context_store is None:
            return
        try:
            payload = {"scope": scope, "update": update, **(extra or {})}
            await self.context_store.append(
                channel=channel,  # type: ignore[arg-type]
                record_type=record_type,  # type: ignore[arg-type]
                stage=stage,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - journal is fail-open after brief
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)

    async def _persist_checkpoint(
        self,
        stage: str,
        next_stage: str,
        *,
        status: str = "running",
        channel: str = "lead",
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.context_store is None:
            return
        try:
            await self.context_store.checkpoint(
                stage,
                next_stage,
                status=status,
                channel=channel,  # type: ignore[arg-type]
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - checkpoint journal is fail-open
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)

    async def _persist_artifact_event(self, stage: str, path: str, sha256: str) -> None:
        if self.context_store is None:
            return
        try:
            await self.context_store.append(
                channel="lead",
                record_type="artifact_committed",
                stage=stage,
                payload={"path": path, "sha256": sha256},
            )
        except Exception as exc:  # noqa: BLE001 - artifact exists even if journal fails
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)

    async def _write_optional_text_artifact(self, stage: str, path: str, content: str) -> None:
        """Write a non-brief artifact using the configured fail-open policy."""
        if self.context_store is None or not content:
            return
        try:
            digest = self.context_store.write_text_atomic(path, content)
            await self._persist_artifact_event(stage, path, digest)
        except Exception as exc:  # noqa: BLE001 - only research_brief.md is strict
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)

    async def _write_optional_json_artifact(
        self,
        stage: str,
        path: str,
        payload: Any,
    ) -> None:
        """Write one JSON artifact using the same fail-open policy as reports."""
        if self.context_store is None or payload in (None, {}, []):
            return
        try:
            digest = self.context_store.write_json_atomic(path, payload)
            await self._persist_artifact_event(stage, path, digest)
        except Exception as exc:  # noqa: BLE001 - final Markdown remains authoritative
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)

    def _ensure_canonical_report(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Return or lazily derive the canonical model for recovery report paths."""
        existing = state.get("canonical_report")
        report_text = str(state.get("final_report") or "")
        if not report_text:
            return None
        # The report orchestrator already builds the canonical model from the
        # final sanitized Markdown.  Reuse that validated payload so the
        # completion path performs one parse, while still rejecting a stale or
        # malformed state payload during legacy recovery.
        try:
            from open_deep_research.report.models import CanonicalReport

            if isinstance(existing, CanonicalReport):
                candidate = existing
            elif isinstance(existing, dict) and existing:
                candidate = CanonicalReport.model_validate(existing)
            else:
                candidate = None
            expected_hash = hashlib.sha256(report_text.encode("utf-8")).hexdigest()
            if (
                candidate is not None
                and candidate.run_id == self.run_id
                and candidate.source_markdown_sha256 == expected_hash
            ):
                payload = candidate.model_dump(mode="json")
                state["canonical_report"] = payload
                return payload
        except Exception:
            # Fall through to the bounded Markdown backfill below.
            pass
        try:
            from open_deep_research.report.canonical import canonicalize_report
            from open_deep_research.report.references import parse_sources_from_text

            configurable = Configuration.from_runnable_config(self.config)
            metadata = self.config.get("metadata", {})
            raw_theme = metadata.get("publication_theme", {})
            locale = (
                raw_theme.get("locale", "zh-CN")
                if isinstance(raw_theme, dict)
                else "zh-CN"
            )
            sources = state.get("sources", [])
            if (
                isinstance(sources, dict)
                and sources.get("type") == "override"
            ):
                sources = sources.get("value", [])
            if not isinstance(sources, list) or not sources:
                sources = parse_sources_from_text(report_text)
            completion = state.get("completion_decision", {})
            if (
                isinstance(completion, dict)
                and completion.get("type") == "override"
            ):
                completion = completion.get("value", {})
            model = canonicalize_report(
                report_text,
                run_id=self.run_id,
                report_type=str(configurable.report_type or "default"),
                completion_status=(
                    "partial"
                    if isinstance(completion, dict)
                    and completion.get("action") == "complete_partial"
                    else "success"
                ),
                locale=str(locale),
                sources=sources,
                fallback_title=str(state.get("research_brief") or "Research Report"),
            )
        except Exception:
            return None
        payload = model.model_dump(mode="json")
        state["canonical_report"] = payload
        return payload

    async def _write_report_bundle(self, state: dict[str, Any]) -> None:
        """Persist Markdown plus its canonical publisher model before completion."""
        report_text = str(state.get("final_report", ""))
        await self._write_optional_text_artifact(
            "report_generated",
            "final_report.md",
            report_text,
        )
        canonical = self._ensure_canonical_report(state)
        await self._write_optional_json_artifact(
            "report_generated",
            "canonical_report.json",
            canonical,
        )

    def _artifact_journal_sha256(self, relative_path: str) -> str | None:
        """Return the latest committed digest recorded for one report artifact."""
        if self.context_store is None:
            return None
        try:
            records = self.context_store._read_records()  # noqa: SLF001
        except Exception:
            # A caller that cannot read the journal will still get the local
            # content/hash check below; the enclosing stage will surface any
            # journal corruption through its normal persistence path.
            return None
        for record in reversed(records):
            if record.record_type != "artifact_committed":
                continue
            payload = record.payload
            if str(payload.get("path") or "") == relative_path:
                digest = str(payload.get("sha256") or "")
                return digest or None
        return None

    def _read_verified_artifact_text(
        self,
        relative_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> str | None:
        """Read one UTF-8 artifact and fail closed on a persisted hash mismatch."""
        if self.context_store is None:
            return None
        try:
            target = self.context_store._resolve_artifact(relative_path)  # noqa: SLF001
            raw = target.read_bytes()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RuntimeError("report_artifact_unavailable") from exc
        actual = hashlib.sha256(raw).hexdigest()
        journal_sha = self._artifact_journal_sha256(relative_path)
        if journal_sha and actual != journal_sha:
            raise RuntimeError("report_artifact_hash_mismatch")
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError("report_artifact_hash_mismatch")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("report_artifact_encoding_invalid") from exc

    def _load_report_review_artifact(
        self,
        *,
        attempt: int,
        draft_sha256: str,
    ) -> dict[str, Any] | None:
        """Recover a hash-matched Reviewer result written before its state delta."""
        relative_path = f"report_reviews/attempt-{attempt:03d}.json"
        content = self._read_verified_artifact_text(relative_path)
        if content is None:
            return None
        try:
            envelope = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("report_review_artifact_invalid") from exc
        if not isinstance(envelope, dict):
            raise RuntimeError("report_review_artifact_invalid")
        try:
            envelope_attempt = int(envelope.get("attempt") or 0)
        except (TypeError, ValueError):
            raise RuntimeError("report_review_artifact_invalid") from None
        envelope_hash = str(envelope.get("draft_sha256") or "")
        # An older artifact for the same attempt may remain after a manually
        # repaired run. It is not safe to apply it to a different draft.
        if envelope_attempt != attempt or envelope_hash != draft_sha256:
            return None
        review = envelope.get("review")
        if not isinstance(review, dict):
            raise RuntimeError("report_review_artifact_invalid")
        payload = dict(review)
        payload.setdefault("attempt", attempt)
        payload.setdefault("draft_sha256", draft_sha256)
        for key in ("model", "policy_version", "evaluation_epoch"):
            if key in envelope and key not in payload:
                payload[key] = envelope[key]
        return payload

    async def _public_event_for_key(self, dedupe_key: str) -> Any | None:
        """Return one durable public event by its idempotency key."""
        publisher = event_publisher_from_config(self.config)
        return await asyncio.to_thread(
            lambda: next(
                (
                    event
                    for event in publisher.store.read()
                    if event.dedupe_key == dedupe_key
                ),
                None,
            )
        )

    async def _ensure_report_completed_event(self, final_text: str) -> bool:
        """Publish ``report.completed`` once and verify an existing hash."""
        digest = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        dedupe_key = "report:completed"
        existing = await self._public_event_for_key(dedupe_key)
        if existing is not None:
            existing_digest = str(existing.payload.get("sha256") or "")
            if existing_digest and existing_digest != digest:
                raise RuntimeError("report_completed_hash_mismatch")
            return False
        await self._publish_public(
            "report.completed",
            stage="writing",
            payload={
                "status": "completed",
                "result_ref": f"/runs/{self.run_id}",
                "sha256": digest,
                "length": len(final_text),
            },
            dedupe_key=dedupe_key,
        )
        return True

    async def _ensure_revision_completed_event(
        self,
        *,
        attempt: int,
        draft_sha256: str,
    ) -> bool:
        """Reconcile a revision completion event after a checkpoint-first crash."""
        dedupe_key = f"report:revision:{attempt}:completed"
        existing = await self._public_event_for_key(dedupe_key)
        if existing is not None:
            existing_digest = str(existing.payload.get("draft_sha256") or existing.payload.get("sha256") or "")
            if existing_digest and existing_digest != draft_sha256:
                raise RuntimeError("report_revision_event_hash_mismatch")
            return False
        await self._publish_public(
            "report.revision.completed",
            stage="writing",
            payload={
                "attempt": attempt,
                "revision_count": attempt,
                "status": "completed",
                "draft_sha256": draft_sha256,
                "sha256": draft_sha256,
            },
            dedupe_key=dedupe_key,
        )
        return True

    # ------------------------------------------------------------------
    # Final-report reviewer lifecycle
    # ------------------------------------------------------------------
    # The report product system is intentionally optional at this boundary.
    # Keeping these adapters here lets older embedders (and persisted v7-v10
    # runs) continue to provide only ``final_report_generation`` while newer
    # report implementations expose the staged draft/review/revise/finalize
    # functions described by the report contract.

    def _report_config_value(self, name: str, default: Any) -> Any:
        """Resolve a report-review option without requiring a new config class."""
        raw_configurable = self.config.get("configurable", {})
        if isinstance(raw_configurable, dict) and name in raw_configurable:
            return raw_configurable[name]
        try:
            resolved = Configuration.from_runnable_config(self.config)
            value = getattr(resolved, name, None)
        except Exception:  # noqa: BLE001 - optional fields may not exist yet
            value = None
        return default if value is None else value

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        """Parse booleans supplied through environment/configuration layers."""
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "enabled"}:
                return True
            if lowered in {"0", "false", "no", "off", "disabled", ""}:
                return False
        return bool(value)

    def _report_review_enabled(self) -> bool:
        """Return whether the opt-in report Reviewer loop is enabled."""
        metadata = self.config.get("metadata", {})
        # Frozen v7-v10 manifests predate this contract.  Do not let a newer
        # process environment silently change their recovery graph.
        try:
            schema_version = int(
                metadata.get(
                    "run_config_schema_version",
                    RUN_CONFIG_SCHEMA_VERSION,
                )
            )
        except (TypeError, ValueError):
            schema_version = RUN_CONFIG_SCHEMA_VERSION
        if (
            metadata.get("runtime_config_frozen") is True
            and schema_version < 11
        ):
            return False
        return self._coerce_bool(
            self._report_config_value("report_review_enabled", False),
            False,
        )

    def _report_review_max_revisions(self) -> int:
        """Return the bounded number of Revisor passes for this run."""
        raw = self._report_config_value("report_review_max_revisions", 1)
        try:
            return max(0, min(3, int(raw)))
        except (TypeError, ValueError):
            return 1

    def _report_review_fail_open(self) -> bool:
        """Return the configured Reviewer failure policy (default fail-open)."""
        return self._coerce_bool(
            self._report_config_value("report_review_fail_open", True),
            True,
        )

    @staticmethod
    def _report_callable(name: str) -> Callable[..., Any] | None:
        """Resolve a staged report function from package or orchestrator exports."""
        try:
            import open_deep_research.report as report_package

            candidate = getattr(report_package, name, None)
            if callable(candidate):
                return cast(Callable[..., Any], candidate)
        except Exception:  # noqa: BLE001 - legacy report package may be minimal
            pass
        try:
            from open_deep_research.report import orchestrator

            candidate = getattr(orchestrator, name, None)
            if callable(candidate):
                return cast(Callable[..., Any], candidate)
        except Exception:  # noqa: BLE001 - staged APIs are optional on old builds
            return None
        return None

    @staticmethod
    async def _await_report_callable(
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call a report stage while accepting sync test doubles as well as coroutines."""
        value = function(*args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    async def _invoke_review_callable(
        function: Callable[..., Any],
        draft: Any,
        state: dict[str, Any],
        config: RunnableConfig,
        attempt: int,
    ) -> Any:
        """Invoke Reviewer doubles with the optional ``attempt`` keyword."""
        try:
            parameters = inspect.signature(function).parameters
            accepts_attempt = "attempt" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            accepts_attempt = False
        if accepts_attempt:
            return await QueryEngine._await_report_callable(
                function,
                draft,
                state,
                config,
                attempt=attempt,
            )
        return await QueryEngine._await_report_callable(function, draft, state, config)

    @staticmethod
    def _model_to_payload(value: Any) -> Any:
        """Convert Pydantic/dataclass-like report values to JSON-compatible mappings."""
        if value is None:
            return None
        dumper = getattr(value, "model_dump", None)
        if callable(dumper):
            try:
                return dumper(mode="json")
            except TypeError:
                return dumper()
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "__dict__"):
            try:
                return dict(vars(value))
            except TypeError:
                pass
        return value

    @classmethod
    def _report_payload_sha256(cls, value: Any) -> str:
        """Return a stable digest for one JSON-compatible report payload."""
        payload = cls._model_to_payload(value)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _report_draft_text(cls, draft: Any, fallback: str = "") -> str:
        """Extract canonical Markdown from any supported ReportDraft shape."""
        if isinstance(draft, str):
            return draft
        payload = cls._model_to_payload(draft)
        if isinstance(payload, dict):
            for key in (
                "body_markdown",
                "markdown",
                "final_report",
                "content",
                "text",
            ):
                value = payload.get(key)
                if value is not None:
                    return str(value)
        return fallback

    @classmethod
    def _draft_with_text(cls, draft: Any, text: str) -> Any:
        """Return a draft value carrying a revised canonical Markdown body."""
        copier = getattr(draft, "model_copy", None)
        if callable(copier):
            try:
                return copier(update={"body_markdown": text, "markdown": text})
            except Exception:  # noqa: BLE001 - fall back to a plain mapping
                pass
        payload = cls._model_to_payload(draft)
        if isinstance(payload, dict):
            updated = dict(payload)
            # ReportDraft implementations use body_markdown; retaining the
            # aliases makes this adapter friendly to small downstream plugins.
            updated["body_markdown"] = text
            updated["markdown"] = text
            return updated
        return {"body_markdown": text, "markdown": text}

    @staticmethod
    def _review_issues(review: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract issue records from the accepted Reviewer protocol aliases."""
        for key in ("issues", "findings", "problems", "review_issues"):
            value = review.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @classmethod
    def _review_gate(
        cls,
        review: dict[str, Any],
    ) -> tuple[str, int, int, bool]:
        """Compute a deterministic Reviewer gate from protocol output.

        The model's decision is advisory.  Explicit hard-failure markers,
        missing coverage, invalid evidence bindings, and scores below the
        frozen outer policy can turn an optimistic model decision into a
        revision decision.  The fourth return value indicates a blocking
        failure that cannot be safely degraded after the revision budget.
        """
        issues = cls._review_issues(review)
        critical_categories = {
            "coverage",
            "citation_correctness",
            "contradictions",
            "contradiction",
            "unsupported_claims",
            "unsupported_claim",
        }
        critical_count = sum(
            str(item.get("severity", "")).strip().lower()
            in {"high", "critical", "blocking", "error"}
            and str(item.get("category", "")).strip().lower()
            in critical_categories
            for item in issues
        )
        raw_hard_failures = review.get("hard_failures")
        hard_failure_codes = {
            str(item)
            for item in raw_hard_failures
            if str(item)
        } if isinstance(raw_hard_failures, list) else set()
        aggregate_only = bool(hard_failure_codes) and hard_failure_codes <= {
            "score_below_aggregate_floor"
        }
        hard_failure = bool(
            (review.get("hard_failure") and not aggregate_only)
            or review.get("deterministic_failure")
            or review.get("deterministic_failures")
            or (hard_failure_codes - {"score_below_aggregate_floor"})
            or review.get("protocol_error")
            or review.get("unsupported_claims_blocking")
        )
        if review.get("skipped"):
            # Fail-open is limited to Reviewer availability. A caller cannot
            # combine ``skipped`` with deterministic/protocol failures to
            # bypass the report gate.
            return (
                "fail" if hard_failure else "pass",
                len(issues),
                critical_count if hard_failure else 0,
                hard_failure,
            )
        coverage = review.get("coverage") or review.get("coverage_reviews")
        if isinstance(coverage, list):
            missing = [
                item
                for item in coverage
                if isinstance(item, dict)
                and str(item.get("status", item.get("coverage", ""))).lower()
                in {"missing", "unsupported"}
            ]
            if missing:
                hard_failure = True
        citation_reviews = review.get("citation_audit") or review.get("citations")
        if isinstance(citation_reviews, list) and any(
            isinstance(item, dict)
            and item.get("supported") is False
            and str(item.get("severity", "")).lower() in {"critical", "blocking", "error"}
            for item in citation_reviews
        ):
            hard_failure = True

        scores = (
            review.get("dimension_scores")
            or review.get("scores")
            or review.get("dimensions")
            or {}
        )
        scores = cls._model_to_payload(scores)
        if not isinstance(scores, dict):
            scores = {}
        policy = review.get("quality_thresholds")
        if not isinstance(policy, dict):
            policy = {}
        # The frozen quality policy is normally attached by report core.  A
        # conservative fallback is only applied when scores are actually
        # supplied, preserving compatibility with simple test doubles.
        try:
            critical_floor = float(policy.get("outer_critical_floor", 0.70))
            aggregate_floor = float(policy.get("outer_aggregate_floor", 0.75))
        except (TypeError, ValueError):
            critical_floor, aggregate_floor = 0.70, 0.75
        critical_dimensions = (
            "coverage",
            "citation_correctness",
            "contradictions",
            "unsupported_claims",
        )
        present_scores: list[float] = []
        for name in (
            "coverage",
            "citation_correctness",
            "contradictions",
            "unsupported_claims",
            "redundancy",
            "executive_readability",
        ):
            value = scores.get(name)
            if value is None:
                continue
            try:
                present_scores.append(float(value))
            except (TypeError, ValueError):
                continue
        if any(name in scores for name in critical_dimensions):
            for name in critical_dimensions:
                try:
                    if float(scores[name]) < critical_floor:
                        hard_failure = True
                except (KeyError, TypeError, ValueError):
                    hard_failure = True
        aggregate_below_floor = bool(
            present_scores
            and sum(present_scores) / len(present_scores) < aggregate_floor
        )

        raw_decision = str(
            review.get("decision")
            or review.get("status")
            or review.get("verdict")
            or ""
        ).strip().lower()
        if raw_decision not in {"pass", "revise", "fail"}:
            raw_decision = (
                "revise"
                if (issues or hard_failure or aggregate_below_floor)
                else "pass"
            )
        if raw_decision == "pass" and (
            hard_failure or critical_count or aggregate_below_floor
        ):
            raw_decision = "revise"
        return raw_decision, len(issues), critical_count, hard_failure

    @classmethod
    def _review_summary(cls, review: Any) -> dict[str, Any]:
        """Return the bounded report-review projection safe for run output."""
        payload = cls._model_to_payload(review)
        if not isinstance(payload, dict):
            return {}
        decision, issue_count, critical_count, hard_failure = cls._review_gate(payload)
        summary: dict[str, Any] = {
            "schema_version": payload.get("schema_version", 1),
            "decision": decision,
            "status": payload.get("status") or ("passed" if decision == "pass" else decision),
            "attempt": payload.get("attempt"),
            "issue_count": issue_count,
            "critical_issue_count": critical_count,
            "hard_failure": hard_failure,
            "draft_sha256": payload.get("draft_sha256"),
        }
        dimensions = payload.get("dimensions") or payload.get("dimension_scores")
        if isinstance(dimensions, dict):
            safe_dimensions = {
                key: float(value)
                for key, value in dimensions.items()
                if key
                in {
                    "coverage",
                    "citation_correctness",
                    "contradictions",
                    "unsupported_claims",
                    "redundancy",
                    "executive_readability",
                }
                and isinstance(value, int | float)
                and not isinstance(value, bool)
            }
            if safe_dimensions:
                summary["dimensions"] = safe_dimensions
        for key in ("policy_version", "evaluation_epoch"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                summary[key] = value
        if payload.get("degraded") or payload.get("skipped"):
            summary["degraded"] = bool(payload.get("degraded") or payload.get("skipped"))
        return {key: value for key, value in summary.items() if value is not None}

    async def _persist_report_review_artifact(
        self,
        *,
        attempt: int,
        review: dict[str, Any],
        draft_sha256: str,
    ) -> str | None:
        """Persist one redacted Reviewer record and return its artifact digest."""
        if self.context_store is None:
            return None
        metadata = self.config.get("metadata", {})
        envelope = {
            "schema_version": 1,
            "attempt": attempt,
            "draft_sha256": draft_sha256,
            "review": review,
            "model": self._report_config_value(
                "report_review_model",
                self._report_config_value("quality_evaluation_model", None),
            ),
            "policy_version": metadata.get("quality_policy_version"),
            "evaluation_epoch": metadata.get("quality_evaluation_epoch"),
        }
        path = f"report_reviews/attempt-{attempt:03d}.json"
        try:
            digest = self.context_store.write_json_atomic(path, envelope)
            await self._persist_artifact_event("report_reviewed", path, digest)
            return digest
        except Exception as exc:  # noqa: BLE001 - review artifacts are fail-open
            self.persistence_degraded = True
            self.context_store.mark_persistence_degraded(exc)
            return None

    def _load_report_draft_artifact(self, state: dict[str, Any]) -> str:
        """Recover a draft body from the durable context when journal state is sparse."""
        draft = str(state.get("final_report_draft") or "")
        if self.context_store is None:
            return draft
        try:
            revision_count = int(state.get("report_revision_count") or 0)
        except (TypeError, ValueError):
            revision_count = 0
        metadata = state.get("report_draft_metadata")
        provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
        is_recovery = bool(
            isinstance(provenance, dict)
            and provenance.get("report_review_recovery")
        )
        expected_state_hash = (
            str(metadata.get("sha256") or "")
            if isinstance(metadata, dict)
            else ""
        )
        if draft:
            draft_hash = hashlib.sha256(draft.encode("utf-8")).hexdigest()
            if expected_state_hash and expected_state_hash != draft_hash:
                raise RuntimeError("report_draft_hash_mismatch")
            # When the state delta and artifact are both present, require them
            # to describe the same bytes. A missing artifact is tolerated for a
            # degraded journal, because the durable state remains recoverable.
            relative = (
                "report_drafts/evidence-limited-recovery.md"
                if is_recovery
                else f"report_drafts/revision-{revision_count:03d}.md"
                if revision_count > 0
                else "final_draft.md"
            )
            artifact = self._read_verified_artifact_text(relative)
            if artifact is not None:
                artifact_hash = hashlib.sha256(artifact.encode("utf-8")).hexdigest()
                if artifact_hash != draft_hash:
                    raise RuntimeError("report_draft_hash_mismatch")
                return artifact
            return draft
        candidates = (
            ["report_drafts/evidence-limited-recovery.md"]
            if is_recovery
            else [f"report_drafts/revision-{revision_count:03d}.md", "final_draft.md"]
            if revision_count > 0
            else ["final_draft.md"]
        )
        for relative in candidates:
            artifact = self._read_verified_artifact_text(relative)
            if artifact is not None:
                return artifact
        return ""

    def _load_report_draft_object(self, state: dict[str, Any], text: str) -> Any:
        """Rehydrate ReportDraft metadata after a process-level resume."""
        metadata = state.get("report_draft_metadata")
        if isinstance(metadata, dict) and metadata:
            payload = dict(metadata)
            payload.setdefault("markdown", text)
            payload.setdefault("body_markdown", text)
            expected = str(payload.get("sha256") or "")
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if expected and expected != actual:
                raise RuntimeError("report_draft_hash_mismatch")
            return payload
        if self.context_store is not None:
            content = self._read_verified_artifact_text("final_draft.json")
            if content is not None:
                try:
                    raw = json.loads(content)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("report_draft_metadata_invalid") from exc
                if isinstance(raw, dict):
                    payload = raw.get("draft", raw)
                    if isinstance(payload, dict):
                        payload = dict(payload)
                        payload.setdefault("markdown", text)
                        payload.setdefault("body_markdown", text)
                        for key in (
                            "attempt",
                            "model",
                            "policy_version",
                            "evaluation_epoch",
                        ):
                            if key in raw and key not in payload:
                                payload[key] = raw[key]
                        expected = str(payload.get("sha256") or "")
                        envelope_expected = str(raw.get("draft_sha256") or "")
                        actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        if (expected and expected != actual) or (
                            envelope_expected and envelope_expected != actual
                        ):
                            raise RuntimeError("report_draft_hash_mismatch")
                        payload.setdefault("sha256", actual)
                        return payload
                raise RuntimeError("report_draft_metadata_invalid")
        return {"markdown": text, "body_markdown": text}

    def _load_report_revision_artifact(
        self,
        revision_count: int,
        *,
        source_draft_sha256: str,
        source_review_attempt: int,
        source_review_sha256: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Load a revision artifact bound to the draft and review that produced it."""
        if self.context_store is None or revision_count <= 0:
            return "", None
        markdown_path = f"report_drafts/revision-{revision_count:03d}.md"
        metadata_path = f"report_drafts/revision-{revision_count:03d}.json"
        metadata_content = self._read_verified_artifact_text(metadata_path)
        # The binding envelope is the first durable commit after the Revisor
        # returns and contains the complete Markdown. It can therefore recover
        # the narrow crash window before the presentation-friendly .md copy.
        if metadata_content is None:
            return "", None
        try:
            raw = json.loads(metadata_content)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("report_revision_metadata_invalid") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("report_revision_metadata_invalid")
        try:
            envelope_attempt = int(raw.get("attempt") or 0)
            envelope_review_attempt = int(raw.get("source_review_attempt") or 0)
        except (TypeError, ValueError):
            raise RuntimeError("report_revision_metadata_invalid") from None
        if (
            envelope_attempt != revision_count
            or str(raw.get("source_draft_sha256") or "")
            != source_draft_sha256
            or envelope_review_attempt != source_review_attempt
            or str(raw.get("source_review_sha256") or "")
            != source_review_sha256
        ):
            return "", None
        candidate = raw.get("draft")
        if not isinstance(candidate, dict):
            raise RuntimeError("report_revision_metadata_invalid")
        metadata = dict(candidate)
        envelope_text = self._report_draft_text(metadata, "")
        text = self._read_verified_artifact_text(markdown_path)
        if text is None:
            text = envelope_text
        elif envelope_text and envelope_text != text:
            raise RuntimeError("report_revision_hash_mismatch")
        if not text:
            raise RuntimeError("report_revision_metadata_invalid")
        envelope_hash = str(raw.get("draft_sha256") or "")
        if envelope_hash and envelope_hash != hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest():
            raise RuntimeError("report_revision_hash_mismatch")
        for key in (
            "attempt",
            "model",
            "policy_version",
            "evaluation_epoch",
            "source_draft_sha256",
            "source_review_attempt",
            "source_review_sha256",
        ):
            if key in raw and key not in metadata:
                metadata[key] = raw[key]
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected_hash = str(metadata.get("sha256") or "")
        if expected_hash and expected_hash != actual_hash:
            raise RuntimeError("report_revision_hash_mismatch")
        metadata.setdefault("sha256", actual_hash)
        metadata.setdefault("attempt", revision_count)
        metadata.setdefault("markdown", text)
        metadata.setdefault("body_markdown", text)
        return text, metadata

    def _load_report_recovery_artifact(
        self,
        *,
        source_draft_sha256: str,
        source_review_attempt: int,
        source_review_sha256: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Load a recovery bound to the blocked draft and review that caused it."""
        if self.context_store is None:
            return "", None
        markdown_path = "report_drafts/evidence-limited-recovery.md"
        metadata_content = self._read_verified_artifact_text(
            "report_drafts/evidence-limited-recovery.json"
        )
        # As with revision artifacts, the source binding is mandatory and is
        # sufficient to recover the crash window before the .md copy exists.
        if metadata_content is None:
            return "", None
        try:
            raw = json.loads(metadata_content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("report_recovery_metadata_invalid") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("report_recovery_metadata_invalid")
        try:
            envelope_review_attempt = int(raw.get("source_review_attempt") or 0)
        except (TypeError, ValueError):
            raise RuntimeError("report_recovery_metadata_invalid") from None
        if (
            str(raw.get("source_draft_sha256") or "")
            != source_draft_sha256
            or envelope_review_attempt != source_review_attempt
            or str(raw.get("source_review_sha256") or "")
            != source_review_sha256
        ):
            return "", None
        candidate = raw.get("draft")
        if not isinstance(candidate, dict):
            raise RuntimeError("report_recovery_metadata_invalid")
        metadata = dict(candidate)
        envelope_text = self._report_draft_text(metadata, "")
        text = self._read_verified_artifact_text(markdown_path)
        if text is None:
            text = envelope_text
        elif envelope_text and envelope_text != text:
            raise RuntimeError("report_recovery_hash_mismatch")
        if not text:
            raise RuntimeError("report_recovery_metadata_invalid")
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        envelope_hash = str(raw.get("draft_sha256") or "")
        expected_hash = str(metadata.get("sha256") or "")
        if (envelope_hash and envelope_hash != actual_hash) or (
            expected_hash and expected_hash != actual_hash
        ):
            raise RuntimeError("report_recovery_hash_mismatch")
        provenance = metadata.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get(
            "report_review_recovery"
        ):
            raise RuntimeError("report_recovery_metadata_invalid")
        metadata["sha256"] = actual_hash
        metadata["markdown"] = text
        metadata["body_markdown"] = text
        return text, metadata

    @classmethod
    def _is_report_review_recovery(
        cls,
        draft: Any,
        state: dict[str, Any],
    ) -> bool:
        """Return whether the current draft is the one terminal recovery draft."""
        candidates = [cls._model_to_payload(draft), state.get("report_draft_metadata")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            provenance = candidate.get("provenance")
            if isinstance(provenance, dict) and provenance.get(
                "report_review_recovery"
            ):
                return True
        return False

    @classmethod
    def _report_review_reason_codes(cls, review: dict[str, Any]) -> list[str]:
        """Return bounded, stable reason codes for evidence-limited recovery."""
        values: list[str] = []
        for key in ("hard_failures", "deterministic_failures"):
            raw = review.get(key)
            if isinstance(raw, list):
                values.extend(str(item)[:200] for item in raw if str(item))
        values.extend(
            f"review_issue:{str(issue.get('category') or 'other')[:120]}"
            for issue in cls._review_issues(review)
            if str(issue.get("severity") or "").lower()
            in {"high", "critical", "blocking", "error"}
        )
        return list(dict.fromkeys(values))[:100]

    @classmethod
    def _report_review_has_recoverable_content_failure(
        cls,
        review: dict[str, Any],
        *,
        critical_count: int,
        hard_failure: bool,
    ) -> bool:
        """Separate report defects from Reviewer availability/protocol defects."""
        hard = {
            str(item)
            for item in review.get("hard_failures", [])
            if str(item)
        } if isinstance(review.get("hard_failures"), list) else set()
        deterministic = {
            str(item)
            for item in review.get("deterministic_failures", [])
            if str(item)
        } if isinstance(review.get("deterministic_failures"), list) else set()
        if "report_reviewer_unavailable" in hard:
            return False
        protocol_failures = {
            "review_protocol_invalid",
            "review_dimensions_missing",
            "coverage_contract_rows_missing_or_invalid",
            "citation_audit_missing",
        }
        content_codes = {
            code
            for code in hard | deterministic
            if code not in protocol_failures
            and code != "report_reviewer_unavailable"
            and code != "score_below_aggregate_floor"
            and not code.startswith("review_issue:other")
        }
        if deterministic and deterministic <= protocol_failures and not content_codes:
            return False
        # A critical-dimension score can block the gate without producing an
        # issue row. Treat that as a report-content failure after excluding the
        # availability/protocol-only cases above.
        return bool(critical_count or content_codes or hard_failure)

    async def _mark_report_review_insufficient_evidence(
        self,
        state: dict[str, Any],
        review: dict[str, Any],
        *,
        reason_codes: list[str],
    ) -> None:
        """Persist the terminal report-review failure before raising it."""
        failed_review = {
            **review,
            "decision": "fail",
            "gate_decision": "fail",
            "status": "failed",
            "hard_failure": True,
            "error_code": "insufficient_evidence",
        }
        history = state.get("report_review_history", [])
        if not isinstance(history, list):
            history = []
        if history and isinstance(history[-1], dict) and history[-1].get(
            "attempt"
        ) == failed_review.get("attempt"):
            history = [*history[:-1], failed_review]
        else:
            history = [*history, failed_review]
        completion = {
            "action": "fail",
            "reason": "insufficient_evidence",
            "gaps": reason_codes,
        }
        state["report_review"] = failed_review
        state["report_review_history"] = history
        state["completion_decision"] = completion
        await self._persist_update(
            channel="lead",
            stage="report_review_failed",
            update={
                "report_review": failed_review,
                "report_review_history": history,
                "completion_decision": {"type": "override", "value": completion},
            },
            extra={"error_code": "insufficient_evidence"},
        )

    async def _recover_report_review_draft(
        self,
        *,
        draft: Any,
        state: dict[str, Any],
        review: dict[str, Any],
    ) -> tuple[Any, str, str]:
        """Create or recover exactly one accepted-evidence-only report draft."""
        reason_codes = self._report_review_reason_codes(review)
        source_text = self._report_draft_text(
            draft,
            str(state.get("final_report_draft") or ""),
        )
        if not source_text:
            raise RuntimeError("report_draft_missing")
        source_draft_sha256 = hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest()
        try:
            source_review_attempt = int(review.get("attempt") or 0)
        except (TypeError, ValueError):
            source_review_attempt = 0
        source_review_sha256 = self._report_payload_sha256(review)
        artifact_text, artifact_metadata = self._load_report_recovery_artifact(
            source_draft_sha256=source_draft_sha256,
            source_review_attempt=source_review_attempt,
            source_review_sha256=source_review_sha256,
        )
        if artifact_text:
            recovered_obj: Any = artifact_metadata or self._draft_with_text(
                draft,
                artifact_text,
            )
        else:
            recover_fn = self._report_callable("recover_report_draft")
            if recover_fn is None:
                await self._mark_report_review_insufficient_evidence(
                    state,
                    review,
                    reason_codes=reason_codes,
                )
                raise RuntimeError("insufficient_evidence")
            try:
                recovered_obj = await self.cancellation_scope.run(
                    self._await_report_callable(
                        recover_fn,
                        draft,
                        state,
                        self.config,
                        reason_codes=reason_codes,
                    ),
                    stage="report_revision",
                )
            except RunCancelled:
                raise
            except Exception as exc:
                await self._mark_report_review_insufficient_evidence(
                    state,
                    review,
                    reason_codes=reason_codes,
                )
                raise RuntimeError("insufficient_evidence") from exc
            artifact_text = self._report_draft_text(recovered_obj, "")
            if not artifact_text:
                await self._mark_report_review_insufficient_evidence(
                    state,
                    review,
                    reason_codes=reason_codes,
                )
                raise RuntimeError("insufficient_evidence")

        payload = self._model_to_payload(recovered_obj)
        if not isinstance(payload, dict):
            payload = self._model_to_payload(
                self._draft_with_text(draft, artifact_text)
            )
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload["markdown"] = artifact_text
        payload["body_markdown"] = artifact_text
        digest = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
        payload["sha256"] = digest
        provenance = payload.get("provenance")
        provenance = dict(provenance) if isinstance(provenance, dict) else {}
        provenance.update(
            {
                "source": "report_review_evidence_limited_recovery",
                "report_review_recovery": True,
                "recovery_reason_codes": reason_codes,
                "recovery_source_draft_sha256": source_draft_sha256,
                "recovery_source_review_attempt": source_review_attempt,
                "recovery_source_review_sha256": source_review_sha256,
            }
        )
        payload["provenance"] = provenance
        completion = {
            "action": "complete_partial",
            "reason": "report_review_evidence_limited_recovery",
            "gaps": reason_codes,
        }
        finalization = payload.get("finalization")
        finalization = dict(finalization) if isinstance(finalization, dict) else {}
        finalization["completion_decision"] = {
            "type": "override",
            "value": completion,
        }
        payload["finalization"] = finalization
        recovered_obj = payload

        if self.context_store is not None:
            try:
                metadata_sha = self.context_store.write_json_atomic(
                    "report_drafts/evidence-limited-recovery.json",
                    {
                        "schema_version": 1,
                        "draft_sha256": digest,
                        "source_draft_sha256": source_draft_sha256,
                        "source_review_attempt": source_review_attempt,
                        "source_review_sha256": source_review_sha256,
                        "model": self._report_config_value("final_report_model", None),
                        "policy_version": self.config.get("metadata", {}).get(
                            "quality_policy_version"
                        ),
                        "evaluation_epoch": self.config.get("metadata", {}).get(
                            "quality_evaluation_epoch"
                        ),
                        "draft": payload,
                    },
                )
                await self._persist_artifact_event(
                    "report_recovered",
                    "report_drafts/evidence-limited-recovery.json",
                    metadata_sha,
                )
            except Exception as exc:  # noqa: BLE001 - Markdown remains recoverable
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
        await self._write_optional_text_artifact(
            "report_recovered",
            "report_drafts/evidence-limited-recovery.md",
            artifact_text,
        )

        state["final_report_draft"] = artifact_text
        state["report_draft_metadata"] = payload
        state["completion_decision"] = completion
        await self._persist_update(
            channel="lead",
            stage="report_recovered",
            update={
                "final_report_draft": artifact_text,
                "report_draft_metadata": payload,
                "completion_decision": {"type": "override", "value": completion},
            },
            extra={"draft_sha256": digest, "reason_codes": reason_codes},
        )
        return recovered_obj, artifact_text, digest

    async def _stream_report_pipeline(
        self,
        state: dict[str, Any],
        start_stage: str,
        recorder: Any,
        graph: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run the staged report lifecycle from a durable internal boundary.

        This generator deliberately owns no terminal run bookkeeping.  It
        leaves that to :meth:`_finish_success`, after the final report has been
        persisted and the one ``report.completed`` event has been emitted.
        """
        if not self._report_review_enabled():
            return

        stage = start_stage
        draft_obj: Any = None
        draft_text = self._load_report_draft_artifact(state)
        if draft_text:
            draft_obj = self._load_report_draft_object(state, draft_text)
        draft_sha256 = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()

        # ``report.started`` is idempotent in the public event store, so it is
        # safe to repeat this call when a process resumes after a crash.
        await self._publish_public(
            "report.started",
            stage="writing",
            payload={"status": "running"},
            dedupe_key="report:started",
        )

        while stage in {
            "final_report_generation",
            "report_review",
            "report_revision",
            "report_finalize",
        }:
            if stage == "final_report_generation":
                build_draft = self._report_callable("build_report_draft")
                try:
                    # The artifact/state write precedes the stage checkpoint.
                    # A crash in that interval must reuse the exact draft rather
                    # than invoking the writer a second time.
                    if draft_text and draft_obj is not None:
                        pass
                    elif build_draft is None:
                        # Compatibility path for report packages predating the
                        # staged API.  Existing tests and embedders monkeypatch
                        # this graph node, so preserve its exact call contract.
                        with recorder.start_span(
                            name="node.final_report_generation",
                            kind="agent",
                            agent_role="lead",
                        ):
                            raw_draft = await self.cancellation_scope.run(
                                graph.final_report_generation(state, self.config),
                                stage="final_report_generation",
                            )
                        draft_obj = raw_draft
                        raw_payload = self._model_to_payload(raw_draft)
                        if isinstance(raw_payload, dict):
                            apply_update_to_state(state, raw_payload)
                    else:
                        with recorder.start_span(
                            name="node.final_report_draft",
                            kind="agent",
                            agent_role="lead",
                        ):
                            draft_obj = await self.cancellation_scope.run(
                                self._await_report_callable(
                                    build_draft,
                                    state,
                                    self.config,
                                ),
                                stage="final_report_generation",
                            )
                    draft_text = self._report_draft_text(
                        draft_obj,
                        str(state.get("final_report") or ""),
                    )
                except RunCancelled:
                    raise
                except Exception:
                    # A draft generation failure is never safe to turn into a
                    # successful report.  The outer loop records the failed
                    # stage and preserves the last stable checkpoint.
                    raise
                if not draft_text:
                    raise RuntimeError("report_draft_empty")
                state["final_report_draft"] = draft_text
                draft_payload = self._model_to_payload(draft_obj)
                if not isinstance(draft_payload, dict):
                    draft_payload = {
                        "markdown": draft_text,
                        "body_markdown": draft_text,
                    }
                else:
                    draft_payload = dict(draft_payload)
                    draft_payload["markdown"] = draft_text
                    draft_payload["body_markdown"] = draft_text
                state["report_draft_metadata"] = draft_payload
                # A legacy update may have populated final_report; clear it
                # until the reviewer-approved version is finalized.
                state["final_report"] = ""
                draft_sha256 = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
                draft_payload["sha256"] = draft_sha256
                draft_payload.setdefault("attempt", 0)
                draft_payload.setdefault(
                    "model",
                    self._report_config_value("final_report_model", None),
                )
                draft_payload.setdefault(
                    "policy_version",
                    self.config.get("metadata", {}).get("quality_policy_version"),
                )
                draft_payload.setdefault(
                    "evaluation_epoch",
                    self.config.get("metadata", {}).get("quality_evaluation_epoch"),
                )
                state["report_draft_metadata"] = draft_payload
                await self._write_optional_text_artifact(
                    "report_draft_generated",
                    "final_draft.md",
                    draft_text,
                )
                if self.context_store is not None:
                    try:
                        draft_metadata_sha = self.context_store.write_json_atomic(
                            "final_draft.json",
                            {
                                "schema_version": 1,
                                "attempt": 0,
                                "draft_sha256": draft_sha256,
                                "model": draft_payload.get("model"),
                                "policy_version": draft_payload.get("policy_version"),
                                "evaluation_epoch": draft_payload.get(
                                    "evaluation_epoch"
                                ),
                                "draft": draft_payload,
                            },
                        )
                        await self._persist_artifact_event(
                            "report_draft_generated",
                            "final_draft.json",
                            draft_metadata_sha,
                        )
                    except Exception as exc:  # noqa: BLE001 - metadata is advisory
                        self.persistence_degraded = True
                        self.context_store.mark_persistence_degraded(exc)
                await self._persist_update(
                    channel="lead",
                    stage="report_draft_generated",
                    update={
                        "final_report_draft": draft_text,
                        "final_report": "",
                        "report_draft_metadata": draft_payload,
                    },
                    extra={
                        "draft_sha256": draft_sha256,
                        "attempt": 0,
                    },
                )
                await self._persist_checkpoint(
                    "report_draft_generated",
                    "report_review",
                    payload={"draft_sha256": draft_sha256, "attempt": 0},
                )
                yield self._event(
                    "report.draft_generated",
                    {
                        "run_id": self.run_id,
                        "sha256": draft_sha256,
                        "length": len(draft_text),
                    },
                )
                stage = "report_review"

            if stage == "report_review":
                draft_text = self._load_report_draft_artifact(state) or draft_text
                if not draft_text:
                    raise RuntimeError("report_draft_missing")
                if draft_obj is None:
                    draft_obj = {"body_markdown": draft_text, "markdown": draft_text}
                draft_sha256 = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
                try:
                    current_revision_count = int(
                        state.get("report_revision_count") or 0
                    )
                except (TypeError, ValueError):
                    current_revision_count = 0
                current_is_recovery = self._is_report_review_recovery(
                    draft_obj,
                    state,
                )
                if current_revision_count > 0 and not current_is_recovery:
                    revision_event_created = await self._ensure_revision_completed_event(
                        attempt=current_revision_count,
                        draft_sha256=draft_sha256,
                    )
                    if revision_event_created:
                        yield self._event(
                            "report.revision.completed",
                            {
                                "run_id": self.run_id,
                                "attempt": current_revision_count,
                                "revision_count": current_revision_count,
                                "status": "completed",
                                "draft_sha256": draft_sha256,
                                "sha256": draft_sha256,
                            },
                        )
                history = state.get("report_review_history", [])
                if not isinstance(history, list):
                    history = []
                attempt = len(history) + 1
                review_fn = self._report_callable("review_report")
                review_payload: dict[str, Any] = {}
                review_error: Exception | None = None
                review_already_in_history = False
                # If the process died after persisting the review delta but
                # before its stage checkpoint, replay points back at
                # ``report_review``.  Reuse the hash-matched review instead of
                # charging a second Reviewer call.
                existing_review = state.get("report_review")
                latest_history = history[-1] if history else None
                reuse_review = bool(
                    isinstance(existing_review, dict)
                    and isinstance(latest_history, dict)
                    and str(existing_review.get("draft_sha256") or "")
                    == draft_sha256
                    and str(latest_history.get("draft_sha256") or "")
                    == draft_sha256
                    and existing_review.get("attempt")
                    == latest_history.get("attempt")
                )
                if reuse_review:
                    review_already_in_history = True
                    review_payload = dict(cast(dict[str, Any], existing_review))
                    try:
                        attempt = int(review_payload.get("attempt") or len(history))
                    except (TypeError, ValueError):
                        attempt = len(history)
                if not reuse_review:
                    # The review JSON is committed before its state delta. A
                    # process crash in that narrow window leaves no replay
                    # history, so recover the hash/attempt-matched artifact.
                    artifact_review = self._load_report_review_artifact(
                        attempt=attempt,
                        draft_sha256=draft_sha256,
                    )
                    if artifact_review is not None:
                        review_payload = artifact_review
                        reuse_review = True
                await self._publish_public(
                    "report.review.started",
                    stage="writing",
                    payload={
                        "attempt": attempt,
                        "revision_count": int(state.get("report_revision_count") or 0),
                        "status": "running",
                        "draft_sha256": draft_sha256,
                        "sha256": draft_sha256,
                    },
                    dedupe_key=f"report:review:{attempt}:started",
                )
                yield self._event(
                    "report.review.started",
                    {
                        "run_id": self.run_id,
                        "attempt": attempt,
                        "revision_count": int(state.get("report_revision_count") or 0),
                        "status": "running",
                        "draft_sha256": draft_sha256,
                        "sha256": draft_sha256,
                    },
                )
                try:
                    if reuse_review:
                        # The durable state already contains the normalized
                        # Reviewer result; no model invocation is needed.
                        pass
                    elif review_fn is None:
                        raise RuntimeError("report_reviewer_unavailable")
                    else:
                        with recorder.start_span(
                            name="node.report_review",
                            kind="agent",
                            agent_role="report_reviewer",
                        ):
                            raw_review = await self.cancellation_scope.run(
                                self._invoke_review_callable(
                                    review_fn,
                                    draft_obj,
                                    state,
                                    self.config,
                                    attempt,
                                ),
                                stage="report_review",
                            )
                        normalized = self._model_to_payload(raw_review)
                        if not isinstance(normalized, dict):
                            raise ValueError("report_review_protocol_invalid")
                        review_payload = normalized
                except RunCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - fail-open is explicit policy
                    review_error = exc
                    review_payload = {
                        "schema_version": 1,
                        "decision": "fail",
                        "status": "error",
                        "protocol_error": "report_reviewer_failed",
                    }

                if review_error is not None and not self._report_review_fail_open():
                    # Persist a minimal failure record before propagating so a
                    # resume can explain why no final report was published.
                    review_payload["error_code"] = "report_reviewer_unavailable"
                    review_payload["gate_decision"] = "fail"
                    review_payload["attempt"] = attempt
                    review_payload["draft_sha256"] = draft_sha256
                    state["report_review"] = review_payload
                    state["report_review_history"] = [
                        *(
                            history
                            if isinstance(history, list)
                            else []
                        ),
                        review_payload,
                    ]
                    await self._persist_update(
                        channel="lead",
                        stage="report_reviewed",
                        update={
                            "report_review": review_payload,
                            "report_review_history": state[
                                "report_review_history"
                            ],
                        },
                        extra={"attempt": attempt, "draft_sha256": draft_sha256},
                    )
                    await self._persist_report_review_artifact(
                        attempt=attempt,
                        review=review_payload,
                        draft_sha256=draft_sha256,
                    )
                    raise RuntimeError("report_reviewer_failed") from review_error

                decision, issue_count, critical_count, hard_failure = self._review_gate(
                    review_payload
                )
                recovery_draft = self._is_report_review_recovery(draft_obj, state)
                if review_payload.get("skipped") and not hard_failure:
                    # Fail-open availability degradation publishes the already
                    # sanitized draft without routing an empty review through
                    # the Revisor or semantic hard-failure branch.
                    decision = "pass"
                    hard_failure = False
                    critical_count = 0
                    review_payload["decision"] = "pass"
                elif str(review_payload.get("status") or "").lower() in {
                    "failed",
                    "error",
                }:
                    decision = "fail"
                    hard_failure = True
                if review_error is not None:
                    decision = "pass" if self._report_review_fail_open() else "fail"
                    if decision == "pass":
                        review_payload["status"] = "skipped"
                        review_payload["skipped"] = True
                        review_payload["degraded"] = True
                        hard_failure = False
                        critical_count = 0
                    else:
                        review_payload["status"] = "failed"
                if recovery_draft and decision == "pass":
                    # Evidence-limited recovery is an explicit partial delivery
                    # even when its own final review passes.
                    review_payload["status"] = "degraded"
                    review_payload["degraded"] = True
                review_payload["gate_decision"] = decision
                review_payload["attempt"] = attempt
                review_payload["draft_sha256"] = draft_sha256
                review_payload["issue_count"] = issue_count
                review_payload["critical_issue_count"] = critical_count
                review_payload["hard_failure"] = hard_failure
                revision_count = int(state.get("report_revision_count") or 0)
                if (
                    decision == "revise"
                    and not hard_failure
                    and critical_count == 0
                    and (
                        recovery_draft
                        or revision_count >= self._report_review_max_revisions()
                    )
                ):
                    review_payload["status"] = "degraded"
                    review_payload["degraded"] = True
                history = (
                    [*history[:-1], review_payload]
                    if review_already_in_history
                    else [*history, review_payload]
                )
                state["report_review"] = review_payload
                state["report_review_history"] = history
                review_artifact_sha = await self._persist_report_review_artifact(
                    attempt=attempt,
                    review=review_payload,
                    draft_sha256=draft_sha256,
                )
                await self._persist_update(
                    channel="lead",
                    stage="report_reviewed",
                    update={
                        "report_review": review_payload,
                        "report_review_history": history,
                    },
                    extra={
                        "attempt": attempt,
                        "draft_sha256": draft_sha256,
                        "review_sha256": review_artifact_sha,
                    },
                )
                status = str(review_payload.get("status") or "completed")
                if decision == "pass" and status not in {"skipped", "error", "degraded"}:
                    status = "passed"
                elif decision == "pass" and status == "completed":
                    status = "passed"
                await self._publish_public(
                    "report.review.completed",
                    stage="writing",
                    payload={
                        "attempt": attempt,
                        "decision": decision,
                        "revision_count": int(state.get("report_revision_count") or 0),
                        "status": status,
                        "issue_count": issue_count,
                        "issues_count": issue_count,
                        "critical_issue_count": critical_count,
                        "critical_issues_count": critical_count,
                        "draft_sha256": draft_sha256,
                        "sha256": draft_sha256,
                    },
                    dedupe_key=f"report:review:{attempt}:completed",
                )
                yield self._event(
                    "report.review.completed",
                    {
                        "run_id": self.run_id,
                        "attempt": attempt,
                        "decision": decision,
                        "revision_count": int(state.get("report_revision_count") or 0),
                        "status": status,
                        "issue_count": issue_count,
                        "issues_count": issue_count,
                        "critical_issue_count": critical_count,
                        "critical_issues_count": critical_count,
                        "draft_sha256": draft_sha256,
                        "sha256": draft_sha256,
                    },
                )

                if decision == "pass" or (
                    review_error is not None and self._report_review_fail_open()
                ):
                    await self._persist_checkpoint(
                        "report_reviewed",
                        "report_finalize",
                        payload={
                            "attempt": attempt,
                            "decision": decision,
                            "draft_sha256": draft_sha256,
                        },
                    )
                    stage = "report_finalize"
                elif recovery_draft and decision in {"revise", "fail"}:
                    if decision == "revise" and not hard_failure and critical_count == 0:
                        # The evidence-limited draft is a terminal recovery. It
                        # may be delivered with presentation-only defects, but
                        # must not be passed through another generative Revisor.
                        await self._persist_checkpoint(
                            "report_reviewed",
                            "report_finalize",
                            payload={
                                "attempt": attempt,
                                "decision": "degraded",
                                "draft_sha256": draft_sha256,
                            },
                        )
                        stage = "report_finalize"
                    else:
                        await self._mark_report_review_insufficient_evidence(
                            state,
                            review_payload,
                            reason_codes=self._report_review_reason_codes(
                                review_payload
                            ),
                        )
                        raise RuntimeError("insufficient_evidence")
                elif decision == "revise" and revision_count < self._report_review_max_revisions():
                    await self._persist_checkpoint(
                        "report_reviewed",
                        "report_revision",
                        payload={
                            "attempt": attempt,
                            "decision": decision,
                            "draft_sha256": draft_sha256,
                            "revision_count": revision_count,
                        },
                    )
                    stage = "report_revision"
                elif decision == "revise" and (hard_failure or critical_count):
                    # Revisor budget is exhausted. Replace the blocked product
                    # once with the existing accepted-evidence-only synthesis,
                    # then review that recovery draft before finalization.
                    recoverable = self._report_review_has_recoverable_content_failure(
                        review_payload,
                        critical_count=critical_count,
                        hard_failure=hard_failure,
                    )
                    if not recoverable:
                        raise RuntimeError("report_review_revision_limit_exceeded")
                    draft_obj, draft_text, draft_sha256 = (
                        await self._recover_report_review_draft(
                            draft=draft_obj,
                            state=state,
                            review=review_payload,
                        )
                    )
                    await self._persist_checkpoint(
                        "report_recovered",
                        "report_review",
                        payload={
                            "attempt": attempt,
                            "draft_sha256": draft_sha256,
                            "status": "degraded",
                        },
                    )
                    stage = "report_review"
                    continue
                elif decision == "revise" and not hard_failure and critical_count == 0:
                    # Revision budget exhausted, but deterministic hard checks
                    # pass.  Deliver with an explicit degraded marker.
                    review_payload["status"] = "degraded"
                    state["report_review"] = review_payload
                    await self._persist_checkpoint(
                        "report_reviewed",
                        "report_finalize",
                        payload={
                            "attempt": attempt,
                            "decision": "degraded",
                            "draft_sha256": draft_sha256,
                        },
                    )
                    stage = "report_finalize"
                elif decision == "fail":
                    recoverable = self._report_review_has_recoverable_content_failure(
                        review_payload,
                        critical_count=critical_count,
                        hard_failure=hard_failure,
                    )
                    if recoverable:
                        draft_obj, draft_text, draft_sha256 = (
                            await self._recover_report_review_draft(
                                draft=draft_obj,
                                state=state,
                                review=review_payload,
                            )
                        )
                        await self._persist_checkpoint(
                            "report_recovered",
                            "report_review",
                            payload={
                                "attempt": attempt,
                                "draft_sha256": draft_sha256,
                                "status": "degraded",
                            },
                        )
                        stage = "report_review"
                        continue
                    # Availability and Reviewer protocol failures are not report
                    # defects and cannot be repaired by evidence synthesis.
                    raise RuntimeError("report_review_failed")
                else:
                    if self._report_review_fail_open():
                        review_payload["status"] = "degraded"
                        state["report_review"] = review_payload
                        await self._persist_checkpoint(
                            "report_reviewed",
                            "report_finalize",
                            payload={
                                "attempt": attempt,
                                "decision": "degraded",
                                "draft_sha256": draft_sha256,
                            },
                        )
                        stage = "report_finalize"
                    else:
                        raise RuntimeError("report_review_failed")

            if stage == "report_revision":
                draft_text = self._load_report_draft_artifact(state) or draft_text
                review_payload = state.get("report_review", {})
                if not isinstance(review_payload, dict):
                    review_payload = {}
                persisted_revision_count = int(
                    state.get("report_revision_count") or 0
                )
                revision_count = persisted_revision_count + 1
                source_draft_sha256 = hashlib.sha256(
                    draft_text.encode("utf-8")
                ).hexdigest()
                try:
                    source_review_attempt = int(review_payload.get("attempt") or 0)
                except (TypeError, ValueError):
                    source_review_attempt = 0
                source_review_sha256 = self._report_payload_sha256(review_payload)
                # The source-bound JSON envelope is committed before the state
                # delta and contains the complete Markdown. Recover it without
                # repeating the Revisor call, even if the .md copy is missing.
                artifact_text, artifact_metadata = self._load_report_revision_artifact(
                    revision_count,
                    source_draft_sha256=source_draft_sha256,
                    source_review_attempt=source_review_attempt,
                    source_review_sha256=source_review_sha256,
                )
                if artifact_text:
                    draft_text = artifact_text
                    if artifact_metadata:
                        draft_obj = artifact_metadata
                        state["report_draft_metadata"] = artifact_metadata
                    else:
                        draft_obj = self._draft_with_text(draft_obj or {}, artifact_text)
                    state["final_report_draft"] = artifact_text
                    state["report_revision_count"] = revision_count
                    artifact_hash = hashlib.sha256(
                        artifact_text.encode("utf-8")
                    ).hexdigest()
                    await self._write_optional_text_artifact(
                        "report_revised",
                        f"report_drafts/revision-{revision_count:03d}.md",
                        artifact_text,
                    )
                    await self._persist_update(
                        channel="lead",
                        stage="report_revised",
                        update={
                            "final_report_draft": artifact_text,
                            "report_revision_count": revision_count,
                            "report_draft_metadata": state.get(
                                "report_draft_metadata", {}
                            ),
                        },
                        extra={
                            "revision_count": revision_count,
                            "draft_sha256": artifact_hash,
                            "recovered": True,
                        },
                    )
                    revision_event_created = await self._ensure_revision_completed_event(
                        attempt=revision_count,
                        draft_sha256=artifact_hash,
                    )
                    await self._persist_checkpoint(
                        "report_revised",
                        "report_review",
                        payload={
                            "revision_count": revision_count,
                            "draft_sha256": artifact_hash,
                            "recovered": True,
                        },
                    )
                    if revision_event_created:
                        yield self._event(
                            "report.revision.completed",
                            {
                                "run_id": self.run_id,
                                "attempt": revision_count,
                                "revision_count": revision_count,
                                "status": "completed",
                                "draft_sha256": artifact_hash,
                                "sha256": artifact_hash,
                            },
                        )
                    stage = "report_review"
                    continue
                # A revision delta is persisted before its checkpoint.  If a
                # worker crashed in that narrow window, replay still advertises
                # ``report_revision``; the hash/count pair lets us continue at
                # the subsequent review without invoking the Revisor twice.
                persisted_metadata = state.get("report_draft_metadata")
                persisted_hash = (
                    str(persisted_metadata.get("sha256") or "")
                    if isinstance(persisted_metadata, dict)
                    else ""
                )
                current_hash = source_draft_sha256
                if persisted_revision_count > 0 and persisted_hash == current_hash:
                    stage = "report_review"
                    continue
                revise_fn = self._report_callable("revise_report")
                if revise_fn is None:
                    raise RuntimeError("report_revisor_unavailable")
                await self._publish_public(
                    "report.revision.started",
                    stage="writing",
                    payload={
                        "attempt": revision_count,
                        "revision_count": revision_count,
                        "status": "running",
                        "draft_sha256": source_draft_sha256,
                        "sha256": source_draft_sha256,
                    },
                    dedupe_key=f"report:revision:{revision_count}:started",
                )
                yield self._event(
                    "report.revision.started",
                    {
                        "run_id": self.run_id,
                        "attempt": revision_count,
                        "revision_count": revision_count,
                        "status": "running",
                    },
                )
                revision_draft = self._draft_with_text(draft_obj or {}, draft_text)
                with recorder.start_span(
                    name="node.report_revision",
                    kind="agent",
                    agent_role="report_revisor",
                ):
                    revised_raw = await self.cancellation_scope.run(
                        self._await_report_callable(
                            revise_fn,
                            revision_draft,
                            review_payload,
                            state,
                            self.config,
                        ),
                        stage="report_revision",
                    )
                revised_text = self._report_draft_text(revised_raw, "")
                if not revised_text:
                    raise RuntimeError("report_revision_empty")
                draft_text = revised_text
                draft_obj = self._draft_with_text(revision_draft, revised_text)
                state["final_report_draft"] = revised_text
                revised_payload = self._model_to_payload(draft_obj)
                if not isinstance(revised_payload, dict):
                    revised_payload = {
                        "markdown": revised_text,
                        "body_markdown": revised_text,
                    }
                else:
                    revised_payload = dict(revised_payload)
                    revised_payload["markdown"] = revised_text
                    revised_payload["body_markdown"] = revised_text
                revised_payload["sha256"] = hashlib.sha256(
                    revised_text.encode("utf-8")
                ).hexdigest()
                revised_payload["attempt"] = revision_count
                provenance = revised_payload.get("provenance")
                if not isinstance(provenance, dict):
                    provenance = {}
                provenance = dict(provenance)
                provenance["revision_count"] = revision_count
                provenance["stage"] = "report_revision"
                provenance["revision_source_draft_sha256"] = source_draft_sha256
                provenance["revision_source_review_attempt"] = source_review_attempt
                provenance["revision_source_review_sha256"] = source_review_sha256
                revised_payload["provenance"] = provenance
                state["report_draft_metadata"] = revised_payload
                state["report_revision_count"] = revision_count
                draft_sha256 = hashlib.sha256(revised_text.encode("utf-8")).hexdigest()
                revision_path = f"report_drafts/revision-{revision_count:03d}.md"
                if self.context_store is not None:
                    try:
                        revision_metadata_sha = self.context_store.write_json_atomic(
                            f"report_drafts/revision-{revision_count:03d}.json",
                            {
                                "schema_version": 1,
                                "attempt": revision_count,
                                "draft_sha256": draft_sha256,
                                "source_draft_sha256": source_draft_sha256,
                                "source_review_attempt": source_review_attempt,
                                "source_review_sha256": source_review_sha256,
                                "model": self._report_config_value(
                                    "final_report_model", None
                                ),
                                "policy_version": self.config.get(
                                    "metadata", {}
                                ).get("quality_policy_version"),
                                "evaluation_epoch": self.config.get(
                                    "metadata", {}
                                ).get("quality_evaluation_epoch"),
                                "draft": revised_payload,
                            },
                        )
                        await self._persist_artifact_event(
                            "report_revised",
                            f"report_drafts/revision-{revision_count:03d}.json",
                            revision_metadata_sha,
                        )
                    except Exception as exc:  # noqa: BLE001 - metadata is advisory
                        self.persistence_degraded = True
                        self.context_store.mark_persistence_degraded(exc)
                await self._write_optional_text_artifact(
                    "report_revised",
                    revision_path,
                    revised_text,
                )
                await self._persist_update(
                    channel="lead",
                    stage="report_revised",
                    update={
                        "final_report_draft": revised_text,
                        "report_revision_count": revision_count,
                        "report_draft_metadata": revised_payload,
                    },
                    extra={
                        "revision_count": revision_count,
                        "draft_sha256": draft_sha256,
                    },
                )
                revision_event_created = await self._ensure_revision_completed_event(
                    attempt=revision_count,
                    draft_sha256=draft_sha256,
                )
                await self._persist_checkpoint(
                    "report_revised",
                    "report_review",
                    payload={
                        "revision_count": revision_count,
                        "draft_sha256": draft_sha256,
                    },
                )
                if revision_event_created:
                    yield self._event(
                        "report.revision.completed",
                        {
                            "run_id": self.run_id,
                            "attempt": revision_count,
                            "revision_count": revision_count,
                            "status": "completed",
                            "draft_sha256": draft_sha256,
                            "sha256": draft_sha256,
                        },
                    )
                stage = "report_review"

            if stage == "report_finalize":
                draft_text = self._load_report_draft_artifact(state) or draft_text
                if not draft_text:
                    raise RuntimeError("report_draft_missing")
                finalize_fn = self._report_callable("finalize_report")
                final_result: Any
                already_finalized = bool(
                    str(state.get("final_report") or "")
                    and str(state.get("final_report") or "") == draft_text
                )
                if already_finalized:
                    # A crash after the finalization state delta but before the
                    # checkpoint must not invoke deterministic artifact
                    # rendering twice.
                    final_result = {
                        "final_report": str(state.get("final_report") or "")
                    }
                elif finalize_fn is None:
                    final_result = {"final_report": draft_text}
                else:
                    final_draft = self._draft_with_text(draft_obj or {}, draft_text)
                    with recorder.start_span(
                        name="node.report_finalize",
                        kind="agent",
                        agent_role="lead",
                    ):
                        final_result = await self.cancellation_scope.run(
                            self._await_report_callable(
                                finalize_fn,
                                final_draft,
                                state,
                                self.config,
                            ),
                            stage="report_finalize",
                        )
                final_payload = self._model_to_payload(final_result)
                if isinstance(final_payload, dict):
                    apply_update_to_state(state, final_payload)
                else:
                    final_payload = {}
                final_text = self._report_draft_text(
                    final_result,
                    str(state.get("final_report") or draft_text),
                )
                if not final_text:
                    raise RuntimeError("final_report_empty")
                state["final_report"] = final_text
                state["final_report_draft"] = final_text
                final_sha256 = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
                persisted_final_update = {
                    **final_payload,
                    "final_report": final_text,
                    "final_report_draft": final_text,
                }
                await self._persist_update(
                    channel="lead",
                    stage="report_finalized",
                    update=persisted_final_update,
                    extra={"sha256": final_sha256},
                )
                await self._write_optional_text_artifact(
                    "report_finalized",
                    "final_report.md",
                    final_text,
                )
                canonical = self._ensure_canonical_report(state)
                await self._write_optional_json_artifact(
                    "report_finalized",
                    "canonical_report.json",
                    canonical,
                )
                report_event_created = await self._ensure_report_completed_event(
                    final_text
                )
                await self._persist_checkpoint(
                    "report_finalized",
                    "memory_extract_and_write",
                    payload={"sha256": final_sha256},
                )
                if report_event_created:
                    yield self._event(
                        "report.completed",
                        {
                            "run_id": self.run_id,
                            "status": "completed",
                            "sha256": final_sha256,
                        },
                    )
                await self._public_stage_event("writing", "completed")
                await self._public_stage_event("finalizing", "started")
                return

    def _budget_gate(self) -> BudgetGate:
        """Resolve the run-scoped budget gate from the current configuration."""
        configurable = Configuration.from_runnable_config(self.config)
        return BudgetGate.from_config(
            configurable,
            self.run_id,
            started_at=self.started_at,
        )

    def interrupt(self) -> None:
        """Request cooperative cancellation of the active run."""
        if not self.cancellation_scope.request("cancel_requested"):
            return
        self.cancelled = True
        if self.status not in {"completed", "failed", "cancelled"}:
            self.status = "cancelling"

    async def submit_message(
        self,
        messages: list[Any],
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        """Run a complete research request and return the final state."""
        async for _event in self.stream_message(messages, config):
            pass
        return self.final_state or {}

    def _hitl_config(self) -> Configuration:
        return Configuration.from_runnable_config(self.config)

    def _hitl_enabled(self) -> bool:
        return self._hitl_config().enable_human_in_loop

    def _latest_revision_message(self) -> str | None:
        for item in reversed(self.human_feedback):
            if item.get("type") in {"plan_revision", "outline_revision"}:
                return str(item.get("message") or "")
        return None

    def _draft_research_plan(self, state: dict[str, Any]) -> str:
        brief = str(state.get("research_brief") or _message_text(state.get("messages", []))).strip()
        memory = str(state.get("memory_context") or "").strip()
        revision = self._latest_revision_message()
        parts = [
            "# Research Plan",
            "## Research objective",
            brief or "Clarify and answer the user's research request with grounded evidence.",
            "## Subquestions",
            "- What are the core claims, entities, and decision criteria in the request?",
            "- Which primary or high-authority sources can verify each important claim?",
            "- Where do sources disagree, and what uncertainty should be preserved in the report?",
            "## Expected task split",
            "- Gather authoritative background and primary sources.",
            "- Cross-check disputed or high-impact claims with independent evidence.",
            "- Synthesize findings into a report-ready evidence map.",
            "## Evidence to prioritize",
            "- Primary documentation, official data, direct publications, and timestamped source pages.",
            "- Source metadata sufficient to support citations in the final report.",
            "## Risks and uncertainties",
            "- Search APIs may miss JS-rendered, gated, or interaction-heavy pages.",
            "- Claims without direct evidence should be marked as uncertain instead of over-stated.",
        ]
        if memory:
            parts.extend(["## Memory context", memory])
        if revision:
            parts.extend(["## User-requested revision", revision])
        return "\n".join(parts)

    def _draft_report_outline(self, state: dict[str, Any]) -> str:
        brief = str(state.get("research_brief") or "Research findings").strip()
        notes = state.get("notes") or []
        evidence_count = len(notes)
        return "\n".join([
            "# Report Outline",
            "## 1. Executive answer",
            brief,
            "## 2. Key findings and evidence",
            f"Summarize and cite {evidence_count} collected research note(s).",
            "## 3. Uncertainties and conflicting evidence",
            "Call out unresolved questions, weak sources, and areas requiring caution.",
            "## 4. Recommendations or next steps",
            "Translate the evidence into the user's requested decision or deliverable.",
        ])

    def _restored_human_action(self, action_type: str) -> dict[str, Any] | None:
        """Return the pending operation for the stage being resumed."""
        if self.context_store is None:
            return None
        pending = self.context_store.load_manifest().pending_human_action
        return pending if pending and pending.get("type") == action_type else None

    def _open_human_action(
        self,
        action_type: str,
        payload: dict[str, Any],
        *,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._pending_action_loop = loop
        self._pending_action_future = loop.create_future()
        self.pending_human_action = {
            "action_id": action_id or str(uuid.uuid4()),
            "type": action_type,
            "payload": payload,
            "created_at": time.time(),
        }
        if self.context_store is not None and self.context_store.manifest_path.exists():
            try:
                self.context_store._update_manifest(  # noqa: SLF001
                    status=self.status,
                    pending_human_action=self.pending_human_action,
                )
            except Exception as exc:  # noqa: BLE001 - in-memory HITL remains usable
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
            decision = self.context_store.load_human_decision(
                self.pending_human_action["action_id"]
            )
            if decision is not None:
                self._pending_action_future.set_result(decision)
        return self.pending_human_action

    async def _wait_for_human_action(self) -> dict[str, Any]:
        if self._pending_action_future is None:
            raise RuntimeError("No pending human action")
        future = self._pending_action_future
        try:
            return await self.cancellation_scope.run(
                asyncio.shield(future),
                stage="human_action",
            )
        finally:
            if not future.done():
                future.cancel()
            self.pending_human_action = None
            self._pending_action_future = None
            self._pending_action_loop = None
            # The next stable checkpoint retires the durable pending action.
            # Shutdown and the decision/application crash gap must retain it.

    def handle_human_action(self, action_id: str, action: str, message: str = "") -> dict[str, Any]:
        """Resolve a pending approval or clarification action."""
        decision = {"action": action, "message": message or "", "action_id": action_id}
        if self.context_store is not None and self.context_store.manifest_path.exists():
            existing = self.context_store.load_human_decision(action_id)
            if existing is not None:
                self.context_store.save_human_decision(decision)
                return {"status": "accepted", "action": action}
        pending = self.pending_human_action
        if not pending or pending.get("action_id") != action_id:
            raise ValueError("No matching pending human action")
        allowed = (
            {"answer", "cancel"}
            if pending.get("type") == "clarification"
            else {"approve", "deny", "cancel"}
            if pending.get("type") == "fetch_budget_approval"
            else {"approve", "revise", "cancel"}
        )
        if action not in allowed:
            raise ValueError("Human action does not match the pending action type")
        if action in {"answer", "revise"} and not message.strip():
            raise ValueError("A message is required for this human action")
        if self._pending_action_future is None or self._pending_action_future.done():
            raise ValueError("Pending human action is not awaiting input")
        if self.context_store is not None and self.context_store.manifest_path.exists():
            self.context_store.save_human_decision(decision)

        def set_result() -> None:
            if self._pending_action_future and not self._pending_action_future.done():
                self._pending_action_future.set_result(decision)

        if self._pending_action_loop and self._pending_action_loop.is_running():
            self._pending_action_loop.call_soon_threadsafe(set_result)
        else:
            set_result()
        return {"status": "accepted", "action": action}

    async def _await_clarification(
        self,
        state: dict[str, Any],
        *,
        restored_action: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Pause for one clarification answer and continue the same run."""
        question = str(
            (restored_action or {}).get("payload", {}).get("question")
            or _message_text(state.get("messages", []))
        ).strip()
        self.status = "awaiting_clarification"
        pending = self._open_human_action(
            "clarification",
            {"question": question},
            action_id=(restored_action or {}).get("action_id"),
        )
        await self._persist_checkpoint(
            "awaiting_clarification",
            "clarification_wait",
            status=self.status,
            payload={"pending_human_action": pending},
        )
        await self._publish_public(
            "clarification.required",
            stage="preparing",
            payload={
                "action_id": pending["action_id"],
                "question": question,
                "status": "pending",
                "allowed_actions": ["answer", "cancel"],
            },
            dedupe_key=f"clarification:{pending['action_id']}:required",
        )
        yield self._event(
            "hitl.clarification_pending",
            {
                "run_id": self.run_id,
                "status": self.status,
                "question": question,
                "pending_human_action": pending,
            },
        )
        decision = await self._wait_for_human_action()
        if decision["action"] == "cancel":
            self.cancelled = True
            self.status = "cancelled"
        else:
            answer = str(decision.get("message") or "").strip()
            state["messages"] = [*state.get("messages", []), HumanMessage(content=answer)]
            await self._persist_checkpoint(
                "clarified",
                "write_research_brief",
                payload={
                    "update": {
                        "messages": {"type": "override", "value": state["messages"]},
                    },
                },
            )
            self.status = "running"
        await self._publish_public(
            "clarification.resolved",
            stage="preparing",
            payload={
                "action_id": decision["action_id"],
                "action": decision["action"],
                "status": "resolved",
            },
            dedupe_key=f"clarification:{decision['action_id']}:resolved",
        )
        yield self._event(
            "hitl.clarification_resolved",
            {"run_id": self.run_id, "status": self.status, "action": decision["action"]},
        )

    async def submit_feedback(self, feedback: dict[str, Any]) -> dict[str, Any]:
        """Accept user direction or evidence questions while a run is active."""
        feedback_type = str(feedback.get("type") or "direction")
        if feedback_type not in {"direction", "evidence_question"}:
            raise ValueError("Unsupported feedback type")
        message = str(feedback.get("message") or "").strip()
        if not message:
            raise ValueError("Feedback message is required")
        feedback_id = str(feedback.get("command_id") or uuid.uuid4())
        existing = next(
            (item for item in self.human_feedback if item.get("feedback_id") == feedback_id),
            None,
        )
        if existing is not None:
            return {"status": "accepted", "feedback_id": feedback_id}
        entry = {
            "feedback_id": feedback_id,
            "type": feedback_type,
            "message": message,
            "task_id": feedback.get("task_id"),
            "source_url": feedback.get("source_url"),
            "claim_text": feedback.get("claim_text"),
            "created_at": time.time(),
        }
        self.human_feedback.append(entry)
        await self._queue_task_feedback_if_possible(entry)
        await self._publish_public(
            "feedback.received",
            stage="researching",
            payload={
                "feedback_id": entry["feedback_id"],
                "feedback_type": feedback_type,
                "task_id": entry.get("task_id"),
                "status": "accepted",
            },
            dedupe_key=f"feedback:{entry['feedback_id']}",
        )
        return {"status": "accepted", "feedback_id": entry["feedback_id"]}

    async def _queue_task_feedback_if_possible(self, entry: dict[str, Any]) -> None:
        cfg = self._hitl_config()
        task_id = entry.get("task_id")
        if cfg.hitl_feedback_mode != "task_queue" or not task_id:
            return
        try:
            from open_deep_research.tasks.registry import TaskStatus, get_task_registry
        except Exception:  # noqa: BLE001 - task registry is optional for sync research paths
            return
        record = get_task_registry().get(str(task_id))
        if (
            record is None
            or record.run_id != self.run_id
            or record.status != TaskStatus.RUNNING
        ):
            return
        await record.control_queue.put({"type": "update", "instruction": self._format_feedback_instruction(entry)})
        entry["queued_to_task"] = True

    def _format_feedback_instruction(self, entry: dict[str, Any]) -> str:
        label = "Evidence question" if entry.get("type") == "evidence_question" else "User feedback"
        details = [f"{label}: {entry.get('message', '')}"]
        if entry.get("source_url"):
            details.append(f"Source URL: {entry['source_url']}")
        if entry.get("claim_text"):
            details.append(f"Claim text: {entry['claim_text']}")
        return "\n".join(details)

    def _drain_human_feedback(self, supervisor_state: dict[str, Any]) -> None:
        new_feedback = self.human_feedback[self._feedback_cursor:]
        if not new_feedback:
            return
        messages = list(supervisor_state.get("supervisor_messages", []))
        for entry in new_feedback:
            prefix = "[Evidence Question]" if entry.get("type") == "evidence_question" else "[User Feedback]"
            messages.append(HumanMessage(content=f"{prefix} {self._format_feedback_instruction(entry)}"))
        supervisor_state["supervisor_messages"] = messages
        supervisor_state["human_feedback"] = list(self.human_feedback)
        self._feedback_cursor = len(self.human_feedback)

    async def _await_fetch_budget_approval(self, state: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Wait durably for a fetch grant; silence is never a budget denial."""
        cfg = self._hitl_config()
        extension = dict(state.get("fetch_budget_extension") or {})
        current = cfg.max_fetches_per_run + int(extension.get("extra_fetches", 0))
        increment = cfg.max_fetches_per_run
        content = (
            f"本次研究的抓取预算已耗尽，当前上限为 {current} 次。\n"
            f"是否增加 {increment} 次，将上限提高到 {current + increment} 次并继续补充研究？\n"
            "继续研究可能增加耗时和模型/检索费用。拒绝后将使用现有证据生成部分报告。"
        )
        restored = self.context_store.load_manifest().pending_human_action if self.context_store else None
        state["fetch_budget_resolution"] = None
        await self._persist_update(channel="lead", stage="fetch_budget_requested",
                                   update={"fetch_budget_resolution": None})
        self.status = "awaiting_fetch_budget_approval"
        pending = self._open_human_action(
            "fetch_budget_approval", {"content_markdown": content},
            action_id=(restored or {}).get("action_id") if (restored or {}).get("type") == "fetch_budget_approval" else None,
        )
        await self._persist_checkpoint(
            "awaiting_fetch_budget_approval", "fetch_budget_approval",
            status=self.status, payload={"pending_human_action": pending},
        )
        await self._publish_public(
            "approval.required", stage="researching",
            payload={"action_id": pending["action_id"], "approval_type": "fetch_budget",
                     "status": "pending", "content_markdown": content,
                     "allowed_actions": ["approve", "deny", "cancel"]},
            dedupe_key=f"approval:fetch_budget:{pending['action_id']}:required",
        )
        yield self._event("hitl.fetch_budget_pending", {"status": self.status, "run_id": self.run_id})
        decision = await self._wait_for_human_action()
        if decision["action"] == "cancel":
            self.cancelled = True
            self.status = "cancelled"
            raise RunCancelled("cancel_requested", "fetch_budget_approval")
        self.status = "running"
        if decision["action"] == "approve":
            extension = {"extra_fetches": current + increment - cfg.max_fetches_per_run,
                         "granted_at": time.time()}
            update = {
                "fetch_budget_resolution": "approve",
                "fetch_budget_action_id": decision["action_id"],
                "fetch_budget_extension": extension,
                "completion_decision": {"type": "override", "value": {}},
                "supervisor_messages": [HumanMessage(content=(
                    f"用户已批准增加 {increment} 次抓取预算。保留已有证据，继续委派补证任务，"
                    "优先解决上轮因抓取预算耗尽而未完成的需求。"
                ))],
            }
            apply_update_to_state(state, update)
            await self._persist_update(channel="lead", stage="fetch_budget_granted", update=update)
            self.config.setdefault("metadata", {})["fetch_budget_extension"] = extension
            await self._register_sandbox_gateway()
            self._restored_query_states.pop("supervisor", None)
        else:
            update = {"fetch_budget_resolution": "deny", "fetch_budget_action_id": decision["action_id"], "completion_decision": {"type": "override", "value": {
                **state.get("completion_decision", {}),
                "action": CompletionDecision.COMPLETE_PARTIAL.value,
                "reason": "fetch_budget_exhausted",
            }}}
            apply_update_to_state(state, update)
            await self._persist_update(channel="lead", stage="fetch_budget_declined", update=update)
        await self._persist_checkpoint("fetch_budget_resolved", "fetch_budget_approval", status="running")
        await self._publish_public(
            "approval.resolved", stage="researching",
            payload={"action_id": decision["action_id"], "approval_type": "fetch_budget",
                     "action": decision["action"], "status": "resolved"},
            dedupe_key=f"approval:fetch_budget:{decision['action_id']}:resolved",
        )
        yield self._event("hitl.fetch_budget_resolved", {"status": self.status, "action": decision["action"]})

    async def _maybe_await_plan_approval(self, state: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if not self._hitl_enabled():
            return
        cfg = self._hitl_config()
        revisions = int(state.get("hitl_plan_revisions", 0))
        restored_action = self._restored_human_action("plan_approval")
        if not cfg.hitl_require_plan_approval:
            plan = self._draft_research_plan(state)
            state["research_plan"] = plan
            state["approved_research_plan"] = plan
            state["human_feedback"] = list(self.human_feedback)
            return
        while True:
            plan = (
                (restored_action or {}).get("payload", {}).get("research_plan")
                or self._draft_research_plan(state)
            )
            state["research_plan"] = plan
            state["human_feedback"] = list(self.human_feedback)
            state["hitl_plan_revisions"] = revisions
            self.status = "awaiting_plan_approval"
            pending = self._open_human_action(
                "plan_approval", {"research_plan": plan},
                action_id=(restored_action or {}).get("action_id"),
            )
            restored_action = None
            await self._persist_checkpoint(
                "awaiting_plan_approval",
                "plan_approval",
                status=self.status,
                payload={
                    "pending_human_action": pending,
                    "update": {
                        "research_plan": plan,
                        "human_feedback": {"type": "override", "value": list(self.human_feedback)},
                        "hitl_plan_revisions": revisions,
                    },
                },
            )
            await self._publish_public(
                "approval.required",
                stage="planning",
                payload={
                    "action_id": pending["action_id"],
                    "approval_type": "plan",
                    "status": "pending",
                    "plan_id": f"plan-{self.run_id}",
                    "revision": revisions + 1,
                    "content_markdown": plan,
                    "allowed_actions": ["approve", "revise", "cancel"],
                },
                dedupe_key=f"approval:plan:{pending['action_id']}:required",
            )
            yield self._event(
                "hitl.plan_pending",
                {
                    "run_id": self.run_id,
                    "status": self.status,
                    "research_plan": plan,
                    "pending_human_action": pending,
                },
            )
            decision = await self._wait_for_human_action()
            if decision["action"] == "approve":
                state["approved_research_plan"] = plan
                state["pending_human_action"] = None
                self.status = "running"
                await self._publish_public(
                    "approval.resolved",
                    stage="planning",
                    payload={
                        "action_id": decision["action_id"],
                        "approval_type": "plan",
                        "action": "approve",
                        "status": "resolved",
                    },
                    dedupe_key=f"approval:plan:{decision['action_id']}:resolved",
                )
                yield self._event("hitl.plan_approved", {"run_id": self.run_id, "status": self.status})
                return
            if decision["action"] == "cancel":
                self.cancelled = True
                self.status = "cancelled"
                yield self._event("hitl.cancelled", {"run_id": self.run_id, "status": self.status})
                return
            revisions += 1
            if revisions > cfg.hitl_max_plan_revisions:
                raise RuntimeError("Research plan revision limit exceeded")
            self.human_feedback.append({
                "feedback_id": str(uuid.uuid4()),
                "type": "plan_revision",
                "message": decision.get("message", ""),
                "created_at": time.time(),
            })
            state["human_feedback"] = list(self.human_feedback)
            self.status = "running"
            await self._publish_public(
                "plan.revised",
                stage="planning",
                payload={"plan_id": f"plan-{self.run_id}", "revision": revisions + 1},
                dedupe_key=f"plan:revised:{revisions + 1}",
            )
            yield self._event(
                "hitl.plan_revised",
                {"run_id": self.run_id, "status": self.status, "revision_count": revisions},
            )

    async def _maybe_await_outline_approval(self, state: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if not self._hitl_enabled():
            return
        cfg = self._hitl_config()
        restored_action = self._restored_human_action("outline_approval")
        outline = (
            (restored_action or {}).get("payload", {}).get("report_outline")
            or self._draft_report_outline(state)
        )
        state["report_outline"] = outline
        if not cfg.hitl_require_outline_approval:
            return
        while True:
            self.status = "awaiting_outline_approval"
            pending = self._open_human_action(
                "outline_approval", {"report_outline": outline},
                action_id=(restored_action or {}).get("action_id"),
            )
            restored_action = None
            await self._persist_checkpoint(
                "awaiting_outline_approval",
                "outline_approval",
                status=self.status,
                payload={
                    "pending_human_action": pending,
                    "update": {
                        "report_outline": outline,
                        "human_feedback": {"type": "override", "value": list(self.human_feedback)},
                    },
                },
            )
            await self._publish_public(
                "approval.required",
                stage="synthesizing",
                payload={
                    "action_id": pending["action_id"],
                    "approval_type": "outline",
                    "status": "pending",
                    "content_markdown": outline,
                    "allowed_actions": ["approve", "revise", "cancel"],
                },
                dedupe_key=f"approval:outline:{pending['action_id']}:required",
            )
            yield self._event(
                "hitl.outline_pending",
                {
                    "run_id": self.run_id,
                    "status": self.status,
                    "report_outline": outline,
                    "pending_human_action": pending,
                },
            )
            decision = await self._wait_for_human_action()
            if decision["action"] == "approve":
                state["pending_human_action"] = None
                self.status = "running"
                await self._publish_public(
                    "approval.resolved",
                    stage="synthesizing",
                    payload={
                        "action_id": decision["action_id"],
                        "approval_type": "outline",
                        "action": "approve",
                        "status": "resolved",
                    },
                    dedupe_key=f"approval:outline:{decision['action_id']}:resolved",
                )
                yield self._event("hitl.outline_approved", {"run_id": self.run_id, "status": self.status})
                return
            if decision["action"] == "cancel":
                self.cancelled = True
                self.status = "cancelled"
                yield self._event("hitl.cancelled", {"run_id": self.run_id, "status": self.status})
                return
            self.human_feedback.append({
                "feedback_id": str(uuid.uuid4()),
                "type": "outline_revision",
                "message": decision.get("message", ""),
                "created_at": time.time(),
            })
            state["human_feedback"] = list(self.human_feedback)
            outline = self._draft_report_outline(state)
            if decision.get("message"):
                outline = f"{outline}\n## User-requested outline revision\n{decision['message']}"
            state["report_outline"] = outline
            self.status = "running"

    def _cancelled_state(self, state: dict[str, Any]) -> dict[str, Any]:
        result = {
            "status": "cancelled",
            "usage": self._usage_subset(self.total_usage),
            "usage_accounting": self.total_usage.get("usage_accounting"),
            "metrics": self._metrics_subset(self.total_usage),
            "permission_denials": self.permission_denials,
        }
        self.final_state = {**state, "result": result}
        return self.final_state

    @staticmethod
    def _usage_subset(total: dict[str, Any]) -> dict[str, Any]:
        """Token-only view of a finish_run summary (backward-compatible usage payload)."""
        return {
            "input_tokens": total.get("input_tokens", 0),
            "output_tokens": total.get("output_tokens", 0),
            "total_tokens": total.get("total_tokens", 0),
            "cached_input_tokens": total.get("cached_input_tokens", 0),
            "cache_creation_input_tokens": total.get("cache_creation_input_tokens", 0),
            "reasoning_tokens": total.get("reasoning_tokens", 0),
            "estimated_cost_usd": total.get("estimated_cost_usd", 0.0),
        }

    @staticmethod
    def _metrics_subset(total: dict[str, Any]) -> dict[str, Any]:
        """Retry/429 view of a finish_run summary (the new metrics payload)."""
        return {
            "retry_count": total.get("retry_count", 0),
            "rate_limit_events": total.get("rate_limit_events", 0),
            "rate_limited_count": total.get("rate_limited_count", 0),
            "terminal_rate_limited_count": total.get("terminal_rate_limited_count", 0),
            "rate_429": total.get("rate_429", 0.0),
            "total_llm_tool_calls": total.get("total_llm_tool_calls", 0),
            "attempt_count": total.get("attempt_count", 0),
            "llm_call_count": total.get("llm_call_count", 0),
            "cache_eligible_count": total.get("cache_eligible_count", 0),
            "cache_hit_count": total.get("cache_hit_count", 0),
            "cache_hit_rate": total.get("cache_hit_rate", 0.0),
            "cache_input_ratio": total.get("cache_input_ratio", 0.0),
            "llm_output_input_ratio": total.get("llm_output_input_ratio", 0.0),
            "llm_reasoning_output_ratio": total.get(
                "llm_reasoning_output_ratio", 0.0
            ),
            "llm_output_tokens_per_second": total.get(
                "llm_output_tokens_per_second", 0.0
            ),
            "tool_call_count": total.get("tool_call_count", 0),
            "tool_success_count": total.get("tool_success_count", 0),
            "tool_success_rate": total.get("tool_success_rate", 0.0),
            "empty_tool_result_count": total.get("empty_tool_result_count", 0),
            "zero_source_search_count": total.get("zero_source_search_count", 0),
        }

    async def stream_message(
        self,
        messages: list[Any],
        config: RunnableConfig | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream protocol events for a complete research request."""
        validate_client_messages(messages)
        if self.run_fence_token is not None:
            raise RuntimeError("run_already_active")
        self.config = freeze_run_config(_ensure_config(config, self.config))
        self.run_id = self.config["metadata"]["run_id"]
        self._restored_query_states = {}
        self._configure_run_lease()
        self._configure_context_store()
        self.status = "running"
        state: dict[str, Any] = {
            "messages": normalize_messages(messages),
            "human_feedback": list(self.human_feedback),
        }
        self.messages = state["messages"]
        self._set_egress_intent(self.messages)
        try:
            await self.acquire_run_lease()
            await self._ensure_litellm_run_key()
            await self._register_sandbox_gateway()
            async for event in self._stream_new_message(state):
                yield event
        finally:
            try:
                await self._shutdown_teammates_before_gateway_release()
            finally:
                try:
                    await self._unregister_sandbox_gateway()
                finally:
                    try:
                        await self._finalize_litellm_run_key_if_terminal()
                    finally:
                        self._unbind_litellm_run_key()
                        try:
                            await close_run_gateway(self.run_id)
                        finally:
                            await self._close_litellm_key_manager()
                            await self.release_run_lease()

    async def _stream_new_message(
        self,
        state: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Initialize and execute a new run while its caller owns the lease."""
        await self._publish_public(
            "run.created",
            payload={"status": "pending"},
            dedupe_key="run:created",
        )
        if self.context_store is not None:
            try:
                self.context_store.initialize(_config_user_id(self.config), self.config)
                key_metadata = self.config.get("metadata", {}).get(
                    "litellm_run_key_metadata"
                )
                if key_metadata:
                    self.context_store._update_manifest(  # noqa: SLF001
                        litellm_key=RunKeyMetadata.model_validate(key_metadata),
                    )
                await self._persist_update(
                    channel="lead",
                    stage="received",
                    update={
                        "messages": {"type": "override", "value": state["messages"]},
                        "human_feedback": {"type": "override", "value": state["human_feedback"]},
                    },
                    extra={
                        "owner_id": _config_user_id(self.config),
                        "config": self.context_store._safe_config(self.config),  # noqa: SLF001
                        "coordination_schema_version": 1,
                        "coordination_backend": "rocketmq" if Configuration.from_runnable_config(self.config).enable_async_research else "file_mailbox",
                    },
                )
                await self._persist_checkpoint("received", "summarize_messages")
            except Exception as exc:  # noqa: BLE001 - brief persistence remains the strict gate
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
        async for event in self._stream_execution(state, "summarize_messages"):
            yield event

    async def stream_resume(self) -> AsyncIterator[dict[str, Any]]:
        """Replay a persisted Query run and continue from its stable checkpoint."""
        if self.context_store is None:
            raise JournalCorruptedError("run_context_persistence_disabled")
        self._validate_resume_manifest(self.context_store.load_manifest())
        try:
            await self.acquire_run_lease()
            await self._ensure_litellm_run_key()
            replay = self.context_store.replay()
            self._validate_resume_manifest(replay.manifest)
            self.persistence_degraded = replay.manifest.persistence_degraded
            state = replay.state
            if "sandbox_egress_intent" not in self.config.get("metadata", {}):
                self._set_egress_intent(state.get("messages", []))
            if state.get("fetch_budget_extension"):
                self.config.setdefault("metadata", {})["fetch_budget_extension"] = state["fetch_budget_extension"]
            await self._register_sandbox_gateway()
            self._restored_query_states = dict(replay.query_states)
            resume_stage = replay.manifest.next_stage
            if self.config.get("metadata", {}).get("legacy_config_migration"):
                await self._migrate_legacy_quality_artifacts(state)
                for field_name in (
                    "notes",
                    "raw_notes",
                    "candidate_registry",
                    "document_registry",
                    "evidence_registry",
                    "web_research_iterations",
                    "completed_task_outputs",
                    "handoff_assessments",
                    "completion_decision",
                    "result_assessment",
                    "quality_gate",
                ):
                    if field_name in state:
                        replay.supervisor_state[field_name] = state[field_name]
                await self._persist_update(
                    channel="lead",
                    stage="legacy_config_migrated",
                    update=self._legacy_migration_state_update(state),
                    extra={
                        "quality_evaluation_epoch": self.config.get(
                            "metadata", {}
                        ).get("quality_evaluation_epoch"),
                    },
                )
                resume_stage = "supervisor.supervisor"
                await self._persist_checkpoint(
                    "legacy_config_migrated",
                    resume_stage,
                )
                if not self.persistence_degraded:
                    self.config.get("metadata", {}).pop(
                        "legacy_config_migration",
                        None,
                    )
                self.context_store._update_manifest(  # noqa: SLF001
                    config=self.context_store._safe_config(self.config),  # noqa: SLF001
                    config_fingerprint=self.config.get("metadata", {}).get(
                        "run_config_fingerprint"
                    ),
                    quality_policy_version=self.config.get("metadata", {}).get(
                        "quality_policy_version"
                    ),
                    quality_evaluation_epoch=self.config.get("metadata", {}).get(
                        "quality_evaluation_epoch"
                    ),
                    quality_evaluation_rigor=self.config.get(
                        "metadata", {}
                    ).get("quality_rigor_policy", {}).get("rigor"),
                    quality_rigor_policy=self.config.get(
                        "metadata", {}
                    ).get("quality_rigor_policy", {}),
                    quality_configuration_warnings=self.config.get(
                        "metadata", {}
                    ).get("quality_configuration_warnings", []),
                )
            self.messages = list(state.get("messages", []))
            self.human_feedback = list(state.get("human_feedback", []))
            self._feedback_cursor = len(self.human_feedback)
            self.status = "running"
            if state.get("enable_async_research"):
                from open_deep_research.agents import deep_researcher as graph

                await graph.restore_async_research_tasks(self.config)
            try:
                self.context_store._update_manifest(  # noqa: SLF001
                    allow_failed_resume=True,
                    status="running",
                    result=None,
                    pending_human_action=replay.manifest.pending_human_action,
                    recovered_from_degraded_persistence=(
                        replay.manifest.persistence_degraded
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
            async for event in self._stream_execution(
                state,
                resume_stage,
                restored_supervisor_state=replay.supervisor_state or None,
                recovered=True,
            ):
                yield event
        finally:
            try:
                await self._shutdown_teammates_before_gateway_release()
            finally:
                try:
                    await self._unregister_sandbox_gateway()
                finally:
                    try:
                        await self._finalize_litellm_run_key_if_terminal()
                    finally:
                        self._unbind_litellm_run_key()
                        try:
                            await close_run_gateway(self.run_id)
                        finally:
                            await self._close_litellm_key_manager()
                            await self.release_run_lease()

    async def _stream_execution(
        self,
        state: dict[str, Any],
        start_stage: str,
        *,
        restored_supervisor_state: dict[str, Any] | None = None,
        recovered: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute or resume the outer Query pipeline from a stable stage."""
        recorder = get_trace_recorder(self.config)
        run_metadata = {
            "thread_id": self.config.get("configurable", {}).get("thread_id"),
            "message_count": len(state["messages"]),
            "recovered": recovered,
        }

        with recorder.start_run(
            self.run_id,
            name="lead.run",
            user_id=_config_user_id(self.config),
            metadata=run_metadata,
            input_payload=state.get("messages", []),
        ):
            event_name = "run.resumed" if recovered else "run.started"
            run_transition_key = "run:started"
            if recovered:
                publisher = event_publisher_from_config(self.config)
                previous_resumes = await asyncio.to_thread(
                    lambda: sum(
                        event.type == "run.resumed"
                        for event in publisher.store.read()
                    )
                )
                run_transition_key = f"run:resumed:{previous_resumes + 1}"
            await self._publish_public(
                event_name,
                payload={"status": "running", "recovered": recovered},
                dedupe_key=run_transition_key,
            )
            public_stage = self._public_stage_for_internal(start_stage)
            await self._public_stage_event(public_stage, "started")
            yield self._event(event_name, {"run_id": self.run_id, "recovered": recovered})
            try:
                from open_deep_research.agents import deep_researcher as graph
                stage = start_stage

                if stage == "summarize_messages":
                    await self._run_node("summarize_messages", graph.summarize_messages, state)
                    await self._persist_checkpoint("messages_compacted", "memory_recall")
                    yield self._event("lead.message", {"stage": "summarize_messages"})
                    stage = "memory_recall"

                if stage == "memory_recall":
                    await self._run_node("memory_recall", graph.memory_recall, state)
                    await self._persist_checkpoint("memory_recalled", "clarify_with_user")
                    yield self._event("lead.message", {"stage": "memory_recall"})
                    stage = "clarify_with_user"

                if stage == "clarify_with_user":
                    clarify = await self._run_node("clarify_with_user", graph.clarify_with_user, state)
                    yield self._event("lead.message", {"stage": "clarify_with_user", "goto": clarify.goto})
                    if clarify.goto == END:
                        async for hitl_event in self._await_clarification(state):
                            yield hitl_event
                        if self.cancelled:
                            await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                            self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                            self._cancelled_state(state)
                            await self._publish_public_cancelled()
                            yield self._event("run.cancelled", {"run_id": self.run_id, "status": "cancelled"})
                            return
                    await self._persist_checkpoint("clarified", "write_research_brief")
                    stage = "write_research_brief"

                if stage == "clarification_wait":
                    restored_action = None
                    if self.context_store is not None:
                        restored_action = self.context_store.load_manifest().pending_human_action
                    async for hitl_event in self._await_clarification(
                        state,
                        restored_action=restored_action,
                    ):
                        yield hitl_event
                    if self.cancelled:
                        await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                        self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                        self._cancelled_state(state)
                        await self._publish_public_cancelled()
                        yield self._event("run.cancelled", {"run_id": self.run_id, "status": "cancelled"})
                        return
                    await self._persist_checkpoint("clarified", "write_research_brief")
                    stage = "write_research_brief"

                if stage == "write_research_brief":
                    await self._run_node("write_research_brief", graph.write_research_brief, state)
                    if self.context_store is not None:
                        # Brief persistence is intentionally the only strict local write.
                        try:
                            if not self.context_store.manifest_path.exists():
                                self.context_store.initialize(_config_user_id(self.config), self.config)
                            digest = self.context_store.persist_research_brief(str(state.get("research_brief", "")))
                        except ResearchBriefPersistenceError:
                            raise
                        except Exception as exc:
                            raise ResearchBriefPersistenceError("research_brief_persistence_failed") from exc
                        await self._persist_artifact_event("research_brief_written", "research_brief.md", digest)
                        coverage_contract = state.get("coverage_contract")
                        if isinstance(coverage_contract, dict):
                            coverage_digest = (
                                self.context_store.write_json_atomic(
                                    "coverage_contract.json",
                                    coverage_contract,
                                )
                            )
                            await self._persist_artifact_event(
                                "coverage_contract_written",
                                "coverage_contract.json",
                                coverage_digest,
                            )
                    await self._persist_checkpoint("research_brief_written", "plan_approval")
                    yield self._event("lead.message", {"stage": "write_research_brief"})
                    await self._public_stage_event("preparing", "completed")
                    await self._public_stage_event("planning", "started")
                    stage = "plan_approval"

                if self.context_store is not None and self.context_store.brief_path.exists():
                    state["research_brief"] = self.context_store.load_research_brief()

                if stage == "plan_approval":
                    if not state.get("research_plan"):
                        plan = self._draft_research_plan(state)
                        state["research_plan"] = plan
                        state["approved_research_plan"] = plan
                    await self._publish_public(
                        "plan.created",
                        stage="planning",
                        payload={
                            "plan_id": f"plan-{self.run_id}",
                            "revision": 1,
                            "objective": str(state.get("research_brief") or "")[:1200],
                            "stages": [
                                {"id": "preparing", "title": "理解请求"},
                                {"id": "planning", "title": "制定计划"},
                                {"id": "researching", "title": "并行研究"},
                                {"id": "synthesizing", "title": "汇总证据"},
                                {"id": "writing", "title": "生成报告"},
                                {"id": "finalizing", "title": "完成研究"},
                            ],
                        },
                        dedupe_key="plan:created:1",
                    )
                    with recorder.start_span(
                        name="node.plan_approval",
                        kind="agent",
                        agent_role="lead",
                        attributes={"hitl_enabled": self._hitl_enabled()},
                    ):
                        async for hitl_event in self._maybe_await_plan_approval(state):
                            yield hitl_event
                    if self.cancelled:
                        await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                        self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                        self._cancelled_state(state)
                        await self._publish_public_cancelled()
                        yield self._event(
                            "run.cancelled",
                            {
                                "run_id": self.run_id,
                                "usage": self._usage_subset(self.total_usage),
                                "usage_accounting": self.total_usage.get(
                                    "usage_accounting"
                                ),
                                "metrics": self._metrics_subset(self.total_usage),
                            },
                        )
                        return
                    approved_plan = str(state.get("approved_research_plan") or "")
                    await self._write_optional_text_artifact(
                        "plan_approved", "approved_plan.md", approved_plan
                    )
                    await self._persist_update(
                        channel="lead",
                        stage="plan_approved",
                        update={
                            "research_plan": state.get("research_plan"),
                            "approved_research_plan": state.get("approved_research_plan"),
                            "human_feedback": {"type": "override", "value": list(self.human_feedback)},
                        },
                    )
                    await self._persist_checkpoint("plan_approved", "supervisor.supervisor")
                    await self._public_stage_event("planning", "completed")
                    await self._public_stage_event("researching", "started")
                    stage = "supervisor.supervisor"

                if stage.startswith("supervisor.") or stage == "fetch_budget_approval":
                    # A crash after the durable decision must neither re-ask
                    # nor replay the exhausted supervisor's terminal query.
                    resolved_budget = state.get("fetch_budget_resolution") if stage == "fetch_budget_approval" else None
                    if resolved_budget and state.get("fetch_budget_action_id"):
                        action_id = state["fetch_budget_action_id"]
                        await self._publish_public(
                            "approval.resolved", stage="researching",
                            payload={"action_id": action_id, "approval_type": "fetch_budget",
                                     "action": resolved_budget, "status": "resolved"},
                            dedupe_key=f"approval:fetch_budget:{action_id}:resolved",
                        )
                    if resolved_budget == "approve":
                        self._restored_query_states.pop("supervisor", None)
                        restored_supervisor_state = None
                        stage = "supervisor.supervisor"
                    while True:
                        if resolved_budget == "deny":
                            break
                        if stage != "fetch_budget_approval":
                            supervisor_step = stage.split(".", 1)[1]
                            if restored_supervisor_state is None and supervisor_step == "supervisor":
                                supervisor_update = await self._run_supervisor(state)
                            else:
                                supervisor_update = await self._run_supervisor(
                                    state, restored_state=restored_supervisor_state,
                                    start_step=supervisor_step,
                                )
                            apply_update_to_state(state, supervisor_update)
                            await self._persist_update(
                                channel="lead", stage="research_complete", update=supervisor_update,
                            )
                        if state.get("completion_decision", {}).get("reason") != "fetch_budget_exhausted":
                            break
                        async for budget_event in self._await_fetch_budget_approval(state):
                            yield budget_event
                        if state.get("completion_decision", {}).get("reason") == "fetch_budget_exhausted":
                            break
                        # A grant starts a new bounded research cycle with all
                        # collected evidence and the previous conversation.
                        restored_supervisor_state = None
                        stage = "supervisor.supervisor"
                    completion_action = str(
                        state.get("completion_decision", {}).get("action", "")
                    )
                    if completion_action == CompletionDecision.TERMINATE.value:
                        gaps = list(
                            state.get("completion_decision", {}).get("gaps", [])
                        )
                        recovery = {"mode": "failed", "artifact_refs": []}
                        if "accepted_evidence" in gaps:
                            recovery = await self._recover_quality_gate_termination(
                                state
                            )
                            await self._persist_update(
                                channel="lead",
                                stage="quality_gate_reassessed",
                                update=self._quality_recovery_state_update(state),
                            )
                        if recovery["mode"] == "accepted":
                            caveat_task_ids = set(
                                recovery.get("caveat_task_ids", [])
                            )
                            for task_id in recovery.get(
                                "accepted_task_ids", []
                            ):
                                admission_status = (
                                    AdmissionStatus.ACCEPTED_WITH_CAVEATS.value
                                    if task_id in caveat_task_ids
                                    else AdmissionStatus.ACCEPTED.value
                                )
                                await self._publish_public(
                                    "research.task.completed",
                                    stage="researching",
                                    payload={
                                        "task_id": task_id,
                                        "wave_id": "",
                                        "mode": "sync",
                                        "status": "completed",
                                        "phase": "completed",
                                        "admission_status": admission_status,
                                        "reason_code": "quality_gate_reassessed",
                                        "source_count": len(
                                            eligible_evidence_records(
                                                state.get(
                                                    "evidence_registry", []
                                                )
                                            )
                                        ),
                                        "summary_status": "unavailable",
                                        "message": (
                                            "Research evidence was admitted after "
                                            "automatic SHA-verified reassessment."
                                        ),
                                    },
                                    dedupe_key=(
                                        f"task:{task_id}:admission:"
                                        f"{admission_status}"
                                    ),
                                )
                            completion_action = CompletionDecision.COMPLETE.value
                        elif recovery["mode"] == "partial":
                            report_text = str(state.get("final_report", ""))
                            if self._report_review_enabled():
                                # Evidence-limited recovery still produces a
                                # draft first.  It must pass through the same
                                # Reviewer/Revisor loop as a model-written
                                # report before becoming ``final_report``.
                                await self._publish_public(
                                    "system.warning",
                                    stage="researching",
                                    payload={
                                        "warning_code": "quality_gate_recovery",
                                        "message": (
                                            "The quality gate rejected the full handoff; "
                                            "a deterministic accepted-evidence report "
                                            "was recovered."
                                        ),
                                    },
                                    dedupe_key="quality-gate:recovery-warning",
                                )
                                await self._public_stage_event(
                                    "researching", "completed"
                                )
                                await self._public_stage_event(
                                    "synthesizing", "started"
                                )
                                await self._public_stage_event(
                                    "synthesizing", "completed"
                                )
                                await self._public_stage_event("writing", "started")
                                state["final_report_draft"] = report_text
                                state["final_report"] = ""
                                state["report_draft_metadata"] = {
                                    "markdown": report_text,
                                    "body_markdown": report_text,
                                    "report_type": "default",
                                    "output_format": "markdown",
                                    "reference_style": "numbered",
                                }
                                draft_sha256 = hashlib.sha256(
                                    report_text.encode("utf-8")
                                ).hexdigest()
                                await self._write_optional_text_artifact(
                                    "report_draft_generated",
                                    "final_draft.md",
                                    report_text,
                                )
                                await self._persist_update(
                                    channel="lead",
                                    stage="report_draft_generated",
                                    update={
                                        "final_report_draft": report_text,
                                        "final_report": "",
                                        "report_draft_metadata": state[
                                            "report_draft_metadata"
                                        ],
                                    },
                                    extra={
                                        "draft_sha256": draft_sha256,
                                        "attempt": 0,
                                        "source": "evidence_limited_recovery",
                                    },
                                )
                                await self._persist_checkpoint(
                                    "report_draft_generated",
                                    "report_review",
                                    payload={
                                        "draft_sha256": draft_sha256,
                                        "attempt": 0,
                                        "source": "evidence_limited_recovery",
                                    },
                                )
                                async for report_event in self._stream_report_pipeline(
                                    state,
                                    "report_review",
                                    recorder,
                                    graph,
                                ):
                                    yield report_event
                                with recorder.start_span(
                                    name="node.memory_extract_and_write",
                                    kind="agent",
                                    agent_role="lead",
                                ):
                                    memory_cmd = coerce_command(
                                        await self.cancellation_scope.run(
                                            graph.memory_extract_and_write(
                                                state,
                                                self.config,
                                            ),
                                            stage="memory_extract_and_write",
                                        ),
                                        default_goto=END,
                                    )
                                apply_update_to_state(state, memory_cmd.update)
                                await self._persist_update(
                                    channel="lead",
                                    stage="memory_written",
                                    update=memory_cmd.update,
                                )
                                await self._persist_checkpoint(
                                    "memory_written",
                                    "completed",
                                )
                                await self._public_stage_event(
                                    "finalizing",
                                    "completed",
                                )
                                async for event in self._finish_success(
                                    state,
                                    recorder,
                                    str(state.get("final_report", "")),
                                ):
                                    yield event
                                return
                            await self._publish_public(
                                "system.warning",
                                stage="researching",
                                payload={
                                    "warning_code": "quality_gate_recovery",
                                    "message": (
                                        "The quality gate rejected the full handoff; "
                                        "a deterministic accepted-evidence report "
                                        "was recovered."
                                    ),
                                },
                                dedupe_key="quality-gate:recovery-warning",
                            )
                            await self._public_stage_event(
                                "researching", "completed"
                            )
                            await self._public_stage_event(
                                "synthesizing", "started"
                            )
                            await self._public_stage_event(
                                "synthesizing", "completed"
                            )
                            await self._public_stage_event("writing", "started")
                            await self._publish_public(
                                "report.started",
                                stage="writing",
                                payload={"status": "running"},
                                dedupe_key="report:started",
                            )
                            await self._write_report_bundle(state)
                            await self._persist_checkpoint(
                                "report_generated", "completed"
                            )
                            await self._publish_public(
                                "report.completed",
                                stage="writing",
                                payload={
                                    "status": "completed",
                                    "result_ref": f"/runs/{self.run_id}",
                                    "sha256": hashlib.sha256(
                                        report_text.encode("utf-8")
                                    ).hexdigest(),
                                    "length": len(report_text),
                                },
                                dedupe_key="report:completed",
                            )
                            yield self._event(
                                "report.completed", {"run_id": self.run_id}
                            )
                            await self._public_stage_event(
                                "writing", "completed"
                            )
                            await self._public_stage_event(
                                "finalizing", "started"
                            )
                            await self._public_stage_event(
                                "finalizing", "completed"
                            )
                            async for event in self._finish_success(
                                state,
                                recorder,
                                report_text,
                            ):
                                yield event
                            return
                        else:
                            reason = str(
                                state.get("completion_decision", {}).get(
                                    "reason", "insufficient_evidence"
                                )
                            )
                            self.status = "failed"
                            result = {
                                "status": "failed",
                                "error_code": "insufficient_evidence",
                                "termination_reason": reason,
                                "completion": state.get(
                                    "completion_decision", {}
                                ),
                                "quality_gate": state.get("quality_gate")
                                or self._quality_gate_from_handoff_assessments(
                                    state
                                )
                                or self._quality_gate_payload(
                                    "failed",
                                    reason_codes=[
                                        "no_eligible_evidence"
                                    ],
                                ),
                                "recoverable_artifacts": recovery.get(
                                    "artifact_refs", []
                                ),
                            }
                            self.final_state = {**state, "result": result}
                            await self._persist_update(
                                channel="lead",
                                stage="terminated",
                                update={
                                    "completion_decision": state.get(
                                        "completion_decision", {}
                                    ),
                                    "quality_gate": result["quality_gate"],
                                    "recoverable_artifacts": result[
                                        "recoverable_artifacts"
                                    ],
                                },
                            )
                            await self._persist_checkpoint(
                                "terminated",
                                "terminated",
                                status="failed",
                            )
                            if self.context_store is not None:
                                self.context_store._update_manifest(  # noqa: SLF001
                                    status="failed",
                                    result=result,
                                )
                            self.total_usage = recorder.finish_run(
                                self.run_id, "failed"
                            )
                            await self._publish_public(
                                "run.failed",
                                stage="researching",
                                payload={
                                    "status": "failed",
                                    "error_code": "insufficient_evidence",
                                    "message": (
                                        "Research ended without eligible "
                                        "accepted evidence."
                                    ),
                                    "termination_reason": reason,
                                    "result_status": "failed",
                                    "permission_denial_count": len(
                                        self.permission_denials
                                    ),
                                },
                                dedupe_key="run:terminal",
                            )
                            yield self._event(
                                "run.failed", {"run_id": self.run_id, **result}
                            )
                            return
                    state["human_feedback"] = list(self.human_feedback)
                    await self._persist_task_outputs(state)
                    await self._persist_checkpoint("research_complete", "outline_approval")
                    yield self._event("lead.message", {"stage": "research_supervisor"})
                    await self._public_stage_event("researching", "completed")
                    await self._public_stage_event("synthesizing", "started")
                    stage = "outline_approval"

                if stage == "outline_approval":
                    with recorder.start_span(
                        name="node.outline_approval",
                        kind="agent",
                        agent_role="lead",
                        attributes={"hitl_enabled": self._hitl_enabled()},
                    ):
                        async for hitl_event in self._maybe_await_outline_approval(state):
                            yield hitl_event
                    if self.cancelled:
                        await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                        self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                        self._cancelled_state(state)
                        await self._publish_public_cancelled()
                        yield self._event(
                            "run.cancelled",
                            {
                                "run_id": self.run_id,
                                "usage": self._usage_subset(self.total_usage),
                                "usage_accounting": self.total_usage.get(
                                    "usage_accounting"
                                ),
                                "metrics": self._metrics_subset(self.total_usage),
                            },
                        )
                        return
                    outline = str(state.get("report_outline") or "")
                    await self._write_optional_text_artifact(
                        "outline_approved", "report_outline.md", outline
                    )
                    await self._persist_update(
                        channel="lead",
                        stage="outline_approved",
                        update={"report_outline": state.get("report_outline")},
                    )
                    await self._persist_checkpoint("outline_approved", "final_report_generation")
                    await self._public_stage_event("synthesizing", "completed")
                    await self._public_stage_event("writing", "started")
                    stage = "final_report_generation"

                if stage == "final_report_generation":
                    if self._report_review_enabled():
                        if self.context_store is not None:
                            state["research_brief"] = self.context_store.load_research_brief()
                        async for report_event in self._stream_report_pipeline(
                            state,
                            "final_report_generation",
                            recorder,
                            graph,
                        ):
                            yield report_event
                        stage = "memory_extract_and_write"
                    else:
                        # Legacy one-shot report path.  Keep this branch byte and
                        # event compatible for the default (review-disabled)
                        # profile and for old monkeypatched graph nodes.
                        await self._publish_public(
                            "report.started",
                            stage="writing",
                            payload={"status": "running"},
                            dedupe_key="report:started",
                        )
                        if self.context_store is not None:
                            state["research_brief"] = self.context_store.load_research_brief()
                        with recorder.start_span(name="node.final_report_generation", kind="agent", agent_role="lead"):
                            report_update = await self.cancellation_scope.run(
                                graph.final_report_generation(state, self.config),
                                stage="final_report_generation",
                            )
                        self.cancellation_scope.checkpoint("report_generated")
                        apply_update_to_state(state, report_update)
                        await self._persist_update(
                            channel="lead",
                            stage="report_generated",
                            update=report_update,
                        )
                        await self._write_report_bundle(state)
                        await self._persist_checkpoint("report_generated", "memory_extract_and_write")
                        report_text = str(state.get("final_report", ""))
                        await self._publish_public(
                            "report.completed",
                            stage="writing",
                            payload={
                                "status": "completed",
                                "result_ref": f"/runs/{self.run_id}",
                                "sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
                                "length": len(report_text),
                            },
                            dedupe_key="report:completed",
                        )
                        yield self._event("report.completed", {"run_id": self.run_id})
                        await self._public_stage_event("writing", "completed")
                        await self._public_stage_event("finalizing", "started")
                        stage = "memory_extract_and_write"

                if stage == "report_review":
                    if not self._report_review_enabled():
                        # Internal Reviewer stages belong to v11 runs. A
                        # manually-authored old checkpoint cannot safely skip
                        # straight to memory with an empty report.
                        raise RuntimeError("report_review_disabled_for_checkpoint")
                    async for report_event in self._stream_report_pipeline(
                        state,
                        "report_review",
                        recorder,
                        graph,
                    ):
                        yield report_event
                    stage = "memory_extract_and_write"

                if stage == "report_revision":
                    async for report_event in self._stream_report_pipeline(
                        state,
                        "report_revision",
                        recorder,
                        graph,
                    ):
                        yield report_event
                    stage = "memory_extract_and_write"

                if stage == "report_finalize":
                    async for report_event in self._stream_report_pipeline(
                        state,
                        "report_finalize",
                        recorder,
                        graph,
                    ):
                        yield report_event
                    stage = "memory_extract_and_write"

                if stage == "memory_extract_and_write":
                    with recorder.start_span(name="node.memory_extract_and_write", kind="agent", agent_role="lead"):
                        memory_cmd = coerce_command(
                            await self.cancellation_scope.run(
                                graph.memory_extract_and_write(state, self.config),
                                stage="memory_extract_and_write",
                            ),
                            default_goto=END,
                        )
                    apply_update_to_state(state, memory_cmd.update)
                    await self._persist_update(
                        channel="lead",
                        stage="memory_written",
                        update=memory_cmd.update,
                    )
                    await self._persist_checkpoint("memory_written", "completed")
                    await self._public_stage_event("finalizing", "completed")

                async for event in self._finish_success(state, recorder, str(state.get("final_report", ""))):
                    yield event
                return
            except RunCancelled:
                from open_deep_research.tasks.teammate_pool import (
                    shutdown_teammate_pool,
                )

                try:
                    await shutdown_teammate_pool(self.config)
                except Exception:
                    pass
                self._clear_run_resources()
                self.status = "cancelled"
                await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                self._cancelled_state(state)
                await self._publish_public_cancelled()
                yield self._event(
                    "run.cancelled",
                    {
                        "run_id": self.run_id,
                        "usage": self._usage_subset(self.total_usage),
                        "usage_accounting": self.total_usage.get(
                            "usage_accounting"
                        ),
                        "metrics": self._metrics_subset(self.total_usage),
                    },
                )
                return
            except Exception as exc:
                from open_deep_research.tasks.teammate_pool import (
                    shutdown_teammate_pool,
                )

                try:
                    await shutdown_teammate_pool(self.config)
                except Exception:
                    pass
                self._clear_run_resources()
                if self.cancelled:
                    self.status = "cancelled"
                    await self._persist_checkpoint("cancelled", "cancelled", status="cancelled")
                    self.total_usage = recorder.finish_run(self.run_id, "cancelled")
                    self._cancelled_state(state)
                    await self._publish_public_cancelled()
                    yield self._event(
                        "run.cancelled",
                        {
                            "run_id": self.run_id,
                            "usage": self._usage_subset(self.total_usage),
                            "usage_accounting": self.total_usage.get(
                                "usage_accounting"
                            ),
                            "metrics": self._metrics_subset(self.total_usage),
                        },
                    )
                    return
                self.status = "failed"
                try:
                    failed_stage = self._public_stage_for_internal(stage)
                    await self._publish_public(
                        "stage.failed",
                        stage=failed_stage,
                        payload={
                            "stage_id": failed_stage,
                            "stage_index": PUBLIC_STAGES.index(failed_stage) + 1,
                            "stage_count": len(PUBLIC_STAGES),
                            "error_code": "run_stage_failed",
                            "message": "The research stage failed.",
                        },
                        dedupe_key=f"stage:{failed_stage}:failed",
                    )
                    await self._publish_public(
                        "run.interrupted",
                        stage=failed_stage,
                        payload={
                            "status": "interrupted",
                            "error_code": "research_run_interrupted",
                            "message": "The research run was interrupted and can be resumed.",
                            "termination_reason": "internal_error",
                            "result_status": "error",
                            "permission_denial_count": len(self.permission_denials),
                        },
                        dedupe_key=f"run:interrupted:{self.run_id}",
                    )
                except Exception:
                    pass
                if self.context_store is not None:
                    try:
                        await self.context_store.append(
                            channel="lead",
                            record_type="run_status",
                            stage="failed",
                            payload={"status": "failed", "error": str(exc)},
                        )
                        # Preserve the last stable next_stage for explicit recovery.
                        self.context_store._update_manifest(  # noqa: SLF001
                            status="failed",
                            result={"status": "error", "error": str(exc)},
                        )
                    except Exception:
                        pass
                self.total_usage = recorder.finish_run(self.run_id, "error", str(exc))
                self.final_state = {
                    **state,
                    "result": {
                        "status": "error",
                        "error": str(exc),
                        "usage": self._usage_subset(self.total_usage),
                        "usage_accounting": self.total_usage.get(
                            "usage_accounting"
                        ),
                        "metrics": self._metrics_subset(self.total_usage),
                        "permission_denials": self.permission_denials,
                        "persistence_degraded": self.persistence_degraded,
                    },
                }
                yield self._event(
                    "run.failed",
                    {
                        "run_id": self.run_id,
                        "error": str(exc),
                        "usage": self._usage_subset(self.total_usage),
                        "usage_accounting": self.total_usage.get(
                            "usage_accounting"
                        ),
                        "metrics": self._metrics_subset(self.total_usage),
                    },
                )
    async def _migrate_legacy_quality_artifacts(
        self,
        state: dict[str, Any],
    ) -> None:
        """Re-evaluate legacy SHA artifacts under the newly pinned epoch."""
        state["result_assessment"] = {}
        state["evaluation_snapshot"] = {}
        state["quality_gate"] = {}
        state["completion_decision"] = {}
        state["final_report"] = ""
        state["final_report_draft"] = ""
        state["report_review"] = {}
        state["report_review_history"] = []
        state["report_revision_count"] = 0
        state["report_outline"] = ""
        state["report_artifacts"] = {}
        state["canonical_report"] = {}
        state["sources"] = []
        state["coverage_checklist"] = []
        configurable = Configuration.from_runnable_config(self.config)
        if not configurable.quality_evaluation_enabled:
            state["handoff_assessments"] = []
            return

        for field_name in (
            "notes",
            "raw_notes",
            "candidate_registry",
            "document_registry",
            "evidence_registry",
            "web_research_iterations",
            "completed_task_outputs",
        ):
            state[field_name] = []

        raw_refs = state.get("research_artifact_refs", {})
        if isinstance(raw_refs, dict) and raw_refs.get("type") == "override":
            raw_refs = raw_refs.get("value", {})
        assessments: list[dict[str, Any]] = []
        if not isinstance(raw_refs, dict) or self.context_store is None:
            state["handoff_assessments"] = assessments
            return

        metadata = self.config.get("metadata", {})
        for task_id, raw_ref in raw_refs.items():
            if not isinstance(raw_ref, dict) or not raw_ref.get("sha256"):
                assessments.append(
                    {
                        "tool_call_id": str(task_id),
                        "trigger": "legacy_migration_reassessment",
                        "accepted": False,
                        "reason": "artifact_reference_invalid",
                        "evaluator_error": "artifact_reference_invalid",
                        "evaluator_model": configurable.quality_evaluation_model,
                        "policy_version": metadata.get(
                            "quality_policy_version", QUALITY_POLICY_VERSION
                        ),
                        "evaluation_epoch": metadata.get(
                            "quality_evaluation_epoch", "legacy-unpinned"
                        ),
                        "quality_rigor": metadata.get(
                            "quality_rigor_policy", {}
                        ).get("rigor", configurable.quality_evaluation_rigor.value),
                        "quality_thresholds": dict(
                            metadata.get("quality_rigor_policy", {})
                        ),
                    }
                )
                continue
            artifact_ref = {
                "task_id": str(task_id),
                "path": str(
                    raw_ref.get("path")
                    or f"context/artifacts/research_tasks/{task_id}.json"
                ),
                "sha256": str(raw_ref["sha256"]),
            }
            try:
                artifact = self.context_store.load_task_result(
                    str(task_id),
                    expected_sha256=artifact_ref["sha256"],
                )
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                assessments.append(
                    {
                        "tool_call_id": str(task_id),
                        "trigger": "legacy_migration_reassessment",
                        "artifact_sha256": artifact_ref["sha256"],
                        "accepted": False,
                        "reason": "artifact_integrity_failed",
                        "evaluator_error": str(exc),
                        "evaluator_model": configurable.quality_evaluation_model,
                        "policy_version": metadata.get(
                            "quality_policy_version", QUALITY_POLICY_VERSION
                        ),
                        "evaluation_epoch": metadata.get(
                            "quality_evaluation_epoch", "legacy-unpinned"
                        ),
                        "quality_rigor": metadata.get(
                            "quality_rigor_policy", {}
                        ).get("rigor", configurable.quality_evaluation_rigor.value),
                        "quality_thresholds": dict(
                            metadata.get("quality_rigor_policy", {})
                        ),
                    }
                )
                continue
            topic = str(
                artifact.get("research_topic")
                or artifact.get("topic")
                or state.get("research_brief", "")
            )
            assessment = await evaluate_subagent_handoff(
                topic,
                artifact,
                self.config,
                **(
                    {
                        "coverage_contract": artifact["coverage_contract"],
                        "requirement_ids": list(
                            artifact.get("requirement_ids", [])
                        ),
                        "risk_profile": (
                            ResearchRiskProfile.model_validate(
                                artifact.get("research_risk_profile")
                                or state.get("research_risk_profile")
                                or {"level": "standard"}
                            )
                        ),
                    }
                    if artifact.get("coverage_contract")
                    else {}
                ),
            )
            record = {
                "tool_call_id": str(task_id),
                "trigger": "legacy_migration_reassessment",
                "artifact_sha256": artifact_ref["sha256"],
                **assessment.model_dump(mode="json"),
            }
            assessments.append(record)
            if assessment.accepted:
                admission_status = (
                    assessment.admission_status.value
                    if assessment.admission_status is not None
                    else AdmissionStatus.ACCEPTED.value
                )
                self._merge_admitted_artifact(
                    state,
                    str(task_id),
                    artifact,
                    artifact_ref,
                    admission_status=admission_status,
                )
                state["coverage_ledger"] = merge_coverage_ledger(
                    dict(state.get("coverage_ledger", {})),
                    task_id=str(task_id),
                    assessment=assessment,
                    owned_requirement_ids=artifact.get("requirement_ids", []),
                )
        state["handoff_assessments"] = assessments

    @staticmethod
    def _legacy_migration_state_update(
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a reducer-safe journal update for legacy re-evaluation."""
        list_fields = (
            "notes",
            "raw_notes",
            "candidate_registry",
            "document_registry",
            "evidence_registry",
            "web_research_iterations",
            "completed_task_outputs",
            "handoff_assessments",
            "sources",
            "coverage_checklist",
        )
        update: dict[str, Any] = {
            field_name: {
                "type": "override",
                "value": list(state.get(field_name, [])),
            }
            for field_name in list_fields
            if field_name in state
        }
        update["completion_decision"] = {
            "type": "override",
            "value": {},
        }
        update["result_assessment"] = {
            "type": "override",
            "value": {},
        }
        update["evaluation_snapshot"] = {
            "type": "override",
            "value": {},
        }
        update["quality_gate"] = {
            "type": "override",
            "value": {},
        }
        update["final_report"] = ""
        update["final_report_draft"] = ""
        update["report_draft_metadata"] = {}
        update["report_review"] = {}
        update["report_review_history"] = []
        update["report_revision_count"] = 0
        update["report_outline"] = ""
        update["report_artifacts"] = {}
        update["canonical_report"] = {}
        return update

    @staticmethod
    def _extend_unique(target: list[Any], values: list[Any]) -> None:
        """Append JSON-native values without duplicating existing state."""
        for value in values:
            if value not in target:
                target.append(value)

    def _quality_gate_from_handoff_assessments(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Derive an honest terminal gate from the latest handoff assessments.

        Returns ``None`` when no assessment ever ran; callers fall back to an
        explicit failure payload instead of fabricating one that contradicts
        the admitted evidence (E2E round 9: 27 admitted sources projected as
        ``no_eligible_evidence`` with empty assessment refs).
        """
        assessments = [
            item
            for item in state.get("handoff_assessments", [])
            if isinstance(item, dict)
        ]
        if not assessments:
            return None
        latest_by_task = _latest_handoff_assessments(assessments)
        latest_records = list(latest_by_task.values())
        reason_codes: list[str] = []
        if any(item.get("evaluator_error") for item in latest_records):
            reason_codes.append("quality_evaluator_error")
        if any(item.get("accepted") is False for item in latest_records):
            reason_codes.append("handoff_rejected")
        has_caveat_admission = any(
            item.get("admission_status")
            == AdmissionStatus.ACCEPTED_WITH_CAVEATS.value
            for item in latest_records
        )
        quality_status = "degraded" if reason_codes else "passed"
        if has_caveat_admission and not reason_codes:
            quality_status = "passed_with_caveats"
            reason_codes.append("handoff_accepted_with_caveats")
        return self._quality_gate_payload(
            quality_status,
            reason_codes=reason_codes,
            assessment_refs=[
                {
                    "task_id": task_id,
                    "evaluator_model": str(item.get("evaluator_model", "")),
                    "evaluation_epoch": str(item.get("evaluation_epoch", "")),
                }
                for task_id, item in latest_by_task.items()
            ],
        )

    def _quality_gate_payload(
        self,
        status: str,
        *,
        reason_codes: list[str] | None = None,
        assessment_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        configurable = Configuration.from_runnable_config(self.config)
        metadata = self.config.get("metadata", {})
        return {
            "status": status,
            "evaluator_model": configurable.quality_evaluation_model,
            "policy_version": metadata.get(
                "quality_policy_version", QUALITY_POLICY_VERSION
            ),
            "evaluation_epoch": metadata.get(
                "quality_evaluation_epoch", "legacy-unpinned"
            ),
            "reason_codes": list(dict.fromkeys(reason_codes or [])),
            "assessment_refs": assessment_refs or [],
            "quality_rigor": metadata.get(
                "quality_rigor_policy", {}
            ).get("rigor", configurable.quality_evaluation_rigor.value),
            "quality_thresholds": dict(
                metadata.get("quality_rigor_policy", {})
            ),
        }

    def _merge_admitted_artifact(
        self,
        state: dict[str, Any],
        task_id: str,
        artifact: dict[str, Any],
        artifact_ref: dict[str, Any],
        *,
        admission_status: str = AdmissionStatus.ACCEPTED.value,
    ) -> None:
        """Merge a SHA-verified, newly admitted handoff into report state."""
        compressed = str(artifact.get("compressed_research", "")).strip()
        notes = list(state.get("notes", []))
        if compressed and compressed not in notes:
            notes.append(compressed)
        state["notes"] = notes
        for field_name in (
            "raw_notes",
            "candidate_registry",
            "document_registry",
            "evidence_registry",
            "web_research_iterations",
        ):
            current = list(state.get(field_name, []))
            incoming = artifact.get(field_name, [])
            if isinstance(incoming, list):
                self._extend_unique(current, incoming)
            state[field_name] = current
        outputs = list(state.get("completed_task_outputs", []))
        admitted_output = {
            **artifact,
            "task_id": task_id,
            "artifact_ref": artifact_ref,
            "admission_status": admission_status,
        }
        self._extend_unique(outputs, [admitted_output])
        state["completed_task_outputs"] = outputs

    async def _recover_quality_gate_termination(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Reassess rejected artifacts once, then recover safe evidence if needed."""
        configurable = Configuration.from_runnable_config(self.config)
        raw_refs = state.get("research_artifact_refs", {})
        if (
            isinstance(raw_refs, dict)
            and raw_refs.get("type") == "override"
        ):
            raw_refs = raw_refs.get("value", {})
        if (
            not configurable.quality_evaluation_enabled
            or not isinstance(raw_refs, dict)
            or not raw_refs
            or self.context_store is None
        ):
            return {"mode": "failed", "artifact_refs": []}

        assessment_history = [
            item
            for item in state.get("handoff_assessments", [])
            if isinstance(item, dict)
        ]
        latest_by_task: dict[str, dict[str, Any]] = {}
        for assessment in assessment_history:
            task_id = str(assessment.get("tool_call_id", ""))
            if task_id:
                latest_by_task[task_id] = assessment

        verified_artifacts: list[dict[str, Any]] = []
        verified_refs: list[dict[str, Any]] = []
        # Recovery conclusions must come from an artifact whose digest was
        # verified in this recovery pass, never from ambient/reduced state.
        recovered_evidence: list[dict[str, Any]] = []
        admitted_task_ids: list[str] = []
        caveat_task_ids: list[str] = []
        rejection_reasons: list[str] = []
        reason_codes: list[str] = []
        new_assessments: list[dict[str, Any]] = []

        for task_id, raw_ref in raw_refs.items():
            if not isinstance(raw_ref, dict) or not raw_ref.get("sha256"):
                reason_codes.append("artifact_reference_invalid")
                continue
            artifact_ref = {
                "task_id": str(task_id),
                "path": str(
                    raw_ref.get("path")
                    or f"context/artifacts/research_tasks/{task_id}.json"
                ),
                "sha256": str(raw_ref["sha256"]),
            }
            try:
                artifact = self.context_store.load_task_result(
                    str(task_id),
                    expected_sha256=artifact_ref["sha256"],
                )
            except (
                FileNotFoundError,
                ValueError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                reason_codes.append("artifact_integrity_failed")
                continue
            artifact["task_id"] = str(task_id)
            artifact["artifact_ref"] = artifact_ref
            verified_artifacts.append(artifact)
            verified_refs.append(artifact_ref)
            self._extend_unique(
                recovered_evidence,
                eligible_evidence_records(
                    artifact.get("evidence_registry", [])
                ),
            )

            latest = latest_by_task.get(str(task_id))
            already_reassessed = bool(
                latest
                and latest.get("trigger")
                in {
                    "artifact_read_reassessment",
                    "automatic_termination_reassessment",
                    "legacy_migration_reassessment",
                }
                and str(latest.get("artifact_sha256", artifact_ref["sha256"]))
                == artifact_ref["sha256"]
            )
            if latest and latest.get("accepted") is True:
                admission_status = str(
                    latest.get("admission_status")
                    or AdmissionStatus.ACCEPTED.value
                )
                self._merge_admitted_artifact(
                    state,
                    str(task_id),
                    artifact,
                    artifact_ref,
                    admission_status=admission_status,
                )
                state["coverage_ledger"] = merge_coverage_ledger(
                    dict(state.get("coverage_ledger", {})),
                    task_id=str(task_id),
                    assessment=HandoffAssessment.model_validate(latest),
                    owned_requirement_ids=artifact.get("requirement_ids", []),
                )
                admitted_task_ids.append(str(task_id))
                if (
                    admission_status
                    == AdmissionStatus.ACCEPTED_WITH_CAVEATS.value
                ):
                    caveat_task_ids.append(str(task_id))
                continue
            if already_reassessed and latest is not None:
                rejection_reasons.append(
                    str(latest.get("reason") or "artifact_reassessment_rejected")
                )
                continue

            topic = str(
                artifact.get("research_topic")
                or artifact.get("topic")
                or state.get("research_brief", "")
            )
            reassessment = await evaluate_subagent_handoff(
                topic,
                artifact,
                self.config,
                **(
                    {
                        "coverage_contract": artifact["coverage_contract"],
                        "requirement_ids": list(
                            artifact.get("requirement_ids", [])
                        ),
                        "risk_profile": (
                            ResearchRiskProfile.model_validate(
                                artifact.get("research_risk_profile")
                                or state.get("research_risk_profile")
                                or {"level": "standard"}
                            )
                        ),
                    }
                    if artifact.get("coverage_contract")
                    else {}
                ),
            )
            record = {
                "tool_call_id": str(task_id),
                "trigger": "automatic_termination_reassessment",
                "artifact_sha256": artifact_ref["sha256"],
                **reassessment.model_dump(mode="json"),
            }
            new_assessments.append(record)
            latest_by_task[str(task_id)] = record
            if reassessment.accepted:
                admission_status = (
                    reassessment.admission_status.value
                    if reassessment.admission_status is not None
                    else AdmissionStatus.ACCEPTED.value
                )
                self._merge_admitted_artifact(
                    state,
                    str(task_id),
                    artifact,
                    artifact_ref,
                    admission_status=admission_status,
                )
                state["coverage_ledger"] = merge_coverage_ledger(
                    dict(state.get("coverage_ledger", {})),
                    task_id=str(task_id),
                    assessment=reassessment,
                    owned_requirement_ids=artifact.get("requirement_ids", []),
                )
                admitted_task_ids.append(str(task_id))
                if (
                    admission_status
                    == AdmissionStatus.ACCEPTED_WITH_CAVEATS.value
                ):
                    caveat_task_ids.append(str(task_id))
            else:
                reason_codes.append("handoff_reassessment_rejected")
                rejection_reasons.append(reassessment.reason)
                rejection_reasons.extend(reassessment.missing_information)
                rejection_reasons.extend(reassessment.unsupported_claims)
                if reassessment.evaluator_error:
                    reason_codes.append("quality_evaluator_error")

        if new_assessments:
            assessment_history.extend(new_assessments)
            state["handoff_assessments"] = assessment_history
        assessment_refs = [
            {
                "task_id": ref["task_id"],
                "artifact_sha256": ref["sha256"],
                "evaluation_epoch": self.config.get("metadata", {}).get(
                    "quality_evaluation_epoch", "legacy-unpinned"
                ),
            }
            for ref in verified_refs
        ]

        if admitted_task_ids:
            coverage_contract = state.get("coverage_contract")
            contract_requirements = (
                coverage_contract.get("requirements", [])
                if isinstance(coverage_contract, dict)
                else []
            )
            coverage_ledger = dict(state.get("coverage_ledger", {}))
            # Process directives are satisfied by orchestration and deliverable
            # formats belong to the final report; only factual requirements
            # count as research coverage gaps here.
            uncovered_requirement_ids = [
                str(requirement.get("requirement_id", ""))
                for requirement in contract_requirements
                if isinstance(requirement, dict)
                and requirement.get("requirement_id")
                and is_delegable_requirement(requirement)
                and coverage_ledger.get(
                    str(requirement.get("requirement_id")),
                    {},
                ).get("status")
                != "supported"
            ]
            state["completion_decision"] = {
                "action": (
                    CompletionDecision.COMPLETE_PARTIAL.value
                    if uncovered_requirement_ids
                    else CompletionDecision.COMPLETE.value
                ),
                "reason": (
                    "coverage_requirements_incomplete"
                    if uncovered_requirement_ids
                    else "quality_gate_reassessed"
                ),
                "gaps": uncovered_requirement_ids,
            }
            state["quality_gate"] = self._quality_gate_payload(
                (
                    "passed_with_caveats"
                    if caveat_task_ids
                    else "passed"
                ),
                reason_codes=[
                    "quality_gate_reassessed",
                    *(
                        ["handoff_accepted_with_caveats"]
                        if caveat_task_ids
                        else []
                    ),
                ],
                assessment_refs=assessment_refs,
            )
            return {
                "mode": "accepted",
                "accepted_task_ids": admitted_task_ids,
                "caveat_task_ids": caveat_task_ids,
                "uncovered_requirement_ids": uncovered_requirement_ids,
                "artifact_refs": verified_refs,
            }

        if recovered_evidence:
            completion = state.get("completion_decision", {})
            gaps = [str(item) for item in completion.get("gaps", [])]
            state["evidence_registry"] = recovered_evidence
            state["completion_decision"] = {
                "action": CompletionDecision.COMPLETE_PARTIAL.value,
                "reason": "quality_gate_recovery",
                "gaps": gaps,
            }
            state["quality_gate"] = self._quality_gate_payload(
                "degraded",
                reason_codes=[
                    "quality_gate_recovery",
                    *(reason_codes or ["handoff_rejected"]),
                ],
                assessment_refs=assessment_refs,
            )
            coverage_contract = state.get("coverage_contract")
            contract_requirements = (
                coverage_contract.get("requirements", [])
                if isinstance(coverage_contract, dict)
                else []
            )
            contract_requirement_ids = {
                str(requirement.get("requirement_id", ""))
                for requirement in contract_requirements
                if isinstance(requirement, dict)
                and requirement.get("requirement_id")
            }
            source_scoped_recovered_evidence = (
                source_scoped_evidence_records(
                    recovered_evidence,
                    coverage_contract,
                )
            )
            coverage_ledger = _evidence_limited_coverage_map(
                authoritative_ledger=dict(
                    state.get("coverage_ledger", {})
                ),
                latest_assessments=latest_by_task,
                verified_artifacts=verified_artifacts,
                eligible_evidence=source_scoped_recovered_evidence,
                contract_requirement_ids=contract_requirement_ids,
            )
            uncovered_requirement_ids = [
                str(requirement.get("requirement_id", ""))
                for requirement in contract_requirements
                if isinstance(requirement, dict)
                and requirement.get("requirement_id")
                and is_delegable_requirement(requirement)
                and coverage_ledger.get(
                    str(requirement.get("requirement_id")),
                    {},
                ).get("status")
                != "supported"
            ]
            caveats = [
                str(caveat)
                for assessment in assessment_history
                if isinstance(assessment, dict)
                for caveat in assessment.get("caveats", [])
                if str(caveat).strip()
            ]
            state["final_report"] = await build_evidence_limited_report(
                recovered_evidence,
                coverage_contract=coverage_contract,
                coverage_ledger=coverage_ledger,
                caveats=caveats,
                uncovered_requirement_ids=uncovered_requirement_ids,
                rejection_reasons=rejection_reasons,
                artifact_refs=verified_refs,
                config=self.config,
            )
            state["coverage_checklist"] = derive_state_coverage_checklist(state)
            state["evaluation_snapshot"] = build_evaluation_snapshot(
                state,
                coverage_checklist=state["coverage_checklist"],
                researcher_task_artifacts=verified_artifacts,
            ).model_dump(mode="json")
            return {
                "mode": "partial",
                "artifact_refs": verified_refs,
            }

        state["quality_gate"] = self._quality_gate_payload(
            "failed",
            reason_codes=reason_codes or ["no_eligible_evidence"],
            assessment_refs=assessment_refs,
        )
        return {"mode": "failed", "artifact_refs": verified_refs}

    def _quality_recovery_state_update(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        fields = (
            "notes",
            "raw_notes",
            "candidate_registry",
            "document_registry",
            "evidence_registry",
            "web_research_iterations",
            "completed_task_outputs",
            "handoff_assessments",
            "coverage_checklist",
        )
        update: dict[str, Any] = {
            field_name: {
                "type": "override",
                "value": list(state.get(field_name, [])),
            }
            for field_name in fields
            if field_name in state
        }
        update["completion_decision"] = {
            "type": "override",
            "value": dict(state.get("completion_decision", {})),
        }
        update["quality_gate"] = dict(state.get("quality_gate", {}))
        if "coverage_ledger" in state:
            update["coverage_ledger"] = dict(
                state.get("coverage_ledger", {})
            )
        if state.get("final_report"):
            update["final_report"] = str(state["final_report"])
        if "final_report_draft" in state:
            update["final_report_draft"] = str(state.get("final_report_draft") or "")
        if "report_draft_metadata" in state:
            update["report_draft_metadata"] = dict(
                state.get("report_draft_metadata") or {}
            )
        if "report_review" in state:
            update["report_review"] = dict(state.get("report_review") or {})
        if "report_review_history" in state:
            update["report_review_history"] = list(
                state.get("report_review_history") or []
            )
        if "report_revision_count" in state:
            update["report_revision_count"] = int(
                state.get("report_revision_count") or 0
            )
        if state.get("evaluation_snapshot"):
            update["evaluation_snapshot"] = state["evaluation_snapshot"]
        return update

    async def _finish_success(
        self,
        state: dict[str, Any],
        recorder: Any,
        result_text: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Finish a successful run, persist terminal status, and emit its event."""
        from open_deep_research.tasks.teammate_pool import shutdown_teammate_pool

        self.cancellation_scope.checkpoint("finish_success")
        await self.cancellation_scope.run(
            shutdown_teammate_pool(self.config),
            stage="shutdown_teammate_pool",
        )
        self.cancellation_scope.claim_completion("finish_success")
        self._clear_run_resources()
        recorder.active_span().set_output(result_text)
        self.total_usage = recorder.finish_run(self.run_id, "success")
        result_status = (
            "partial"
            if state.get("completion_decision", {}).get("action")
            == CompletionDecision.COMPLETE_PARTIAL.value
            else "success"
        )
        configurable = Configuration.from_runnable_config(self.config)
        if configurable.quality_evaluation_enabled and not state.get(
            "quality_gate"
        ):
            derived_gate = self._quality_gate_from_handoff_assessments(state)
            if derived_gate is not None:
                state["quality_gate"] = derived_gate
        result = {
            "status": result_status,
            "result": result_text,
            "termination_reason": str(
                state.get("completion_decision", {}).get("reason", "completed")
            ),
            "completion": state.get("completion_decision", {}),
            "usage": self._usage_subset(self.total_usage),
            "usage_accounting": self.total_usage.get("usage_accounting"),
            "metrics": self._metrics_subset(self.total_usage),
            "permission_denials": self.permission_denials,
            "persistence_degraded": self.persistence_degraded,
        }
        if state.get("quality_gate"):
            result["quality_gate"] = state["quality_gate"]
        if state.get("report_review"):
            # Keep the public run result bounded to decision/metrics; detailed
            # reviewer prose remains in the redacted context artifact.
            result["report_review"] = self._review_summary(
                state.get("report_review")
            )
        report_artifacts = state.get("report_artifacts")
        if report_artifacts:
            result["artifacts"] = report_artifacts
        report_event_created = False
        if result_text:
            # A prior process may have persisted the finalization checkpoint
            # immediately before its public event. Reconcile that durable event
            # before marking the run terminal, and keep the hash tied to this
            # exact final Markdown body.
            report_event_created = await self._ensure_report_completed_event(
                result_text
            )
        self.status = "completed"
        self.final_state = {**state, "result": result}
        await self._persist_checkpoint("completed", "completed", status="completed")
        if self.context_store is not None:
            try:
                manifest = self.context_store.load_manifest()
                final_artifacts = dict(manifest.final_artifacts)
                if state.get("final_report"):
                    final_artifacts["final_report"] = "final_report.md"
                if state.get("canonical_report"):
                    final_artifacts["canonical_report"] = "canonical_report.json"
                self.context_store._update_manifest(  # noqa: SLF001
                    status="completed",
                    result={
                        "status": result_status,
                        **(
                            {"quality_gate": result["quality_gate"]}
                            if result.get("quality_gate")
                            else {}
                        ),
                        **(
                            {"report_review": result["report_review"]}
                            if result.get("report_review")
                            else {}
                        ),
                    },
                    final_artifacts=final_artifacts,
                )
            except Exception as exc:  # noqa: BLE001
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
        if report_event_created:
            yield self._event(
                "report.completed",
                {
                    "run_id": self.run_id,
                    "status": "completed",
                    "sha256": hashlib.sha256(
                        result_text.encode("utf-8")
                    ).hexdigest(),
                },
            )
        await self._publish_public(
            "run.completed",
            stage="finalizing",
            payload={
                "status": "completed",
                "result_ref": f"/runs/{self.run_id}",
                "termination_reason": str(
                    state.get("completion_decision", {}).get("reason", "completed")
                ),
                "result_status": result["status"],
                "permission_denial_count": len(self.permission_denials),
            },
            dedupe_key="run:terminal",
        )
        yield self._event(
            "run.completed",
            {
                "run_id": self.run_id,
                "status": result_status,
                "result": result,
                "usage": self._usage_subset(self.total_usage),
                "usage_accounting": self.total_usage.get("usage_accounting"),
                "metrics": self._metrics_subset(self.total_usage),
            },
        )

    async def _persist_task_outputs(self, state: dict[str, Any]) -> None:
        """Persist completed async task outputs and their findings manifest."""
        if self.context_store is None:
            return
        outputs = list(state.get("completed_task_outputs", []))
        refs: list[dict[str, Any]] = []
        for index, output in enumerate(outputs):
            task_id = str(output.get("task_id") or f"task-{index}")
            try:
                digest = self.context_store.persist_task_result(task_id, output)
                refs.append({"task_id": task_id, "path": f"artifacts/research_tasks/{task_id}.json", "sha256": digest})
            except Exception as exc:  # noqa: BLE001
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)
        if refs:
            try:
                self.context_store.write_json_atomic("findings_manifest.json", {"tasks": refs})
            except Exception as exc:  # noqa: BLE001
                self.persistence_degraded = True
                self.context_store.mark_persistence_degraded(exc)

    async def _run_node(self, name: str, node: Any, state: dict[str, Any]) -> RuntimeCommand:
        self.cancellation_scope.checkpoint(name)
        recorder = get_trace_recorder(self.config)
        with recorder.start_span(name=f"node.{name}", kind="agent", agent_role="lead"):
            result = await self.cancellation_scope.run(
                node(state, self.config),
                stage=name,
            )
        command = coerce_command(result)
        apply_update_to_state(state, command.update)
        record_type = "context_compacted" if "conversation_summary" in command.update else "state_delta"
        await self._persist_update(
            channel="lead",
            stage=name,
            update=command.update,
            record_type=record_type,
            extra={"summary": command.update.get("conversation_summary")} if record_type == "context_compacted" else None,
        )
        self._record("node", {"name": name, "goto": command.goto})
        return command

    async def _run_supervisor(
        self,
        main_state: dict[str, Any],
        *,
        restored_state: dict[str, Any] | None = None,
        start_step: str = "supervisor",
    ) -> dict[str, Any]:
        from open_deep_research.agents import deep_researcher as graph

        runtime_configuration = Configuration.from_runnable_config(self.config)
        base_state: dict[str, Any] = {
            "supervisor_messages": list(main_state.get("supervisor_messages", [])),
            "research_brief": main_state.get("research_brief", ""),
            "coverage_contract": dict(
                main_state.get("coverage_contract", {})
            ),
            "coverage_ledger": dict(
                main_state.get("coverage_ledger", {})
            ),
            "research_risk_profile": dict(
                main_state.get("research_risk_profile", {})
            ),
            "notes": list(main_state.get("notes", [])),
            "research_iterations": 0,
            "raw_notes": list(main_state.get("raw_notes", [])),
            "candidate_registry": list(main_state.get("candidate_registry", [])),
            "document_registry": list(main_state.get("document_registry", [])),
            "evidence_registry": list(main_state.get("evidence_registry", [])),
            "web_research_iterations": list(main_state.get("web_research_iterations", [])),
            "enable_async_research": runtime_configuration.enable_async_research,
            "sandbox_enabled": runtime_configuration.sandbox_enabled,
            "memory_context": main_state.get("memory_context"),
            "approved_research_plan": main_state.get("approved_research_plan"),
            "human_feedback": list(self.human_feedback),
            "processed_mailbox_message_ids": list(
                main_state.get("processed_mailbox_message_ids", [])
            ),
            "research_artifact_refs": dict(main_state.get("research_artifact_refs", {})),
            "handoff_assessments": list(
                main_state.get("handoff_assessments", [])
            ),
            "applied_query_event_ids": list(
                main_state.get("applied_query_event_ids", [])
            ),
        }
        supervisor_state = {**base_state, **(restored_state or {})}
        supervisor_state["enable_async_research"] = (
            runtime_configuration.enable_async_research
        )
        supervisor_state["sandbox_enabled"] = runtime_configuration.sandbox_enabled
        # The authoritative brief always wins over journal/state caches.
        if self.context_store is not None and self.context_store.brief_path.exists():
            supervisor_state["research_brief"] = self.context_store.load_research_brief()
        if not restored_state:
            await self._persist_update(
                channel="supervisor",
                stage="supervisor_running",
                scope="supervisor",
                update={
                    "supervisor_messages": {
                        "type": "override",
                        "value": supervisor_state["supervisor_messages"],
                    },
                    "research_brief": supervisor_state["research_brief"],
                    "coverage_contract": dict(
                        supervisor_state.get("coverage_contract", {})
                    ),
                    "coverage_ledger": dict(
                        supervisor_state.get("coverage_ledger", {})
                    ),
                    "research_risk_profile": dict(
                        supervisor_state.get(
                            "research_risk_profile",
                            {},
                        )
                    ),
                    "research_iterations": 0,
                    "enable_async_research": supervisor_state["enable_async_research"],
                    "sandbox_enabled": supervisor_state["sandbox_enabled"],
                    "memory_context": supervisor_state.get("memory_context"),
                    "approved_research_plan": supervisor_state.get("approved_research_plan"),
                    "applied_query_event_ids": {
                        "type": "override",
                        "value": list(
                            supervisor_state.get(
                                "applied_query_event_ids",
                                [],
                            )
                        ),
                    },
                },
            )
        recorder = get_trace_recorder(self.config)
        configurable = Configuration.from_runnable_config(self.config)
        restored_query_state = None
        extra_fetches = int(self.config.get("metadata", {}).get("fetch_budget_extension", {}).get("extra_fetches", 0))
        query_state_key = f"supervisor:fetch:{extra_fetches}" if extra_fetches else "supervisor"
        restored_query_payload = self._restored_query_states.get(
            query_state_key
        )
        if restored_query_payload is not None:
            restored_query_state = QueryLoopState.from_snapshot(
                restored_query_payload
            )
        checkpoint_sink = (
            RunContextQueryCheckpointSink(self.context_store)
            if self.context_store is not None
            else None
        )

        async def commit_supervisor_update(
            update: dict[str, Any],
            *,
            step: str,
            goto: str,
        ) -> None:
            apply_update_to_state(supervisor_state, update)
            await self._persist_update(
                channel="supervisor",
                stage="supervisor_running",
                scope="supervisor",
                update=update,
            )
            pending_acks = list(update.get("pending_mailbox_acks", []))
            if pending_acks and configurable.enable_async_research and self.persistence_degraded:
                raise RuntimeError("team_input_journal_commit_failed")
            if pending_acks:
                from open_deep_research.tasks.coordination import ack_lead_updates

                for ack in pending_acks:
                    await ack_lead_updates(
                        configurable,
                        run_id=str(ack["run_id"]),
                        consumer_id=str(ack["consumer_id"]),
                        message_ids=list(ack["message_ids"]),
                    )
                supervisor_state["pending_mailbox_acks"] = []
            self._record("supervisor", {"step": step, "goto": goto})
            await self._persist_checkpoint(
                "supervisor_running",
                f"supervisor.{goto}",
                channel="supervisor",
                payload={"supervisor_step": goto},
            )

        async def execute_supervisor_tools(
            messages: list[BaseMessage],
            turn: int,
            *,
            committed_outcomes: dict[
                str,
                GovernedToolCallResult,
            ] | None = None,
            on_committed: Callable[
                [dict[str, Any], GovernedToolCallResult],
                Awaitable[None],
            ] | None = None,
        ) -> RuntimeCommand:
            self.cancellation_scope.checkpoint("supervisor.tools")
            tool_state = {
                **supervisor_state,
                "supervisor_messages": list(messages),
                "research_iterations": turn,
            }
            with recorder.start_span(
                name="supervisor.tools",
                kind="agent",
                agent_role="supervisor",
                attributes={"iteration": turn},
            ):
                supervisor_tool_executor = (
                    graph._execute_supervisor_tools
                )
                supports_durable_commit = (
                    "committed_outcomes"
                    in inspect.signature(
                        supervisor_tool_executor
                    ).parameters
                )
                execution = (
                    supervisor_tool_executor(
                        tool_state,
                        self.config,
                        committed_outcomes=committed_outcomes,
                        on_committed=on_committed,
                    )
                    if supports_durable_commit
                    else supervisor_tool_executor(
                        tool_state,
                        self.config,
                    )
                )
                return coerce_command(
                    await self.cancellation_scope.run(
                        execution,
                        stage="supervisor.tools",
                    ),
                    default_goto="supervisor",
                )

        next_step = END if start_step == END else start_step
        if restored_query_state is not None:
            # The inner checkpoint is more precise than the outer legacy
            # supervisor/model boundary and owns pending-tool recovery.
            next_step = "supervisor"
        if next_step == "supervisor_tools":
            restored_messages = normalize_messages(
                list(supervisor_state.get("supervisor_messages", []))
            )
            restored_turn = int(supervisor_state.get("research_iterations", 0) or 0)
            command = await execute_supervisor_tools(restored_messages, restored_turn)
            await commit_supervisor_update(
                dict(command.update),
                step="supervisor_tools",
                goto=command.goto,
            )
            next_step = command.goto
        elif next_step not in {"supervisor", END}:
            raise RuntimeError(f"Unknown supervisor step: {next_step}")

        if next_step != END:
            supervisor_assembly = await graph.prepare_existing_toolset(
                list(graph.build_supervisor_tool_registry(supervisor_state).values()),
                graph.AgentRole.SUPERVISOR,
                self.config,
            )
            model_tools = supervisor_assembly.tools
            model_candidates = (
                [
                    ModelCandidate(
                        model_id=configurable.supervisor_model
                        or configurable.research_model,
                        model=None,
                        model_config={
                            "model": configurable.supervisor_model
                            or configurable.research_model,
                            "max_tokens": configurable.research_model_max_tokens,
                            "max_retries": 0,
                        },
                    )
                ]
                if configurable.model_backend == "litellm"
                else build_model_candidate_chain(
                    configurable.research_model,
                    configurable.model_fallbacks.get("supervisor", []),
                    max_tokens=configurable.research_model_max_tokens,
                    config=self.config,
                    role="supervisor",
                    model=graph.configurable_model,
                )
            )

            async def before_turn(
                messages: list[BaseMessage],
                _next_turn: int,
                _config: RunnableConfig,
            ) -> BeforeTurnHookResult | None:
                self.cancellation_scope.checkpoint("supervisor.before_turn")
                prepared_state = {
                    **supervisor_state,
                    "supervisor_messages": list(messages),
                }
                self._drain_human_feedback(prepared_state)
                prepared_messages = normalize_messages(
                    list(prepared_state.get("supervisor_messages", []))
                )
                feedback_changed = prepared_messages != messages
                compacted = await graph.compact_query_context(
                    prepared_messages,
                    research_brief=str(supervisor_state.get("research_brief", "")),
                    channel="supervisor",
                    config=self.config,
                )
                if compacted is None and not feedback_changed:
                    return None

                replacement = (
                    normalize_messages(compacted["messages"])
                    if compacted is not None
                    else prepared_messages
                )
                control_update: dict[str, Any] = {
                    "supervisor_messages": {
                        "type": "override",
                        "value": replacement,
                    },
                    "human_feedback": {
                        "type": "override",
                        "value": list(self.human_feedback),
                    },
                }
                apply_update_to_state(supervisor_state, control_update)
                await self._persist_update(
                    channel="supervisor",
                    stage="supervisor_running",
                    scope="supervisor",
                    update=control_update,
                    record_type=(
                        "context_compacted" if compacted is not None else "state_delta"
                    ),
                    extra=(
                        {
                            "summary": compacted["summary"],
                            "recent_messages": compacted["recent_messages"],
                        }
                        if compacted is not None
                        else None
                    ),
                )
                return BeforeTurnHookResult(replace_messages=replacement)

            completion_policy = ResearchCompletionPolicy(
                min_evidence=(
                    1 if configurable.web_pipeline_mode == "enforced" else 0
                ),
                min_sources=0,
            )

            def supervisor_completion_context(
                *,
                explicit_succeeded: bool = False,
                explicit_failed: bool = False,
                has_remaining_budget: bool = True,
                exhausted_reason: str | None = None,
            ):
                return completion_policy_context(
                    supervisor_state,
                    explicit_completion_succeeded=explicit_succeeded,
                    explicit_completion_failed=explicit_failed,
                    has_remaining_budget=has_remaining_budget,
                    exhausted_reason=exhausted_reason,
                )

            async def run_tool_batch(
                messages: list[BaseMessage],
                _tool_calls: list[dict[str, Any]],
                _tools_by_name: dict[str, Any],
                turn: int,
                _config: RunnableConfig,
                committed_outcomes: dict[
                    str,
                    GovernedToolCallResult,
                ],
                on_committed: Callable[
                    [dict[str, Any], GovernedToolCallResult],
                    Awaitable[None],
                ],
            ) -> ToolResultsHookResult:
                command = await execute_supervisor_tools(
                    messages,
                    turn,
                    committed_outcomes=committed_outcomes,
                    on_committed=on_committed,
                )
                update = dict(command.update)
                tool_messages = normalize_messages(update.pop("supervisor_messages", []))
                if configurable.enable_async_research:
                    from open_deep_research.agents.deep_researcher import (
                        fetch_budget_exhausted_task_streak,
                        search_provider_exhausted_streak,
                    )

                    try:
                        supervisor_state[
                            "search_provider_exhausted_streak"
                        ] = await search_provider_exhausted_streak(_config)
                        supervisor_state[
                            "fetch_budget_exhausted_task_streak"
                        ] = await fetch_budget_exhausted_task_streak(_config)
                    except Exception:  # noqa: BLE001 - streak is advisory
                        logger.exception("research exhaustion streak refresh failed")
                drained_update: dict[str, Any] | None = None
                terminal_resource_exhaustion = (
                    supervisor_state.get("search_provider_exhausted_streak", 0) >= 3
                    or supervisor_state.get("fetch_budget_exhausted_task_streak", 0) >= 1
                )
                if configurable.enable_async_research and (
                    turn >= configurable.max_researcher_iterations
                    or terminal_resource_exhaustion
                ):
                    from open_deep_research.agents.deep_researcher import (
                        drain_unfinished_async_tasks,
                    )

                    try:
                        drained_update = await drain_unfinished_async_tasks(
                            supervisor_state,
                            _config,
                            configurable,
                            event_publisher_from_config(_config),
                            timeout_seconds=(
                                configurable.task_timeout_seconds + 60
                            ),
                        )
                    except Exception:  # noqa: BLE001 - drain is a best-effort rescue
                        logger.exception(
                            "supervisor budget drain failed; terminating undrained"
                        )
                    if drained_update:
                        update.update(drained_update)
                projected_state = dict(supervisor_state)
                apply_update_to_state(projected_state, update)
                requested = any(call.get("name") == "ResearchComplete" for call in _tool_calls)
                successful = requested and command.goto == END
                decision = completion_policy.evaluate(completion_policy_context(
                    projected_state,
                    explicit_completion_succeeded=successful,
                    explicit_completion_failed=requested and not successful,
                    has_remaining_budget=turn < configurable.max_researcher_iterations,
                    exhausted_reason=(
                        "max_turns_drained" if drained_update else "max_turns"
                    ),
                ))
                decision_update = {
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "gaps": list(decision.gaps),
                }
                should_continue = decision.action is CompletionDecision.CONTINUE_WITH_GAPS
                additional_messages: list[BaseMessage] = []
                if should_continue and decision.gaps:
                    additional_messages.append(HumanMessage(content=(
                        "[Research Completion Policy] Continue research and resolve: "
                        + ", ".join(decision.gaps)
                    )))
                return ToolResultsHookResult(
                    messages=tool_messages,
                    additional_messages=additional_messages,
                    updates={**update, "completion_decision": decision_update},
                    should_continue=should_continue,
                    reason=(
                        "completion_policy_satisfied"
                        if not should_continue
                        else None
                    ),
                )

            async def handle_no_tool_stop(
                messages: list[BaseMessage],
                _config: RunnableConfig,
            ) -> StopHookResult:
                turn = int(supervisor_state.get("research_iterations", 0) or 0)
                drained_update: dict[str, Any] | None = None
                terminal_resource_exhaustion = False
                if configurable.enable_async_research:
                    from open_deep_research.agents.deep_researcher import (
                        drain_unfinished_async_tasks,
                        fetch_budget_exhausted_task_streak,
                        search_provider_exhausted_streak,
                    )

                    try:
                        supervisor_state[
                            "search_provider_exhausted_streak"
                        ] = await search_provider_exhausted_streak(_config)
                        supervisor_state[
                            "fetch_budget_exhausted_task_streak"
                        ] = await fetch_budget_exhausted_task_streak(_config)
                    except Exception:  # noqa: BLE001 - streak is advisory
                        logger.exception("research exhaustion streak refresh failed")
                    terminal_resource_exhaustion = (
                        supervisor_state.get("search_provider_exhausted_streak", 0) >= 3
                        or supervisor_state.get(
                            "fetch_budget_exhausted_task_streak", 0
                        ) >= 1
                    )
                    try:
                        if (
                            turn >= configurable.max_researcher_iterations
                            or terminal_resource_exhaustion
                        ):
                            drained_update = await drain_unfinished_async_tasks(
                                supervisor_state,
                                _config,
                                configurable,
                                event_publisher_from_config(_config),
                                timeout_seconds=(
                                    configurable.task_timeout_seconds + 60
                                ),
                            )
                    except Exception:  # noqa: BLE001 - drain is a best-effort rescue
                        logger.exception(
                            "supervisor budget drain failed; terminating undrained"
                        )
                projected_state = dict(supervisor_state)
                if drained_update:
                    apply_update_to_state(projected_state, drained_update)
                decision = completion_policy.evaluate(completion_policy_context(
                    projected_state,
                    has_remaining_budget=(
                        turn < configurable.max_researcher_iterations
                        and not terminal_resource_exhaustion
                    ),
                    exhausted_reason=(
                        "max_turns_drained" if drained_update else "max_turns"
                    ),
                ))
                if decision.action is CompletionDecision.CONTINUE_WITH_GAPS:
                    return StopHookResult(
                        should_continue=True,
                        messages=[HumanMessage(content=(
                            "[Research Completion Policy] Do not stop yet. "
                            "Use research tools and resolve: "
                            + ", ".join(decision.gaps)
                        ))],
                        updates={
                            **(drained_update or {}),
                            "completion_decision": {
                                "action": decision.action.value,
                                "reason": decision.reason,
                                "gaps": list(decision.gaps),
                            },
                        },
                        reason="stop_hook_blocked",
                    )
                command = await execute_supervisor_tools(messages, turn)
                update = dict(command.update)
                if drained_update:
                    update.update(drained_update)
                tool_messages = normalize_messages(update.pop("supervisor_messages", []))
                return StopHookResult(
                    should_continue=command.goto != END,
                    messages=tool_messages,
                    updates={
                        **update,
                        "completion_decision": {
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "gaps": list(decision.gaps),
                        },
                    },
                    reason=(
                        "completion_policy_satisfied"
                        if command.goto == END
                        else "stop_hook_blocked"
                    ),
                )

            completed_messages = normalize_messages(
                list(supervisor_state.get("supervisor_messages", []))
            )
            completed_turn = int(supervisor_state.get("research_iterations", 0) or 0)
            terminal_tool_update_handled = False
            async for event in query(QueryParams(
                messages=completed_messages,
                system_prompt=None,
                model=graph.configurable_model,
                config=self.config,
                tools=model_tools,
                role=graph.AgentRole.SUPERVISOR,
                model_span_name="supervisor.model",
                model_config=model_candidates[0].model_config,
                initial_turn=completed_turn,
                turn_advance_policy=_supervisor_turn_advance_policy,
                max_tool_description_chars=configurable.max_tool_description_chars,
                context_policy=ContextPolicy(
                    max_tool_result_chars=configurable.max_mcp_output_chars,
                ),
                before_turn_hooks=[before_turn],
                stop_hooks=[handle_no_tool_stop],
                durable_tool_batch_hook=run_tool_batch,
                max_concurrent_tools=configurable.max_concurrent_tool_calls,
                max_tool_batch_size=configurable.max_tool_batch_size,
                tool_timeout_seconds=configurable.tool_call_timeout_seconds,
                hook_timeout_seconds=configurable.hook_timeout_seconds,
                # task_timeout_seconds is a per-Researcher deadline. The batch
                # needs additional bounded time to assess and summarize the
                # successfully completed handoffs after a sibling times out.
                tool_batch_timeout_seconds=(
                    configurable.task_timeout_seconds
                    + (
                        configurable.model_call_timeout_seconds
                        * configurable.model_transport_max_attempts
                    )
                    + configurable.hook_timeout_seconds
                ),
                model_timeout_seconds=configurable.model_call_timeout_seconds,
                model_transport_max_attempts=(
                    1
                    if configurable.model_backend == "litellm"
                    else configurable.model_transport_max_attempts
                ),
                budget_gate=self._budget_gate(),
                execution_namespace=query_state_key,
                cancellation_scope=self.cancellation_scope,
                initial_state=restored_query_state,
                state_key=query_state_key,
                checkpoint_sink=checkpoint_sink,
                acknowledged_event_ids=tuple(
                    supervisor_state.get(
                        "applied_query_event_ids",
                        [],
                    )
                ),
                model_candidates=model_candidates,
                context_recovery_max_attempts=(
                    configurable.context_recovery_max_attempts
                ),
                output_token_escalation_enabled=(
                    configurable.output_token_escalation_enabled
                ),
                output_continuation_max_attempts=(
                    configurable.output_continuation_max_attempts
                ),
                model_max_output_tokens_overrides=(
                    configurable.model_max_output_tokens_overrides
                ),
            )):
                if event.type == "query.model_event":
                    completed_turn = int(event.data["turn"])
                    terminal_tool_update_handled = False
                    await commit_supervisor_update(
                        {
                            "supervisor_messages": [event.data["message"]],
                            "research_iterations": completed_turn,
                        },
                        step="supervisor",
                        goto="supervisor_tools",
                    )
                elif event.type == "query.tool_result":
                    event_id = str(event.data.get("event_id") or "")
                    applied_event_ids = list(
                        supervisor_state.get(
                            "applied_query_event_ids",
                            [],
                        )
                    )
                    if event_id and event_id in applied_event_ids:
                        terminal_tool_update_handled = not bool(
                            event.data.get("should_continue", True)
                        )
                        continue
                    event_update = dict(event.data.get("updates", {}))
                    tool_messages = [
                        *event.data.get("messages", []),
                        *event.data.get("additional_messages", []),
                    ]
                    if tool_messages:
                        event_update["supervisor_messages"] = tool_messages
                    if event_id:
                        event_update["applied_query_event_ids"] = {
                            "type": "override",
                            "value": [*applied_event_ids, event_id],
                        }
                    should_continue = bool(event.data.get("should_continue", True))
                    await commit_supervisor_update(
                        event_update,
                        step="supervisor_tools",
                        goto="supervisor" if should_continue else END,
                    )
                    terminal_tool_update_handled = not should_continue
                elif event.type == "query.transition":
                    event_id = str(event.data.get("event_id") or "")
                    applied_event_ids = list(
                        supervisor_state.get(
                            "applied_query_event_ids",
                            [],
                        )
                    )
                    if event_id and event_id in applied_event_ids:
                        continue
                    event_update = dict(event.data.get("updates", {}))
                    transition_messages = list(
                        event.data.get("messages", [])
                    )
                    if transition_messages:
                        event_update["supervisor_messages"] = (
                            transition_messages
                        )
                    if event_id:
                        event_update["applied_query_event_ids"] = {
                            "type": "override",
                            "value": [*applied_event_ids, event_id],
                        }
                    await commit_supervisor_update(
                        event_update,
                        step="supervisor_stop_governance",
                        goto="supervisor",
                    )
                elif event.type == "query.completed":
                    completed_messages = normalize_messages(
                        list(event.data.get("messages", completed_messages))
                    )
                    completed_turn = int(
                        event.data.get("transition", {}).get("turn", completed_turn)
                    )
                    completion_reason = str(
                        event.data.get("transition", {}).get("reason", "completed")
                    )
                    if completion_reason == "cancelled":
                        raise RunCancelled("cancel_requested", "supervisor.query")
                    if completion_reason in {
                        "budget_exhausted",
                        "deadline_exceeded",
                        "model_timeout",
                        "prompt_too_long",
                        "output_recovery_exhausted",
                        "model_error",
                        "hook_stopped",
                    }:
                        decision = completion_policy.evaluate(supervisor_completion_context(
                            has_remaining_budget=False,
                            exhausted_reason=completion_reason,
                        ))
                        supervisor_state["completion_decision"] = {
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "gaps": list(decision.gaps),
                        }
                    if not terminal_tool_update_handled:
                        event_id = str(event.data.get("event_id") or "")
                        applied_event_ids = list(
                            supervisor_state.get(
                                "applied_query_event_ids",
                                [],
                            )
                        )
                        if (
                            not event_id
                            or event_id not in applied_event_ids
                        ):
                            completion_update = dict(
                                event.data.get("updates", {})
                            )
                            if event_id:
                                completion_update[
                                    "applied_query_event_ids"
                                ] = {
                                    "type": "override",
                                    "value": [
                                        *applied_event_ids,
                                        event_id,
                                    ],
                                }
                            await commit_supervisor_update(
                                completion_update,
                                step="supervisor_tools",
                                goto=END,
                            )

            supervisor_state["supervisor_messages"] = completed_messages
            supervisor_state["research_iterations"] = completed_turn
        return {
            "supervisor_messages": {"type": "override", "value": supervisor_state.get("supervisor_messages", [])},
            "notes": {"type": "override", "value": supervisor_state.get("notes", [])},
            "raw_notes": {"type": "override", "value": supervisor_state.get("raw_notes", [])},
            "candidate_registry": {"type": "override", "value": supervisor_state.get("candidate_registry", [])},
            "document_registry": {"type": "override", "value": supervisor_state.get("document_registry", [])},
            "evidence_registry": {"type": "override", "value": supervisor_state.get("evidence_registry", [])},
            "web_research_iterations": {"type": "override", "value": supervisor_state.get("web_research_iterations", [])},
            "completed_task_outputs": {
                "type": "override",
                "value": supervisor_state.get("completed_task_outputs", []),
            },
            "processed_mailbox_message_ids": {
                "type": "override",
                "value": supervisor_state.get("processed_mailbox_message_ids", []),
            },
            "research_artifact_refs": {
                "type": "override",
                "value": dict(supervisor_state.get("research_artifact_refs", {})),
            },
            "handoff_assessments": {
                "type": "override",
                "value": list(supervisor_state.get("handoff_assessments", [])),
            },
            "research_brief": supervisor_state.get("research_brief", main_state.get("research_brief", "")),
            "coverage_contract": dict(
                supervisor_state.get(
                    "coverage_contract",
                    main_state.get("coverage_contract", {}),
                )
            ),
            "coverage_ledger": dict(
                supervisor_state.get(
                    "coverage_ledger",
                    main_state.get("coverage_ledger", {}),
                )
            ),
            "research_risk_profile": dict(
                supervisor_state.get(
                    "research_risk_profile",
                    main_state.get("research_risk_profile", {}),
                )
            ),
            "approved_research_plan": supervisor_state.get("approved_research_plan"),
            "completion_decision": {
                "type": "override",
                "value": supervisor_state.get("completion_decision", {}),
            },
            "applied_query_event_ids": {
                "type": "override",
                "value": list(
                    supervisor_state.get(
                        "applied_query_event_ids",
                        [],
                    )
                ),
            },
            "human_feedback": {"type": "override", "value": list(self.human_feedback)},
        }

    def _event(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        payload = {"event": event_type, "data": data}
        self._record(event_type, data)
        return payload

    def _record(self, event_type: str, data: dict[str, Any]) -> None:
        record = {"ts": time.time(), "event": event_type, "data": data}
        self.transcript.append(record)
        configurable = Configuration.from_runnable_config(self.config)
        if not configurable.event_log_enabled:
            return
        path = Path(configurable.runs_dir) / self.run_id / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

class ResearcherQueryEngine:
    """Runs a single focused Researcher with a clean context window."""

    def __init__(self, config: RunnableConfig | None = None):
        """Create a researcher engine."""
        self.config = _ensure_config(config)

    async def run_topic(
        self,
        research_topic: str,
        *,
        memory_context: str | None = None,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        """Run one focused topic in a clean researcher context."""
        from langchain_core.messages import HumanMessage

        state = {
            "researcher_messages": [HumanMessage(content=research_topic)],
            "research_topic": research_topic,
            "memory_context": memory_context,
        }
        return await self.ainvoke(state, config)

    async def ainvoke(
        self,
        state: dict[str, Any],
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        """Invoke one focused Researcher through the shared Query runtime."""
        from open_deep_research.agents import deep_researcher as graph

        cfg = _ensure_config(config, self.config)
        configurable = Configuration.from_runnable_config(cfg)
        researcher_state = dict(state)
        researcher_state["researcher_messages"] = normalize_messages(
            researcher_state.get("researcher_messages", []),
        )
        recorder = get_trace_recorder(cfg)
        topic = str(researcher_state.get("research_topic", ""))
        with recorder.start_span(
            name="researcher.topic",
            kind="agent",
            agent_role="researcher",
            attributes={"research_topic": topic[:500]},
        ):
            all_tools = await graph.get_all_tools(cfg)
            researcher_assembly = await graph.prepare_existing_toolset(
                all_tools,
                graph.AgentRole.RESEARCHER,
                cfg,
            )
            tools = researcher_assembly.tools

            memory_context = str(researcher_state.get("memory_context") or "")
            runtime_messages: list[BaseMessage] = []
            if memory_context:
                runtime_messages.append(HumanMessage(content=memory_context))
            memory_prefix_count = len(runtime_messages)
            runtime_messages.extend(researcher_state["researcher_messages"])
            quality_recovery_state = QualityRecoveryState()
            approval_pending_turns = 0
            completion_policy = ResearchCompletionPolicy(
                min_evidence=(
                    1 if configurable.web_pipeline_mode == "enforced" else 0
                ),
                min_sources=0,
            )

            def researcher_completion_context(
                *,
                explicit_succeeded: bool = False,
                explicit_failed: bool = False,
                has_remaining_budget: bool = True,
                exhausted_reason: str | None = None,
            ):
                return completion_policy_context(
                    researcher_state,
                    explicit_completion_succeeded=explicit_succeeded,
                    explicit_completion_failed=explicit_failed,
                    has_remaining_budget=has_remaining_budget,
                    exhausted_reason=exhausted_reason,
                    cancelled=bool(researcher_state.get("cancelled")),
                )

            async def handle_no_tool_stop(
                _messages: list[BaseMessage],
                _config: RunnableConfig,
            ) -> StopHookResult:
                decision = completion_policy.evaluate(researcher_completion_context())
                researcher_state["completion_decision"] = {
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "gaps": list(decision.gaps),
                }
                should_continue = (
                    decision.action is CompletionDecision.CONTINUE_WITH_GAPS
                )
                gap_message = HumanMessage(
                    content=(
                        "[Research Completion Policy] Research is not complete. "
                        "Resolve these gaps before stopping: "
                        + ", ".join(decision.gaps)
                    )
                ) if should_continue else None
                return StopHookResult(
                    should_continue=should_continue,
                    messages=[gap_message] if gap_message is not None else [],
                    updates={"completion_decision": researcher_state["completion_decision"]},
                    reason=(
                        "stop_hook_blocked"
                        if should_continue
                        else "completion_policy_satisfied"
                    ),
                )

            consumed_team_inputs: set[str] = set()

            async def before_turn(
                _messages: list[BaseMessage],
                _next_turn: int,
                _config: RunnableConfig,
            ) -> BeforeTurnHookResult | None:
                if (
                    _next_turn - approval_pending_turns
                    > configurable.max_react_tool_calls
                    and not quality_recovery_state.active
                ):
                    return BeforeTurnHookResult(
                        should_stop=True,
                        reason="max_turns",
                    )
                task_id = str(cfg.get("metadata", {}).get("task_id", ""))
                if not task_id:
                    return None
                from open_deep_research.tasks.team_protocol import member_identity
                identity = member_identity.get()
                team_input = None
                if cfg.get("configurable", {}).get("team_coordination_enabled"):
                    from open_deep_research.tasks.team_bridge import worker_request
                    team_input = await worker_request(cfg, "input")
                elif identity is not None:
                    from open_deep_research.tasks.team_members import MemberSessions
                    from open_deep_research.tasks.team_runtime import team_runtime
                    service = await team_runtime.start()
                    team_input = await MemberSessions(service.store).load(identity.run_id, identity.member_id)
                if team_input is not None:
                    if team_input.get("cancelled"):
                        return BeforeTurnHookResult(updates={"cancelled": True}, should_stop=True, reason="cancelled")
                    present = consumed_team_inputs | {str(message.id) for message in _messages if message.id}
                    incoming = [HumanMessage(id=item["event_id"], content=(
                        f"[Team message from {item['sender']}; guidance, not admitted evidence] "
                        f"{item['payload'].get('message', '')}"
                    )) for item in team_input.get("messages", [])
                        if item.get("type") == "message" and item["event_id"] not in present]
                    if incoming:
                        ids = [str(message.id) for message in incoming]
                        consumed_team_inputs.update(ids)
                        return BeforeTurnHookResult(messages=incoming, consumed_input_ids=ids)
                from open_deep_research.tasks.registry import get_task_registry

                task_record = get_task_registry().get(task_id)
                expected_run_id = str(cfg.get("metadata", {}).get("run_id", "default"))
                if task_record is None or task_record.run_id != expected_run_id:
                    return None
                if task_record.cancelled.is_set():
                    return BeforeTurnHookResult(
                        updates={"cancelled": True},
                        should_stop=True,
                        reason="cancelled",
                    )
                instructions: list[BaseMessage] = []
                while not task_record.control_queue.empty():
                    control = task_record.control_queue.get_nowait()
                    if control.get("type") == "update":
                        instructions.append(HumanMessage(
                            content=f"[Supervisor Instruction] {control['instruction']}"
                        ))
                if not instructions:
                    return None
                return BeforeTurnHookResult(messages=instructions)

            async def after_tools(
                messages: list[BaseMessage],
                tool_calls: list[dict[str, Any]],
                outcomes: list[Any],
                tools_by_name: dict[str, Any],
                turn: int,
                _config: RunnableConfig,
            ) -> ToolResultsHookResult:
                nonlocal quality_recovery_state
                nonlocal approval_pending_turns
                approval_pending_outcomes = 0
                tool_outputs, batch_update = await graph.prepare_researcher_tool_outcomes(
                    tool_calls,
                    outcomes,
                    tools_by_name,
                    cfg,
                )
                task_id = str(cfg.get("metadata", {}).get("task_id", ""))
                if task_id:
                    from open_deep_research.tasks.registry import get_task_registry

                    task_record = get_task_registry().get(task_id)
                    source_values = [
                        getattr(getattr(outcome, "result", None), "output", None)
                        for outcome in outcomes
                    ]
                    sources = extract_public_sources(
                        {"candidate_registry": source_values},
                        limit=configurable.public_event_source_limit,
                    )
                    if task_record is not None:
                        task_record.source_urls.update(source["url"] for source in sources)
                        task_record.source_count = len(task_record.source_urls)
                    publisher = event_publisher_from_config(cfg)
                    categories: set[str] = set()
                    for call in tool_calls:
                        categories.add(
                            classify_tool_name(str(call.get("name", "")))
                        )
                    await publisher.publish(
                        "research.task.progress",
                        stage="researching",
                        payload={
                            "task_id": task_id,
                            "wave_id": task_record.wave_id if task_record else "",
                            "mode": str(cfg.get("metadata", {}).get("research_mode") or "async"),
                            "status": "running",
                            "phase": "researching",
                            "iteration": turn,
                            "source_count": task_record.source_count if task_record else len(sources),
                            "tool_categories": sorted(categories),
                        },
                        dedupe_key=f"task:{task_id}:progress:{turn}",
                    )
                    for source in sources:
                        await publish_task_activity(
                            cfg,
                            "source.discovered",
                            kind="source",
                            phase="evidence_review",
                            status="success",
                            title="发现可追溯来源",
                            summary=str(source.get("title") or source.get("domain") or "新来源"),
                            iteration=turn,
                            duration_ms=None,
                            payload=source,
                            dedupe_key=f"activity:source:{source['source_id']}",
                        )
                        await publisher.publish(
                            "research.source.discovered",
                            stage="researching",
                            payload={"task_id": task_id, **source},
                            dedupe_key=f"source:{source['source_id']}",
                        )
                domain_updates = {
                    key: value
                    for key, value in batch_update.items()
                    if key not in {
                        "pending_tool_results",
                        "research_complete_requested",
                        "research_complete_succeeded",
                    }
                }
                denial_types = {
                    "permission_denied",
                    "egress_domain_denied",
                    "egress_domain_pending",
                    "sensitive_tool_approval_required",
                }
                permission_denials = list(researcher_state.get("permission_denials", []))
                seen_denials = {
                    (item.get("tool_call_id"), item.get("reason_code"))
                    for item in permission_denials
                }
                for call, outcome in zip(tool_calls, outcomes):
                    error = getattr(outcome, "error", None)
                    reason_code = getattr(getattr(error, "error_type", None), "value", None)
                    if reason_code == "egress_domain_pending":
                        approval_pending_outcomes += 1
                    key = (str(call.get("id", "")), reason_code)
                    if reason_code not in denial_types or key in seen_denials:
                        continue
                    permission_denials.append({
                        "tool_call_id": key[0],
                        "tool_name": str(call.get("name", "")),
                        "role": "researcher",
                        "reason_code": reason_code,
                        "turn": turn,
                        "task_id": str(cfg.get("metadata", {}).get("task_id", "")) or None,
                    })
                    seen_denials.add(key)
                if permission_denials:
                    domain_updates["permission_denials"] = {
                        "type": "override",
                        "value": permission_denials,
                    }
                additional_messages: list[BaseMessage] = []
                approval_pending_this_turn = (
                    bool(outcomes)
                    and approval_pending_outcomes == len(outcomes)
                )
                if approval_pending_this_turn:
                    approval_pending_turns += 1
                    additional_messages.append(
                        HumanMessage(content=_APPROVAL_PENDING_MESSAGE)
                    )
                limit_reached = (
                    turn - approval_pending_turns
                    >= configurable.max_react_tool_calls
                )
                apply_update_to_state(researcher_state, domain_updates)
                # Deterministic zero-allocation wall: when structured Worker
                # telemetry proves the run fetch budget is exhausted, stop
                # without waiting for the model to request completion. The
                # LLM gap-analysis decision is advisory only — a missing
                # ``budget_exhausted`` decision must not keep a task spinning
                # until max_turns (observed as 12 wasted iterations in E2E).
                from open_deep_research.agents.deep_researcher import (
                    _FETCH_BUDGET_EXHAUSTION_STREAK_LIMIT,
                    fetch_budget_exhausted_iteration_streak,
                )

                fetch_budget_streak = fetch_budget_exhausted_iteration_streak(
                    researcher_state.get("web_research_iterations", [])
                )
                fetch_budget_exhausted = (
                    fetch_budget_streak >= _FETCH_BUDGET_EXHAUSTION_STREAK_LIMIT
                )
                if fetch_budget_exhausted:
                    additional_messages.append(HumanMessage(content=(
                        "运行时已停止继续研究：连续多个 Web 迭代均未获得物理抓取配额。"
                    )))
                decision = completion_policy.evaluate(researcher_completion_context(
                    explicit_succeeded=bool(
                        batch_update["research_complete_succeeded"]
                    ),
                    explicit_failed=bool(
                        batch_update["research_complete_requested"]
                        and not batch_update["research_complete_succeeded"]
                    ),
                    has_remaining_budget=not (limit_reached or fetch_budget_exhausted),
                    exhausted_reason=(
                        "fetch_budget_exhausted"
                        if fetch_budget_exhausted
                        else "max_turns" if limit_reached else None
                    ),
                ))
                domain_updates["completion_decision"] = {
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "gaps": list(decision.gaps),
                }
                should_continue = (
                    decision.action is CompletionDecision.CONTINUE_WITH_GAPS
                )
                if should_continue and decision.gaps:
                    additional_messages.append(HumanMessage(content=(
                        "[Research Completion Policy] Continue research and resolve: "
                        + ", ".join(decision.gaps)
                    )))

                quality_stop_reason: str | None = None
                if configurable.quality_evaluation_enabled:
                    assessment_state = {
                        **researcher_state,
                        "researcher_messages": [
                            *messages[memory_prefix_count:],
                            *tool_outputs,
                        ],
                        "tool_call_iterations": turn,
                        "pending_tool_results": batch_update["pending_tool_results"],
                        "research_complete_requested": batch_update[
                            "research_complete_requested"
                        ],
                    }
                    assessment = await graph.assess_research_results(
                        assessment_state,
                        cfg,
                    )
                    assessment_update = dict(assessment.update)
                    additional_messages = list(
                        assessment_update.pop("researcher_messages", [])
                    )
                    assessment_update.pop("pending_tool_results", None)
                    assessment_update.pop("research_complete_requested", None)
                    domain_updates.update(assessment_update)
                    # The quality node owns normal continuation; only the
                    # bounded recovery branch below may reopen a stopped turn.
                    should_continue = (
                        assessment.goto == "researcher"
                        and not fetch_budget_exhausted
                        and not limit_reached
                    )

                    if "result_assessment" in assessment_update:
                        current_assessment = assessment_update[
                            "result_assessment"
                        ]
                    else:
                        current_assessment = researcher_state.get(
                            "result_assessment",
                            {},
                        )
                    assessment_payload = (
                        current_assessment
                        if isinstance(current_assessment, dict)
                        else {}
                    )
                    if (
                        assessment.goto == "compress_research"
                        and assessment_payload.get("decision") == "complete"
                        and not fetch_budget_exhausted
                    ):
                        evaluator_failed = bool(assessment_payload.get("evaluator_error"))
                        quality_stop_reason = (
                            "quality_evaluator_unavailable" if evaluator_failed
                            else "quality_complete"
                        )
                        domain_updates["completion_decision"] = {
                            "action": (
                                CompletionDecision.TERMINATE.value if evaluator_failed
                                else CompletionDecision.COMPLETE.value
                            ),
                            "reason": quality_stop_reason,
                            "gaps": list(assessment_payload.get("missing_information", [])),
                        }
                    missing_information = [
                        str(item)
                        for item in assessment_payload.get(
                            "missing_information",
                            [],
                        )
                        if str(item).strip()
                    ]
                    suggested_queries = [
                        str(item)
                        for item in assessment_payload.get(
                            "suggested_queries",
                            [],
                        )
                        if str(item).strip()
                    ]
                    owned_requirement_ids = [
                        str(item)
                        for item in researcher_state.get(
                            "requirement_ids",
                            [],
                        )
                        if str(item).strip()
                    ]
                    recovery_budget = (
                        configurable.quality_gap_recovery_max_attempts
                    )
                    recovery_is_actionable = (
                        limit_reached
                        and not fetch_budget_exhausted
                        and not assessment_payload.get("evaluator_error")
                        and assessment_payload.get("decision") != "complete"
                        and bool(missing_information)
                        and bool(suggested_queries)
                        and bool(owned_requirement_ids)
                        and not bool(researcher_state.get("cancelled"))
                        and quality_recovery_state.attempts
                        < recovery_budget
                    )
                    if recovery_is_actionable:
                        quality_recovery_state = QualityRecoveryState(
                            attempts=quality_recovery_state.attempts + 1,
                            active=True,
                            target_requirement_ids=tuple(
                                owned_requirement_ids
                            ),
                            triggering_assessment_revision=None,
                        )
                        await publish_task_activity(
                            cfg,
                            "recovery.started",
                            kind="quality",
                            phase="gap_recovery",
                            status="warning",
                            title="进入定向补证",
                            summary="质量门禁发现用户需求覆盖缺口，正在执行一次受限补证。",
                            iteration=turn,
                            duration_ms=None,
                            payload={
                                "attempt": quality_recovery_state.attempts,
                                "requirement_count": len(owned_requirement_ids),
                            },
                            dedupe_key=f"activity:recovery:{quality_recovery_state.attempts}:started",
                            update_run_summary=True,
                        )
                        evidence_summary = [
                            {
                                "evidence_id": str(
                                    item.get("evidence_id", "")
                                ),
                                "claim": str(item.get("claim", ""))[:500],
                            }
                            for item in eligible_evidence_records(
                                researcher_state
                            )[:8]
                        ]
                        recovery_instruction = {
                            "requirement_ids": owned_requirement_ids,
                            "missing_information": missing_information,
                            "suggested_queries": suggested_queries,
                            "existing_evidence_summary": evidence_summary,
                        }
                        additional_messages = [HumanMessage(content=(
                            "[Quality Gap Recovery] This is the single "
                            "bounded recovery round. Search only for the "
                            "listed user requirements and gaps, then stop. "
                            "Do not expand the task scope.\n"
                            + json.dumps(
                                recovery_instruction,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        ))]
                        should_continue = True
                    elif quality_recovery_state.active:
                        completed_recovery_attempt = quality_recovery_state.attempts
                        quality_recovery_state = QualityRecoveryState(
                            attempts=quality_recovery_state.attempts,
                            active=False,
                            target_requirement_ids=(
                                quality_recovery_state
                                .target_requirement_ids
                            ),
                            triggering_assessment_revision=(
                                quality_recovery_state
                                .triggering_assessment_revision
                            ),
                        )
                        await publish_task_activity(
                            cfg,
                            "recovery.completed",
                            kind="quality",
                            phase="evidence_review",
                            status="success",
                            title="定向补证结束",
                            summary="受限补证轮已完成，正在重新判断证据覆盖。",
                            iteration=turn,
                            duration_ms=None,
                            payload={
                                "attempt": completed_recovery_attempt,
                                "decision": assessment_payload.get("decision"),
                            },
                            dedupe_key=f"activity:recovery:{completed_recovery_attempt}:completed",
                            update_run_summary=True,
                        )

                return ToolResultsHookResult(
                    messages=cast(list[BaseMessage], tool_outputs),
                    additional_messages=additional_messages,
                    updates=domain_updates,
                    should_continue=should_continue,
                    quality_recovery=quality_recovery_state,
                    approval_pending_turns=approval_pending_turns,
                    reason=quality_stop_reason or (
                        "completion_policy_satisfied"
                        if decision.action in {
                            CompletionDecision.COMPLETE,
                            CompletionDecision.COMPLETE_PARTIAL,
                        }
                        else "max_turns"
                        if decision.action is CompletionDecision.TERMINATE
                        else None
                    ),
                )

            model_candidates = (
                [
                    ModelCandidate(
                        model_id=configurable.research_model,
                        model=None,
                        model_config={
                            "model": configurable.research_model,
                            "max_tokens": configurable.research_model_max_tokens,
                            "max_retries": 0,
                        },
                    )
                ]
                if configurable.model_backend == "litellm"
                else build_model_candidate_chain(
                    configurable.research_model,
                    configurable.model_fallbacks.get("researcher", []),
                    max_tokens=configurable.research_model_max_tokens,
                    config=cfg,
                    role="researcher",
                    model=graph.configurable_model,
                )
            )
            model_config = model_candidates[0].model_config
            task_id = str(cfg.get("metadata", {}).get("task_id") or "")
            researcher_state_key = (
                f"researcher:{task_id}"
                if task_id
                else "researcher:standalone"
            )
            restored_query_state = None
            query_state_payload = researcher_state.get(
                "query_state_snapshot"
            )
            if isinstance(query_state_payload, dict):
                restored_query_state = QueryLoopState.from_snapshot(
                    query_state_payload
                )
            checkpoint_callback = researcher_state.get(
                "_query_checkpoint_callback"
            )
            researcher_checkpoint_sink: QueryCheckpointSink | None = (
                CallbackQueryCheckpointSink(checkpoint_callback)
                if callable(checkpoint_callback)
                else None
            )
            if (
                researcher_checkpoint_sink is None
                and configurable.query_session_persistence_enabled
                and cfg.get("metadata", {}).get("run_fence_token")
                is not None
                and cfg.get("metadata", {}).get("run_lease_owner_id")
            ):
                run_store = graph._bind_run_context_fence(  # noqa: SLF001
                    RunContextStore(
                        str(
                            cfg.get("metadata", {}).get(
                                "run_id", "default"
                            )
                        ),
                        runs_dir=configurable.runs_dir,
                        inline_content_max_chars=(
                            configurable
                            .query_journal_inline_content_max_chars
                        ),
                    ),
                    cfg,
                )
                if run_store.manifest_path.exists():
                    if restored_query_state is None:
                        stored_payload = run_store.replay().query_states.get(
                            researcher_state_key
                        )
                        if stored_payload is not None:
                            restored_query_state = (
                                QueryLoopState.from_snapshot(
                                    stored_payload
                                )
                            )
                    researcher_checkpoint_sink = (
                        RunContextQueryCheckpointSink(run_store)
                    )
            if restored_query_state is not None:
                consumed_team_inputs.update(restored_query_state.consumed_input_ids)
                quality_recovery_state = (
                    restored_query_state.quality_recovery
                )
                approval_pending_turns = (
                    restored_query_state.approval_pending_turns
                )
            completed_messages = runtime_messages
            completed_turn = int(researcher_state.get("tool_call_iterations", 0) or 0)
            async for event in query(QueryParams(
                messages=runtime_messages,
                system_prompt=graph.build_researcher_system_prompt(
                    configurable,
                    tools,
                    cfg,
                ),
                model=graph.configurable_model,
                config=cfg,
                tools=tools,
                execution_tools=all_tools,
                role=graph.AgentRole.RESEARCHER,
                model_span_name="researcher.model",
                model_config=model_config,
                max_turns=(
                    configurable.max_react_tool_calls
                    + configurable.quality_gap_recovery_max_attempts
                    + configurable.approval_pending_turn_allowance
                ),
                initial_turn=completed_turn,
                max_tool_description_chars=configurable.max_tool_description_chars,
                context_policy=ContextPolicy(
                    max_tool_result_chars=configurable.max_mcp_output_chars,
                ),
                before_turn_hooks=[before_turn],
                stop_hooks=[handle_no_tool_stop],
                tool_results_hook=after_tools,
                max_concurrent_tools=configurable.max_concurrent_tool_calls,
                max_tool_batch_size=configurable.max_tool_batch_size,
                tool_timeout_seconds=configurable.research_tool_call_timeout_seconds,
                hook_timeout_seconds=configurable.hook_timeout_seconds,
                model_timeout_seconds=configurable.model_call_timeout_seconds,
                model_transport_max_attempts=(
                    1
                    if configurable.model_backend == "litellm"
                    else configurable.model_transport_max_attempts
                ),
                budget_gate=BudgetGate.from_config(
                    configurable,
                    str(cfg.get("metadata", {}).get("run_id", "default")),
                    started_at=cfg.get("metadata", {}).get("run_started_at"),
                ),
                execution_namespace=(
                    researcher_state_key
                ),
                initial_state=restored_query_state,
                state_key=researcher_state_key,
                checkpoint_sink=researcher_checkpoint_sink,
                acknowledged_event_ids=tuple(
                    researcher_state.get(
                        "applied_query_event_ids",
                        [],
                    )
                ),
                model_candidates=model_candidates,
                context_recovery_max_attempts=(
                    configurable.context_recovery_max_attempts
                ),
                output_token_escalation_enabled=(
                    configurable.output_token_escalation_enabled
                ),
                output_continuation_max_attempts=(
                    configurable.output_continuation_max_attempts
                ),
                model_max_output_tokens_overrides=(
                    configurable.model_max_output_tokens_overrides
                ),
            )):
                event_id = str(event.data.get("event_id") or "")
                applied_event_ids = list(
                    researcher_state.get(
                        "applied_query_event_ids",
                        [],
                    )
                )
                event_already_applied = (
                    bool(event_id) and event_id in applied_event_ids
                )
                updates = event.data.get("updates")
                if updates and not event_already_applied:
                    apply_update_to_state(researcher_state, updates)
                if event_id and not event_already_applied:
                    researcher_state["applied_query_event_ids"] = [
                        *applied_event_ids,
                        event_id,
                    ]
                if event.type == "query.state_changed":
                    query_state = event.data.get("state")
                    if isinstance(query_state, QueryLoopState):
                        researcher_state["query_state_snapshot"] = (
                            query_state.to_snapshot()
                        )
                if event.type == "query.completed":
                    completed_messages = list(event.data.get("messages", runtime_messages))
                    completed_turn = int(
                        event.data.get("transition", {}).get("turn", completed_turn)
                    )
                    if event.data.get("transition", {}).get("reason") == "cancelled":
                        researcher_state["cancelled"] = True
                    elif event.data.get("transition", {}).get("reason") in {
                        "budget_exhausted",
                        "deadline_exceeded",
                        "model_timeout",
                    }:
                        reason = str(event.data["transition"]["reason"])
                        decision = completion_policy.evaluate(
                            researcher_completion_context(
                                has_remaining_budget=False,
                                exhausted_reason=reason,
                            )
                        )
                        researcher_state["completion_decision"] = {
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "gaps": list(decision.gaps),
                        }

            # A control signal may arrive while the final model/tool call is in
            # flight.  The legacy step loop observed it before entering the
            # compression node, so preserve that terminal-boundary check here.
            final_control = await before_turn(completed_messages, completed_turn + 1, cfg)
            if final_control is not None:
                completed_messages.extend(final_control.messages)
                if final_control.updates:
                    apply_update_to_state(researcher_state, final_control.updates)

            researcher_state["researcher_messages"] = completed_messages[
                memory_prefix_count:
            ]
            researcher_state["tool_call_iterations"] = completed_turn
            if researcher_state.get("cancelled"):
                return {
                    **researcher_state,
                    "compressed_research": "",
                    "raw_notes": [],
                }
            task_id = str(cfg.get("metadata", {}).get("task_id", ""))
            if task_id:
                from open_deep_research.tasks.registry import (
                    TaskPhase,
                    get_task_registry,
                )

                task_record = get_task_registry().get(task_id)
                if task_record is not None:
                    task_record.phase = TaskPhase.COMPRESSING
                await event_publisher_from_config(cfg).publish(
                    "research.task.progress",
                    stage="researching",
                    payload={
                        "task_id": task_id,
                        "wave_id": task_record.wave_id if task_record else "",
                        "mode": str(cfg.get("metadata", {}).get("research_mode") or "async"),
                        "status": "running",
                        "phase": "compressing",
                        "iteration": completed_turn,
                        "source_count": task_record.source_count if task_record else 0,
                        "tool_categories": [],
                    },
                    dedupe_key=f"task:{task_id}:compressing",
                )
            await publish_task_activity(
                cfg,
                "task.phase.changed",
                kind="lifecycle",
                phase="compressing",
                status="running",
                title="压缩研究发现",
                summary="正在把已验证证据整理为可交接的结构化研究发现。",
                iteration=completed_turn,
                duration_ms=None,
                payload={"activity_label": "压缩研究发现"},
                dedupe_key=f"activity:phase:compressing:{completed_turn}",
                update_run_summary=True,
            )
            update = await graph.compress_research(researcher_state, cfg)
            apply_update_to_state(researcher_state, update)
            return researcher_state

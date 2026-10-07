"""Composition root for an authorized native research run."""

from pathlib import Path
from types import SimpleNamespace

from open_deep_research.agentscope_runtime.research_agents import Researcher, Supervisor
from open_deep_research.agentscope_runtime.research_models import ResearchModels
from open_deep_research.agentscope_runtime.research_pipeline import (
    FileResearchCheckpoint,
    ResearchPipeline,
    ResearchSnapshot,
)
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.agentscope_runtime.research_stages import (
    NativeResearchStages,
)
from open_deep_research.configuration import Configuration
from open_deep_research.tools.base import ToolExecutionZone


def build_research_pipeline(
    *,
    run_id,
    run_config,
    model_factory,
    config_provider,
    tools_for,
    checkpoint_path: Path,
    local_zones: frozenset[ToolExecutionZone],
    dispatcher=None,
    offloader=None,
    model_for=None,
    memory_recall=None,
    memory_write=None,
    report_writer=None,
    budget_available=None,
    context_chars=120_000,
    recovery=None,
    team=None,
    team_artifact_dir=None,
    external_team_workers=False,
    team_launcher=None,
    authorized_user_id=None,
    worker_only=False,
):
    """Bind frozen config, native model/tool adapters and stage persistence.

    Caller resolves ownership before constructing this run. A sandbox run must
    supply its authorized model resolver and dispatcher; no direct-model fallback
    is introduced here. The old HTTP execution path is not switched implicitly.
    """
    fingerprint = run_config.compatibility_projection()["metadata"][
        "run_config_fingerprint"
    ]
    checkpoint = FileResearchCheckpoint(checkpoint_path)
    original_provider = config_provider
    if recovery:
        config_provider = lambda: recovery.config(original_provider)
    state = (
        recovery.snapshot
        if recovery
        else checkpoint.load()
        if checkpoint_path.exists() or recovery
        else ResearchSnapshot(run_id=run_id, config_fingerprint=fingerprint)
    )
    if state.run_id != run_id:
        raise ValueError("research checkpoint belongs to another run")
    models = ResearchModels(
        model_factory,
        model_for=model_for,
        context_chars=context_chars,
        recovery=recovery,
    )
    if recovery and not hasattr(recovery, "research_cache"):
        from open_deep_research.agentscope_runtime.efficiency import ResearchCache

        recovery.research_cache = ResearchCache(recovery, checkpoint_path.parent.parent)
    if Configuration.from_runnable_config(config_provider()).enable_memory:
        from open_deep_research.agentscope_runtime.memory import ResearchMemory

        user_id = recovery.lease.user_id if recovery else authorized_user_id
        if not user_id:
            raise ValueError("memory requires an authenticated host user")
        memory = ResearchMemory(user_id, models)
        memory_recall = memory_recall or memory.recall
        memory_write = memory_write or memory.write
    quality = NativeResearchQuality(models, config_provider)
    if report_writer is None:
        from open_deep_research.agentscope_runtime.report import NativeReportWriter
        report_writer = NativeReportWriter(models)
    researcher = Researcher(
        models,
        config_provider,
        tools_for,
        run_id=run_id,
        local_zones=local_zones,
        dispatcher=dispatcher,
        offloader=offloader,
        quality=quality,
        context_chars=context_chars,
    )
    workers = None
    if (
        recovery is not None
        and Configuration.from_runnable_config(config_provider()).enable_async_research
        and team is None
    ):
        raise ValueError("durable async research requires a bound native team")
    if team is not None:
        from open_deep_research.agentscope_runtime.team_worker import TeamWorkers

        if recovery is None or team_artifact_dir is None:
            raise ValueError("durable teams require recovery and an artifact directory")
        if team.lease != recovery.lease:
            raise ValueError(
                "team and research must share the same authorized run lease"
            )
        cfg = Configuration.from_runnable_config(config_provider())
        if cfg.async_research_mode == "teams":
            from open_deep_research.agentscope_runtime.teams_worker import TeamsWorkers
            worker_class = TeamsWorkers
        else:
            worker_class = TeamWorkers
        workers = worker_class(
            team,
            recovery,
            researcher,
            quality,
            team_artifact_dir,
            external=external_team_workers,
            launcher=team_launcher,
        )
    if worker_only:
        # Worker 借用正在运行的领队租约，只装配子任务执行器；不能恢复或
        # 执行领队尚在 inflight 的外层阶段。
        if workers is None:
            raise ValueError("team executor requires a bound native team")
        return SimpleNamespace(team_workers=workers)
    supervisor = Supervisor(
        models,
        config_provider,
        researcher,
        quality=quality,
        completion_policy=True,
        budget_available=budget_available,
        run_id=run_id,
        offloader=offloader,
        context_chars=context_chars,
        team_workers=workers,
    )
    stages = NativeResearchStages(
        models,
        supervisor,
        config_provider,
        memory_recall=memory_recall,
        memory_write=memory_write,
        report_writer=report_writer,
    )
    if recovery:
        from open_deep_research.agentscope_runtime.recovery import RecoveryStages

        stages = RecoveryStages(stages, recovery)
    pipeline = ResearchPipeline(
        state,
        stages,
        recovery.save if recovery else checkpoint.save,
        config_fingerprint=fingerprint,
        recovery=recovery,
    )
    pipeline.team_workers = workers
    return pipeline


async def open_durable_research_pipeline(
    *,
    store,
    user_id,
    ttl=30,
    approval_applier=None,
    public_publisher=None,
    team_factory=None,
    **kwargs,
):
    """Rebuild an existing authorized run; release the lease if assembly fails."""
    from open_deep_research.agentscope_runtime.recovery import RecoverySession

    recovery = await RecoverySession.open(store, kwargs["run_id"], user_id, ttl=ttl)
    recovery.approval_applier = approval_applier
    recovery.public_publisher = public_publisher
    try:
        if team_factory is not None:
            kwargs["team"] = await team_factory(recovery)
        flow = build_research_pipeline(**kwargs, recovery=recovery)
        await recovery.consume_decisions(flow)
        return flow
    except BaseException:
        await recovery.close()
        raise

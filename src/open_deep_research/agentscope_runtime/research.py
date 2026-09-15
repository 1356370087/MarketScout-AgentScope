"""Composition root for an authorized native research run."""

from pathlib import Path

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
    state = (
        checkpoint.load()
        if checkpoint_path.exists()
        else ResearchSnapshot(run_id=run_id, config_fingerprint=fingerprint)
    )
    if state.run_id != run_id:
        raise ValueError("research checkpoint belongs to another run")
    models = ResearchModels(
        model_factory, model_for=model_for, context_chars=context_chars
    )
    quality = NativeResearchQuality(models, config_provider)
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
    )
    stages = NativeResearchStages(
        models,
        supervisor,
        config_provider,
        memory_recall=memory_recall,
        memory_write=memory_write,
        report_writer=report_writer,
    )
    return ResearchPipeline(
        state, stages, checkpoint.save, config_fingerprint=fingerprint
    )

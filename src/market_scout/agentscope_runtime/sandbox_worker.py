"""Capability-only native Researcher composition inside a sandbox container."""

from market_scout.platform.runtime import environment as config_environment

import os

from pydantic import SecretStr

from market_scout.agentscope_runtime.gateway import SandboxBinding
from market_scout.agentscope_runtime.models import (
    ROLES,
    CredentialBinding,
    ModelFactory,
)
from market_scout.agentscope_runtime.research_agents import (
    ResearchAssignment,
    Researcher,
)
from market_scout.agentscope_runtime.research_models import ResearchModels
from market_scout.agentscope_runtime.research_quality import NativeResearchQuality
from market_scout.agentscope_runtime.run_config import RunConfig
from market_scout.sandbox.gateway_catalog import load_gateway_catalog_tools
from market_scout.sandbox.gateway_tool import (
    AuthorizedLocalToolProxy,
    GatewayToolProxy,
)
from market_scout.tools.base import ToolExecutionZone


async def execute_worker(payload, config):
    """Run the native loop; all model/tool effects remain behind the Gateway."""
    token = config_environment.get("SANDBOX_TASK_TOKEN")
    url = config_environment.get("SANDBOX_GATEWAY_URL")
    if not token or not url:
        raise ValueError("sandbox_gateway_capability_missing")
    if payload.researcher_state.get("query_state_snapshot"):
        raise ValueError("legacy_worker_checkpoint_read_only")
    run = RunConfig.compile(config)
    # These are capability references, never provider credentials. The resolver
    # below exclusively constructs SandboxChatModel, so build() is not used.
    bindings = {
        role: CredentialBinding(
            reference="task-capability",
            scope="run",
            owner=payload.run_id,
            allowed_models=(run.get(field) or run.get(fallback),),
            key=SecretStr(""),
        )
        for role, (field, fallback, _tokens) in ROLES.items()
    }
    factory = ModelFactory(run, scope="run", owner=payload.run_id, bindings=bindings)

    def model_for(role, task_id):
        # Compression and quality calls use the same task capability; the
        # generic pipeline label must never escape the authorized task scope.
        return factory.build_sandbox(
            role,
            SandboxBinding(
                url,
                payload.run_id,
                payload.task_id,
                role,
                "researching",
                SecretStr(token),
            ),
        )

    async def tools_for(assignment):
        from market_scout.tools.read_file import read_file
        from market_scout.tools.shell_exec import shell_exec
        from market_scout.tools.write_file import write_file

        local = [read_file, write_file, shell_exec]
        return [
            *local,
            *await load_gateway_catalog_tools(
                "researcher", config, {tool.name for tool in local}
            ),
        ]

    async def dispatch(tool, input, context):
        proxy = (
            AuthorizedLocalToolProxy
            if tool.execution_zone is ToolExecutionZone.SANDBOX_LOCAL
            else GatewayToolProxy
        )
        return await proxy(tool).call(input, context)

    models = ResearchModels(factory, model_for=model_for)
    quality = NativeResearchQuality(models, lambda: config)
    researcher = Researcher(
        models,
        lambda: config,
        tools_for,
        run_id=payload.run_id,
        local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
        dispatcher=dispatch,
        quality=quality,
    )
    try:
        handoff = await researcher.run(
            ResearchAssignment(
                task_id=payload.task_id,
                research_topic=payload.research_topic,
                requirement_ids=payload.researcher_state.get("requirement_ids", []),
            ),
            payload.researcher_state.get("coverage_contract") or {},
        )
        return {
            "compressed_research": handoff.compressed_research,
            "raw_notes": [handoff.compressed_research],
            "evidence_registry": handoff.evidence_registry,
            "metrics": {"sources_read": len(handoff.evidence_registry)},
            "completion_decision": {"reason": handoff.termination},
        }
    finally:
        await factory.aclose()

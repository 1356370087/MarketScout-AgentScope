"""Deterministic container probe for the native application host, not a live-model eval."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import httpx

from open_deep_research.agentscope_runtime.app import ASRuntime
from open_deep_research.agentscope_runtime.research_pipeline import (
    PendingDecision,
    ResearchPipeline,
)
from open_deep_research.agentscope_runtime.settings import ASRuntimeSettings
from open_deep_research.api.native_runs import NativeRuns
from security.rbac.dependencies import get_current_principal
from security.rbac.principal import synthetic_dev_principal


async def main():
    runtime = await ASRuntime.create(ASRuntimeSettings(None, "unused", True, "m10_"))
    try:
        store = await runtime.create_recovery_store()

        class Stages:
            async def execute(self, stage, state):
                if stage == "plan_approval":
                    return PendingDecision(
                        stage=stage, question="Fixture plan approval"
                    )
                if stage == "final_report_generation":
                    state.final_report = "Synthetic container acceptance report"

        @asynccontextmanager
        async def factory(state, config, recovery):
            yield ResearchPipeline(
                state,
                Stages(),
                recovery.save,
                config_fingerprint=state.config_fingerprint,
                recovery=recovery,
            )

        async def prepare(request, principal):
            return {
                "configurable": {
                    "model_backend": "legacy",
                    "sandbox_enabled": False,
                    "enable_async_research": False,
                }
            }

        service = NativeRuns(store, factory, prepare)
        app = runtime.build_app(research_runs=service)
        app.dependency_overrides[get_current_principal] = synthetic_dev_principal
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://native-host"
            ) as client,
        ):
            response = await client.post(
                "/runs",
                json={
                    "messages": [{"role": "user", "content": "Synthetic acceptance"}]
                },
            )
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            await asyncio.gather(*list(service.tasks.values()))
            snapshot = (await client.get("/runs/" + run_id)).json()
            assert snapshot["status"] == "awaiting_plan_approval", snapshot
            pending = snapshot["pending_human_action"]["action_id"]
            response = await client.post(
                f"/runs/{run_id}/human-actions/{pending}",
                json={"action": "approve"},
            )
            assert response.status_code == 200, response.text
            await asyncio.gather(*list(service.tasks.values()))
            final = (await client.get("/runs/" + run_id)).json()
            assert final["status"] == "completed", final
            replay = await client.get(f"/runs/{run_id}/events")
            assert "event: run.completed" in replay.text
            assert not any(name.startswith("langchain") for name in sys.modules)
        assert service.closed and not service.tasks
        print(
            json.dumps(
                {
                    "probe": "synthetic_native_host",
                    "python": sys.version.split()[0],
                    "status": "passed",
                    "approval": True,
                    "sse": True,
                    "langchain_loaded": False,
                    "shutdown": True,
                }
            )
        )
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())

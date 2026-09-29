"""Isolated HTTP process for native startup/admission E2E, without external models."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
from open_deep_research.agentscope_runtime.research_pipeline import PendingDecision, ResearchPipeline
from open_deep_research.api.native_runs import NativeRuns
from open_deep_research.api.research_router import build_research_router
from security.rbac.dependencies import get_current_principal
from tests.auth_helpers import research_principal


def main():
    directory, port = Path(sys.argv[1]), int(sys.argv[2])
    store = RecoveryStore("sqlite+aiosqlite:///" + (directory / "recovery.db").as_posix())

    class Stages:
        async def execute(self, stage, state):
            if stage == "plan_approval":
                return PendingDecision(stage=stage, question="Confirm fixture")
            if stage == "research_supervisor":
                while not (directory / "continue").exists():
                    await asyncio.sleep(0.05)
            if stage == "final_report_generation":
                state.final_report = "Native process recovered successfully."

    @asynccontextmanager
    async def factory(state, config, recovery):
        yield ResearchPipeline(state, Stages(), recovery.save,
                               config_fingerprint=state.config_fingerprint, recovery=recovery)

    async def prepare(request, principal):
        return {"configurable": request.configurable}

    service = NativeRuns(store, factory, prepare, runs_dir=directory)

    @asynccontextmanager
    async def lifespan(app):
        await store.create_tables()
        await service.recover_interrupted()
        try:
            yield
        finally:
            await service.aclose()
            await store.aclose()

    if os.getenv("TEST_APPLICATION_ENTRYPOINT") == "true":
        from open_deep_research.agentscope_runtime import native_host
        from open_deep_research import server as application

        async def build(**kwargs):
            await store.create_tables()
            service.admission = kwargs["admission"]

            async def close():
                await service.aclose()
                await store.aclose()

            service.native_aclose = close
            return service

        native_host.build_native_research_service = build
        app = application.app
    else:
        app = FastAPI(lifespan=lifespan)
        app.include_router(build_research_router(service))
    app.dependency_overrides[get_current_principal] = lambda: research_principal("alice")

    @app.get("/testing/health")
    async def health():
        return {"connections": service.admission.connection_limiter.active}

    @app.post("/testing/shutdown")
    async def shutdown():
        server.should_exit = True
        return {"status": "stopping"}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.run()


if __name__ == "__main__":
    main()

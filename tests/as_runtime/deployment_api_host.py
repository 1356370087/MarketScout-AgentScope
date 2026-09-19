"""部署故障矩阵的 API 宿主进程：真实容器内原生组合 + uvicorn HTTP + 崩溃窗口。

用法（容器内）::

    python deployment_api_host.py /evidence/host-request.json

request 字段：database_url（容器视角 PG DSN）、schema、port、window、
evidence_dir。window 取值：

- ``approval_pause``：不注入失败点；管线在计划审批处持久暂停，外层用
  ``docker kill`` 强杀（稳定窗口）。
- ``model_committed`` / ``tool_committed``：journal 失败点在首个模型/工具
  操作**提交后** ``os._exit(73)``（不依赖 finally），验证部署边界回放。
- ``report_committed``：原生报告模型结果提交后立即退出，新容器重放正文，报告模型只调用一次；
- ``approval_committed`` / ``event_committed``：审批消费或完成状态与事件
  已提交、宿主尚未继续处理时退出，验证持久决策及事件重放。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path


def _append_evidence(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    evidence_dir = Path(request["evidence_dir"])
    window = request.get("window", "")
    from fastapi import FastAPI
    from uvicorn import Config, Server

    from open_deep_research.agentscope_runtime.native_host import mount_native_research
    from open_deep_research.agentscope_runtime.recovery_store import RecoveryStore
    from open_deep_research.api.native_runs import NativeRuns

    store = RecoveryStore(
        request["database_url"],
        engine_kwargs={
            "connect_args": {"server_settings": {"search_path": request["schema"]}}
        },
    )
    await store.create_tables()

    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse, StructuredResponse
    from sqlalchemy import select

    from open_deep_research.agentscope_runtime.model_policy import (
        ModelCallPolicy,
        ModelPolicyMiddleware,
    )
    from open_deep_research.agentscope_runtime.recovery import RecoverySession
    from open_deep_research.agentscope_runtime.research import build_research_pipeline
    from open_deep_research.tools.base import (
        ToolExecutionZone,
        ToolOrigin,
        ToolResult,
        build_tool,
    )
    from test_research_migration import Empty, ScriptedModel, cfg, evidence, tool_call

    from open_deep_research.report import orchestrator
    original_review = orchestrator.review_report

    async def traced_review(*args, **kwargs):
        result = await original_review(*args, **kwargs)
        _append_evidence(evidence_dir / "reviews.jsonl", json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
        return result

    orchestrator.review_report = traced_review

    config = cfg(max_researcher_iterations=4, quality_evaluation_min_sources=1)

    class Model(ScriptedModel):
        async def _call_api(self, *args, **kwargs):
            _append_evidence(evidence_dir / "model-calls.txt", "call:agent")
            return await super()._call_api(*args, **kwargs)

        async def generate_structured_output(self, messages, schema):
            _append_evidence(evidence_dir / "model-calls.txt", "call:structured")
            if schema.__name__ == "ReportReview":
                _append_evidence(evidence_dir / "review-calls.txt", "review")
                return StructuredResponse(content={
                    "decision": "pass",
                    "coverage": [
                        {"requirement_id": requirement_id, "status": "covered", "explanation": "Synthetic evidence covers the fixture requirement."}
                        for requirement_id in sorted(set(re.findall(
                            r"COV-\d+-[a-f0-9]+", str(messages),
                        )))
                    ],
                    "dimensions": {key: 1.0 for key in (
                        "coverage", "citation_correctness", "contradictions",
                        "unsupported_claims", "redundancy", "executive_readability",
                    )},
                    "citation_audit": [{
                        "claim": "市场增长", "citation_target": "https://example.test/source",
                        "supported": True, "evidence_ids": ["ev1"],
                    }],
                })
            return StructuredResponse(content={"research_brief": "市场规模"})

    class Factory:
        run = SimpleNamespace(get=lambda name: {})

        def __init__(self):
            self.models = {
                "supervisor": Model(
                    [
                        [
                            tool_call(
                                "ConductResearch", "delegate", research_topic="市场规模"
                            )
                        ],
                        [tool_call("ResearchComplete", "lead-done")],
                    ]
                ),
                "researcher": Model(
                    [
                        [tool_call("web_research", "search")],
                        [tool_call("ResearchComplete", "worker-done")],
                    ]
                ),
            }

        def build(self, role):
            # 装配模型不等于物理调用；计数在真正调用入口完成。
            if role == "report_review":
                return Model([])
            return self.models[role]

        def descriptor(self, role):
            return {"model": "fixture", "max_output_tokens": 1000}

        def policy_middleware(self, role, candidates=None):
            return ModelPolicyMiddleware(
                ModelCallPolicy(candidates or [self.build(role)], circuit_enabled=False)
            )

        async def complete_with_recovery(self, *args, **kwargs):
            _append_evidence(evidence_dir / "model-calls.txt", "call:complete")
            role = args[0] if args else kwargs.get("role")
            if role == "final_report":
                _append_evidence(evidence_dir / "report-calls.txt", "final_report")
            return ChatResponse(
                content=[
                    TextBlock(
                        text="报告明确基于[测试证据](https://example.test/source)。"
                    )
                ],
                is_last=True,
            )

    async def tool_call_effect(input, context, progress=None):
        _append_evidence(evidence_dir / "tool-effects.txt", "effect")
        return ToolResult(output={"evidence": [evidence()]})

    async def tools_for(assignment):
        return [
            build_tool(
                name="web_research",
                input_schema=Empty,
                description="research",
                call=tool_call_effect,
                origin=ToolOrigin.SYSTEM,
                execution_zone=ToolExecutionZone.HOST_CONTROL,
            )
        ]

    @asynccontextmanager
    async def factory(state, run_config, recovery: RecoverySession):
        run_id = recovery.lease.run_id

        async def crash(point):
            if point != "operation_committed":
                return
            if window == "review_committed":
                if recovery.task_id.get().startswith("report:lead.report_review:"):
                    _append_evidence(evidence_dir / "crash-window.txt", window)
                    os._exit(73)
                return
            if window == "report_committed":
                if recovery.stage.get().startswith("final_report_generation:"):
                    _append_evidence(evidence_dir / "crash-window.txt", window)
                    os._exit(73)
                return
            if window not in {"model_committed", "tool_committed"}:
                return
            wanted = "model" if window == "model_committed" else "tool"
            async with store.engine.connect() as conn:
                committed = await conn.scalar(
                    select(store.ops.c.key).where(
                        store.ops.c.run_id == run_id,
                        store.ops.c.kind.like(wanted + "%"),
                        store.ops.c.state == "committed",
                    )
                )
            if committed:
                # 模拟部署实例在提交后、响应前失联：不经过 finally。
                os._exit(73)

        recovery.failpoint = crash
        original_save = store.save

        async def crash_after_save(lease, snapshot, **kwargs):
            await original_save(lease, snapshot, **kwargs)
            if (
                window == "approval_committed" and kwargs.get("command_id")
                and snapshot.decisions
            ) or (window == "event_committed" and snapshot.status == "completed"):
                _append_evidence(evidence_dir / "crash-window.txt", window)
                os._exit(73)

        store.save = crash_after_save
        merged = run_config.compatibility_projection()
        effective = {
            "configurable": {
                **config["configurable"],
                **state.application.get("request_configurable", {}),
                **merged.get("configurable", {}),
            },
            "metadata": {**config["metadata"], **merged.get("metadata", {})},
        }
        try:
            pipeline = build_research_pipeline(
                run_id=run_id,
                run_config=run_config,
                model_factory=Factory(),
                config_provider=lambda: effective,
                tools_for=tools_for,
                checkpoint_path=evidence_dir / "native-checkpoint.json",
                local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
                recovery=recovery,
            )
            original_execute = pipeline.stages.execute

            async def traced_execute(stage, snapshot):
                result = await original_execute(stage, snapshot)
                _append_evidence(evidence_dir / "stages.jsonl", json.dumps({
                    "stage": stage,
                    "hitl": effective["configurable"].get("enable_human_in_loop"),
                    "pending": result.model_dump(mode="json") if result else None,
                }, ensure_ascii=False))
                return result

            pipeline.stages.execute = traced_execute
            yield pipeline
        finally:
            recovery.failpoint = None
            store.save = original_save

    async def prepare_config(http_request, principal):
        configurable = dict(http_request.configurable)
        configurable.setdefault("event_log_enabled", False)
        configurable.setdefault("quality_evaluation_enabled", False)
        configurable.setdefault("allow_clarification", False)
        return {
            "configurable": configurable,
            "metadata": {"user_id": principal.user_id},
        }

    service = NativeRuns(
        store, factory, prepare_config, runs_dir=evidence_dir / "runs"
    )
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    mount_native_research(app, service)
    server = Server(
        Config(app, host="0.0.0.0", port=int(request["port"]), log_level="warning")
    )
    await server.serve()
    await service.aclose()
    await store.aclose()


if __name__ == "__main__":
    asyncio.run(main())

"""Explicit M5 live-model acceptance using a scoped, short-lived Proxy key.

Reads local .env; reports only sanitized status, counts and cleanup evidence.
The research source is an identified synthetic fixture, not a live Web claim.
"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from agentscope.message import UserMsg
from dotenv import dotenv_values
from pydantic import BaseModel, SecretStr

from open_deep_research.agentscope_runtime.models import CredentialBinding, ModelFactory
from open_deep_research.agentscope_runtime.research import build_research_pipeline
from open_deep_research.agentscope_runtime.run_config import RunConfig
from open_deep_research.models.catalog import parse_model_info
from open_deep_research.tools.base import (
    ToolExecutionZone,
    ToolOrigin,
    ToolResult,
    build_tool,
)


class Empty(BaseModel):
    pass


async def main():
    values = dotenv_values(".env")
    proxy = values["LITELLM_BASE_URL"].rstrip("/").removesuffix("/v1")
    route = values.get("LITELLM_SUMMARIZATION_MODEL", "if-summarization-v1")
    run_id = "m5-live-" + uuid4().hex
    result = {
        "run_id": run_id,
        "route": route,
        "fixture": "synthetic_market",
        "cleanup": {},
    }
    key, factory = None, None
    headers = {"Authorization": "Bearer " + values["LITELLM_MASTER_KEY"]}
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        try:
            response = await client.get(proxy + "/model/info", headers=headers)
            response.raise_for_status()
            catalog = parse_model_info(response.json())
            if route not in catalog:
                raise ValueError("configured_route_missing_catalog")
            response = await client.post(
                proxy + "/key/generate",
                headers=headers,
                json={
                    "models": [route],
                    "duration": "10m",
                    "max_budget": 0.05,
                    "key_alias": run_id,
                },
            )
            response.raise_for_status()
            key = response.json()["key"]
            config = {
                "configurable": {
                    "model_backend": "litellm",
                    "sandbox_enabled": False,
                    "research_model": route,
                    "compression_model": route,
                    "final_report_model": route,
                    "quality_evaluation_model": route,
                    "message_summary_model": route,
                    "model_catalog_snapshot": {route: catalog[route].model_dump()},
                    "research_model_max_tokens": 4096,
                    "compression_model_max_tokens": 4096,
                    "final_report_model_max_tokens": 4096,
                    "quality_evaluation_model_max_tokens": 4096,
                    "allow_clarification": False,
                    "enable_human_in_loop": False,
                    "enable_memory": False,
                    "quality_evaluation_enabled": True,
                    "quality_evaluation_min_sources": 1,
                    "max_researcher_iterations": 4,
                    "max_react_tool_calls": 3,
                    "max_concurrent_research_units": 1,
                    "event_log_enabled": False,
                },
                "metadata": {"run_id": run_id},
            }
            run = RunConfig.compile(config)
            binding = CredentialBinding(
                "m5-scoped-key",
                "run",
                run_id,
                (route,),
                SecretStr(key),
                proxy + "/v1",
                gateway=True,
            )
            factory = ModelFactory(
                run,
                scope="run",
                owner=run_id,
                bindings={
                    role: binding
                    for role in (
                        "supervisor",
                        "researcher",
                        "compression",
                        "final_report",
                        "quality_evaluation",
                        "message_summary",
                    )
                },
            )
            frozen = run.compatibility_projection()
            config = {
                "configurable": {**config["configurable"], **frozen["configurable"]},
                "metadata": frozen["metadata"],
            }
            config["metadata"]["run_id"] = run_id

            async def source(input, context, progress):
                return ToolResult(
                    output={
                        "evidence": [
                            {
                                "evidence_id": "fixture-1",
                                "claim": "模拟市场收入由100增长到120，增长20%。",
                                "supporting_excerpt": "验收虚构数据：2024年收入100，2025年收入120；仅供软件验证，不是真实市场信息。",
                                "document_id": "fixture",
                                "chunk_id": "one",
                                "locator": "table1",
                                "source_url": "https://example.test/fixture",
                                "security_status": "accepted",
                            }
                        ]
                    }
                )

            async def tools_for(assignment):
                return [
                    build_tool(
                        name="web_research",
                        input_schema=Empty,
                        description="读取唯一的合成验收资料（并非实时互联网）；必须调用后才可总结。",
                        call=source,
                        origin=ToolOrigin.SYSTEM,
                        execution_zone=ToolExecutionZone.HOST_CONTROL,
                    )
                ]

            flow = build_research_pipeline(
                run_id=run_id,
                run_config=run,
                model_factory=factory,
                config_provider=lambda: config,
                tools_for=tools_for,
                checkpoint_path=Path(".runs") / run_id / "research.json",
                local_zones=frozenset({ToolExecutionZone.HOST_CONTROL}),
            )
            async with asyncio.timeout(240):
                async for _ in flow.reply_stream(
                    UserMsg(
                        "user",
                        "这是软件验收的合成数据任务。仅调用提供的资料工具，计算2024年至2025年的模拟收入增长率，报告不超过300字，明确标为虚构验收数据并给出资料引用。",
                    )
                ):
                    pass
            result.update(
                status=flow.state.status,
                completion=flow.state.completion_outcome,
                findings=len(flow.state.findings),
                report_chars=len(flow.state.final_report),
            )
        except Exception as exc:  # noqa: BLE001 - sanitized acceptance boundary
            import traceback

            result.update(
                status="failed",
                error_type=type(exc).__name__,
                frames=[
                    {
                        "file": Path(f.filename).name,
                        "line": f.lineno,
                        "function": f.name,
                    }
                    for f in traceback.extract_tb(exc.__traceback__)
                ],
            )
            if hasattr(exc, "outcomes"):
                result["terminal_reason"] = exc.reason
                result["gaps"] = exc.gaps
                result["tool_results"] = [
                    {
                        "name": b.get("name"),
                        "state": b.get("state"),
                        "error_type": b.get("metadata", {}).get("error_type"),
                    }
                    for m in (exc.agent_state or {}).get("context", [])
                    for b in m.get("content", [])
                    if b.get("type") == "tool_result"
                ]
                result["outcomes"] = [
                    {
                        "evidence_count": row["evidence_count"],
                        "handoff_reasons": (row["handoff"] or {}).get(
                            "hard_rejection_reasons"
                        ),
                        "batch_checks": [
                            b.get("deterministic_checks") for b in row["tool_batches"]
                        ],
                        "batch_evaluator_errors": [
                            bool(b.get("evaluator_error")) for b in row["tool_batches"]
                        ],
                    }
                    for row in exc.outcomes
                ]
        finally:
            if factory:
                await factory.aclose()
            if key:
                response = await client.post(
                    proxy + "/key/delete", headers=headers, json={"keys": [key]}
                )
                result["cleanup"]["run_key_revoked"] = response.is_success
    return result


if __name__ == "__main__":
    outcome = asyncio.run(main())
    Path(".runs/m5-live-result.json").write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(outcome, ensure_ascii=False))

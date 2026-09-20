"""Framework-independent research-quality inputs shared by domain tests."""


def config(**overrides):
    return {"configurable": {"search_api": "none", "observability_enabled": False,
                             "quality_evaluation_enabled": False, "max_react_tool_calls": 5, **overrides},
            "metadata": {"run_id": "quality-runtime", "task_id": "researcher-1"}}


def evidence(count):
    return [{"evidence_id": f"ev-{i}", "claim": "Requested fact", "supporting_excerpt": "Supporting official passage",
             "security_status": "accepted", "source_url": f"https://source-{i}.example/page"} for i in range(count)]


def patch_native_evaluator(monkeypatch, invoke):
    """Replace provider IO while preserving native protocol-repair and hard gates."""
    import json
    from types import SimpleNamespace
    from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
    from open_deep_research.report.runtime import ReportMessage

    async def evaluate(schema, system_prompt, payload, config, **kwargs):
        async def structured(role, prompt, output_schema, state, *, messages=None):
            request = [ReportMessage(message.get_text_content() or "", type="system" if message.role == "system" else "human") for message in messages] if messages else [ReportMessage(prompt)]
            response = await invoke(None, request, config, **kwargs)
            content = getattr(response, "content", response)
            values = json.loads(content) if isinstance(content, str) else content
            return output_schema.model_validate(values)

        quality = NativeResearchQuality(SimpleNamespace(structured=structured), lambda: config)
        return await quality.evaluate(schema, system_prompt, payload, config, **kwargs)

    monkeypatch.setattr("open_deep_research.quality.gate._evaluate_json", evaluate)

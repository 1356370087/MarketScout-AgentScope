"""AgentScope model adapter for the existing deterministic quality gates."""

import json
from agentscope.message import SystemMsg, UserMsg

from open_deep_research.configuration import QUALITY_POLICY_VERSION, Configuration
from open_deep_research.evidence import source_scoped_evidence_records
from open_deep_research.quality.gate import (
    QualityProtocolError,
    evaluate_subagent_handoff,
    evaluate_tool_results,
)
from open_deep_research.quality.policy import (
    get_run_quality_rigor_policy,
    scores_meet_runtime_policy,
)


class NativeResearchQuality:
    """Reuse hard admission and five-level policy; inject only the Judge call."""

    def __init__(self, models, config_provider):
        self.models = models
        self.config_provider = config_provider

    async def evaluate(
        self, schema, system_prompt, payload, config, *, span_name, protocol_validator=None,
    ):
        from open_deep_research.agentscope_runtime.efficiency import enabled
        from open_deep_research.agentscope_runtime.runtime_limits import structured_attempt_budget

        async def compute():
            budget = structured_attempt_budget.set([Configuration.from_runnable_config(config).max_structured_output_retries]) if enabled(config) else None
            try:
                result = await self._evaluate(schema, system_prompt, payload, config,
                    span_name=span_name, protocol_validator=protocol_validator)
                return result.model_dump(mode="json")
            finally:
                if budget is not None:
                    structured_attempt_budget.reset(budget)

        cache = getattr(getattr(self.models, "recovery", None), "research_cache", None)
        if enabled(config) and cache is not None:
            semantic = {k: v for k, v in payload.items() if k != "tool_results"}
            if "deterministic_checks" in semantic:
                semantic["deterministic_checks"] = {k: v for k, v in semantic["deterministic_checks"].items()
                                                    if k not in {"evidence_result_count", "error_count", "batch_failures"}}
            value = await cache.compute("assessment", {"version": 1, "kind": span_name,
                "model": Configuration.from_runnable_config(config).quality_evaluation_model,
                "schema": schema.model_json_schema(), "rules": system_prompt, "payload": semantic}, compute)
        else:
            value = await compute()
        return schema.model_validate(value)

    async def _evaluate(
        self,
        schema,
        system_prompt,
        payload,
        config,
        *,
        span_name,
        protocol_validator=None,
    ):
        cfg = Configuration.from_runnable_config(config)
        from open_deep_research.quality.context import CONTEXT_RULES, research_context_xml
        modern = config.get("metadata", {}).get("run_config_schema_version", 18) >= 18
        messages = [SystemMsg("quality_rules", system_prompt + ("\n" + CONTEXT_RULES if modern else "")),
                    UserMsg("research_evidence", research_context_xml(payload.get("coverage_contract"), payload=payload)
                            if modern else "Evaluate this JSON research payload:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=cfg.research_efficiency_mode == "bounded"))]
        errors = []
        for attempt in range(max(1, cfg.max_structured_output_retries)):
            from open_deep_research.agentscope_runtime.recovery import (
                ModelOutputProtocolError,
            )

            try:
                from open_deep_research.agentscope_runtime.runtime_limits import (
                    attributed,
                    structured_attempt_budget,
                )

                remaining = structured_attempt_budget.get()
                if remaining is not None and remaining[0] <= 0:
                    raise QualityProtocolError(errors or ["structured_attempt_budget_exhausted"])
                with attributed(purpose=span_name):
                    result = await self.models.structured("quality_evaluation", "", schema, {}, messages=messages)
            except ModelOutputProtocolError as exc:
                # Gateway already exhausted format repair. Let the domain's
                # fail-open/closed policy decide; never poison the run lease.
                raise QualityProtocolError([str(exc)]) from exc
            # Model output cannot supply runtime diagnostics or hard-check results.
            allowed = schema.model_json_schema()["properties"]
            result = schema.model_validate(
                {k: v for k, v in result.model_dump().items() if k in allowed}
            )
            current = protocol_validator(result) if protocol_validator else []
            if not current:
                result.protocol_repair_count = attempt
                result.protocol_errors = list(dict.fromkeys(errors))
                return result
            errors.extend(current)
            messages.append(UserMsg("protocol_feedback", "Correct these protocol errors and return a replacement JSON object:\n" + json.dumps(current)))
        raise QualityProtocolError(list(dict.fromkeys(errors)))

    async def batch(self, assignment, contract, rows, evidence):
        config = self.config_provider()
        from open_deep_research.agentscope_runtime.efficiency import enabled

        if enabled(config) and not evidence:
            from open_deep_research.quality.gate import deterministic_tool_checks

            cfg = Configuration.from_runnable_config(config)
            checks = deterministic_tool_checks(rows, min_sources=cfg.quality_evaluation_min_sources,
                evidence_registry=[], coverage_contract=contract)
            return {"decision": "retry", "accepted": False, "evaluation_source": "deterministic",
                "reason": "尚无可评估证据；原文、搜索摘要和被拒绝的来源不能计入证据。请用 web_research 批量读取所选资料，或 fetch_url(mode=evidence)。",
                "deterministic_checks": checks, "evaluator_error": None,
                "gaps": [{"requirement_id": rid, "kind": "factual", "reason": "no_eligible_evidence",
                          "checked_evidence_ids": [], "next_query": "读取已选资料并提取证据"} for rid in assignment.requirement_ids]}
        result = await evaluate_tool_results(
            assignment.research_topic,
            rows,
            config,
            evidence_registry=source_scoped_evidence_records(evidence, contract),
            coverage_contract=contract,
            requirement_ids=assignment.requirement_ids,
            evaluator=self.evaluate,
        )
        cfg = Configuration.from_runnable_config(config)
        policy = get_run_quality_rigor_policy(
            cfg.quality_evaluation_rigor,
            policy_version=config.get("metadata", {}).get(
                "quality_policy_version", QUALITY_POLICY_VERSION
            ),
            legacy_min_score=config.get("configurable", {}).get(
                "quality_evaluation_min_score"
            ),
        )
        accepted = (
            result.evaluator_error is None
            and result.deterministic_checks.get("passed", False)
            and scores_meet_runtime_policy(
                (
                    result.relevance,
                    result.source_quality,
                    result.evidence_coverage,
                    result.corroboration,
                ),
                policy,
            )
        )
        return {**result.model_dump(mode="json"), "accepted": accepted}

    async def handoff(self, outcome, contract):
        return await evaluate_subagent_handoff(
            outcome.research_topic,
            outcome.model_dump(mode="json"),
            self.config_provider(),
            coverage_contract=contract,
            requirement_ids=outcome.requirement_ids,
            evaluator=self.evaluate,
        )

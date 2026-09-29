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
        messages = [SystemMsg("quality_rules", system_prompt),
                    UserMsg("research_evidence", "Evaluate this JSON research payload:\n" + json.dumps(payload, ensure_ascii=False))]
        errors = []
        for attempt in range(max(1, cfg.max_structured_output_retries)):
            from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
            try:
                result = await self.models.structured(
                    "quality_evaluation", "", schema, {}, messages=messages
                )
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

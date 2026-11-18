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


def skipped_handoff_assessment():
    """临时交付回执：接纳成果不代表已经执行或通过质量评审。"""
    return {
        "accepted": True,
        "admission_status": "accepted_with_caveats",
        "evaluation_status": "skipped",
        "reason": "任务交接质量门禁暂时停用，研究成果已交付，未执行交接质量评审。",
        "caveats": ["交接质量未评估；保留工具反馈、原始停止原因和证据中的不确定性。"],
    }


def quality_enforced(config):
    """v23 restores admission; historical runs keep their delivery semantics."""
    return config.get("metadata", {}).get("run_config_schema_version", 22) >= 23


async def assess_handoff(quality, outcome, contract, config):
    """One admission policy for direct, adaptive and durable team delivery.

    Keep raw evidence on the handoff. Consumers use its admission result to
    select report inputs; a rejected summary never destroys source artifacts.
    """
    if not quality_enforced(config):
        return skipped_handoff_assessment()
    from open_deep_research.quality.gate import deterministic_handoff_checks
    cfg = Configuration.from_runnable_config(config)
    if not cfg.quality_evaluation_enabled:
        checks = deterministic_handoff_checks(outcome.model_dump(mode="json"),
            min_sources=cfg.quality_evaluation_min_sources, coverage_contract=contract)
        return {"accepted": checks["passed"],
            "admission_status": "accepted_with_caveats" if checks["passed"] else "rejected",
            "evaluation_status": "skipped", "deterministic_checks": checks,
            "reason": "用户关闭语义质量评估；确定性准入仍生效。", "caveats": ["semantic_quality_disabled"]}
    if quality is None:
        raise RuntimeError("native_handoff_quality_required")
    if outcome.termination in {"report_budget_reserved", "report_time_reserved", "task_time_reserved"}:
        return {"accepted": False, "admission_status": "rejected", "evaluation_status": "unavailable",
                "reason": outcome.termination, "missing_information": list(outcome.requirement_ids)}
    result = await quality.handoff(outcome, contract)
    value = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    value["evaluation_status"] = "error" if value.get("evaluator_error") else "evaluated"
    value["admission_status"] = value.get("admission_status") or ("accepted" if value.get("accepted") else "rejected")
    if value.get("evaluator_error") and not cfg.quality_evaluation_fail_open:
        value.update(accepted=False, admission_status="rejected")
    return value


def admitted_findings(findings, config):
    """Select only admitted handoffs for evidence-based report composition."""
    if not quality_enforced(config):
        return findings
    return [row for row in findings if row.get("assessment", {}).get("handoff", {}).get("accepted") is True]


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

        cache = getattr(getattr(self.models, "recovery", None), "research_cache", None)
        if cache is not None and config.get("metadata", {}).get("run_config_schema_version", 0) >= 30:
            from .execution_receipts import submitted_read_receipts
            submitted = payload.get("cumulative_evidence") or payload.get("evidence_registry") or []
            reads = submitted_read_receipts(submitted, await cache.progress()) if submitted else []
            if reads:
                payload = {**payload, "source_read_receipts": reads}
                limit = Configuration.from_runnable_config(config).quality_evaluation_max_input_chars
                if len(json.dumps(payload, ensure_ascii=False, default=str)) > limit:
                    from open_deep_research.quality.gate import _bounded_quality_payload
                    payload = _bounded_quality_payload(payload, max_chars=limit)
                system_prompt += ("\nThe runtime supplies source_read_receipts from the SQL fetch ledger for these submitted "
                    "source versions. A successful read_at proves when that source was checked. It is separate from "
                    "published_at. Read times can satisfy verification-date requirements; price and feature claims "
                    "still require the cited original excerpts.")

        async def compute():
            budget = structured_attempt_budget.set([Configuration.from_runnable_config(config).max_structured_output_retries]) if enabled(config) else None
            try:
                result = await self._evaluate(schema, system_prompt, payload, config,
                    span_name=span_name, protocol_validator=protocol_validator)
                return result.model_dump(mode="json")
            finally:
                if budget is not None:
                    structured_attempt_budget.reset(budget)

        if enabled(config) and cache is not None:
            semantic = {k: v for k, v in payload.items() if k != "tool_results"}
            if "deterministic_checks" in semantic:
                semantic["deterministic_checks"] = {k: v for k, v in semantic["deterministic_checks"].items()
                                                    if k not in {"evidence_result_count", "error_count", "batch_failures"}}
            value = await cache.compute("assessment", {"version": 1, "kind": span_name,
                **({"policy_version": config.get("metadata", {}).get("quality_policy_version"),
                    "run_schema": config.get("metadata", {}).get("run_config_schema_version")}
                   if quality_enforced(config) else {}),
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
        from open_deep_research.quality.context import CONTEXT_RULES, SCOPE_CONTEXT_RULES, research_context_xml
        if payload.get("task_scope"):
            from open_deep_research.quality.task_scope import SCOPE_RULES
            system_prompt += "\n" + SCOPE_RULES
        modern = config.get("metadata", {}).get("run_config_schema_version", 18) >= 18
        messages = [SystemMsg("quality_rules", system_prompt + ("\n" + CONTEXT_RULES if modern else "")
                             + ("\n" + SCOPE_CONTEXT_RULES if payload.get("coverage_contract", {}).get("scope_context") else "")),
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
        if config.get("metadata", {}).get("run_config_schema_version", 18) >= 22:
            from open_deep_research.evidence import required_source_count
            from open_deep_research.quality.gate import deterministic_tool_checks, count_traceable_sources
            cfg = Configuration.from_runnable_config(config)
            scoped_evidence = source_scoped_evidence_records(evidence, contract)
            checks = deterministic_tool_checks(rows, min_sources=cfg.quality_evaluation_min_sources,
                evidence_registry=scoped_evidence, coverage_contract=contract)
            checks["source_count"] = count_traceable_sources(scoped_evidence)
            required = required_source_count(cfg.quality_evaluation_min_sources, contract,
                                             leaf=assignment.task_id != "main-research")
            if checks["source_count"] < required:
                checks["required_source_count"] = required
                checks["passed"] = False
                checks["failures"] = list(dict.fromkeys([*checks["failures"], "insufficient_traceable_sources"]))
                reason = f"当前只有 {checks['source_count']} 个可追溯来源，本任务至少需要 {required} 个。继续在已授权范围内补充相关来源；数量达标后再评估事实覆盖。"
                return {"decision": "retry", "accepted": False, "evaluation_source": "deterministic",
                    "reason": reason, "deterministic_checks": checks, "evaluator_error": None,
                    "missing_information": [reason], "suggested_queries": ["针对当前问题补充不同的已授权来源"],
                    "gaps": [], "requirement_checks": []}
        from open_deep_research.quality.task_scope import QualityScopeViolation
        from open_deep_research.quality.gate import QualityInputBudgetExceeded
        try:
            result = await evaluate_tool_results(
                assignment.research_topic, rows, config,
                evidence_registry=source_scoped_evidence_records(evidence, contract),
                coverage_contract=contract, requirement_ids=assignment.requirement_ids,
                evaluator=self.evaluate,
            )
        except (QualityScopeViolation, QualityInputBudgetExceeded) as exc:
            if config.get("metadata", {}).get("run_config_schema_version", 18) < 21:
                raise
            code = "quality_scope_violation" if isinstance(exc, QualityScopeViolation) else "quality_input_incomplete"
            return {"decision": "retry", "accepted": False, "evaluation_status": "error",
                    "evaluator_error": code, "protocol_errors": getattr(exc, "errors", [str(exc)]),
                    "reason": "评估范围或输入无法确认，保留已有证据并停止本次纠偏；不追加外部研究。",
                    "gaps": [], "requirement_checks": [], "missing_information": [],
                    "suggested_queries": [], "unresolved_conflicts": []}
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
        result = await evaluate_subagent_handoff(
            outcome.research_topic,
            outcome.model_dump(mode="json"),
            self.config_provider(),
            coverage_contract=contract,
            requirement_ids=outcome.requirement_ids,
            evaluator=self.evaluate,
        )
        config = self.config_provider()
        if (outcome.task_id == "main-research"
                and config.get("metadata", {}).get("run_config_schema_version", 0) >= 25):
            return await self._repair_main_handoff(outcome, contract, result, config)
        supported = {row.requirement_id for row in result.requirement_coverage if row.status == "supported"}
        # If every owned fact is already supported, a misleading compression
        # should be repaired from the same evidence, not sent back to Web search.
        if (config.get("metadata", {}).get("quality_policy_version", QUALITY_POLICY_VERSION) == "quality-gate-v6"
                and not result.accepted and result.unsupported_claims and not result.missing_information
                and set(outcome.requirement_ids) <= supported
                and not any(gap.kind != "advisory" for gap in result.gaps)
                and result.deterministic_checks.get("passed") and not result.evaluator_error
                and set(result.hard_rejection_reasons) <= {"unsupported_claims"}):
            from .research_agents import ResearchAssignment, _evidence_handoff_notes
            assignment = ResearchAssignment(task_id=outcome.task_id, research_topic=outcome.research_topic,
                                            requirement_ids=outcome.requirement_ids)
            outcome.assessment["summary_repair"] = {"attempts": 1, "reason": "supported_evidence_summary_mismatch",
                "original_summary": outcome.compressed_research, "initial_assessment": result.model_dump(mode="json")}
            options = {"reference_only": True} if config.get("metadata", {}).get("run_config_schema_version", 18) >= 20 else {}
            outcome.compressed_research = await _evidence_handoff_notes(self.models, assignment, outcome.evidence_registry,
                                                                       Configuration.from_runnable_config(config), **options)
            result = await evaluate_subagent_handoff(outcome.research_topic, outcome.model_dump(mode="json"), config,
                coverage_contract=contract, requirement_ids=outcome.requirement_ids, evaluator=self.evaluate)
            outcome.assessment["summary_repair"]["accepted"] = result.accepted
        return result

    async def _repair_main_handoff(self, outcome, contract, initial, config):
        """Rebuild one rejected summary from its own checked excerpts, then re-admit."""
        from .efficiency import research_budget_reason

        if (initial.accepted or initial.evaluator_error or not initial.unsupported_claims
                or not initial.deterministic_checks.get("passed")
                or outcome.termination not in {"research_complete", "iteration_limit"}
                or "summary_repair" in outcome.assessment
                or any(gap.kind not in {"factual", "advisory"} for gap in initial.gaps)
                or any(code.split(":", 1)[0] not in {"unsupported_claims", "required_information_missing",
                       "unresolved_requirement_gap", "required_coverage_missing"} for code in initial.hard_rejection_reasons)):
            return initial
        records = {row["evidence_id"]: row for row in source_scoped_evidence_records(outcome.evidence_registry, contract)}
        supported, gaps = [], []
        for requirement in initial.requirement_coverage:
            if requirement.status != "supported":
                gaps.append(requirement.requirement_id)
                continue
            for eid in requirement.evidence_ids:
                excerpt = records.get(eid, {}).get("supporting_excerpt")
                if excerpt:
                    supported.append(f"[{eid}] {excerpt}")
        if not supported:
            return initial
        from .efficiency import fingerprint
        from open_deep_research.quality.gate import HandoffAssessment
        cache = getattr(getattr(self.models, "recovery", None), "research_cache", None)
        identity = fingerprint([outcome.task_id, contract, outcome.compressed_research, sorted(records),
                                initial.model_dump(mode="json")])
        saved = (await cache.progress()).get("handoff_repairs", {}).get(identity) if cache else None
        if saved and saved.get("final_assessment") is not None:
            outcome.assessment["summary_repair"] = saved["receipt"]
            outcome.compressed_research = saved["receipt"]["replacement_summary"]
            return HandoffAssessment.model_validate(saved["final_assessment"])
        if await research_budget_reason(self.models, config):
            return initial
        replacement = "\n".join(dict.fromkeys(supported))
        if gaps:
            replacement += "\n尚缺直接证据的需求：" + "、".join(gaps)
        receipt = {"attempts": 1, "reason": "unsupported_claims", "original_summary": outcome.compressed_research,
                   "initial_assessment": initial.model_dump(mode="json"), "replacement_summary": replacement}
        if cache:
            await cache.progress({"handoff_repairs": {identity: {"receipt": receipt}}})
        outcome.assessment["summary_repair"] = receipt
        outcome.compressed_research = replacement
        final = await evaluate_subagent_handoff(outcome.research_topic, outcome.model_dump(mode="json"), config,
            coverage_contract=contract, requirement_ids=outcome.requirement_ids, evaluator=self.evaluate)
        receipt["accepted"] = final.accepted
        if cache:
            await cache.progress({"handoff_repairs": {identity: {"receipt": receipt, "final_assessment": final.model_dump(mode="json")}}})
        return final

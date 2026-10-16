"""A named prerequisite without its enabling conditions is not a complete procedure."""

import pytest

from open_deep_research.agentscope_runtime.research_agents import ResearchHandoff
from open_deep_research.agentscope_runtime.research_quality import NativeResearchQuality
from open_deep_research.quality.context import context_payload
from open_deep_research.quality.contract import build_research_coverage_contract
from open_deep_research.quality.procedures import procedural_requirement_ids


@pytest.mark.parametrize("text,expected", [
    ("说明如何生成增量备份", True), ("恢复前如何合并", True),
    ("给出官方说明的重建方法", True), ("How to enable encrypted backups", True),
    ("异常如何传播", False), ("创建顺序为何重要", False), ("比较两个系统的职责", False),
])
def test_operational_checks_do_not_expand_all_explanatory_questions(text, expected):
    assert bool(procedural_requirement_ids([{"requirement_id": "R1", "text": text}])) is expected


@pytest.mark.parametrize("version,status,accepted", [
    ("quality-gate-v6", "missing", False), ("quality-gate-v6", "supported", True),
    ("quality-gate-v5", "missing", True),
])
@pytest.mark.asyncio
async def test_native_gate_requires_supported_activation_before_admitting_procedure(version, status, accepted):
    question = "Explain how to generate incremental backups."
    contract = build_research_coverage_contract([{"role": "user", "content": question}]).model_dump(mode="json")
    rid = contract["requirements"][0]["requirement_id"]
    evidence = {"evidence_id": "ev1", "claim": "Incremental backups require change summaries.",
                "supporting_excerpt": "Incremental backups require change summaries." + (" Enable track_changes, whose default is off." if status == "supported" else ""),
                "source_url": "https://example.test/backup", "source_title": "Backup guide", "security_status": "accepted"}

    class Judge:
        async def structured(self, role, prompt, schema, state, *, messages):
            payload = context_payload(messages[1].get_text_content())
            extras = {}
            if version == "quality-gate-v6":
                assert payload["procedural_requirement_ids"] == [rid]
                assert "procedure_checks" in schema.model_json_schema()["required"]
                supported = {"status": "supported", "explanation": "Concrete documented steps.", "evidence_ids": ["ev1"]}
                extras["procedure_checks"] = [{"requirement_id": rid, "required_inputs": supported,
                    "activation_and_defaults": {"status": status, "explanation": "How to enable change-summary generation and its default.", "evidence_ids": ["ev1"]},
                    "steps_and_result": supported,
                    "activation_controls": [{"subsystem": "change summaries", "enabling_control": "track_changes" if status == "supported" else None,
                        "control_evidence_ids": ["ev1"] if status == "supported" else [], "default_value": "off" if status == "supported" else None,
                        "default_evidence_ids": ["ev1"] if status == "supported" else []}],
                    "limitations": {"status": "not_applicable", "explanation": "No additional operational restrictions in this minimal fixture.", "evidence_ids": []}}]
            else:
                assert "procedure_checks" not in schema.model_json_schema()["properties"]
            return schema(accepted=True, relevance=5, source_quality=5, evidence_coverage=5, groundedness=5,
                reason="The high-level steps are present.", requirement_coverage=[{"requirement_id": rid,
                    "status": "supported", "evidence_ids": ["ev1"], "explanation": "Steps are present."}], **extras)

    config = {"configurable": {"quality_evaluation_enabled": True, "quality_evaluation_fail_open": False,
                              "quality_evaluation_min_sources": 1},
              "metadata": {"quality_policy_version": version, "run_config_schema_version": 19}}
    outcome = ResearchHandoff(task_id="t1", research_topic=question, requirement_ids=[rid], evidence_registry=[evidence],
                              compressed_research=("The official guide describes generating incremental backups from a previous backup "
                                  "and available change summaries. The summaries determine which blocks need copying. "
                                  "The fixture checks whether merely naming those summaries proves all setup requirements. [ev1]"))
    result = await NativeResearchQuality(Judge(), lambda: config).handoff(outcome, contract)
    assert result.evaluator_error is None, result.model_dump()
    assert result.accepted is accepted, result.model_dump()
    if not accepted:
        assert result.requirement_coverage[0].status == "partial"
        assert any("activation_and_defaults" in gap.reason and gap.kind == "factual" for gap in result.gaps)
        assert result.gaps[0].next_query


def test_missing_and_fabricated_procedure_evidence_are_protocol_errors():
    from open_deep_research.quality.procedures import ProcedureCoverage, procedure_protocol_errors
    facet = {"status": "supported", "explanation": "A purported prerequisite.", "evidence_ids": ["invented"]}
    row = ProcedureCoverage(requirement_id="R1", required_inputs=facet, activation_and_defaults=facet,
                            steps_and_result=facet, limitations=facet)
    assert procedure_protocol_errors([], ["R1"], ["ev1"])
    assert all("procedure_evidence_invalid" in error for error in procedure_protocol_errors([row], ["R1"], ["ev1"]))


def test_named_subsystem_cannot_substitute_for_its_missing_activation_or_default():
    from open_deep_research.quality.gate import HandoffAssessment
    from open_deep_research.quality.procedures import ProcedureCoverage, apply_procedure_gaps
    supported = {"status": "supported", "explanation": "The procedure depends on change summaries.", "evidence_ids": ["ev1"]}
    row = ProcedureCoverage(requirement_id="R1", required_inputs=supported, activation_and_defaults=supported,
        steps_and_result=supported, limitations=supported, activation_controls=[{"subsystem": "change summaries",
            "enabling_control": None, "control_evidence_ids": [], "default_value": None, "default_evidence_ids": []}])
    result = HandoffAssessment(accepted=True, relevance=5, source_quality=5, evidence_coverage=5, groundedness=5,
        reason="An optimistic model decision.", procedure_checks=[row])
    apply_procedure_gaps(result)
    assert not result.accepted
    assert result.gaps[0].kind == "factual"
    assert "change summaries" in result.gaps[0].next_query


def test_default_on_is_not_grounded_by_substring_in_configuration():
    from open_deep_research.quality.gate import HandoffAssessment
    from open_deep_research.quality.procedures import ProcedureCoverage, procedure_protocol_errors, apply_procedure_gaps
    facet = {"status": "supported", "explanation": "Configuration is mentioned.", "evidence_ids": ["ev1"]}
    row = ProcedureCoverage(requirement_id="R1", required_inputs=facet, activation_and_defaults=facet,
        steps_and_result=facet, limitations=facet, activation_controls=[{"subsystem": "change tracking",
            "enabling_control": "track_changes", "control_evidence_ids": ["ev1"],
            "default_value": "on", "default_evidence_ids": ["ev1"]}])
    records = [{"evidence_id": "ev1", "supporting_excerpt": "Configuration parameter track_changes controls change tracking."}]
    # The schema and IDs are valid. Missing source support is a research gap,
    # not a malformed Judge reply that erases the useful remediation detail.
    assert procedure_protocol_errors([row], ["R1"], ["ev1"]) == []
    result = HandoffAssessment(accepted=True, relevance=5, source_quality=5, evidence_coverage=5, groundedness=5,
        reason="Optimistic proposal.", procedure_checks=[row])
    apply_procedure_gaps(result, records)
    assert not result.accepted and result.evaluator_error is None
    assert "documented default" in result.reason
    assert "change tracking" in result.gaps[0].next_query


def test_parameter_annotation_does_not_discard_its_grounded_identifier():
    from open_deep_research.quality.gate import HandoffAssessment
    from open_deep_research.quality.procedures import ProcedureCoverage, apply_procedure_gaps
    facet = {"status": "supported", "explanation": "The parameter enables the required feature.", "evidence_ids": ["ev1"]}
    row = ProcedureCoverage(requirement_id="R1", required_inputs=facet, activation_and_defaults=facet,
        steps_and_result=facet, limitations=facet, activation_controls=[{"subsystem": "change tracking",
            "enabling_control": "track_changes (configured in the server configuration file)", "control_evidence_ids": ["ev1"],
            "default_value": "off", "default_evidence_ids": ["ev1"]}])
    result = HandoffAssessment(accepted=True, relevance=5, source_quality=5, evidence_coverage=5, groundedness=5,
        reason="Supported.", procedure_checks=[row])
    apply_procedure_gaps(result, [{"evidence_id": "ev1", "supporting_excerpt": "`track_changes`\n: Enables change tracking. The default is off."}])
    assert result.accepted and not result.gaps

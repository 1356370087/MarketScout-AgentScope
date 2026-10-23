"""Replay the real false pass and verify answer/example obligations generically."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.report import reviewer
from open_deep_research.report.models import ReportDraft, ReportReview
from open_deep_research.report.writing import fit_writing_messages


def recorded(name="sidecar_report_false_pass.json"):
    value = json.loads((Path(__file__).parents[1] / "fixtures/research_quality" / name).read_text(encoding="utf-8"))
    if any("reuse" in row for row in value["evidence"]):
        reusable = {row["evidence_id"]: row for row in recorded()["evidence"]}
        value["evidence"] = [{**reusable[row["reuse"]], **{k:v for k,v in row.items() if k != "reuse"}}
            if "reuse" in row else row for row in value["evidence"]]
    # The source fixture stores identical claim/excerpt text once.
    for row in value["evidence"]:
        row.setdefault("claim", row.get("supporting_excerpt", ""))
    return value


def config(version=20):
    return {"configurable": {"report_review_fail_open": False, "max_structured_output_retries": 1},
            "metadata": {"run_config_schema_version": version, "quality_policy_version": "quality-gate-v6"}}


@pytest.mark.parametrize("version", [19, 20])
def test_report_goal_preserves_user_requirements_without_generated_brief_prerequisites(version):
    from open_deep_research.report.writing import writing_messages
    from open_deep_research.quality.context import context_payload
    user = "说明原生 Sidecar 的适用前提。"
    generated = user + " 必须证明 KubeletInUserNamespace 前提，即使用户和证据没有提到。"
    payload = {"research_brief": generated, "topic": generated, "_run_schema_version": version,
        "coverage_contract": {"requirements": [{"requirement_id": "r1", "text": user, "kind": "factual"}]}}
    messages = writing_messages("Write or review the report", payload, [])
    sent = context_payload(next(m.content for m in messages if m.name == "report_context"))
    assert sent["research_brief"] == (user if version >= 20 else generated)
    assert sent["topic"] == sent["research_brief"]
    assert payload["research_brief"] == generated  # Preserve the diagnostic state.


def state(value):
    return {"research_brief": value["research_brief"], "coverage_contract": value["coverage_contract"],
            "coverage_ledger": value["coverage_ledger"], "evidence_registry": value["evidence"]}


def response_for_stage(raw, kwargs, messages):
    if kwargs["span_name"] == "lead.report_guarantee_review":
        return input_only_guarantee_review(messages)
    value = deepcopy(raw)
    if kwargs["span_name"] == "lead.report_review":
        value.pop("example_checks", None)
        records = {r["evidence_id"]:r for r in json.loads(next(m.content for m in messages if m.name == "report_evidence"))["records"]}
        draft_passages = json.loads(next(m.content for m in messages if m.name == "report_context"))["draft_passages"]
        for row in value["coverage"]:
            answer = row.pop("answer_excerpt", "")
            row["answer_passage_index"] = next((int(i) for i, text in draft_passages.items() if answer and answer in text), 9999 if answer else None)
            for support in row.get("supporting_evidence", []):
                passages = records[support["evidence_id"]]["source_passages"]
                quote = support.pop("quote", "")
                index = next((i for i, text in passages.items() if quote in text), "9999")
                support["source_passage_id"] = support.pop("evidence_id") + ":" + index
    return kwargs["schema"].model_validate(value)


def original_fields(row):
    result = dict(row)
    if "source_passages" in result:
        passages = result.pop("source_passages")
        result["supporting_excerpt"] = "".join(passages[key] for key in sorted(passages, key=int))
    result.setdefault("claim", result.get("supporting_excerpt"))
    return result


def input_only_guarantee_review(messages):
    from open_deep_research.report.guarantees import GuaranteeAudit
    payload = json.loads(next(m.content for m in messages if m.name == "report_context"))
    records = json.loads(next(m.content for m in messages if m.name == "report_evidence"))["records"]
    return GuaranteeAudit(checks=[{"claim_id": row["claim_id"], "supported": True,
        "evidence_ids": [records[0]["evidence_id"]], "explanation": "Input selection fixture only."}
        for row in payload["claims"]])


def input_only_example_review(messages):
    payload = json.loads(next(m.content for m in messages if m.name == "report_context"))
    return ReportReview(example_checks=[{"example_id": row["example_id"],
        "expected_behavior": "Input selection fixture", "observed_behavior": "Input selection fixture",
        "expected_lifecycle": "not_applicable", "observed_lifecycle": "not_applicable",
        "status": "satisfies", "explanation": "This test inspects input selection only.",
        "execution_checks": [{"unit_id": unit["unit_id"], "expected_lifecycle": "not_applicable",
            "observed_lifecycle": "not_applicable", "stopping_condition": "Input fixture"} for unit in row["execution_units"]]
        } for row in payload["examples"]])


def input_only_boundary_review():
    from open_deep_research.report.boundaries import BoundaryAudit, CASES
    return BoundaryAudit(checks=[{"case_id": case["case_id"], "applicable": False,
        "explanation": "This test inspects input selection only."} for case in CASES])


@pytest.mark.asyncio
async def test_recorded_false_pass_requires_actual_answer_and_example_verification(monkeypatch):
    value = recorded()
    async def original_verdict(*args, **kwargs):
        return deepcopy(value["review_responses"][-1]["response"])
    monkeypatch.setattr(reviewer, "_invoke_reviewer", original_verdict)
    legacy = await reviewer.review_report(value["draft"], state(value), config(19))
    assert legacy.decision == "pass"  # Reproduces the production false pass.
    protected = await reviewer.review_report(value["draft"], state(value), config())
    assert protected.decision == "fail"
    assert {"report_answer_witness_invalid", "report_example_checks_missing_or_invalid"} <= set(protected.hard_failures)


@pytest.mark.asyncio
async def test_actual_source_formula_and_official_job_survive_bounded_report_inputs(monkeypatch):
    from open_deep_research.report.assembly import ReportContext
    from open_deep_research.report.profiles import get_profile
    value = recorded()
    cfg = Configuration.from_runnable_config(config())
    expected = {"ev_486fae438ab4574412e7", "ev_32705ac14739c59e9627"}
    projected = []

    async def capture(role, messages, cfg, **kwargs):
        if kwargs.get("span_name") == "lead.report_guarantee_review":
            return input_only_guarantee_review(messages)
        if kwargs.get("span_name") == "lead.report_example_review":
            return input_only_example_review(messages)
        if kwargs.get("span_name") == "lead.report_boundary_review":
            return input_only_boundary_review()
        # The deployed immutable model catalog declared a 131072-token window.
        fitted, _ = fit_writing_messages(messages, "fixture", cfg, output_tokens=8192, context_window=131072)
        records = [original_fields(r) for r in json.loads(next(m.content for m in fitted if m.name == "report_evidence"))["records"]]
        projected.append({row["evidence_id"] for row in records})
        assert expected <= projected[-1]
        formula = next(row for row in records if row["evidence_id"] == "ev_486fae438ab4574412e7")
        assert "pod overhead" in formula["supporting_excerpt"]
        assert "app and sidecar containers" in formula["supporting_excerpt"]
        return ReportReview()

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=capture))
    payload = reviewer.build_reviewer_payload(value["draft"], state(value), config())
    await reviewer._invoke_reviewer(payload, config(), cfg, attempt=1)
    ctx = ReportContext.from_state(state(value), config(), get_profile(cfg.report_type))
    await capture("final_report", ctx.stage_messages(ctx.prompt_template, {"research_brief": value["research_brief"]}), cfg)
    assert len(projected) == 2
    from open_deep_research.report.evidence_synthesis import _select_evidence_for_budget, _project_evidence
    limited = _select_evidence_for_budget(_project_evidence(value["evidence"]), token_budget=90000,
        requirements=value["coverage_contract"]["requirements"], requirement_to_evidence=ctx.requirement_to_evidence)
    assert expected <= {row["evidence_id"] for row in limited}


@pytest.mark.asyncio
async def test_review_retrieves_support_for_written_answers_under_real_input_budget(monkeypatch):
    from open_deep_research.report.writing import answer_evidence_order, project_evidence
    path = Path(__file__).parents[1] / "fixtures/research_quality/sidecar_review_selection_replay.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    reusable = {row["evidence_id"]: row for row in recorded()["evidence"]}
    value["evidence"] = [{**reusable[row["reuse"]], **{k:v for k,v in row.items() if k != "reuse"}}
        if "reuse" in row else {"claim": row.get("supporting_excerpt", ""), **row} for row in value["evidence"]]
    data = state(value)
    payload = reviewer.build_reviewer_payload(value["draft"], data, config())
    selections = []
    async def capture(role, messages, cfg, **kwargs):
        if kwargs.get("span_name") == "lead.report_guarantee_review":
            return input_only_guarantee_review(messages)
        if kwargs.get("span_name") == "lead.report_example_review":
            return input_only_example_review(messages)
        if kwargs.get("span_name") == "lead.report_boundary_review":
            return input_only_boundary_review()
        fitted, _ = fit_writing_messages(messages, "fixture", cfg, output_tokens=8192, context_window=131072)
        selections.append({row["evidence_id"] for row in json.loads(next(m.content for m in fitted if m.name == "report_evidence"))["records"]})
        return ReportReview()
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=capture))
    cfg = Configuration.from_runnable_config(config())
    await reviewer._invoke_reviewer(payload, config(), cfg, attempt=1)
    # The recorded monolithic review omitted this source. The current separate
    # code audit also frees input space, so replay the actual historical selection.
    assert "ev_486fae438ab4574412e7" not in value["historical_review_evidence_ids"]
    assert "ev_486fae438ab4574412e7" in selections[0]


@pytest.mark.asyncio
async def test_verbatim_duplicate_fields_do_not_displace_scoped_formula_and_version(monkeypatch):
    from open_deep_research.report.assembly import ReportContext
    from open_deep_research.report.profiles import get_profile
    value = recorded("sidecar_input_compaction_replay.json")
    data = state(value)
    payload = reviewer.build_reviewer_payload(value["draft"], data, config())
    cfg = Configuration.from_runnable_config(config())
    selections = []
    async def capture(role, messages, cfg, **kwargs):
        if kwargs.get("span_name") == "lead.report_guarantee_review":
            return input_only_guarantee_review(messages)
        if kwargs.get("span_name") == "lead.report_example_review":
            return input_only_example_review(messages)
        if kwargs.get("span_name") == "lead.report_boundary_review":
            return input_only_boundary_review()
        before = {r["evidence_id"]:original_fields(r) for r in json.loads(next(m.content for m in messages if m.name == "report_evidence"))["records"]}
        fitted, _ = fit_writing_messages(messages, "fixture", cfg, output_tokens=8192, context_window=131072)
        records = [original_fields(r) for r in json.loads(next(m.content for m in fitted if m.name == "report_evidence"))["records"]]
        selections.append({r["evidence_id"] for r in records})
        assert "ev_486fae438ab4574412e7" in selections[-1]
        assert any("Stable since Kubernetes v1.33" in r.get("supporting_excerpt", "") for r in records)
        for r in records:
            assert {**r, "claim": r.get("claim", r.get("supporting_excerpt"))} == before[r["evidence_id"]]
        return ReportReview()
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=capture))
    await reviewer._invoke_reviewer(payload, config(), cfg, attempt=1)
    ctx = ReportContext.from_state(data, config(), get_profile(cfg.report_type))
    await capture("final_report", ctx.stage_messages(ctx.prompt_template, {"research_brief": value["research_brief"]}), cfg)
    assert len(selections) == 2
    assert "ev_486fae438ab4574412e7" not in value["historical_review_evidence_ids"]


def test_revision_keeps_sources_for_correct_answers_alongside_blocking_issue_sources():
    value = recorded("sidecar_revision_selection_replay.json")
    stable_ids = {r["evidence_id"] for r in value["evidence"]
                  if "Stable since Kubernetes v1.33" in r.get("supporting_excerpt", "")}
    assert stable_ids and not stable_ids.intersection(value["historical_revision_evidence_ids"])
    draft = ReportDraft(markdown=value["draft"])
    review = ReportReview.model_validate(value["review"])
    messages = reviewer._revision_prompt(draft, review, state(value), config())
    fitted, _ = fit_writing_messages(messages, "fixture", Configuration.from_runnable_config(config()),
                                    output_tokens=value["output_tokens"], context_window=value["context_window"])
    records = json.loads(next(m.content for m in fitted if m.name == "report_evidence"))["records"]
    selected = {r["evidence_id"]: r for r in records}
    assert stable_ids.intersection(selected)
    originals = {r["evidence_id"]: r for r in value["evidence"]}
    for identity in stable_ids.intersection(selected):
        assert selected[identity]["supporting_excerpt"] == originals[identity]["supporting_excerpt"]
    for issue in review.issues:
        if issue.blocking:
            assert set(issue.evidence_ids) <= set(selected)


@pytest.mark.parametrize("version", [19, 20])
def test_lossless_evidence_compaction_keeps_distinct_claims_and_legacy_input(version):
    from open_deep_research.report.writing import writing_messages
    rows = [{"evidence_id": "same", "claim": "Complete passage.", "supporting_excerpt": "Complete passage."},
            {"evidence_id": "distinct", "claim": "A qualified inference.", "supporting_excerpt": "Its original source passage."}]
    messages = writing_messages("Inspect the evidence.", {"_run_schema_version": version}, rows)
    cfg = Configuration.from_runnable_config(config(version))
    first, _ = fit_writing_messages(messages, "fixture", cfg, output_tokens=1000, context_window=32768)
    second, _ = fit_writing_messages(first, "fixture", cfg, output_tokens=1000, context_window=32768)
    content = next(m.content for m in first if m.name == "report_evidence")
    assert content == next(m.content for m in second if m.name == "report_evidence")
    fitted = json.loads(content)["records"]
    assert ("claim" in fitted[0]) == (version == 19)
    assert fitted[1] == rows[1] and fitted[0]["supporting_excerpt"] == rows[0]["supporting_excerpt"]
    assert json.loads(next(m.content for m in messages if m.name == "report_evidence"))["records"] == rows


def test_numbered_source_passages_preserve_every_character_and_fit_quote_limits():
    from open_deep_research.report.verification import evidence_passages
    text = "Heading\n\n" + "原文" * 2100 + "\n\n```sh\necho done\n```\n"
    passages = evidence_passages(text)
    assert "".join(passages) == text
    assert all(0 < len(part) <= 1800 for part in passages)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [None, "outside", "unsubmitted", "mismatched_record", "mismatched_index"])
async def test_bound_source_reference_cannot_invent_or_borrow_a_witness(monkeypatch, problem):
    from open_deep_research.report.verification import source_passage_id
    draft, data, raw = simple_review()
    original = data["evidence_registry"][0]["supporting_excerpt"]
    data["evidence_registry"][0]["supporting_excerpt"] = "Source heading\n\n" + original
    reference = {"source_passage_id": source_passage_id("ev-batch", 99 if problem == "outside" else 1)}
    if problem == "mismatched_record":
        reference["evidence_id"] = "another-record"
    if problem == "mismatched_index":
        reference["passage_index"] = 0
    raw["coverage"][0]["supporting_evidence"] = [reference]

    async def invoke(*args, **kwargs):
        result = ReportReview.model_validate(deepcopy(raw))
        result._submitted_evidence_ids = set() if problem == "unsubmitted" else {"ev-batch"}
        return result

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert ("report_answer_witness_invalid" in result.hard_failures) == bool(problem)
    if not problem:
        support = result.coverage[0].supporting_evidence[0]
        assert (support.evidence_id, support.passage_index, support.quote) == ("ev-batch", 1, original)
        assert result.decision == "revise"  # A valid witness cannot erase the infinite Job.


@pytest.mark.asyncio
async def test_model_witness_schema_selects_one_bound_source_reference(monkeypatch):
    draft, data, raw = simple_review()

    async def invoke(role, messages, cfg, **kwargs):
        if kwargs["span_name"] == "lead.report_review":
            schema = kwargs["schema"].model_json_schema()["$defs"]["ReportSourcePassage"]
            assert schema["required"] == ["source_passage_id"]
            assert set(schema["properties"]) == {"source_passage_id"}
            records = json.loads(next(m.content for m in messages if m.name == "report_evidence"))["records"]
            identities = {f"{record['evidence_id']}:{index}" for record in records for index in record["source_passages"]}
            assert set(schema["properties"]["source_passage_id"]["enum"]) == identities
        return response_for_stage(raw, kwargs, messages)

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert "report_answer_witness_invalid" not in result.hard_failures
    assert result.coverage[0].supporting_evidence[0].evidence_id == "ev-batch"
    assert result.decision == "revise"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [None, "outside", "unsubmitted"])
async def test_source_reference_resolves_actual_passage_without_model_transcription(monkeypatch, problem):
    draft, data, raw = simple_review()
    passage = data["evidence_registry"][0]["supporting_excerpt"]
    data["evidence_registry"][0]["supporting_excerpt"] = "Source heading\n\n" + passage
    raw["coverage"][0]["supporting_evidence"] = [{"evidence_id": "ev-batch", "quote": "", "passage_index": 9 if problem == "outside" else 1}]
    async def invoke(*args, **kwargs):
        result = ReportReview.model_validate(raw)
        result._submitted_evidence_ids = set() if problem == "unsubmitted" else {"ev-batch"}
        return result
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    if problem:
        assert "report_answer_witness_invalid" in result.hard_failures
    else:
        assert result.coverage[0].supporting_evidence[0].quote == passage
        assert "report_answer_witness_invalid" not in result.hard_failures
        assert result.decision == "revise"  # The infinite example remains blocked.


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [1, 9999, None])
async def test_answer_reference_must_point_to_actual_draft_passage(monkeypatch, index):
    from open_deep_research.report.verification import evidence_passages
    draft, data, raw = simple_review()
    raw["coverage"][0].update(answer_excerpt="", answer_passage_index=index)
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    if index == 1:
        assert result.coverage[0].answer_excerpt == evidence_passages(draft.markdown)[1]
        assert "report_answer_witness_invalid" not in result.hard_failures
    else:
        assert "report_answer_witness_invalid" in result.hard_failures


def simple_review():
    url = "https://docs.example/process"
    sentence = "A completed batch emits one receipt and exits."
    draft = ReportDraft(markdown=f"# Batch example\n\n{sentence} [Guide]({url})\n\n```python\nwhile True:\n    emit_receipt()\n```\n")
    state = {"coverage_contract": {"schema_version": 1, "requirements": [
        {"requirement_id": "batch", "kind": "factual", "text": "Explain finite batch completion."}]},
        "evidence_registry": [{"evidence_id": "ev-batch", "source_url": url,
            "supporting_excerpt": sentence, "claim": sentence, "security_status": "accepted"}]}
    payload = reviewer.build_reviewer_payload(draft, state, config())
    raw = {"decision": "pass", "dimensions": {name: .95 for name in reviewer._DIMENSIONS},
        "coverage": [{"requirement_id": "batch", "status": "covered", "explanation": "Checked",
            "answer_excerpt": sentence, "supporting_evidence": [{"evidence_id": "ev-batch", "quote": sentence}], "missing_aspects": []}],
        "citation_audit": [{"claim": sentence, "citation_target": url, "supported": True, "evidence_ids": ["ev-batch"]}],
        "example_checks": [{"example_id": payload["examples"][0]["example_id"],
            "expected_behavior": "Emit one receipt and exit", "observed_behavior": "Unbounded loop emitting receipts",
            "expected_lifecycle": "finite", "observed_lifecycle": "persistent",
            "execution_checks": [{"unit_id": "unit-1", "expected_lifecycle": "finite", "observed_lifecycle": "persistent", "stopping_condition": "No normal exit."}],
            "status": "contradicts", "explanation": "The loop has no normal stopping condition."}],
        "issues": [], "issue_resolutions": []}
    return draft, state, raw


@pytest.mark.asyncio
async def test_behavioral_contradiction_blocks_high_scores_and_model_pass(monkeypatch):
    draft, data, raw = simple_review()
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "revise"
    assert any(issue.category == "contradictions" and issue.blocking for issue in result.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["invented_answer", "invented_source", "omitted_source", "missing_aspect", "missing_example"])
async def test_claimed_pass_cannot_override_verification_failures(monkeypatch, defect):
    draft, data, raw = simple_review()
    raw["example_checks"][0]["status"] = "satisfies"
    raw["example_checks"][0]["observed_lifecycle"] = "finite"
    raw["example_checks"][0]["execution_checks"][0]["observed_lifecycle"] = "finite"
    row = raw["coverage"][0]
    if defect == "invented_answer": row["answer_excerpt"] = "The draft never contains this answer."
    elif defect == "invented_source": row["supporting_evidence"][0]["quote"] = "Unseen supporting fact."
    elif defect == "missing_aspect": row["missing_aspects"] = ["The required result is not established."]
    elif defect == "missing_example": raw["example_checks"] = []
    async def invoke(*args, **kwargs):
        result = ReportReview.model_validate(raw)
        if defect == "omitted_source": result._submitted_evidence_ids = set()
        return result
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.decision != "pass"
    if defect == "missing_aspect": assert result.coverage[0].status == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "deliverable"])
async def test_inventory_verified_nonfactual_requirements_do_not_need_a_factual_quote(monkeypatch, kind):
    draft, data, raw = simple_review()
    data["coverage_contract"]["requirements"].append({"requirement_id": "format", "kind": kind, "text": "Use a Markdown code example."})
    raw["coverage"].append({"requirement_id": "format", "status": "covered", "explanation": "The example inventory contains a complete fenced example.",
                            "missing_aspects": []})
    async def invoke(role, messages, cfg, **kwargs):
        result = response_for_stage(raw, kwargs, messages)
        if kwargs["span_name"] == "lead.report_review":
            wire = result.model_dump(mode="json")
            for field in ("answer_passage_index", "supporting_evidence", "missing_aspects"):
                wire["coverage"][-1].pop(field, None)
            wire.pop("issue_resolutions")  # No earlier issues exist in this review.
            return kwargs["schema"].model_validate(wire)
        return result
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert "report_answer_witness_invalid" not in result.hard_failures
    assert result.decision == "revise"  # The real behavioral contradiction is still blocking.


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["supporting_evidence", "answer_excerpt", "missing_aspects"])
async def test_omitted_factual_support_still_fails_after_schema_normalization(monkeypatch, missing):
    draft, data, raw = simple_review()
    raw["coverage"][0].pop(missing)
    async def invoke(role, messages, cfg, **kwargs):
        result = response_for_stage(raw, kwargs, messages)
        # The Gateway serializes validated defaults before the native adapter
        # revalidates them. An omitted completeness declaration must survive it.
        return kwargs["schema"].model_validate(result.model_dump(mode="json"))
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert "report_answer_witness_invalid" in result.hard_failures


@pytest.mark.asyncio
@pytest.mark.parametrize("code,behavior", [("emit_receipt()", "Emit one receipt and exit"),
                                         ("while True:\n    serve_request()", "Continuously serve requests")])
async def test_valid_finite_and_intentionally_persistent_examples_can_pass(monkeypatch, code, behavior):
    draft, data, raw = simple_review()
    draft.markdown = draft.markdown[:draft.markdown.index("```python")] + f"```python\n{code}\n```\n"
    item = raw["example_checks"][0]
    persistent = "while True" in code
    if persistent:
        old = raw["coverage"][0]["answer_excerpt"]
        sentence = "The service processes requests continuously until cancelled."
        draft.markdown = draft.markdown.replace(old, sentence)
        data["coverage_contract"]["requirements"][0]["text"] = "Explain the service lifecycle."
        data["evidence_registry"][0].update(claim=sentence, supporting_excerpt=sentence)
        raw["coverage"][0].update(answer_excerpt=sentence,
            supporting_evidence=[{"evidence_id": "ev-batch", "quote": sentence}])
    item.update(example_id=reviewer.build_reviewer_payload(draft, data, config())["examples"][0]["example_id"],
                expected_behavior=behavior, observed_behavior=behavior, status="satisfies", explanation="Behavior matches purpose.",
                expected_lifecycle="persistent" if persistent else "finite", observed_lifecycle="persistent" if persistent else "finite")
    item["execution_checks"] = [{"unit_id": "unit-1", "expected_lifecycle": item["expected_lifecycle"],
        "observed_lifecycle": item["observed_lifecycle"], "stopping_condition": "Cancellation" if persistent else "Command returns"}]
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "pass"


@pytest.mark.parametrize("unit_problem", [None, "mismatch", "missing_lifecycle"])
@pytest.mark.asyncio
async def test_redundant_example_lifecycle_summary_can_be_omitted_but_commands_remain_checked(monkeypatch, unit_problem):
    draft, data, raw = simple_review()
    draft.markdown = draft.markdown.replace("while True:\n    emit_receipt()", "emit_receipt()")
    item = raw["example_checks"][0]
    item.update(example_id=reviewer.build_reviewer_payload(draft, data, config())["examples"][0]["example_id"],
        status="satisfies", explanation="The command behavior has been inspected.")
    for field in ['expected_lifecycle', 'observed_lifecycle', 'observed_behavior']:
        item.pop(field)
    item["execution_checks"] = [{"unit_id": "unit-1", "expected_lifecycle": "finite",
        "observed_lifecycle": "persistent" if unit_problem == "mismatch" else "finite",
        "stopping_condition": "No normal exit" if unit_problem == "mismatch" else "Command returns"}]
    if unit_problem == "missing_lifecycle":
        item["execution_checks"][0].pop("observed_lifecycle")

    async def invoke(role, messages, cfg, **kwargs):
        return response_for_stage(raw, kwargs, messages)

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    if unit_problem is None:
        assert result.decision == "pass"
        assert result.example_checks[0].observed_behavior
    elif unit_problem == "mismatch":
        assert result.decision == "revise"
        assert result.example_checks[0].status == "contradicts"
    else:
        assert result.decision == "fail"
        assert "report_reviewer_unavailable" in result.hard_failures


@pytest.mark.asyncio
async def test_lifecycle_mismatch_overrides_inconsistent_example_pass(monkeypatch):
    draft, data, raw = simple_review()
    raw["example_checks"][0]["status"] = "satisfies"
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "revise"
    assert result.example_checks[0].status == "contradicts"


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_actual_command_loop_overrides_false_finite_verdict_without_rejecting_services(monkeypatch, background):
    draft, data, raw = simple_review()
    code = "tasks:\n  - command: [sh, -c, 'while true; do echo receipt; sleep 1; done']"
    draft.markdown = draft.markdown[:draft.markdown.index("```python")] + f"```yaml\n{code}\n```\n"
    item = raw["example_checks"][0]
    item.update(example_id=reviewer.build_reviewer_payload(draft, data, config())["examples"][0]["example_id"],
                expected_lifecycle="persistent" if background else "finite", observed_lifecycle="finite", status="satisfies")
    if background:
        item["observed_lifecycle"] = "persistent"
    item["execution_checks"] = [{"unit_id": "unit-1", "expected_lifecycle": item["expected_lifecycle"],
        "observed_lifecycle": "finite", "stopping_condition": "Model incorrectly claims it finishes"}]
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.example_checks[0].execution_checks[0].observed_lifecycle == "persistent"
    assert result.decision == ("pass" if background else "revise")


@pytest.mark.asyncio
async def test_ordered_command_labels_and_redundant_summary_can_be_server_bound(monkeypatch):
    draft, data, raw = simple_review()
    row = raw["example_checks"][0]
    row.pop("observed_behavior")
    row["execution_checks"][0].pop("unit_id")
    async def invoke(role, messages, cfg, **kwargs): return response_for_stage(raw, kwargs, messages)
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "revise"
    assert result.example_checks[0].execution_checks[0].unit_id == "unit-1"
    assert "No normal exit" in result.example_checks[0].observed_behavior
    assert "report_example_checks_missing_or_invalid" not in result.hard_failures


@pytest.mark.asyncio
async def test_declarative_example_without_commands_uses_its_actual_configuration_explanation(monkeypatch):
    draft, data, raw = simple_review()
    draft.markdown = draft.markdown[:draft.markdown.index("```python")] + "```yaml\napiVersion: v1\nkind: ConfigMap\ndata:\n  mode: batch\n```\n"
    example = reviewer.build_reviewer_payload(draft, data, config())["examples"][0]
    raw["example_checks"] = [{"example_id": example["example_id"], "expected_behavior": "Configure batch mode",
        "status": "satisfies", "explanation": "The ConfigMap supplies the mode key with value batch.", "execution_checks": []}]
    calls = []

    async def invoke(role, messages, cfg, **kwargs):
        calls.append(kwargs["span_name"])
        return response_for_stage(raw, kwargs, messages)

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "pass"
    assert result.example_checks[0].observed_behavior == raw["example_checks"][0]["explanation"]
    assert calls.count("lead.report_example_review") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model_check", ["omitted", "claims_finite", "unknown", "missing_label"])
async def test_batch_image_name_does_not_prove_completion_or_exhaust_protocol_repair(monkeypatch, model_check):
    draft, data, raw = simple_review()
    draft.markdown = draft.markdown[:draft.markdown.index("```python")] + '''```yaml
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: work
          image: unspecified-work:1.0
```
'''
    example = reviewer.build_reviewer_payload(draft, data, config())["examples"][0]
    checks = [] if model_check == "omitted" else [{"unit_id": "" if model_check == "missing_label" else "unit-1",
        "expected_lifecycle": "not_applicable", "observed_lifecycle": "unknown" if model_check == "unknown" else "finite",
        "stopping_condition": "The model assumes a Job image finishes."}]
    raw["example_checks"] = [{"example_id": example["example_id"], "expected_behavior": "Complete the batch workload",
        "status": "satisfies", "explanation": "The manifest uses a Job controller.", "execution_checks": checks}]
    calls = []

    async def invoke(role, messages, cfg, **kwargs):
        calls.append(kwargs["span_name"])
        return response_for_stage(raw, kwargs, messages)

    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "revise" and not result.hard_failures
    assert result.example_checks[0].status == "unverified"
    unit = result.example_checks[0].execution_checks[0]
    assert unit.expected_lifecycle == "finite" and unit.observed_lifecycle == "unknown"
    assert "not specified" in unit.stopping_condition
    assert calls.count("lead.report_example_review") == 1
    assert any(issue.blocking and "explicit primary command" in issue.description for issue in result.issues)


@pytest.mark.asyncio
async def test_unknown_command_identity_is_never_rebound(monkeypatch):
    draft, data, raw = simple_review()
    raw["example_checks"][0]["execution_checks"][0]["unit_id"] = "another-command"
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert "report_example_checks_missing_or_invalid" in result.hard_failures


@pytest.mark.parametrize("kind", ["Job", "Deployment"])
@pytest.mark.asyncio
async def test_batch_manifest_cannot_hide_unfinished_primary_work_by_claiming_it_should_persist(monkeypatch, kind):
    draft, data, raw = simple_review()
    sentence = "A completed batch emits one receipt and exits." if kind == "Job" else "The service runs continuously until cancelled."
    data["coverage_contract"]["requirements"][0]["text"] = "Explain finite batch completion." if kind == "Job" else "Explain the continuous service lifecycle."
    version = "batch/v1" if kind == "Job" else "apps/v1"
    draft.markdown = f'''# Workload example

{sentence} [Guide](https://docs.example/process)

```yaml
apiVersion: {version}
kind: {kind}
spec:
  template:
    spec:
      containers:
        - name: work
          command: [sh, -c, 'while true; do echo work; done']
      initContainers:
        - name: helper
          restartPolicy: Always
          command: [sh, -c, 'tail -F /data/logs']
```
'''
    data["evidence_registry"][0].update(claim=sentence, supporting_excerpt=sentence)
    raw["coverage"][0].update(answer_excerpt=sentence,
        supporting_evidence=[{"evidence_id": "ev-batch", "quote": sentence}])
    raw["example_checks"] = [{"example_id": reviewer.build_reviewer_payload(draft, data, config())["examples"][0]["example_id"],
        "expected_behavior": "The primary workload and supporting helper persist.",
        "observed_behavior": "The primary work loops forever and the helper follows logs.",
        "expected_lifecycle": "persistent", "observed_lifecycle": "persistent",
        "status": "satisfies", "explanation": "The model incorrectly treats the batch primary as a service.",
        "execution_checks": [{"unit_id": f"unit-{i}", "expected_lifecycle": "persistent",
            "observed_lifecycle": "persistent", "stopping_condition": "External termination only"} for i in [1, 2]]}]

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == ("revise" if kind == "Job" else "pass")
    assert not result.hard_failures
    main, helper = result.example_checks[0].execution_checks
    assert main.expected_lifecycle == ("finite" if kind == "Job" else "persistent")
    assert helper.expected_lifecycle == helper.observed_lifecycle == "persistent"


def test_recorded_job_inventory_keeps_each_command_separate():
    from open_deep_research.report.verification import example_inventory
    value = recorded()
    examples = example_inventory(reviewer._markdown_regions(value["draft"]))
    units = examples[0]["execution_units"]
    assert len(units) == 2
    assert sum(unit["unconditional_loop"] for unit in units) == 1
    assert any("while true" in unit["code"] and unit["unconditional_loop"] for unit in units)
    assert any("tail -F" in unit["code"] and not unit["unconditional_loop"] for unit in units)


@pytest.mark.parametrize("script", ["echo done", "while true; do if finished; then break; fi; done",
                                  "while true; do exec server; done"])
def test_static_loop_observation_does_not_claim_unproven_infinity(script):
    from open_deep_research.report.verification import _execution_units
    assert not _execution_units(f"```sh\n{script}\n```\n")[0]["unconditional_loop"]


@pytest.mark.asyncio
async def test_quote_repair_feedback_identifies_the_actual_invalid_witness(monkeypatch):
    draft, data, raw = simple_review()
    calls = []
    async def invoke(payload, *args, **kwargs):
        calls.append(deepcopy(payload))
        if len(calls) == 2:
            detail = payload["review_protocol_feedback"]["answer_verification_feedback"]
            assert any(row["requirement_id"] == "batch" and row["field"] == "answer_passage_index" for row in detail)
            return deepcopy(raw)
        changed = deepcopy(raw)
        changed["coverage"][0]["answer_excerpt"] = "A completed batch…exits."
        return changed
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert len(calls) == 2 and "report_answer_witness_invalid" not in result.hard_failures


@pytest.mark.asyncio
async def test_source_index_repair_receives_local_valid_indices_and_keeps_real_behavior_blocking(monkeypatch):
    draft, data, raw = simple_review()
    sentence = data["evidence_registry"][0]["supporting_excerpt"]
    data["evidence_registry"][0]["supporting_excerpt"] = "## Guide\n\n" + sentence
    support = raw["coverage"][0]["supporting_evidence"][0]
    support.update(quote="", passage_index=99)
    calls = []

    async def invoke(payload, *args, **kwargs):
        calls.append(payload)
        value = deepcopy(raw)
        if len(calls) == 2:
            row = next(row for row in payload["review_protocol_feedback"]["answer_verification_feedback"]
                if row.get("field") == "supporting_evidence.passage_index")
            assert row["valid_passage_indices"] == [0, 1]
            value["coverage"][0]["supporting_evidence"][0]["passage_index"] = row["valid_passage_indices"][-1]
        return value

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert len(calls) == 2 and "report_answer_witness_invalid" not in result.hard_failures
    assert result.coverage[0].supporting_evidence[0].quote == sentence
    assert result.decision == "revise"  # The actual infinite batch remains blocked.


@pytest.mark.asyncio
async def test_citation_repair_receives_exact_binding_without_rewriting_draft(monkeypatch):
    draft, data, raw = simple_review()
    calls = []
    async def invoke(payload, *args, **kwargs):
        calls.append(payload)
        assert payload["draft_markdown"] == draft.markdown
        if len(calls) == 2:
            feedback = payload["review_protocol_feedback"]["citation_binding_feedback"]
            assert any(row["source_urls"].get("ev-batch") == "https://docs.example/process" for row in feedback)
            return deepcopy(raw)
        changed = deepcopy(raw)
        changed["citation_audit"][0]["citation_target"] = "https://docs.example/another-page"
        return changed
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert len(calls) == 2 and "review_protocol_invalid" not in result.hard_failures
    assert result.decision == "revise"  # Real code defect remains.


@pytest.mark.asyncio
@pytest.mark.parametrize("version,category,error", [(20, "redundancy", False), (20, "unsupported_claims", True), (19, "redundancy", True)])
async def test_no_change_editorial_note_does_not_poison_factual_revision(monkeypatch, version, category, error):
    draft, data, raw = simple_review()
    raw["issues"] = [{"category": category, "severity": "low", "requires_change": False,
        "description": "Optional formatting observation.", "location": "References", "revision_instruction": ""}]
    async def invoke(*args, **kwargs): return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config(version))
    assert ("review_issue_not_actionable" in result.hard_failures) == error
    if not error:
        assert result.decision == "revise"
        assert any(issue.category == "contradictions" and issue.blocking for issue in result.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [19, 20])
@pytest.mark.parametrize("category", ["unsupported_claims", "contradictions", "other"])
async def test_concrete_blocking_correction_survives_optional_change_flag(monkeypatch, version, category):
    draft, data, raw = simple_review()
    raw["issues"] = [{"category": category, "severity": "low", "requires_change": False,
        "description": "The report promises readiness from ordered process startup.",
        "location": "Migration guidance", "revision_instruction": "Replace the unsupported guarantee with the source's startup condition."}]

    async def invoke(*args, **kwargs):
        return deepcopy(raw)

    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    result = await reviewer.review_report(draft, data, config(version))
    issue = next(item for item in result.issues if item.location == "Migration guidance")
    assert issue.blocking and issue.requires_change == (version >= 20)
    assert ("review_issue_not_actionable" in result.hard_failures) == (version < 20)
    if version >= 20:
        assert result.decision == "revise"


@pytest.mark.asyncio
async def test_omitted_change_flag_conservatively_keeps_defect_blocking(monkeypatch):
    draft, data, raw = simple_review()
    raw["issues"] = [{"category": "coverage", "severity": "major", "description": "A required result is missing.",
                      "location": "Batch example", "revision_instruction": "Include the required result."}]
    async def invoke(role, messages, cfg, **kwargs): return response_for_stage(raw, kwargs, messages)
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert result.decision == "revise"
    assert any(issue.description == "A required result is missing." and issue.requires_change and issue.blocking for issue in result.issues)
    assert "report_reviewer_unavailable" not in result.hard_failures


@pytest.mark.asyncio
async def test_single_example_identity_is_bound_by_server_but_behavior_still_blocks(monkeypatch):
    draft, data, raw = simple_review()
    raw["example_checks"][0].pop("example_id")
    async def invoke(role, messages, cfg, **kwargs):
        return response_for_stage(raw, kwargs, messages)
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    result = await reviewer.review_report(draft, data, config())
    assert result.example_checks[0].example_id == reviewer.build_reviewer_payload(draft, data, config())["examples"][0]["example_id"]
    assert result.decision == "revise"


@pytest.mark.asyncio
async def test_schema_error_uses_remaining_review_attempt_without_relaxing_schema(monkeypatch):
    from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
    draft, data, raw = simple_review()
    calls = []
    async def invoke(payload, *args, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            raise ModelOutputProtocolError("missing execution_checks.unit_id")
        assert "unit_id" in payload["review_protocol_feedback"]["validation_error"]
        return deepcopy(raw)
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert len(calls) == 2 and result.decision == "revise"


@pytest.mark.asyncio
async def test_code_audit_is_separate_and_reused_during_prose_metadata_repair(monkeypatch):
    draft, data, raw = simple_review()
    calls = []
    async def invoke(role, messages, cfg, **kwargs):
        span = kwargs["span_name"]
        calls.append(span)
        properties = kwargs["schema"].model_json_schema()["properties"]
        if span == "lead.report_example_review":
            assert set(properties) == {"example_checks"}
        else:
            assert "example_checks" not in properties
            context = json.loads(next(m.content for m in messages if m.name == "report_context"))
            assert "_verified_example_checks" not in context and "examples" not in context
            assert "".join(context["draft_passages"][i] for i in sorted(context["draft_passages"], key=int)) == draft.markdown
        value = deepcopy(raw)
        if calls == ["lead.report_review"]:
            value["coverage"][0]["answer_excerpt"] = "Not an actual draft quote"
        return response_for_stage(value, kwargs, messages)
    monkeypatch.setattr(reviewer, "require_report_runtime", lambda: SimpleNamespace(invoke=invoke))
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert calls == ["lead.report_review", "lead.report_example_review", "lead.report_review"]
    assert result.decision == "revise" and result.example_checks[0].status == "contradicts"


@pytest.mark.asyncio
async def test_failed_protocol_repair_retains_real_defects_and_still_blocks(monkeypatch):
    from open_deep_research.agentscope_runtime.recovery import ModelOutputProtocolError
    draft, data, raw = simple_review()
    calls = []
    async def invoke(payload, *args, **kwargs):
        calls.append(payload)
        if len(calls) == 2:
            raise ModelOutputProtocolError("tool arguments truncated")
        value = deepcopy(raw)
        value["coverage"][0]["answer_excerpt"] = "Not a valid quote"
        return value
    monkeypatch.setattr(reviewer, "_invoke_reviewer", invoke)
    cfg = config()
    cfg["configurable"]["max_structured_output_retries"] = 2
    result = await reviewer.review_report(draft, data, cfg)
    assert result.decision == "fail" and "report_review_protocol_repair_failed" in result.hard_failures
    assert any(issue.category == "contradictions" and issue.blocking for issue in result.issues)
    assert result.example_checks[0].status == "contradicts"


def test_scope_projection_keeps_original_subject_without_assigning_sibling_questions():
    from open_deep_research.quality.contract import ResearchCoverageContract, researcher_coverage_view
    from open_deep_research.quality.gate import _owned_coverage_contract_projection
    value = recorded()
    contract = ResearchCoverageContract.model_validate(value["coverage_contract"])
    owned = ("COV-14-75e165663ae7", "COV-15-e2cb7ee84d3f")
    for projected in [researcher_coverage_view(contract, owned, preserve_scope=True),
                      _owned_coverage_contract_projection(contract, owned, preserve_scope=True)]:
        assert any("Sidecar" in text for text in projected["scope_context"])
        assert {r["requirement_id"] for r in projected["requirements"] if r.get("kind") == "factual"} == set(owned)
    assert "scope_context" not in researcher_coverage_view(contract, owned)


def test_v19_restore_keeps_old_review_contract():
    from open_deep_research.agentscope_runtime.run_config import RunConfig
    from open_deep_research.configuration import run_config_fingerprint
    run = RunConfig.compile()
    assert run.snapshot()["contract"]["metadata"]["run_config_schema_version"] == 21
    snapshot = run.snapshot()
    snapshot["contract"]["metadata"]["run_config_schema_version"] = 19
    snapshot["contract"]["metadata"]["run_config_fingerprint"] = run_config_fingerprint(snapshot["contract"])
    assert RunConfig.restore(snapshot).snapshot() == snapshot


@pytest.mark.parametrize("first,second", [("1）", "2）"), ("1)", "2)"), ("（1）", "（2）")])
def test_original_scenario_is_not_a_fact_and_numbered_facets_are_not_duplicated(first, second):
    from open_deep_research.quality.planning import compile_planned_requirements
    scenario = "我们准备将常驻日志收集器用于批处理任务"
    one, two = "容器如何启动", "任务怎样完成"
    message = f"场景：{scenario}。请说明：{first}{one}；{second}{two}。"
    atoms = [{"source_message_index": 0, "source_text": text, "kind": "factual", "facets": facets}
             for text, facets in [(scenario, []), (first + one, [one]), (second + two, [two])]]
    fixed = compile_planned_requirements([{"role": "user", "content": message}], atoms,
        atomic=True, preserve_scope=True)
    facts = [r for r in fixed.requirements if r.kind == "factual"]
    assert {r.text for r in facts} == {one, two}
    assert any(r.text == scenario and r.kind == "process" for r in fixed.requirements)
    assert all(message[r.source_start:r.source_end] == r.text for r in fixed.requirements)


def test_question_in_scenario_keeps_its_factual_obligation():
    from open_deep_research.quality.planning import compile_planned_requirements
    text = "场景：如果限制已启用，能否继续执行？"
    fixed = compile_planned_requirements([{"role": "user", "content": text}],
        [{"source_message_index": 0, "source_text": text, "kind": "factual"}],
        atomic=True, preserve_conditions=True, preserve_scope=True)
    assert any(r.kind == "factual" and "能否" in r.text for r in fixed.requirements)


@pytest.mark.parametrize("goal,context_only", [("研究系统的迁移方案。", True), ("研究系统是否支持并发。", False)])
def test_combined_goal_and_scenario_preamble_is_scoped_without_losing_a_question(goal, context_only):
    from open_deep_research.quality.planning import compile_planned_requirements
    prefix = goal + "场景：我们计划迁移现有服务。"
    first, second = "服务如何启动", "任务如何结束"
    text = prefix + f"请说明：1）{first}；2）{second}。"
    atoms = [{"source_message_index": 0, "source_text": value, "kind": "factual"}
             for value in [prefix, first, second]]
    result = compile_planned_requirements([{"role": "user", "content": text}], atoms,
        atomic=True, preserve_scope=True)
    preamble = next(row for row in result.requirements if row.text == prefix)
    assert preamble.kind == ("process" if context_only else "factual")
    assert any(first in row.text and row.kind == "factual" for row in result.requirements)
    assert any(second in row.text and row.kind == "factual" for row in result.requirements)


@pytest.mark.parametrize("goal,kind", [
    ("研究日志收集器的生产迁移方案", "deliverable"),
    ("Prepare a migration strategy", "deliverable"),
    ("研究系统是否支持该迁移方案", "factual"),
    ("研究日志收集器的容量限制", "factual"),
])
def test_plan_title_is_a_deliverable_without_duplicating_numbered_research(goal, kind):
    from open_deep_research.quality.planning import compile_planned_requirements
    first, second = "服务如何启动", "任务如何结束"
    text = goal + f"。场景：我们计划迁移。请说明：1）{first}；2）{second}。"
    atoms = [{"source_message_index": 0, "source_text": value, "kind": "factual"}
             for value in [goal, first, second]]
    result = compile_planned_requirements([{"role": "user", "content": text}], atoms,
        atomic=True, preserve_scope=True)
    assert next(r for r in result.requirements if r.text == goal).kind == kind
    assert all(any(value in r.text and r.kind == "factual" for r in result.requirements)
               for value in (first, second))
    assert all(text[r.source_start:r.source_end] == r.text for r in result.requirements)


def test_retained_complete_question_does_not_also_assign_its_noun_fragments():
    from open_deep_research.quality.planning import compile_planned_requirements
    text = "1）CPU/内存 requests 与 limits 如何计入 Pod 的有效资源；2）startupProbe、readinessProbe 分别怎样影响后续容器启动和 Pod 就绪"
    atoms = [{"source_message_index": 0, "source_text": text.split("；")[0][2:], "kind": "factual",
              "facets": ["CPU/内存 requests 与 limits", "计入 Pod 的有效资源"]},
             {"source_message_index": 0, "source_text": text.split("；")[1][2:], "kind": "factual",
              "facets": ["startupProbe", "readinessProbe", "后续容器启动", "Pod 就绪"]}]
    previous = compile_planned_requirements([{"role": "user", "content": text}], atoms, atomic=True)
    fixed = compile_planned_requirements([{"role": "user", "content": text}], atoms, atomic=True, preserve_scope=True)
    assert len(previous.requirements) > len(fixed.requirements)
    assert len(fixed.requirements) == 2
    assert "如何计入" in fixed.requirements[0].text and "分别怎样影响" in fixed.requirements[1].text
    assert all(text[r.source_start:r.source_end] == r.text for r in fixed.requirements)


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True])
async def test_partial_writer_corrects_grounding_once_with_same_source_scope(monkeypatch, corrected):
    from open_deep_research.report import evidence_synthesis as synthesis
    from open_deep_research.quality.contract import build_research_coverage_contract
    calls, checks = [], []
    source = {"evidence_id": "ev-stop", "source_url": "https://docs.example/stop",
        "claim": "The runtime sends the stop signal.", "supporting_excerpt": "The runtime sends the stop signal.",
        "security_status": "accepted"}
    async def write(payload, config):
        calls.append(deepcopy(payload))
        assert payload["allowed_source_urls"] == [source["source_url"]]
        if len(calls) == 2:
            assert payload["grounding_feedback"]["unsupported_claim_paths"] == ["sections[0].claims[0]"]
            assert payload["previous_draft"]["sections"][0]["claims"][0]["text"].startswith("The controller")
        return synthesis.EvidenceSynthesisDraft(title="Stop behavior", summary=source["claim"], summary_evidence_ids=["ev-stop"],
            sections=[{"heading": "Behavior", "claims": [{"text": source["claim"] if len(calls) == 2 else "The controller sends the stop signal.",
                       "evidence_ids": ["ev-stop"]}]}])
    async def ground(payload, config):
        checks.append(payload)
        good = corrected and len(checks) == 2
        return synthesis.EvidenceSynthesisGroundingAssessment(supported=good,
            unsupported_claim_paths=[] if good else ["sections[0].claims[0]"], reason="Check the source's named actor.")
    monkeypatch.setattr(synthesis, "_invoke_draft", write)
    monkeypatch.setattr(synthesis, "_invoke_grounding_judge", ground)
    contract = build_research_coverage_contract([{"role": "user", "content": "Who sends the stop signal?"}])
    output = await synthesis.build_evidence_limited_report([source],
        coverage_contract=contract,
        coverage_ledger={contract.requirements[0].requirement_id: {"status": "supported", "evidence_ids": ["ev-stop"]}},
        caveats=[], uncovered_requirement_ids=[], rejection_reasons=[], artifact_refs=[], config=config())
    assert len(calls) == len(checks) == 2
    assert "The controller sends" not in output
    if corrected:
        assert "The runtime sends" in output and "## Behavior" in output
    else:
        assert "evidence_synthesis_grounding_failed" not in output  # Internal diagnostic is not a report claim.

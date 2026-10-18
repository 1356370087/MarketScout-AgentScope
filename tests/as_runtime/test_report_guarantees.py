from types import SimpleNamespace

import pytest

from open_deep_research.quality.context import context_payload

from open_deep_research.report.guarantees import (
    GuaranteeAudit, GuaranteeProtocolError, audit_guarantees, check_guarantees, guarantee_inventory,
)


def test_inventory_preserves_conditions_and_omits_code_and_bibliography():
    text = ('# Can delivery be guaranteed?\n\n## 能否保证日志零丢失\n\nA shared volume guarantees readiness.\n\n'
            'This does not guarantee durable delivery if termination interrupts flushing.\n\n'
            '```yaml\nrestartPolicy: Always\n```\n\n### Sources\n\nAlways use this reference.\n')
    claims = guarantee_inventory(text)
    assert len(claims) == 2
    assert 'if termination interrupts flushing' in claims[1]['text']
    assert guarantee_inventory('A finite command writes a record and exits.') == []


@pytest.mark.parametrize('framing', [
    '本报告仅依据官方文档给出迁移方案。官方保证与工程建议在各节分别标注；版本结论以文档页为准。',
    'Official guarantees and engineering recommendations are separately labeled in this report.',
])
def test_report_labeling_alone_requires_no_external_guarantee_evidence(framing):
    assert guarantee_inventory(framing) == []
    for claim in ['共享卷保证服务就绪。', 'The sidecar must finish before the main process starts.']:
        combined = framing + ' ' + claim
        claims = guarantee_inventory(combined)
        assert len(claims) == 1 and claims[0]['text'].strip() == combined


def test_official_guarantee_labels_on_factual_claims_are_still_checked():
    text = '**规则（官方保证）**：普通初始化阶段必须包含已启动且持续运行的辅助容器。'
    assert guarantee_inventory(text)[0]['text'] == text
    assert guarantee_inventory('本文依据官方资料声称共享卷有官方保证。')


@pytest.mark.parametrize('supported', [True, False])
def test_contradicted_guarantee_blocks_even_with_an_existing_correct_explanation(supported):
    claims = guarantee_inventory('A shared volume guarantees readiness.')
    result = GuaranteeAudit(checks=[{'claim_id': claims[0]['claim_id'], 'supported': supported,
        'evidence_ids': ['life'], 'explanation': 'Process startup alone does not prove service readiness.',
        'revision_instruction': 'State only ordered process startup, with explicit readiness conditions.'}])
    issues = check_guarantees(result, claims, [{'evidence_id': 'life'}])
    assert bool(issues) == (not supported)
    if issues:
        assert issues[0].requires_change and issues[0].severity == 'major'


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'unknown', 'unsubmitted', 'no_support'])
def test_guarantee_checks_require_complete_claims_and_actual_source_visibility(problem):
    claims = guarantee_inventory('A shared volume guarantees readiness.')
    row = {'claim_id': 'claim-0', 'supported': True, 'evidence_ids': ['life'], 'explanation': 'Checked.'}
    if problem == 'unknown': row['evidence_ids'] = ['outside']
    if problem == 'no_support': row['evidence_ids'] = []
    result = GuaranteeAudit(checks=[] if problem == 'missing' else [row, row] if problem == 'duplicate' else [row])
    if problem == 'unsubmitted': result._submitted_evidence_ids = set()
    with pytest.raises(GuaranteeProtocolError):
        check_guarantees(result, claims, [{'evidence_id': 'life'}])


def test_supported_false_with_a_concrete_finding_does_not_require_redundant_correction_text():
    claims = guarantee_inventory('The system must use the lowest component category.')
    result = GuaranteeAudit(checks=[{'claim_id': 'claim-0', 'supported': False, 'evidence_ids': ['life'],
        'explanation': 'The source states that the same criteria apply; it never says the lowest category wins.'}])
    issues = check_guarantees(result, claims, [{'evidence_id': 'life'}])
    assert len(issues) == 1 and issues[0].requires_change and issues[0].severity == 'major'
    assert issues[0].description == result.checks[0].explanation
    assert 'Remove the unsupported assertion' in issues[0].revision_instruction


@pytest.mark.asyncio
async def test_focused_guarantee_repair_is_bounded_and_keeps_the_real_defect_blocking():
    calls = []

    async def invoke(role, messages, cfg, **kwargs):
        data = context_payload(next(m.content for m in messages if m.name == 'report_context'))
        calls.append(data)
        result = GuaranteeAudit(checks=[{'claim_id': data['claims'][0]['claim_id'], 'supported': False,
            'evidence_ids': ['wrong' if len(calls) == 1 else 'life'],
            'explanation': 'Startup is not readiness.', 'revision_instruction': 'Remove the unsupported readiness guarantee.'}])
        result._submitted_evidence_ids = {'life'}
        return result

    payload = {'draft_markdown': 'A shared volume guarantees readiness.',
        'evidence_registry': [{'evidence_id': 'life', 'supporting_excerpt': 'A process can run before it is ready.'}]}
    issues = await audit_guarantees(payload, {'metadata': {'run_config_schema_version': 20}},
        SimpleNamespace(max_structured_output_retries=2), invoke)
    assert len(calls) == 2 and 'protocol_feedback' in calls[1]
    assert len(issues) == 1 and 'readiness' in issues[0].description


@pytest.mark.asyncio
async def test_unknown_guarantee_execution_is_not_retried_as_a_protocol_error():
    from open_deep_research.agentscope_runtime.recovery_store import UnknownOperation
    calls = []

    async def invoke(*args, **kwargs):
        calls.append(1)
        raise UnknownOperation('uncertain')

    with pytest.raises(UnknownOperation):
        await audit_guarantees({'draft_markdown': 'A shared volume guarantees readiness.', 'evidence_registry': []},
            {'metadata': {'run_config_schema_version': 20}}, SimpleNamespace(max_structured_output_retries=2), invoke)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_guarantee_audit_receives_previously_verified_source_phase_arithmetic():
    records = [{'evidence_id': 'life', 'supporting_excerpt': 'The helper remains active while later initialization runs.'}]
    calls = []

    async def invoke(role, messages, cfg, **kwargs):
        data = context_payload(next(m.content for m in messages if m.name == 'report_context'))
        calls.append(data)
        phases = {row['case_id']: row for row in data['verified_phase_calculations']}
        assert phases['helper_first']['phase_totals'] == [.5, 2.5, 1.5]
        assert phases['helper_after']['peak'] == 2.
        assert 'need not occur verbatim' in messages[0].content
        return GuaranteeAudit(checks=[{'claim_id': row['claim_id'], 'supported': True,
            'evidence_ids': ['life'], 'explanation': 'The stated phase-derived peak matches the verified calculation.'}
            for row in data['claims']])

    payload = {'draft_markdown': 'The phase-aware formula must include the persistent helper.',
        '_verified_boundary_cases': {'helper_first', 'helper_after'}, '_verified_boundary_issues': [],
        'evidence_registry': records}
    issues = await audit_guarantees(payload, {'metadata': {'run_config_schema_version': 20}},
        SimpleNamespace(max_structured_output_retries=2), invoke)
    assert not issues and len(calls) == 1
    assert records[0]['supporting_excerpt'] == 'The helper remains active while later initialization runs.'


@pytest.mark.asyncio
async def test_boundary_proof_precedes_guarantee_audit_and_is_reused_for_same_draft(monkeypatch):
    from tests.as_runtime.test_report_answer_verification import simple_review, config, response_for_stage
    from open_deep_research.report import reviewer, guarantees, boundaries
    from open_deep_research.configuration import Configuration
    draft, state, raw = simple_review()
    payload = reviewer.build_reviewer_payload(draft, state, config())
    payload['required_boundary_cases'] = ['helper_first', 'helper_after']
    calls = []

    async def invoke(role, messages, cfg, **kwargs):
        return response_for_stage(raw, kwargs, messages)

    async def phases(current, *args):
        calls.append('phases')
        current['_verified_boundary_cases'] = {'helper_first', 'helper_after'}
        return []

    async def guarantees_checked(current, *args):
        assert current['_verified_boundary_cases'] == {'helper_first', 'helper_after'}
        calls.append('guarantees')
        return []

    monkeypatch.setattr(reviewer, 'require_report_runtime', lambda: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(boundaries, 'audit_boundaries', phases)
    monkeypatch.setattr(guarantees, 'audit_guarantees', guarantees_checked)
    cfg = Configuration.from_runnable_config(config())
    for _ in range(2):
        result = await reviewer._invoke_reviewer(payload, config(), cfg, attempt=1)
        assert result._verified_boundary_cases == {'helper_first', 'helper_after'}
    assert calls == ['phases', 'guarantees']


def test_missing_guarantee_verdict_is_never_defaulted_to_supported():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GuaranteeAudit.model_validate({'checks': [{'claim_id': 'claim-0', 'explanation': 'A missing judgment.'}]})


@pytest.mark.parametrize('supported', [True, False])
def test_long_explanatory_notes_are_bounded_without_changing_the_verdict(supported):
    result = GuaranteeAudit.model_validate({'checks': [{'claim_id': 'claim-0', 'supported': supported,
        'evidence_ids': ['life'], 'explanation': 'Source reasoning. ' * 200,
        'revision_instruction': 'Remove the unsupported promise. ' * 200}]})
    row = result.checks[0]
    assert row.supported is supported and len(row.explanation) == len(row.revision_instruction) == 1500


def test_auxiliary_failure_preserves_completed_factual_findings_without_publication():
    from tests.as_runtime.test_report_answer_verification import simple_review, config
    from open_deep_research.report import reviewer
    from open_deep_research.report.models import ReportReview
    draft, state, raw = simple_review()
    raw['coverage'][0].update(status='partial', missing_aspects=['The requested milestone is absent.'])
    candidate = ReportReview.model_validate(raw)
    candidate._auxiliary_review_error = 'report_guarantee_reviewer_unavailable: missing supported field'
    result = reviewer._normalize_candidate(candidate, draft=draft, state=state, config=config(),
        payload=reviewer.build_reviewer_payload(draft, state, config()), model_name='fixture', attempt=1)
    assert result.decision == 'fail' and result.status == 'failed'
    assert 'report_guarantee_reviewer_unavailable' in result.hard_failures
    assert result.coverage[0].status == 'partial'
    assert any(row.category == 'coverage' and row.blocking for row in result.issues)
    assert 'missing supported field' in result.provenance['auxiliary_review_error']
    from open_deep_research.report.orchestrator import _review_has_recoverable_content_failure
    assert not _review_has_recoverable_content_failure(result)


@pytest.mark.asyncio
async def test_guarantee_unavailability_does_not_erase_the_completed_prose_and_example_review(monkeypatch):
    from tests.as_runtime.test_report_answer_verification import simple_review, config, response_for_stage
    from open_deep_research.report import reviewer, guarantees
    draft, state, raw = simple_review()

    async def invoke(role, messages, cfg, **kwargs):
        return response_for_stage(raw, kwargs, messages)

    async def unavailable(*args, **kwargs):
        raise guarantees.GuaranteeReviewUnavailable('report_guarantee_reviewer_unavailable') from ValueError('malformed verdict')

    monkeypatch.setattr(reviewer, 'require_report_runtime', lambda: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(guarantees, 'audit_guarantees', unavailable)
    result = await reviewer.review_report(draft, state, config())
    assert result.decision == 'fail' and result.status == 'failed'
    assert 'report_guarantee_reviewer_unavailable' in result.hard_failures
    assert result.coverage and result.example_checks
    assert any(row.category == 'contradictions' and row.blocking for row in result.issues)
    assert 'malformed verdict' in result.provenance['auxiliary_review_error']


def test_verified_guarantee_findings_merge_without_lending_sources_to_prose():
    from tests.as_runtime.test_report_answer_verification import simple_review, config
    from open_deep_research.report import reviewer
    from open_deep_research.report.models import ReportReview, ReportReviewIssue
    draft, state, raw = simple_review()
    candidate = ReportReview.model_validate(raw)
    candidate._submitted_evidence_ids = {'ev-batch'}
    candidate._verified_guarantee_issues = [ReportReviewIssue(category='contradictions', severity='major',
        location='Migration guidance', evidence_ids=['independent-source'], description='Startup is not readiness.',
        revision_instruction='Remove the unsupported readiness guarantee.')]
    result = reviewer._normalize_candidate(candidate, draft=draft, state=state, config=config(),
        payload=reviewer.build_reviewer_payload(draft, state, config()), model_name='fixture', attempt=1)
    assert 'unknown_evidence_id' not in result.hard_failures
    assert any(row.location == 'Migration guidance' and row.blocking for row in result.issues)
    assert result.decision == 'revise'

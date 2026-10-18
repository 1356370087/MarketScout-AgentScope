import pytest

from open_deep_research.quality.planning import compile_planned_requirements
from open_deep_research.quality.contract import coverage_requirement_display_text


@pytest.mark.parametrize('text,facets', [
    ('说明组件的声明方式、稳定版本与适用前提', ['声明方式', '稳定版本', '适用前提']),
    ('说明备份的开启方式、默认值与恢复前提', ['开启方式', '默认值', '恢复前提']),
    ('比较服务的成本、性能和适用边界', ['成本', '性能', '适用边界']),
])
def test_model_cannot_mark_a_compound_list_covered_by_answering_only_one_aspect(text, facets):
    messages = [{'role': 'user', 'content': text}]
    atoms = [{'source_text': text, 'kind': 'factual', 'facets': []}]
    result = compile_planned_requirements(messages, atoms, atomic=True, preserve_scope=True)
    factual = [row for row in result.requirements if row.kind == 'factual']
    assert [row.text for row in factual] == facets
    assert len({row.requirement_id for row in factual}) == len(facets)
    for row in factual:
        assert text[row.source_start:row.source_end] == row.text
        assert row.dimension_id
        assert text in coverage_requirement_display_text(result, row)
    assert sum(len(ids) for ids in result.delegation_scopes.values()) == len(facets)
    old = compile_planned_requirements(messages, atoms, atomic=True)
    assert any(row.text == text for row in old.requirements)


@pytest.mark.parametrize('text', [
    'startupProbe、readinessProbe 分别怎样影响后续容器启动和 Pod 就绪',
    '说明组件的启动方式、就绪条件之间的关系',
    '说明系统的存储、计算之间是否存在性能差异',
    '说明系统的默认值与适用前提',
])
def test_relational_questions_and_joint_subjects_keep_their_original_meaning(text):
    result = compile_planned_requirements([{'role': 'user', 'content': text}],
        [{'source_text': text, 'kind': 'factual'}], atomic=True, preserve_scope=True)
    assert any(row.text == text for row in result.requirements)


@pytest.mark.parametrize('facets', [[], ['声明方式', '稳定版本与适用前提'],
    ['声明方式、稳定版本', '适用前提'], ['声明方式', '稳定版本', '适用前提'], ['声明方式', '适用前提']])
def test_original_enumeration_does_not_depend_on_model_facet_grouping_or_omission(facets):
    text = '说明组件的声明方式、稳定版本与适用前提'
    result = compile_planned_requirements([{'role': 'user', 'content': text}],
        [{'source_text': text, 'kind': 'factual', 'facets': facets}], atomic=True, preserve_scope=True)
    assert [row.text for row in result.requirements if row.kind == 'factual'] == ['声明方式', '稳定版本', '适用前提']

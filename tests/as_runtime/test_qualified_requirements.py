"""Do not turn user scenarios and false claims into mandatory true facts."""

import pytest

from open_deep_research.quality.planning import compile_planned_requirements


@pytest.mark.parametrize("question,fragments", [
    ("核实：数据摄取后才建立 payload 索引，若希望利用新增的 filter-aware 边是否需要重建 HNSW；给出官方方法。",
     ["数据摄取后才建立 payload 索引", "若希望利用新增的 filter-aware 边是否需要重建 HNSW", "给出官方方法"]),
    ("If data was ingested before the index was created, does the graph need rebuilding? Explain the method.",
     ["data was ingested before the index was created", "does the graph need rebuilding?", "Explain the method."]),
    ("如果 WAL 摘要没有开启，能否生成增量备份？说明必要条件。",
     ["WAL 摘要没有开启", "能否生成增量备份？", "说明必要条件"]),
    ("核实并纠正‘取消异常可以安全吞掉而不影响 TaskGroup’这一说法。",
     ["取消异常可以安全吞掉而不影响 TaskGroup"]),
])
def test_scenario_and_verification_intent_survive_model_fragmentation(question, fragments):
    atoms = [{"source_text": text, "kind": "factual", "source_message_index": 0} for text in fragments]
    contract = compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True, preserve_conditions=True)
    factual = [r.text for r in contract.requirements if r.kind == "factual"]
    assert fragments[0] not in factual
    assert any(("若" in text or "如果" in text or "If" in text or "核实并纠正" in text) for text in factual)
    for row in contract.requirements:
        assert question[row.source_start:row.source_end] == row.text


def test_old_policy_keeps_existing_atom_identity():
    question = "如果功能关闭，能否使用该功能？"
    atoms = [{"source_text": "功能关闭", "kind": "factual", "source_message_index": 0}]
    old = compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True)
    new = compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True, preserve_conditions=True)
    assert "功能关闭" in [r.text for r in old.requirements]
    assert "功能关闭" not in [r.text for r in new.requirements]


def test_missing_model_atoms_do_not_lose_other_user_requirements():
    question = "说明创建方式。若功能关闭，能否直接使用？报告用中文。"
    contract = compile_planned_requirements([{"role": "user", "content": question}], [], atomic=True, preserve_conditions=True)
    text = " ".join(r.text for r in contract.requirements)
    assert "创建方式" in text and "若功能关闭" in text and "中文" in text


def test_conditional_output_instruction_is_not_promoted_to_factual_question():
    question = "比较两个数据库。若官方文档未覆盖某点，应如实标注为未验证而非推测。"
    atoms = [{"source_text": "比较两个数据库", "kind": "factual"},
             {"source_text": "若官方文档未覆盖某点，应如实标注为未验证而非推测", "kind": "process"}]
    contract = compile_planned_requirements([{"role": "user", "content": question}], atoms, atomic=True, preserve_conditions=True)
    assert next(row for row in contract.requirements if "未验证" in row.text).kind == "process"

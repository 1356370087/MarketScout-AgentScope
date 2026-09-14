"""Structural checks for the KB-08 evaluation-sample draft fixture."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge_eval_draft.json"

_CATEGORIES = {
    "zh_facts_names",
    "en_alias",
    "financial_tables",
    "multi_document",
    "versioning",
    "no_answer",
    "isolation",
}


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_draft_covers_all_categories_with_unique_ids():
    """The draft pool must span every planned category exactly once per item."""
    data = _load()
    ids = [item["id"] for item in data["items"]]
    assert len(ids) == len(set(ids))
    categories = {item["category"] for item in data["items"]}
    assert categories == _CATEGORIES
    assert set(data["meta"]["target_totals"]) == _CATEGORIES


def test_draft_items_declare_answerability_and_synthetic_corpus_policy():
    """Every item states answerability; corpus binding stays unbound (draft)."""
    data = _load()
    for item in data["items"]:
        assert isinstance(item["expected"]["answerable"], bool)
        # No draft item may claim a bound corpus reference yet: binding to
        # fixed artifact+generation versions happens after phase 3 parsing.
        assert item["expected"]["corpus_refs"] in ("TBD", [], "TBD:阶段3绑定 artifact_version+generation+unit")
    no_answer = [item for item in data["items"] if item["category"] == "no_answer"]
    assert no_answer and all(not item["expected"]["answerable"] for item in no_answer)
    assert "合成" in data["meta"]["corpus_policy"] or "公开" in data["meta"]["corpus_policy"]

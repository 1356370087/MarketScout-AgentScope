"""Focused contracts for the bounded canonical report parser."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from open_deep_research.report.canonical import (
    CanonicalizationLimits,
    canonicalize_report,
    runs_to_plain_text,
    validate_canonical_report,
)
from open_deep_research.report.models import (
    CanonicalReport,
    CodeBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
)


def test_max_chars_applies_to_source_markdown_not_canonical_metadata() -> None:
    markdown = "# T\n\n" + "x" * 90
    limits = CanonicalizationLimits(max_chars=len(markdown))

    report = canonicalize_report(
        markdown,
        run_id="r" * 128,
        sources=[
            {
                "title": "Source title",
                "url": "https://example.com/source-with-a-long-name",
            }
        ],
        limits=limits,
    )

    assert runs_to_plain_text(report.summary_blocks[0].runs) == "x" * 90
    with pytest.raises(ValueError, match="canonical_report_input_too_large"):
        canonicalize_report(
            markdown,
            run_id="r",
            limits=CanonicalizationLimits(max_chars=len(markdown) - 1),
        )


def test_nested_headings_lists_and_gfm_table_keep_content_and_order() -> None:
    markdown = """# Top

Intro.

### Findings

- Parent
  - Nested **bold** item
    1. Deep item
- Sibling

#### Evidence

| Source | Result |
| --- | --- |
| A | `pass` |
"""

    report = canonicalize_report(markdown, run_id="nested")

    assert [(section.title, section.level) for section in report.sections] == [
        ("Findings", 3),
        ("Evidence", 4),
    ]
    list_block = report.sections[0].blocks[0]
    assert isinstance(list_block, ListBlock)
    assert [runs_to_plain_text(item) for item in list_block.items] == [
        "Parent",
        "Nested bold item",
        "Deep item",
        "Sibling",
    ]
    assert list_block.items[1][1].bold is True
    table = report.sections[1].blocks[0]
    assert isinstance(table, TableBlock)
    assert [runs_to_plain_text(cell) for cell in table.headers] == [
        "Source",
        "Result",
    ]
    assert table.rows[0][1][0].code is True


@pytest.mark.parametrize(
    "heading",
    ["## 4. 参考资料", "### References and Further Reading"],
)
def test_numbered_and_extended_sources_sections_are_not_body_content(heading: str) -> None:
    markdown = (
        "# Report\n\nIntro.\n\n"
        f"{heading}\n\n- [Primary](https://example.com/source)\n"
    )

    report = canonicalize_report(
        markdown,
        run_id="sources-heading",
        sources=[{"title": "Primary", "url": "https://example.com/source"}],
    )

    assert [runs_to_plain_text(block.runs) for block in report.summary_blocks] == [
        "Intro."
    ]
    assert report.sections == []


def test_only_allowlisted_links_survive_and_remote_images_are_inert() -> None:
    markdown = """# Links

[Allowed](https://example.com/source#finding),
[Local](/documents/doc-1?chunk=chunk-2),
[Other](https://other.example/source), and
[Unsafe](javascript:alert(1)).

![Remote diagram](https://images.example/diagram.png)
"""
    report = canonicalize_report(
        markdown,
        run_id="links",
        sources=[
            {"title": "Allowed", "url": "https://example.com/source"},
            {
                "title": "Local",
                "url": "/documents/doc-1?chunk=chunk-2",
            },
        ],
    )

    paragraph = report.summary_blocks[0]
    assert isinstance(paragraph, ParagraphBlock)
    assert next(run for run in paragraph.runs if run.text == "Allowed").href == (
        "https://example.com/source#finding"
    )
    assert next(run for run in paragraph.runs if run.text == "Local").href == (
        "/documents/doc-1?chunk=chunk-2"
    )
    assert next(run for run in paragraph.runs if "Other" in run.text).href is None
    assert all(
        run.href is None or not run.href.startswith("javascript:")
        for run in paragraph.runs
    )
    payload = report.model_dump_json()
    assert "Remote diagram" in payload
    assert "https://images.example/diagram.png" not in payload


def test_active_html_is_removed_but_inline_and_fenced_code_are_preserved() -> None:
    fence = "`" * 3
    markdown = (
        "# Safe\n\n"
        "<script src=\"https://bad.example/x.js\">attack()</script>\n\n"
        "<div>Visible <b>plain text</b></div>\n\n"
        "Inline `<iframe src='https://bad.example'></iframe>`.\n\n"
        f"{fence}html\n<script>code sample</script>\n{fence}\n"
    )

    report = canonicalize_report(markdown, run_id="html")

    prose = [
        runs_to_plain_text(block.runs)
        for block in report.summary_blocks
        if isinstance(block, ParagraphBlock)
    ]
    assert all("attack" not in value for value in prose)
    assert "Visible plain text" in prose
    inline_code = next(run for block in report.summary_blocks if isinstance(block, ParagraphBlock) for run in block.runs if run.code)
    assert inline_code.text == "<iframe src='https://bad.example'></iframe>"
    code = next(block for block in report.summary_blocks if isinstance(block, CodeBlock))
    assert code.language == "html"
    assert code.text == "<script>code sample</script>\n"


def test_persisted_bundle_validation_is_strict_and_keeps_legacy_section_id() -> None:
    payload = {
        "schema_version": "1.0",
        "run_id": "persisted",
        "title": "Persisted",
        "sections": [
            {
                "section_id": "legacy-id",
                "title": "Section",
                "blocks": [
                    {
                        "type": "paragraph",
                        "runs": [{"text": "body"}],
                    }
                ],
            }
        ],
        "source_markdown_sha256": hashlib.sha256(b"body").hexdigest(),
    }

    report = CanonicalReport.model_validate(payload)
    validate_canonical_report(
        report,
        limits=CanonicalizationLimits(max_chars=len("Sectionbody")),
    )
    assert report.sections[0].id == "legacy-id"
    assert "section_id" not in report.model_dump()["sections"][0]

    payload["unexpected"] = "not part of schema 1.0"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CanonicalReport.model_validate(payload)


def test_persisted_bundle_rechecks_body_budget_and_link_allowlist() -> None:
    report = canonicalize_report(
        "# Report\n\n[Source](https://example.com/source)",
        run_id="persisted",
        sources=[{"title": "Source", "url": "https://example.com/source"}],
    )
    report.summary_blocks[0].runs[0].text = "x" * 11

    with pytest.raises(ValueError, match="canonical_report_input_too_large"):
        validate_canonical_report(
            report,
            limits=CanonicalizationLimits(max_chars=10),
        )

    report.summary_blocks[0].runs[0].text = "Source"
    report.summary_blocks[0].runs[0].href = "https://other.example/source"
    with pytest.raises(ValueError, match="canonical_report_link_not_allowed"):
        validate_canonical_report(report)

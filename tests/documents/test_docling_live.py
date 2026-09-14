"""Live integration against the deployed Docling Serve instance.

Runs only when ``DOCLING_SERVE_URL`` and ``DOCLING_SERVE_API_KEY`` are both
present in the environment (values live in the local .env, never in CI).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from open_deep_research.documents import docling, structuring
from open_deep_research.documents.settings import DocumentSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (
            os.environ.get("DOCLING_SERVE_URL", "").strip()
            and os.environ.get("DOCLING_SERVE_API_KEY", "").strip()
        ),
        reason="DOCLING_SERVE_URL/DOCLING_SERVE_API_KEY not configured",
    ),
]


_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _cjk_font_path() -> str | None:
    for candidate in _CJK_FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _finance_pdf(path: Path) -> Path:
    import fitz

    font_path = _cjk_font_path()
    document = fitz.open()
    page = document.new_page()
    if font_path:
        # A real CJK TTF carries a Unicode cmap; the built-in "china-s" font
        # extracts as placeholder dots through Docling's PDF backend.
        page.insert_font(fontname="cjk", fontfile=font_path)
        fontname = "cjk"
    else:
        fontname = "helv"
    page.insert_text((72, 90), "Starline 2025 Financial Report", fontsize=18, fontname=fontname)
    page.insert_text((72, 130), "Consolidated income statement (CNY thousand)", fontsize=11, fontname=fontname)
    rows = [
        "Item           FY2025       FY2024",
        "Revenue        4,860,000    4,210,000",
        "Cost of sales  3,343,000    2,956,000",
        "Gross margin   31.2%        29.8%",
    ]
    for index, row in enumerate(rows, 1):
        page.insert_text((72, 155 + index * 16), row, fontsize=10, fontname=fontname)
    page.insert_text((72, 260), "This report is audited.", fontsize=10, fontname=fontname)
    document.save(path)
    document.close()
    return path


async def test_live_docling_converts_finance_pdf_to_structured_units(tmp_path: Path):
    settings = DocumentSettings(
        docling_base_url=os.environ["DOCLING_SERVE_URL"].strip().rstrip("/"),
        docling_api_key=os.environ["DOCLING_SERVE_API_KEY"].strip(),
        docling_timeout_seconds=float(os.environ.get("DOCLING_TIMEOUT_SECONDS", "600")),
        docling_poll_seconds=2.0,
    )
    sample = _finance_pdf(tmp_path / "finance.pdf")
    submitted: list[str] = []

    async def on_submitted(task_id: str) -> None:
        submitted.append(task_id)

    task_id, result = await docling.convert(
        sample, "finance.pdf", "application/pdf", settings, on_submitted=on_submitted
    )
    assert submitted == [task_id]
    assert result.get("status") in {"success", "partial_success"}

    prepared = structuring.from_docling(result)
    prepared = structuring.plan_segments(prepared, settings)
    # Docling emits non-breaking spaces between extracted words.
    joined = "\n".join(unit.index_text for unit in prepared.units).replace(" ", " ")
    assert "Financial Report" in joined and "Revenue" in joined
    assert prepared.segment_texts and prepared.page_count == 1
    tables = [unit for unit in prepared.units if unit.unit_type == "table"]
    if tables:  # layout recognition may inline a text-only table
        assert tables[0].attributes["num_cols"] >= 2
    assert any("Revenue" in text.replace(" ", " ") for text in prepared.segment_texts)

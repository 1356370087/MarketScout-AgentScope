"""Phase-C sync and dedup tests (unit-level, no DB for pure functions)."""

from __future__ import annotations

from open_deep_research.knowledge.dedup import (
    DEFAULT_SIMILARITY_THRESHOLD,
    jaccard_similarity,
    normalize_text,
    shingle_hashes,
    text_hash,
)
from open_deep_research.knowledge.sync import _extract_visible_text, normalize_sync_url


class TestTextNormalization:
    def test_normalize_strips_whitespace_and_case(self):
        assert normalize_text("  Hello   World  ") == "helloworld"

    def test_normalize_strips_cjk_punctuation(self):
        assert normalize_text("营收，为48.6亿。") == "营收为486亿"

    def test_same_content_different_formatting_hashes_equal(self):
        """Layer 2: identical text despite formatting differences."""
        pdf_text = "营收 48.6亿\n毛利率 31.2%"
        web_text = "营收 48.6亿 毛利率 31.2%"
        assert text_hash(pdf_text) == text_hash(web_text)

    def test_different_content_hashes_differ(self):
        assert text_hash("营收 48.6亿") != text_hash("营收 49.1亿")


class TestShingleSimilarity:
    def test_identical_text_full_similarity(self):
        text = "This is a test document about competitive pricing analysis"
        shingles = shingle_hashes(text)
        assert jaccard_similarity(shingles, shingles) == 1.0

    def test_similar_text_above_threshold(self):
        """Near-duplicate: same topic, minor wording differences."""
        original = "星澜科技2025财年营收为48.6亿元同比增长15.4%毛利率31.2%"
        repost = "星澜科技2025财年营收为48.6亿元，同比增长15.4%，毛利率31.2%"
        # Only punctuation differs → should be near 1.0 after normalization
        similarity = jaccard_similarity(shingle_hashes(original), shingle_hashes(repost))
        assert similarity >= DEFAULT_SIMILARITY_THRESHOLD

    def test_different_reports_below_threshold(self):
        """Two quarterly reports (same template, different data) must NOT match."""
        q1 = "2024年第四季度营收为42.1亿元毛利率29.8%营业成本29.6亿研发费用3.2亿"
        q2 = "2025年第一季度营收为12.8亿元毛利率28.1%营业成本9.2亿研发费用1.1亿"
        similarity = jaccard_similarity(shingle_hashes(q1), shingle_hashes(q2))
        assert similarity < DEFAULT_SIMILARITY_THRESHOLD

    def test_shingles_bounded(self):
        long_text = "a" * 10000
        hashes = shingle_hashes(long_text)
        assert len(hashes) <= 512


class TestURLNormalization:
    def test_lowercase_scheme_host(self):
        assert normalize_sync_url("HTTPS://Example.COM/path") == "https://example.com/path"

    def test_strips_fragment(self):
        assert normalize_sync_url("https://example.com/page#section") == "https://example.com/page"


class TestHTMLTextExtraction:
    def test_strips_scripts_and_styles(self):
        html = b"<html><style>.x{color:red}</style><script>alert(1)</script><p>Hello</p></html>"
        assert "Hello" in _extract_visible_text(html)
        assert "alert" not in _extract_visible_text(html)
        assert "color" not in _extract_visible_text(html)

    def test_extracts_multiline(self):
        html = b"<div>Line 1</div><div>Line 2</div>"
        text = _extract_visible_text(html)
        assert "Line 1" in text and "Line 2" in text

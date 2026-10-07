"""Bounded web pipeline settings shared by fetch and extraction adapters."""

from dataclasses import dataclass


@dataclass(slots=True)
class WebPipelineSettings:
    """Bounded settings passed from the runtime configuration."""

    fetch_top_k: int = 5
    min_source_authority: float = 0.0
    max_fetches: int = 12
    global_concurrency: int = 4
    per_host_concurrency: int = 2
    timeout_seconds: float = 30.0
    max_redirects: int = 5
    html_max_bytes: int = 2 * 1024 * 1024
    pdf_max_bytes: int = 20 * 1024 * 1024
    pdf_max_pages: int = 100
    max_chunks_per_document: int = 3
    max_chunks_per_iteration: int = 20
    chunk_chars: int = 4000
    chunk_overlap_chars: int = 600
    respect_robots_txt: bool = True
    user_agent: str = "OpenDeepResearchBot/0.0.16"
    cache_namespace: str = "default"
    finite_corpus: bool = False

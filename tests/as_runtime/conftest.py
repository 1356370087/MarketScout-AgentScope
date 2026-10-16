"""原生运行时集成测试使用显式配置的独立远程 PostgreSQL 验收库。"""

from __future__ import annotations

import os

import pytest

@pytest.fixture(scope="session")
def pg_url() -> str:
    """数据库由外部维护；测试进程不创建或启动数据库容器。"""
    url = os.environ.get("AS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("请通过 AS_TEST_DATABASE_URL 指定独立远程 PostgreSQL 验收库")
    return url

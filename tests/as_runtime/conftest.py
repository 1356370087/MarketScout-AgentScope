"""AS 运行时测试共享夹具：一次性 PostgreSQL 容器（session 级）。"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

PG_CONTAINER = "as-m2-pgbus-test"
PG_PORT = 55433
PG_DSN = f"postgresql://postgres:probe@127.0.0.1:{PG_PORT}/postgres"
PG_URL = f"postgresql+asyncpg://postgres:probe@127.0.0.1:{PG_PORT}/postgres"


@pytest.fixture(scope="session")
def pg_url() -> str:
    proc = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", PG_CONTAINER,
         "-e", "POSTGRES_PASSWORD=probe", "-p", f"127.0.0.1:{PG_PORT}:5432",
         "postgres:16-alpine"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    import asyncpg

    async def _probe() -> None:
        conn = await asyncpg.connect(PG_DSN)
        await conn.close()

    for _ in range(60):
        try:
            asyncio.run(_probe())
            break
        except Exception:
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", PG_CONTAINER], capture_output=True)
        pytest.fail("postgres 容器 60s 未就绪")
    yield PG_URL
    subprocess.run(["docker", "rm", "-f", PG_CONTAINER], capture_output=True)

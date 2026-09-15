"""AS-T012/T013 PostgreSQL MessageBus 契约测试（在 .venv 新框架环境运行）。

运行方式（仓库根目录）：
    .venv/Scripts/python.exe -X utf8 -m pytest tests/as_runtime -q

断言基线来自 M1/T006 契约探针（InMemoryMessageBus 参考实现），外加 PG 特有验收：
- AS-A012：多消费者 drain 不重复；log 多游标；trim/TTL 契约。
- AS-A013：**真实 TTL 过期**（InMemory 缺失，AS-R021）；过期持有者不能解锁新持有者；
  CAS 不覆盖并发写；持久状态跨实例（进程死亡后新实例接手）。

需要本机 Docker（postgres:16-alpine）；容器在 fixture teardown 强制回收。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from open_deep_research.as_runtime.pgbus import PostgreSQLMessageBus

# pg_url 由 conftest.py 提供（session 级一次性容器）

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def bus(pg_url: str) -> PostgreSQLMessageBus:
    """每个测试独立表前缀，避免相互污染。"""
    prefix = f"as_bus_t{secrets_token()}__"
    bus = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus:
        yield bus


def secrets_token() -> str:
    import secrets

    return secrets.token_hex(4)


async def test_queue_atomic_disjoint_delivery(bus: PostgreSQLMessageBus) -> None:
    for i in range(30):
        await bus.queue_push("q1", {"n": i})
    results = await asyncio.gather(*(bus.queue_drain("q1", max_count=100) for _ in range(3)))
    seen = [e for r in results for e in r]
    ns = sorted(p["n"] for _, p in seen)
    ids = [eid for r in results for eid, _ in r]
    assert ns == list(range(30)) and len(ids) == len(set(ids))
    assert await bus.queue_drain("q1") == []  # ack-on-read：二次 drain 为空


async def test_queue_ttl_and_delete(bus: PostgreSQLMessageBus) -> None:
    await bus.queue_push("q-ttl", {"x": 1}, ttl_secs=1)
    await asyncio.sleep(1.3)
    assert await bus.queue_drain("q-ttl") == []
    await bus.queue_push("q-del", {"x": 2})
    await bus.queue_delete("q-del")
    assert await bus.queue_drain("q-del") == []


async def test_log_multi_cursor_non_destructive_and_trim(bus: PostgreSQLMessageBus) -> None:
    cursor = None
    for i in range(5):
        eid = await bus.log_append("log1", {"n": i})
        if i == 2:
            cursor = eid
    r_all = await bus.log_read("log1")
    r_cursor = await bus.log_read("log1", since=cursor)
    r_again = await bus.log_read("log1")
    assert [p["n"] for _, p in r_all] == [0, 1, 2, 3, 4]
    assert [p["n"] for _, p in r_cursor] == [3, 4]
    assert [p["n"] for _, p in r_again] == [0, 1, 2, 3, 4]  # 非破坏
    await bus.log_trim("log1", before_id=cursor)
    r3 = await bus.log_read("log1")
    assert [p["n"] for _, p in r3] == [2, 3, 4]  # 严格早于 before_id 删除、保留自身


async def test_log_ttl(bus: PostgreSQLMessageBus) -> None:
    await bus.log_append("log-ttl", {"x": 1}, ttl_secs=1)
    await asyncio.sleep(1.3)
    assert await bus.log_read("log-ttl") == []


async def test_lock_mutex_release_and_real_ttl(bus: PostgreSQLMessageBus) -> None:
    assert await bus.try_lock("k1", ttl_secs=60)
    assert not await bus.try_lock("k1", ttl_secs=60)
    assert await bus.is_locked("k1")
    await bus.unlock("k1")
    assert await bus.try_lock("k1", ttl_secs=60)
    await bus.unlock("k1")

    # 真实 TTL 过期（InMemory 实现缺失的语义；AS-R021 在此适配补齐）
    assert await bus.try_lock("k-exp", ttl_secs=1)
    await asyncio.sleep(1.3)
    assert not await bus.is_locked("k-exp")
    assert await bus.try_lock("k-exp", ttl_secs=60)  # 崩溃持有者不阻塞
    await bus.unlock("k-exp")


async def test_lock_expired_holder_cannot_unlock_successor(pg_url: str) -> None:
    """AS-A013：过期执行者（实例 A）不能解锁新持有者（实例 B）。"""
    prefix = f"as_bus_t{secrets_token()}__"
    bus_a = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    bus_b = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus_a:
        assert await bus_a.try_lock("fence", ttl_secs=1)
    # A"死亡"（连接关闭）；锁 TTL 过期
    await asyncio.sleep(1.3)
    async with bus_b:
        assert await bus_b.try_lock("fence", ttl_secs=60)  # B 夺取（新 token）
        # A 复活并尝试以旧身份解锁——best-effort unlock 只清过期锁或自己的 token
        async with bus_a:
            await bus_a.unlock("fence")
            assert await bus_b.is_locked("fence"), "旧执行者 unlock 不得释放新持有者的锁"


async def test_registry_cas_and_persistence_across_instances(pg_url: str) -> None:
    prefix = f"as_bus_t{secrets_token()}__"
    bus_a = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus_a:
        await bus_a.registry_set("ns", "f", "v1")
        assert not await bus_a.registry_set_if("ns", "f", "v2", expected="wrong")
        assert await bus_a.registry_get("ns", "f") == "v1"
        assert await bus_a.registry_set_if("ns", "f", "v2", expected="v1")
    # 进程死亡后新实例接手：registry 状态持久
    bus_b = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus_b:
        assert await bus_b.registry_get("ns", "f") == "v2"
        assert await bus_b.registry_pop("ns", "f") == "v2"
        assert await bus_b.registry_get("ns", "f") is None


async def test_queue_state_survives_instance_restart(pg_url: str) -> None:
    """业务命令不因 ack-on-read 之外的实例崩溃丢失：未 drain 的条目持久。"""
    prefix = f"as_bus_t{secrets_token()}__"
    bus_a = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus_a:
        await bus_a.queue_push("durable", {"cmd": 1})
    bus_b = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    async with bus_b:
        drained = await bus_b.queue_drain("durable")
        assert [p["cmd"] for _, p in drained] == [1]


async def test_broadcast_lossy_no_history(bus: PostgreSQLMessageBus) -> None:
    received: list[dict] = []
    ready = asyncio.Event()
    gen = bus.subscribe("chan", on_ready=ready.set)
    task = asyncio.create_task(_collect(gen, received))
    await ready.wait()
    await bus.publish("chan", {"live": 1})
    await asyncio.sleep(0.2)
    task.cancel()
    await bus.publish("chan", {"offline": 2})  # 断线后发布
    await asyncio.sleep(0.2)
    received_after: list[dict] = []
    gen2 = bus.subscribe("chan")
    task2 = asyncio.create_task(_collect(gen2, received_after))
    await asyncio.sleep(0.3)
    task2.cancel()
    assert [m.get("live") for m in received] == [1]
    assert received_after == []  # 无历史补发


async def test_derived_inbox_and_wakeup(bus: PostgreSQLMessageBus) -> None:
    await bus.inbox_push("sess-1", {"m": 1})
    await bus.inbox_push("sess-1", {"m": 2})
    drained = await bus.inbox_drain("sess-1")
    assert [p["m"] for _, p in drained] == [1, 2]
    assert await bus.inbox_drain("sess-1") == []
    await bus.enqueue_wakeup("u1", "sess-1", "agent-1")
    woken = await bus.dequeue_wakeups()
    assert len(woken) == 1 and woken[0]["session_id"] == "sess-1"


async def _collect(gen, sink: list[dict]) -> None:
    try:
        async for payload in gen:
            sink.append(payload)
    except (asyncio.CancelledError, StopAsyncIteration, GeneratorExit):
        return

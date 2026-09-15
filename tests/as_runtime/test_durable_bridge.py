"""AS-T014 持久命令桥测试（AS-A014 三要素 + 并发/非幂等分支）。

- ack-on-read 后崩溃不丢业务命令：信号队列被 drain 掉（甚至 delete），
  崩溃后新实例 recover 从持久表应用。
- 重投递不重复应用：同一 command_id 重复提交 + 重复 recover，副作用只发生一次。
- 广播丢失后从持久事实恢复：完全不发信号，仅靠 recover 应用。
- 并发消费者：两实例同时 apply_pending，副作用唯一（CAS 抢占）。
- 非幂等命令 applying 残留：转 quarantined，不自动重放。
"""

from __future__ import annotations

import secrets

import pytest
import pytest_asyncio

from open_deep_research.agentscope_runtime.durable import DurableCommandBridge
from open_deep_research.agentscope_runtime.pgbus import PostgreSQLMessageBus

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def bridge(pg_url: str) -> DurableCommandBridge:
    bridge = DurableCommandBridge(
        pg_url, table_prefix=f"as_bus_d{secrets.token_hex(4)}__"
    )
    async with bridge:
        yield bridge


async def test_command_survives_ack_on_read_crash(pg_url: str) -> None:
    """信号被 drain 消费后实例死亡 → 新实例 recover 不丢命令。"""
    prefix = f"as_bus_d{secrets.token_hex(4)}__"
    bus = PostgreSQLMessageBus(pg_url, table_prefix=prefix)
    bridge = DurableCommandBridge(pg_url, table_prefix=prefix, signal_queue=bus)
    applied: list[str] = []
    async with bus, bridge:
        await bridge.submit("run:1", "cmd-1", {"action": "start"})
        drained = await bus.queue_drain("durable:run:1")  # 信号被消费（ack-on-read）
        assert len(drained) == 1
        # 消费者拿到信号后、应用前崩溃——不调用 apply_pending

    # 新实例接手：仅靠持久事实恢复
    bridge2 = DurableCommandBridge(pg_url, table_prefix=prefix)

    async def applier(command_id: str, payload: dict) -> None:
        applied.append(command_id)

    async with bridge2:
        counts = await bridge2.recover("run:1", applier)
    assert counts["applied"] == 1 and applied == ["cmd-1"]


async def test_redelivery_applies_once(bridge: DurableCommandBridge) -> None:
    applied: list[str] = []

    async def applier(command_id: str, payload: dict) -> None:
        applied.append(command_id)

    await bridge.submit("run:2", "cmd-2", {"n": 1})
    await bridge.submit("run:2", "cmd-2", {"n": 1})  # 重复提交
    counts = await bridge.apply_pending("run:2", applier)
    counts2 = await bridge.recover("run:2", applier)  # 重复恢复
    assert applied == ["cmd-2"], f"副作用只发生一次：{applied}"
    assert counts["applied"] == 1 and counts2["applied"] == 0
    assert await bridge.state_of("cmd-2") == "applied"


async def test_broadcast_loss_recovered_from_persistent_facts(
    bridge: DurableCommandBridge,
) -> None:
    """完全不发信号（广播全丢）→ recover 仍从持久表应用。"""
    await bridge.submit("run:3", "cmd-3", {"x": 1}, signal=False)
    applied: list[str] = []

    async def applier(command_id: str, payload: dict) -> None:
        applied.append(command_id)

    counts = await bridge.recover("run:3", applier)
    assert counts["applied"] == 1 and applied == ["cmd-3"]


async def test_concurrent_consumers_single_side_effect(pg_url: str) -> None:
    prefix = f"as_bus_d{secrets.token_hex(4)}__"
    bridge_a = DurableCommandBridge(pg_url, table_prefix=prefix)
    bridge_b = DurableCommandBridge(pg_url, table_prefix=prefix)
    applied: list[str] = []
    import asyncio

    async def make_applier(tag: str):
        async def applier(command_id: str, payload: dict) -> None:
            await asyncio.sleep(0.02)  # 放大竞争窗口
            applied.append(f"{tag}:{command_id}")

        return applier

    async with bridge_a, bridge_b:
        for i in range(10):
            await bridge_a.submit("run:4", f"cmd-4-{i}", {"n": i}, signal=False)
        await asyncio.gather(
            bridge_a.apply_pending("run:4", await make_applier("A")),
            bridge_b.apply_pending("run:4", await make_applier("B")),
        )
    # 每条命令恰好一个消费者应用
    assert len(applied) == 10
    ids = sorted(x.split(":", 1)[1] for x in applied)
    assert len(set(ids)) == 10


async def test_non_idempotent_applying_residue_quarantined(pg_url: str) -> None:
    """非幂等命令执行中崩溃（applying 残留）→ 隔离待核对，不自动重放。"""
    prefix = f"as_bus_d{secrets.token_hex(4)}__"
    bridge = DurableCommandBridge(pg_url, table_prefix=prefix)
    async with bridge:
        await bridge.submit(
            "run:5", "cmd-5", {"side": "external"}, idempotent=False, signal=False
        )
        # 手工推进到 applying 模拟"副作用已发出但结果未确认"的崩溃窗口
        await bridge._set_state("cmd-5", "applying")

        calls: list[str] = []

        async def applier(command_id: str, payload: dict) -> None:
            calls.append(command_id)

        counts = await bridge.recover("run:5", applier)
        assert counts["quarantined"] == 1 and calls == []
        assert await bridge.state_of("cmd-5") == "quarantined"


async def test_applier_failure_returns_to_pending(bridge: DurableCommandBridge) -> None:
    calls: list[str] = []

    async def failing(command_id: str, payload: dict) -> None:
        calls.append(command_id)
        if len(calls) == 1:
            raise RuntimeError("transient")

    await bridge.submit("run:6", "cmd-6", {}, signal=False)
    counts = await bridge.apply_pending("run:6", failing)
    assert counts["failed"] == 1 and await bridge.state_of("cmd-6") == "pending"
    counts2 = await bridge.apply_pending("run:6", failing)  # 重试成功
    assert counts2["applied"] == 1 and await bridge.state_of("cmd-6") == "applied"


async def test_active_consumer_is_not_crash_residue(pg_url):
    import asyncio

    prefix = f"as_bus_d{secrets.token_hex(4)}__"
    async with (
        DurableCommandBridge(pg_url, table_prefix=prefix) as a,
        DurableCommandBridge(pg_url, table_prefix=prefix) as b,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(cid, payload):
            entered.set()
            await release.wait()
            calls.append(cid)

        await a.submit("race", "race-cmd", {}, idempotent=False, signal=False)
        task = asyncio.create_task(a.apply_pending("race", slow))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            result = await b.recover("race", slow)
            assert result["applied"] == result["quarantined"] == 0
            assert await b.state_of("race-cmd") == "applying"
        finally:
            release.set()
            await task
        assert calls == ["race-cmd"]


async def test_failed_batch_does_not_starve_later_commands(bridge):
    for i in range(3):
        await bridge.submit("batch", f"b{i}", {}, signal=False)
    applied = []

    async def apply(cid, payload):
        if cid == "b0":
            raise RuntimeError("retry later")
        applied.append(cid)

    result = await bridge.apply_pending("batch", apply, batch=1)
    assert result["failed"] == 1 and applied == ["b1", "b2"]


async def test_non_idempotent_error_is_quarantined(bridge):
    async def apply(cid, payload):
        raise RuntimeError("response lost after remote effect")

    await bridge.submit("unknown", "unknown", {}, idempotent=False, signal=False)
    await bridge.apply_pending("unknown", apply)
    assert await bridge.state_of("unknown") == "quarantined"


async def test_command_id_payload_conflict_rejected(bridge):
    await bridge.submit("key", "duplicate", {"n": 1}, signal=False)
    with pytest.raises(ValueError, match="different content"):
        await bridge.submit("key", "duplicate", {"n": 2}, signal=False)

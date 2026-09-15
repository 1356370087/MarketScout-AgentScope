"""持久命令桥（M2/AS-T014，实现 AS-D012）。

框架队列 ``queue_drain`` 是 ack-on-read（drain 即删，调用者崩溃即丢失），
与业务"应用后确认"不等价（M1/T006 契约实证）。本桥把关键业务命令的
**事实**落在 PostgreSQL：信号（队列/广播）只负责唤醒，崩溃后从持久表恢复。

状态机（对照 06 文档故障窗口 #2/#3/#6/#7）::

    pending ──CAS──> applying ──业务成功──> applied
       │                │
       └──(可重试失败)──┘└──崩溃残留：幂等→重放；非幂等→quarantined（待核对）

- ``command_id`` 全局唯一：已完成回执不重复执行；崩溃窗口依赖业务幂等键。
- 按命令键的 advisory 锁覆盖执行窗口；CAS 推进 pending→applying。
- ``applying`` 残留（执行中崩溃）按命令幂等性分流：幂等命令按同一
  command_id 重放（业务侧以 command_id 为幂等键）；非幂等命令转
  ``quarantined``，禁止自动重放，留待核对（不把结果不明的外部副作用
  当作未发生）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_DURABLE_DDL = """
CREATE TABLE IF NOT EXISTS {p}commands (
    id          BIGSERIAL PRIMARY KEY,
    command_key TEXT NOT NULL,
    command_id  TEXT NOT NULL UNIQUE,
    payload     JSONB NOT NULL,
    idempotent  BOOLEAN NOT NULL DEFAULT TRUE,
    state       TEXT NOT NULL DEFAULT 'pending',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {p}commands_key_state_idx ON {p}commands (command_key, state, id);
"""

Applier = Callable[[str, dict], Awaitable[None]]


class DurableCommandBridge:
    """PG 持久命令日志 + 幂等回执 + 崩溃恢复。"""

    def __init__(
        self,
        url: str,
        *,
        table_prefix: str = "as_bus_",
        engine_kwargs: dict[str, Any] | None = None,
        signal_queue: Any | None = None,
        auto_create: bool = True,
        gate: Any | None = None,
    ) -> None:
        """``signal_queue`` 为可选的 MessageBus（仅用于 best-effort 唤醒推送）。"""
        self._url = url
        self._prefix = table_prefix
        self._engine: AsyncEngine | None = None
        self._engine_kwargs = engine_kwargs or {}
        self._signal = signal_queue
        self.auto_create = auto_create
        self.gate = gate

    async def _ensure_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self._url, **self._engine_kwargs)
        return self._engine

    async def __aenter__(self) -> DurableCommandBridge:
        await self._ensure_engine()
        if self.auto_create:
            await self.create_tables()
        return self

    async def create_tables(self) -> None:
        engine = await self._ensure_engine()
        stmts = [
            s.strip()
            for s in _DURABLE_DDL.format(p=self._prefix).split(";")
            if s.strip()
        ]
        async with engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(text(stmt))

    async def __aexit__(self, *exc: object) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------------

    async def submit(
        self,
        command_key: str,
        command_id: str,
        payload: dict,
        *,
        idempotent: bool = True,
        signal: bool = True,
    ) -> str:
        if self.gate is not None:
            self.gate.begin()
        try:
            return await self._submit(
                command_key, command_id, payload, idempotent=idempotent, signal=signal
            )
        finally:
            if self.gate is not None:
                await self.gate.end()

    async def _submit(
        self,
        command_key: str,
        command_id: str,
        payload: dict,
        *,
        idempotent: bool,
        signal: bool,
    ) -> str:
        """登记命令事实；同一 ``command_id`` 重复提交幂等返回当前状态。

        信号（队列推送）失败不影响事实——恢复路径/下一信号仍会扫到该行。
        """
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        f"INSERT INTO {self._prefix}commands "
                        "(command_key, command_id, payload, idempotent) "
                        "VALUES (:k, :cid, CAST(:p AS JSONB), :idem) "
                        "ON CONFLICT (command_id) DO NOTHING "
                        "RETURNING state"
                    ),
                    {
                        "k": command_key,
                        "cid": command_id,
                        "p": json.dumps(payload),
                        "idem": idempotent,
                    },
                )
            ).first()
            if row:
                state = row[0]
            else:
                existing = (
                    await conn.execute(
                        text(
                            f"SELECT command_key, payload, idempotent, state FROM {self._prefix}commands "
                            "WHERE command_id = :cid"
                        ),
                        {"cid": command_id},
                    )
                ).one()
                if (existing[0], existing[1], existing[2]) != (
                    command_key,
                    payload,
                    idempotent,
                ):
                    raise ValueError("command_id reused with different content")
                state = existing[3]
        if signal and self._signal is not None:
            try:
                await self._signal.queue_push(
                    f"durable:{command_key}", {"command_id": command_id}
                )
                await self._signal.publish(
                    f"durable:{command_key}", {"command_id": command_id}
                )
            except Exception:
                pass  # 信号 best-effort；事实已持久
        return state

    async def _state_of(self, conn: Any, command_id: str) -> str:
        row = (
            await conn.execute(
                text(
                    f"SELECT state FROM {self._prefix}commands WHERE command_id = :cid"
                ),
                {"cid": command_id},
            )
        ).first()
        return row[0] if row else "missing"

    async def apply_pending(
        self, command_key: str, applier: Applier, *, batch: int = 50
    ) -> dict[str, int]:
        """同一命令键只有一个活跃消费者；锁随数据库连接/事务结束释放。

        不能仅凭 applying 状态判断进程死亡。事务级 advisory lock 覆盖整个
        执行窗口，取得锁后才能恢复残留。外部副作用仍要求 applier 按
        command_id 幂等；跨数据库与外部服务不承诺 exactly-once。
        """
        engine = await self._ensure_engine()
        async with engine.begin() as guard:
            acquired = await guard.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"{self._prefix}commands:{command_key}"},
            )
            if not acquired:
                return {"applied": 0, "replayed": 0, "quarantined": 0, "failed": 0}
            return await self._apply_owned(command_key, applier, batch=batch)

    async def _apply_owned(
        self, command_key: str, applier: Applier, *, batch: int
    ) -> dict[str, int]:
        """应用该键下全部 pending/applying 残留命令；返回计数摘要。

        - ``pending``：CAS 抢占为 applying 后执行。
        - ``applying`` 残留（执行中崩溃）：幂等命令重放（同一 command_id）；
          非幂等命令转 quarantined。
        - applier 抛错：状态回 pending（下轮/恢复再试），不吞业务异常的
          最终语义（调用方决定重试与告警）。
        """
        engine = await self._ensure_engine()
        counts = {"applied": 0, "replayed": 0, "quarantined": 0, "failed": 0}
        # 本调用内失败的命令不再重扫（避免永败 applier 死循环）；下次调用/恢复再试
        failed_here: set[str] = set()
        while True:
            async with engine.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            f"SELECT command_id, payload, idempotent, state "
                            f"FROM {self._prefix}commands "
                            "WHERE command_key = :k AND state IN ('pending', 'applying') "
                            "AND NOT (command_id = ANY(CAST(:failed AS TEXT[]))) "
                            "ORDER BY id LIMIT :n"
                        ),
                        {"k": command_key, "n": batch, "failed": list(failed_here)},
                    )
                ).fetchall()
            rows = [r for r in rows if r[0] not in failed_here]
            if not rows:
                break
            for command_id, payload, idempotent, state in rows:
                if state == "pending":
                    took = await self._cas(
                        command_id=command_id, frm="pending", to="applying"
                    )
                    if not took:
                        continue  # 其他消费者已抢占
                else:  # applying 残留
                    if not idempotent:
                        await self._set_state(command_id, "quarantined")
                        counts["quarantined"] += 1
                        continue
                    counts["replayed"] += 1
                if isinstance(payload, str):
                    payload = json.loads(payload)
                try:
                    await applier(command_id, dict(payload))
                    await self._set_state(command_id, "applied")
                    counts["applied"] += 1
                except Exception:
                    await self._set_state(
                        command_id, "pending" if idempotent else "quarantined"
                    )
                    failed_here.add(command_id)
                    counts["failed"] += 1
        return counts

    # 恢复入口与常规应用同一路径：持久表是唯一事实
    recover = apply_pending

    async def _cas(self, *, command_id: str, frm: str, to: str) -> bool:
        engine = await self._ensure_engine()
        async with engine.begin() as c:
            row = (
                await c.execute(
                    text(
                        f"UPDATE {self._prefix}commands SET state = :to, updated_at = now() "
                        "WHERE command_id = :cid AND state = :frm RETURNING command_id"
                    ),
                    {"to": to, "cid": command_id, "frm": frm},
                )
            ).first()
        return row is not None

    async def _set_state(self, command_id: str, state: str) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as c:
            await c.execute(
                text(
                    f"UPDATE {self._prefix}commands SET state = :s, updated_at = now() "
                    "WHERE command_id = :cid"
                ),
                {"s": state, "cid": command_id},
            )

    async def state_of(self, command_id: str) -> str:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            return await self._state_of(conn, command_id)

"""PostgreSQL 持久 MessageBus（M2/AS-T012、AS-T013）。

实现 agentscope ``MessageBus`` 的全部 20 个底层原语；inbox/wakeup/session/bg_task
等派生方法由基类基于这些原语提供。设计要点（对照 M1/T006 契约探针）：

- **queue**：``DELETE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING``
  单语句原子 drain——多消费者拿到不相交条目，drain 即删除（ack-on-read，
  at-most-once，与契约文档一致；业务可靠投递须配合持久回执，AS-T014）。
- **log**：单调 ``entry_id``（``{seq:020d}-{rand}``），``since`` 游标按 seq 严格
  更新比较；读非破坏、多读者各自游标；trim 按 entry_id 严格更早删除；
  TTL 惰性过滤 + 读写路径顺带清理。
- **lock**：``INSERT ... ON CONFLICT DO UPDATE ... WHERE 过期`` 原子夺取；
  持有者 token 解锁（过期/易主后旧持有者无法解锁新持有者）；**真实 TTL 过期**
  （AS-R021：InMemory 实现缺失的语义在此补齐）。
- **registry**：主键 (namespace, field)；``set_if`` 为 CAS 条件更新。
- **publish/subscribe**：进程内广播（单进程内唤醒用）；跨进程广播由
  RocketMQ 适配承担（AS-T014），契约本就允许瞬态。

表名带可配置前缀（默认 ``as_bus_``），不与业务表或框架表同名。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from agentscope.app.message_bus import MessageBus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_BUS_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS {p}queue (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL,
    payload    JSONB NOT NULL,
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS {p}queue_key_idx ON {p}queue (key, id);

CREATE TABLE IF NOT EXISTS {p}log (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL,
    entry_id   TEXT UNIQUE,
    payload    JSONB NOT NULL,
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS {p}log_key_idx ON {p}log (key, id);

CREATE TABLE IF NOT EXISTS {p}locks (
    key        TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS {p}registry (
    namespace  TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (namespace, field)
);
"""


def _entry_id(seq: int) -> str:
    return f"{seq:020d}-{secrets.token_hex(4)}"


def _seq_of(entry_id: str) -> int:
    try:
        return int(entry_id.split("-", 1)[0])
    except (ValueError, IndexError):
        return -1


class PostgreSQLMessageBus(MessageBus):
    """PostgreSQL 持久总线；继承基类获得 inbox/wakeup/session/bg_task 派生方法。"""

    def __init__(self, url: str, *, table_prefix: str = "as_bus_",
                 engine_kwargs: dict[str, Any] | None = None,
                 broadcast: Any | None = None, auto_create: bool = True) -> None:
        self._url = url
        self._prefix = table_prefix
        self._engine: AsyncEngine | None = None
        self._engine_kwargs = engine_kwargs or {}
        self.broadcast = broadcast
        self.auto_create = auto_create
        # 本实例持有的锁 token（过期/易主后旧 token 无法解锁）
        self._held: dict[str, str] = {}
        # 进程内广播（publish/subscribe 契约：仅在线订阅者、无历史）
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _ensure_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self._url, **self._engine_kwargs)
        return self._engine

    async def create_tables(self) -> None:
        engine = await self._ensure_engine()
        # asyncpg 预编译不允许一次执行多条 DDL，逐条下发。
        statements = [s.strip() for s in _BUS_SCHEMA_DDL.format(p=self._prefix).split(";") if s.strip()]
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))

    async def __aenter__(self) -> PostgreSQLMessageBus:
        await self._ensure_engine()
        if self.auto_create:
            await self.create_tables()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for key in list(self._held):
            await self.unlock(key)
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._subscribers.clear()

    # ------------------------------------------------------------------
    # queue：原子 drain / at-most-once
    # ------------------------------------------------------------------

    async def queue_push(self, key: str, payload: dict, *, ttl_secs: int | None = None) -> str:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"INSERT INTO {self._prefix}queue (key, payload, expires_at) "
                    "VALUES (:k, CAST(:p AS JSONB), "
                    "CASE WHEN CAST(:ttl AS int) IS NULL THEN NULL ELSE now() + make_interval(secs => CAST(:ttl AS int)) END) "
                    "RETURNING id"
                ),
                {"k": key, "p": json.dumps(payload), "ttl": ttl_secs},
            )).first()
            return str(row[0])

    async def queue_drain(self, key: str, max_count: int = 100) -> list[tuple[str, dict]]:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text(
                    f"DELETE FROM {self._prefix}queue WHERE id IN ( "
                    f"  SELECT id FROM {self._prefix}queue "
                    "  WHERE key = :k AND (expires_at IS NULL OR expires_at > now()) "
                    "  ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED "
                    ") RETURNING id, payload"
                ),
                {"k": key, "n": max_count},
            )).fetchall()
            return [(str(r[0]), r[1]) for r in rows]

    async def queue_delete(self, key: str) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {self._prefix}queue WHERE key = :k"), {"k": key}
            )

    # ------------------------------------------------------------------
    # log：多游标非破坏读 / trim / TTL
    # ------------------------------------------------------------------

    async def log_append(self, key: str, payload: dict, *, ttl_secs: int | None = None,
                         max_len: int | None = None) -> str:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"INSERT INTO {self._prefix}log (key, payload, expires_at) "
                    "VALUES (:k, CAST(:p AS JSONB), "
                    "CASE WHEN CAST(:ttl AS int) IS NULL THEN NULL ELSE now() + make_interval(secs => CAST(:ttl AS int)) END) "
                    "RETURNING id"
                ),
                {"k": key, "p": json.dumps(payload), "ttl": ttl_secs},
            )).first()
            entry_id = _entry_id(row[0])
            await conn.execute(
                text(f"UPDATE {self._prefix}log SET entry_id = :e WHERE id = :id"),
                {"e": entry_id, "id": row[0]},
            )
            if max_len is not None:
                await conn.execute(
                    text(
                        f"DELETE FROM {self._prefix}log WHERE key = :k AND id NOT IN ( "
                        f"  SELECT id FROM {self._prefix}log WHERE key = :k "
                        f"  ORDER BY id DESC LIMIT :n)"
                    ),
                    {"k": key, "n": max_len},
                )
            return entry_id

    async def log_read(self, key: str, since: str | None = None,
                       max_count: int = 100) -> list[tuple[str, dict]]:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            if since is None:
                rows = (await conn.execute(
                    text(
                        f"SELECT entry_id, payload FROM {self._prefix}log "
                        "WHERE key = :k AND (expires_at IS NULL OR expires_at > now()) "
                        "ORDER BY id LIMIT :n"
                    ),
                    {"k": key, "n": max_count},
                )).fetchall()
            else:
                rows = (await conn.execute(
                    text(
                        f"SELECT entry_id, payload FROM {self._prefix}log "
                        "WHERE key = :k AND id > :seq "
                        "  AND (expires_at IS NULL OR expires_at > now()) "
                        "ORDER BY id LIMIT :n"
                    ),
                    {"k": key, "seq": _seq_of(since), "n": max_count},
                )).fetchall()
            return [(r[0], r[1]) for r in rows]

    async def log_trim(self, key: str, before_id: str | None = None) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            if before_id is None:
                await conn.execute(
                    text(f"DELETE FROM {self._prefix}log WHERE key = :k"), {"k": key}
                )
            else:
                # 契约：删除严格早于 before_id 的条目，保留 before_id 自身
                await conn.execute(
                    text(
                        f"DELETE FROM {self._prefix}log "
                        "WHERE key = :k AND id < :seq"
                    ),
                    {"k": key, "seq": _seq_of(before_id)},
                )

    # ------------------------------------------------------------------
    # lock：真实 TTL 租约 + 持有者 token
    # ------------------------------------------------------------------

    async def try_lock(self, key: str, *, ttl_secs: int = 600) -> bool:
        token = uuid.uuid4().hex
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"INSERT INTO {self._prefix}locks (key, holder, expires_at) "
                    "VALUES (:k, :token, now() + make_interval(secs => :ttl)) "
                    "ON CONFLICT (key) DO UPDATE "
                    "SET holder = :token, expires_at = now() + make_interval(secs => :ttl) "
                    f"WHERE {self._prefix}locks.expires_at <= now() "
                    "RETURNING key"
                ),
                {"k": key, "token": token, "ttl": ttl_secs},
            )).first()
            if row is not None:
                self._held[key] = token
                return True
            return False

    async def unlock(self, key: str) -> None:
        token = self._held.pop(key, None)
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            # 未记录 token 时按当前持有者删除（best-effort，与契约一致）；
            # 过期锁顺带清理。
            if token is None:
                await conn.execute(
                    text(
                        f"DELETE FROM {self._prefix}locks "
                        "WHERE key = :k AND (holder IS NOT NULL AND expires_at <= now())"
                    ),
                    {"k": key},
                )
            else:
                await conn.execute(
                    text(
                        f"DELETE FROM {self._prefix}locks "
                        "WHERE key = :k AND holder = :token"
                    ),
                    {"k": key, "token": token},
                )

    async def is_locked(self, key: str) -> bool:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"SELECT 1 FROM {self._prefix}locks "
                    "WHERE key = :k AND expires_at > now()"
                ),
                {"k": key},
            )).first()
            return row is not None

    @asynccontextmanager
    async def acquire_lock(self, key: str, *, ttl_secs: int = 600) -> AsyncGenerator[None, None]:
        while not await self.try_lock(key, ttl_secs=ttl_secs):
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            await self.unlock(key)

    # ------------------------------------------------------------------
    # registry：条件更新（CAS）
    # ------------------------------------------------------------------

    async def registry_set(self, namespace: str, field: str, value: str,
                           *, ttl_secs: int | None = None) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {self._prefix}registry (namespace, field, value, expires_at) "
                    "VALUES (:ns, :f, :v, "
                    "CASE WHEN CAST(:ttl AS int) IS NULL THEN NULL ELSE now() + make_interval(secs => CAST(:ttl AS int)) END) "
                    "ON CONFLICT (namespace, field) DO UPDATE "
                    "SET value = :v, expires_at = "
                    "CASE WHEN CAST(:ttl AS int) IS NULL THEN NULL ELSE now() + make_interval(secs => CAST(:ttl AS int)) END"
                ),
                {"ns": namespace, "f": field, "v": value, "ttl": ttl_secs},
            )

    async def registry_get(self, namespace: str, field: str) -> str | None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"SELECT value FROM {self._prefix}registry "
                    "WHERE namespace = :ns AND field = :f "
                    "  AND (expires_at IS NULL OR expires_at > now())"
                ),
                {"ns": namespace, "f": field},
            )).first()
            return row[0] if row else None

    async def registry_set_if(self, namespace: str, field: str, value: str, *,
                              expected: str, ttl_secs: int | None = None) -> bool:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"UPDATE {self._prefix}registry SET value = :v, expires_at = "
                    "CASE WHEN CAST(:ttl AS int) IS NULL THEN NULL ELSE now() + make_interval(secs => CAST(:ttl AS int)) END "
                    "WHERE namespace = :ns AND field = :f AND value = :expected "
                    "  AND (expires_at IS NULL OR expires_at > now()) "
                    "RETURNING value"
                ),
                {"ns": namespace, "f": field, "v": value, "expected": expected, "ttl": ttl_secs},
            )).first()
            return row is not None

    async def registry_del(self, namespace: str, field: str) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"DELETE FROM {self._prefix}registry WHERE namespace = :ns AND field = :f"
                ),
                {"ns": namespace, "f": field},
            )

    async def registry_drop(self, namespace: str) -> None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {self._prefix}registry WHERE namespace = :ns"),
                {"ns": namespace},
            )

    async def registry_exists(self, namespace: str, field: str) -> bool:
        return await self.registry_get(namespace, field) is not None

    async def registry_pop(self, namespace: str, field: str) -> str | None:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    f"DELETE FROM {self._prefix}registry "
                    "WHERE namespace = :ns AND field = :f "
                    "  AND (expires_at IS NULL OR expires_at > now()) "
                    "RETURNING value"
                ),
                {"ns": namespace, "f": field},
            )).first()
            return row[0] if row else None

    async def registry_getall(self, namespace: str) -> dict[str, str]:
        engine = await self._ensure_engine()
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text(
                    f"SELECT field, value FROM {self._prefix}registry "
                    "WHERE namespace = :ns "
                    "  AND (expires_at IS NULL OR expires_at > now())"
                ),
                {"ns": namespace},
            )).fetchall()
            return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------
    # publish/subscribe：进程内瞬态广播（跨进程广播见 AS-T014 RocketMQ 适配）
    # ------------------------------------------------------------------

    async def publish(self, key: str, payload: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast.publish("wake", {"key": key, "payload": payload})
            return
        for q in list(self._subscribers.get(key, [])):
            q.put_nowait(payload)

    def subscribe(self, key: str, on_ready=None):
        if self.broadcast is not None:
            async def remote():
                stream = self.broadcast.subscribe("wake", on_ready=on_ready)
                try:
                    async for item in stream:
                        if item.get("key") == key:
                            yield item["payload"]
                finally:
                    await stream.aclose()
            return remote()
        bus = self

        async def gen() -> AsyncGenerator[dict, None]:
            q: asyncio.Queue = asyncio.Queue()
            ready = asyncio.Event()

            def _mark() -> None:
                ready.set()

            bus._subscribers[key].append(q)
            try:
                if on_ready is not None:
                    on_ready()
                else:
                    ready.set()
                while True:
                    item = await q.get()
                    yield item
            finally:
                lst = bus._subscribers.get(key, [])
                if q in lst:
                    lst.remove(q)

        return gen()

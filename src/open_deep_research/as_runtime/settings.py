"""AgentScope 运行时配置（M2/AS-T010、AS-T011）。

全部配置来自环境变量（不读取真实 ``.env``，由部署层注入）；
说明见 ``.env.example`` 的 AgentScope 运行时段。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 框架 SQL 存储在业务库中的独立 schema（AS-D009；表名 knowledge_bases 与业务表冲突已实证）
DEFAULT_RUNTIME_SCHEMA = "agentscope_runtime"


@dataclass(frozen=True)
class ASRuntimeSettings:
    """AS 运行时装配配置。

    环境变量（均带 ``AS_`` 前缀）：

    - ``AS_DATABASE_URL``：框架 SQL 存储连接串（``postgresql+asyncpg://``）。
      未设置时进入演示模式（本地 SQLite 文件，``.runs/agentscope-runtime.db``）。
    - ``AS_DATABASE_SCHEMA``：框架表所在 PostgreSQL schema，默认
      ``agentscope_runtime``。业务表保持原 schema，互不影响。
    - ``AS_STORAGE_AUTO_CREATE``：是否允许启动时自动建表/隐式迁移。
      仅开发/演示允许（``true``）；生产必须 ``false`` 并走显式迁移命令
      （AS-T011）。
    - ``AS_BUS_TABLE_PREFIX``：持久总线表名前缀，默认 ``as_bus_``，
      避免与业务表或框架表同名。
    """

    database_url: str | None
    database_schema: str
    storage_auto_create: bool
    bus_table_prefix: str
    rocketmq_endpoint: str = ""
    rocketmq_topic_prefix: str = "insightforge_as"
    rocketmq_group: str = ""
    rocketmq_access_key: str = ""
    rocketmq_secret_key: str = ""
    rocketmq_tls: bool = False
    drain_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> ASRuntimeSettings:
        return cls(
            database_url=os.environ.get("AS_DATABASE_URL") or None,
            database_schema=os.environ.get("AS_DATABASE_SCHEMA")
            or DEFAULT_RUNTIME_SCHEMA,
            storage_auto_create=(
                os.environ.get("AS_STORAGE_AUTO_CREATE", "").lower()
                in ("1", "true", "yes")
            ),
            bus_table_prefix=os.environ.get("AS_BUS_TABLE_PREFIX") or "as_bus_",
            rocketmq_endpoint=os.environ.get("AS_ROCKETMQ_ENDPOINT", ""),
            rocketmq_topic_prefix=os.environ.get(
                "AS_ROCKETMQ_TOPIC_PREFIX", "insightforge_as"
            ),
            rocketmq_group=os.environ.get("AS_ROCKETMQ_GROUP", ""),
            rocketmq_access_key=os.environ.get("AS_ROCKETMQ_ACCESS_KEY", ""),
            rocketmq_secret_key=os.environ.get("AS_ROCKETMQ_SECRET_KEY", ""),
            rocketmq_tls=os.environ.get("AS_ROCKETMQ_TLS", "").lower() in ("true", "1"),
            drain_timeout=float(os.environ.get("AS_DRAIN_TIMEOUT_SECONDS", "30")),
        )

    @property
    def is_demo(self) -> bool:
        """演示模式：无 PostgreSQL，使用本地 SQLite 与进程内组件。"""
        return not self.database_url

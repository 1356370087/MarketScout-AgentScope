"""AgentScope 运行时接入层（M2 服务底座）。

本包把 AgentScope 2.0.8 的服务组件装配进 InsightForge：

- ``settings``：AS 运行时配置（环境变量驱动）。
- ``storage``：隔离的原生服务存储（PostgreSQL 独立 schema / 演示 SQLite）。
- ``pgbus``：PostgreSQL 持久 MessageBus（queue/log/lock/registry 全契约原语）。
- ``identity``：JWT principal 覆盖原生 X-User-ID 依赖（AS-D010）。
- ``app``：应用组合入口（create_app 装配 + 兼容 API 外壳挂载点）。

本包只依赖新框架环境（agentscope + sqlalchemy + asyncpg），不导入旧运行时的
LangChain 路径；旧 ``server:app`` 入口在 M11 前保持不变（AS-T010 兼容外壳）。
"""

from open_deep_research.as_runtime.settings import ASRuntimeSettings
from open_deep_research.as_runtime.storage import build_storage

__all__ = ["ASRuntimeSettings", "build_storage"]

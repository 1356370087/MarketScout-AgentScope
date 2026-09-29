# 默认 HTTP 入口及原生生命周期清退（2026-09-21）

本批推进完整 QueryEngine 清退目标，仍不登记整体完成。默认入口已切为原生，旧循环及残余模型/工具桥接源码尚需继续移除。

## 已改变的运行行为

- `server:app` 只装配 NativeRuns；默认 `RESEARCH_ENGINE=native`，legacy 配置明确报错。研究路由在生命周期重新绑定时替换旧绑定，避免重复注册。原 `api_host` 11 个模块已删除。
- 运行所有权、任务归属、内部 Gateway 账本和安全审批从 SQL 原生服务解析；审批发布原生公共事件，不再更新旧进程任务表。
- DELETE 保留 dry_run、force 和管理员操作。跨用户请求不可见；活动运行先取消；有效租约和待发布/发布中的工件阻止清理。Run Key 撤销失败保留密文及运行以供重试。清理使用已有 SQL fence，删除原生回执/决定/事件、领域团队记录、AgentScope 团队与 Agent 会话、资料绑定、追踪和目录。
- 保留策略依据 SQL 终态事件时间而非创建时间，优先回收较旧的终态运行；活动运行和历史只读归档不会被该策略删除。配额不足且没有可回收数据时报告未达目标。Trace 保留只清理已确认属于原生终态的记录。
- 发布重试也持有运行租约，避免删除与重新入队竞争。服务关闭先停止维护任务再关闭原生存储；启动中途失败进入同一资源释放路径。
- 请求 ID 与允许的请求 metadata 进入持久状态和管线配置，补齐旧入口曾提供的关联能力。

## 验证与测试迁移

| 证据 | 范围和结果 |
|---|---|
| [联合回归](../evidence/native-server-verified-20260921.xml) | 92 passed / 92 warnings，含正式审批接口、跨用户拒绝、旧租约拒绝、SQL 事件、创建限额、断连释放、恢复、保留、报告交付及生命周期 |
| [PostgreSQL](../evidence/native-retention-pg-20260921.xml) | 1 passed / 5 warnings；使用已发布迁移创建专用 schema，实际清除团队/框架记录和回执，保留另一用户的团队与 Agent |
| [真实进程](../evidence/native-entry-process-20260921.xml) | 2 passed / 1 warning；实际 HTTP/SSE socket，含正式 server:app 组合。验证创建、429、断连释放、审批、优雅停机、重启自动完成、删除预览/执行及 404。研究阶段为确定性夹具，未调用外部模型 |
| [路由盘点](../evidence/native-entry-routes-20260921.json) | 170/170 原方法与路径保留，增加 5 个原生接口；不代表全部 schema 字段等价 |
| [原生静态检查](../evidence/native-server-imports-20260921.json) | 通过；独立解释器测试还导入正式 server，确认没有加载 LangChain/QueryEngine |
| [剩余清退](../evidence/retirement-after-server-20260921.json) | 仍有 72 处 LangChain 与 11 处旧引擎导入，不声明清退完成 |

旧 `RunRecord`、文件租约 sweep 和 `DockerSandboxManager` 专用测试退出：终态显示由 SQL 快照测试替代；停机/恢复由 `test_native_startup_recovery.py` 与真实进程测试替代；删除/保留/配额/追踪由 `test_native_retention.py` 及 PostgreSQL 测试替代；运行限额由 `test_native_admission.py` 替代；凭据隔离继续由 `test_production_resources.py` 覆盖。没有通过重新暴露旧执行器来维持测试调用方式。

Ruff F 检查通过。测试中的配置、旧导入和真实流迭代器问题在迁移过程中暴露并修正；最终结果以上述证据为准。专用 `as-m2-pgbus-test` 容器已回收，真实 HTTP 子进程均已退出，未启动其他既有容器。

## 继续工作

剩余旧 Agent 循环、任务执行器、模型调用栈、工具桥接、旧专用测试及过渡依赖仍需移除；默认 Compose 资源接线和完整新版本部署验收还需收口。当前 host 资源模式只接受本地工具，Web 研究必须配置 Gateway；不能回退旧引擎或绕过治理。

整体台账保持 67/81，T080/T081 不因默认入口的代码切换提前完成。内容质量评价仍不扣减进度；质量门禁底层故障仍属于修复范围。

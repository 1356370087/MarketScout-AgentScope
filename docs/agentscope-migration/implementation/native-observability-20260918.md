# 原生可观测性迁移与统计修复

## 核查结论

本轮前，Langfuse/OTel/Prometheus 的实现位于旧 TraceRecorder，已有 Collector、Jaeger、Prometheus、Grafana Compose profile；原生 AgentScope 没有把这些设施完整接到研究生命周期。配置映射保留不等于真实接入。上轮 Docker E2E 没有启动观测栈，也不能作为 Grafana/Langfuse 验收证据。

本轮只读取开关确认当前本地 `.env` 中 `LANGFUSE_ENABLED=false`、`OTEL_ENABLED=false`、`PROMETHEUS_ENABLED=false`；未修改真实配置。新看板和导出适配已编码，尚未在用户真实 Langfuse/Grafana 服务上做验收。

## 本轮实现

| 迁移项 | 实现与状态 |
|---|---|
| AgentScope 生命周期 | 原生 Middleware reply hook，加上恢复层 model/tool 操作 span；父子关系保留。使用内容最小化自定义 Middleware，不直接使用会序列化输入输出的 SDK 默认 TracingMiddleware |
| OTel / Langfuse | 独立进程级 TracerProvider，OTLP/HTTP 双目标导出；Langfuse 使用 Basic 认证及 v4 ingestion header。使用受信服务环境，不接受用户运行参数指定导出凭据。未调用 LangChain callback |
| 数据最小化 | 导出运行 ID、任务 ID、角色、阶段、用量与异常类型；不导出提示词、工具参数、研究正文、凭据或原始异常文本 |
| SQL 观测维度 | 在首次领取操作时持久化内容无关的 started 事件，已有 committed 事件提供结束时间；不修改既有回执内容，不新增数据库表。重放不重复记数 |
| 浏览器用量 | 角色/阶段/模型/任务分组、最多 120 个时间桶、真实工具成功/失败、任务调用计数；远端工具只统计物理 Gateway 回执，跳过未计费的宿主镜像回执 |
| 预算轨道 | 补齐工具调用、网页抓取中文标签；未知维度同时在可见文本与无障碍标签回退为键名，消除 undefined |
| 运行中与历史记录 | 未结束调用不冒充未知失败；无已结算工具结果时显示暂无数据。旧回执缺少维度明确归 unknown，不伪造历史归属 |
| Prometheus / Grafana | API metrics 从共享 SQL 读取持久化统计，覆盖独立 Worker，不依赖 Worker 的进程内计数器。新增原生仪表盘及固定数据源 UID，无 run/task 高基数标签 |

Prometheus 原生指标为持久化快照 Gauge：`insightforge_native_runs`、`insightforge_native_operations`、`insightforge_native_tokens`、`insightforge_native_tools`。不能把这些 Gauge 当作进程 Counter 使用 rate；tokens 包含保守估算，财务核算仍以 SQL 回执与供应商账单为准。

## 配置与运维

1. API/原生 Worker 使用 `OTEL_ENABLED=true`、`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`；分别配置 `OTEL_SERVICE_NAME`。默认开关保持关闭，不改用户 `.env`。
2. 需要直接进入 Langfuse 的可信进程配置 `LANGFUSE_ENABLED=true`、公钥、私钥和 `LANGFUSE_BASE_URL`；Worker 可只配置 Collector，避免分发 Langfuse 凭据。Collector 向 Langfuse 的集中转发需另行配置，当前既有 Collector 默认转发 Jaeger。
3. `observability` profile 中仅按需启动 `prometheus grafana otel-collector jaeger`；不启动无关已停止服务。Grafana 自动加载“InsightForge · AgentScope 原生运行”。
   API 需同时启用 `OBSERVABILITY_ENABLED=true` 与 `PROMETHEUS_ENABLED=true` 才在 metrics 接口追加原生 SQL 指标。
4. 正常运行资源释放时限时 flush；进程强杀仍可能丢失未导出的 sampled spans，持久化 SQL 指标与计账不受此影响。

本轮未声称生产 Langfuse 账户已验收、跨进程单一 trace 已连通或 Grafana 已展示真实生产流量。当前跨进程按 run/task ID 关联；独立 Worker trace 的父上下文跨 MQ/HTTP 传播仍需后续专项验收。未改异步调度拓扑。

## 验证

真实 AgentScope Agent hook、内存 span 导出、真实本地 OTLP/HTTP 接收端双目标传输、SQL 汇总、历史未知维度、权限隔离、重复结算/抓取、120 桶守恒均有回归。前端覆盖预算中文/未知标签、暂无工具统计、任务卡片模型与工具计数。

本轮组合回归为 **138 passed / 4 warnings**（既有 Pydantic 弃用提示），既有可观测性治理兼容回归 **7 passed**，前端 **9 passed**，TypeScript 检查通过。Grafana JSON 可解析且四个面板均引用已实现的指标。OTLP 接收端为测试启动的本地 HTTP 服务，并非真实 Langfuse 账户验收；测试服务已关闭，本轮未启动 Docker 观测栈。

官方参考：[Langfuse OTLP 接口](https://langfuse.com/integrations/native/opentelemetry)。本地 AgentScope 2.x 的 MiddlewareBase 与 TracingMiddleware 源码作为 hook 协议依据。

# MarketScout 容器重新部署与独立 E2E 复验

本轮按用户要求重新构建镜像、创建容器，并对当前修复版本再次执行真实模型与联网研究。业务代码与上一轮最终修复版本一致；原有工作区改动和历史试验均保留。

## 部署与试验条件

- 重建 API、前端、LiteLLM、Gateway、Controller、Sandbox Worker、Publisher 共 7 个镜像；重建并启动 8 个所需服务容器，名称使用 MarketScout。
- 数据库使用 `172.22.208.30:5432` 的既有独立验收库，连接、扩展与 `0019_platform_management` 迁移检查通过。没有启动本地 PostgreSQL 容器。
- API、Gateway、Controller、Publisher 的 404 个 Python 源码文件与工作区逐文件匹配。Publisher 和 Controller 保持无网络模式。
- 生效配置版本：`30ecd13d-50ff-4d56-8660-0f1f8560982c`，API、LiteLLM、Sandbox 均 applied。本次发布仅更新重建后的 Worker 镜像摘要。
- 研究题目继续使用 Kubernetes 原生 Sidecar 生产迁移；模型为原 `zai/glm-5.3-flash` 路由，quality-gate-v6/balanced，两个 fail-open 均关闭。期限 1,500 秒，单轮费用上限 1 美元。
- 唯一新建运行：`0c1adf599995445bba15ad1830d2d950`。结果与工件单独保存在 `.runs/redeploy-e2e-20261016/`。

## 浏览器与观测情况

实际浏览器通过 Nginx、前端 BFF 与 IAM 完成登录。研究详情页可打开，工作台与管理后台切换保留登录；从后台返回时进入同一研究详情，服务器研究继续执行。SSE 请求在切换前后分别使用 `after=64`、`after=204`，均返回 200。

刷新测试出现详情接口 500，API 日志显示 `sqlalchemy.exc.InterfaceError / asyncpg InterfaceError: connection is closed`，涉及恢复库的 `as_recovery_runs`、`as_recovery_outbox` 查询。页面显示连接异常。手动重试后详情和事件流恢复，SSE 使用 `after=413` 返回 200；这次失败没有被计为一次无异常通过。

原始观测脚本也因 HTTP 连接中断退出，堆栈最终停在清理阶段的 cancel 请求。重新查询确认服务器上的同一研究仍在运行，随后恢复了对原运行的观察。原脚本日志和退出前工件单独保留；临时观察器只为只读 GET 增加至多 3 次重试，未重发创建研究、模型或工具调用。原研究的期限与费用上限没有延长。

浏览器证据位于 `output/playwright/marketscout-redeploy-20261016/`；数据库错误与观测器日志保存在本轮 `.runs/` 目录。详情查询连接异常的根因及恢复策略仍需后续专项排查，单次日志不足以确认是网络断连还是连接池生命周期问题。

## 结果

研究流程已完成并发布报告，但**完整验收未通过**：浏览器刷新有一次数据库连接错误，独立内容复核发现两处被模型审阅遗漏的关键问题。

| 项目 | 结果 |
| --- | --- |
| 研究状态 | completed / success / explicit_completion |
| 自动质量判定 | 研究阶段覆盖通过；报告 review=pass，未降级、未跳过 |
| 研究任务 | 7 个完成，包含资源计算补证 |
| 模型调用 | 103 次，全部取得提供商用量，无估算或缺失 |
| 工具回执 | 24 条宿主回执、18 条 Gateway 回执；差额为宿主控制工具 |
| SQL 恢复账本 | 321 条操作全部 committed，没有 started/quarantined 遗留 |
| 结构化异常 | 1 次评审触发 structured_attempt_budget_exhausted，拒绝该次交接后补证；观察到 11 条 JSON 正文响应 |
| 空响应与未知回执 | 空 completed 模型响应 0；unknown 模型调用 0 |
| 发布 | Markdown 生成、下载成功，与报告文本一致 |
| 本轮费用 | 158,040 微美元，即 **$0.158040**；未决预留 0 |

发布文本统一换行后的 SHA-256 为 `51079b260d179bf3a6c6696559cd1d23763ee064728156c71b77a186f2c105e6`。浏览器能打开报告，运行结束状态经事件流展示。观测脚本曾重连，所以其 `elapsed_seconds` 为 null，不把重连后的观察时长作为研究耗时。

## 独立内容检查

报告包含至少三个不同官方页面、声明与版本信息、startupProbe/readinessProbe 区别、Job 完成与终止宽限期说明；YAML 能解析，`metadata`、Sidecar 容器级 `restartPolicy: Always` 和 Job Pod 级 `restartPolicy: Never` 均保留。

以下两项使独立内容验收不通过：

1. **批处理示例不会自然完成。** Job 主容器命令是 `while true; do echo ...; sleep 1; done`，没有退出条件。原生 Sidecar 不阻塞完成，并不能让这个永不结束的主进程变成可完成的批处理示例。审阅返回 pass 时没有指出这一问题。
2. **有效资源公式未完整回答原题。** 正文使用普通 init 容器的 max 规则，将 Sidecar 一并描述为取 max，并明确承认未展开常驻 Sidecar 资源计入方式；公式也遗漏 Pod overhead。研究阶段此前将资源要求标记为 supported，最终审阅仍视为已覆盖，说明证据到写作及报告审阅之间存在漏判。

判断参照此前独立核对的 [Kubernetes 官方资料研究](marketscout-sidecar-codex-20261016.md)，并直接检查本轮最终下载稿。没有修改报告、补写代码、放宽门禁或隐藏失败来获取通过结果。

## 清理与证据

本轮专用 Playwright 会话已关闭，8 个 MarketScout 测试服务已停止，4109、8089、20249 端口已释放。原有 `xxl-job-admin`、`rustfs`、`nacos`、`seata-server` 保持运行，数据库、镜像与共享卷保留。历史取消运行的 14,648 微美元预留未被释放或重试。

主要工件位于 `.runs/redeploy-e2e-20261016/`：`build.log`、`startup.log`、`database-preflight.json`、`verified-deployment.json`、`browser-validation.json`、`refresh-api-redacted.log`、`project.log`、`observer-resumed.log`、`protocol-audit.json`、`final-costs.json`、`cleanup.json`、`cleanup-ports.json`。

本轮报告、下载文件、原始评审与 `independent-content-review.json` 位于 `project/kubernetes-native-sidecar-migration/`。原始观测器退出前工件另存在 `before-observer-reconnect/`。这些目录包含私密部署配置，不应整体提交或分享。业务源码与上轮一致，本轮只增加验证工件和本文，没有 Git 提交。

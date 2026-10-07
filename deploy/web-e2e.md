# Web 检索与质量门禁实网验收

本验收使用当前工作区构建的 Docker 镜像、真实模型和搜索服务、PostgreSQL 恢复账本、Gateway 签名及出网审批。同步研究通过原生 AgentScope 运行，不依赖 RocketMQ。完整 IAM 登录、异步团队和资料入库不属于此验收。

本地 `.env` 需要配置 LiteLLM 路由使用的提供商凭据、Run Key 管理与加密密钥、沙箱签名密钥和 `TAVILY_API_KEY`。Bing 无需密钥；未配置 Brave、Anthropic 或 Firecrawl 时，不得将这些渠道标记为通过实网验收。不要输出或提交真实凭据。

PowerShell 下使用独立项目名。数据库和模型网关默认只在 Compose 网络中通信；控制台默认绑定 `127.0.0.1:8087`，模型代理服务接口默认绑定 `127.0.0.1:4107`。可用 `WEB_E2E_PORT` 和 `WEB_E2E_LITELLM_PORT` 修改宿主端口。

```powershell
$env:COMPOSE_PROJECT_NAME = 'insightforge-web-e2e'
docker compose --profile sandbox --profile sandbox-build build api sandbox-controller sandbox-gateway sandbox-worker-image frontend litellm-proxy publisher-worker

# 使用实际 Worker 摘要生成独立策略副本，保留管理员原有权限规则。
New-Item -ItemType Directory -Force .runs | Out-Null
$workerImage = docker image inspect insightforge-sandbox-worker:local --format '{{.Id}}'
$policyText = Get-Content config/sandbox-policy.toml -Raw
$policyText = $policyText -replace 'deployment_id = "[^"]+"', ('deployment_id = "' + $env:COMPOSE_PROJECT_NAME + '"')
$policyText = $policyText -replace 'worker_image_digest = "sha256:[a-f0-9]+"', ('worker_image_digest = "' + $workerImage.Trim() + '"')
[System.IO.File]::WriteAllText((Join-Path (Get-Location) '.runs/web-docker-e2e-policy.toml'), $policyText)

docker compose -f docker-compose.yaml -f deploy/compose.web-e2e.yaml --profile sandbox up -d --no-build --wait --wait-timeout 180 api sandbox-controller sandbox-gateway frontend proxy publisher-worker
```

覆盖配置执行显式迁移，启用 Tavily/Bing 并行搜索，强制 `quality_evaluation_fail_open=false`。控制台沿用本地开发身份，仅监听本机；不要将这套免登录验收配置用于公开部署。

运行真实 Web 研究：

```powershell
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:8087'
$env:E2E_NATIVE_FULL = 'true'
$env:NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS = 'true'
$env:E2E_RESEARCH_QUESTION = '仅使用 PostgreSQL 官方文档，核实 PostgreSQL 17 的增量备份支持：说明如何生成增量备份、恢复前如何合并、与常规基础备份的关系和主要限制。至少引用3个不同的官方文档页面并给出可核验链接。报告使用中文，控制在1000字左右，不需要性能跑分或市场分析。'
pnpm --dir frontend exec playwright test native-research.spec.ts --project=desktop --workers=1 --grep 'native web:'
```

默认工具集同时包含 `web_research` 和 `fetch_url`，模型可能直接读取已知网址。若要单独验收并行搜索，启动 API 前可设置 `WEB_E2E_RESEARCHER_TOOL_WHITELIST=web_research,ResearchComplete,think_tool,read_research_context`，并重新执行上述 `up` 命令。这仅收窄本轮研究工具，不关闭治理；测试后清除此变量并重建 API 容器。

固定页面及最终报告复核验收：

```powershell
$env:E2E_RUN_CONFIG = '{"report_review_enabled":true,"report_review_fail_open":false}'
$env:E2E_SOURCE_SELECTIONS = '{"specific":{"mode":"specific","sources":[{"type":"url","url":"https://www.postgresql.org/docs/17/app-pgbasebackup.html"},{"type":"url","url":"https://www.postgresql.org/docs/17/app-pgcombinebackup.html"},{"type":"url","url":"https://www.postgresql.org/docs/17/continuous-archiving.html"}]}}'
$env:E2E_RESEARCH_QUESTION = "使用一个研究任务。`n仅依据所选页面，回答 PostgreSQL 的增量备份能否直接使用，以及 pg_combinebackup 在恢复前的作用。`n报告使用中文。"
pnpm --dir frontend exec playwright test native-research.spec.ts --project=desktop --workers=1 --grep 'native specific:'
```

测试会通过实际页面处理审批，并验证完成状态、报告刷新、模型用量、Markdown 发布和下载；启用报告复核时额外断言 `decision=pass` 且未降级。质量门禁拒绝包含未接纳引用的研究时，应保留失败运行及回执分析原因，不能通过关闭门禁改成成功。

从 SQL 恢复快照检查各任务 `assessment.handoff`、`coverage_ledger`、`report_product`，从用量接口核对 settled/reserved，按任务活动接口检查 `query_started`、`query_completed`、抓取后端和审批事件。SSE 重连使用已有事件序号；同一逻辑调用的进度事件应保持稳定 ID。

每次验收结束都停止本项目容器，保留报告和数据库卷供审计。仅对本次专用项目执行，不启动或停止其他项目。

```powershell
docker compose -f docker-compose.yaml -f deploy/compose.web-e2e.yaml --profile sandbox stop --timeout 30
```

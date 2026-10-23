# Docker、远程 PostgreSQL/RocketMQ 与 DeepSeek Fetch 的 Agent Teams 验收

2026-10-20 使用当前工作区源码完成一次真实浏览器验收。成功运行 ID 为 `bd51fad07c2340df900987c1704f21c2`，Playwright 桌面项目通过，耗时约 482 秒。登录、计划审批、两名容器成员、任务依赖、成员复用、报告审阅、刷新展示、用量及 Markdown 发布下载均执行真实实现。

## 部署与实际资源

- 专用 Compose 项目为 `marketscout-teams-e2e-20261018`，复用已有未跟踪的部署环境与 Worker 环境文件。
- PostgreSQL 连接 `172.22.208.30:5432`。应用、IAM、协调及 AgentScope 使用独立验收库 `marketscout_teams_e2e_20261018`；框架表位于 `agentscope_runtime` schema。LiteLLM 使用独立库 `marketscout_teams_litellm_20261018`。
- RocketMQ 使用远程 `172.22.121.109:8081` gRPC Proxy，NameServer 为同主机的 `9876` 端口。未启动本地 PostgreSQL 或 RocketMQ 服务。
- API、前端、Gateway、Controller 和 Publisher 镜像从当前源码构建。API、Gateway、Controller、Publisher 各核对 412 个 Python/JSON 源文件，哈希全部匹配工作区。LiteLLM 复用已有专用镜像，镜像 ID 为 `sha256:2e8ff676a877073c5b3366711a5def31d93595486d491c86b082225f77c1e58b`；其重新构建因依赖下载耗时中止。
- 使用 `RESEARCH_ENGINE=native`、`AS_NATIVE_RESOURCES=gateway`、`enable_async_research=true`、`async_research_mode=teams`、`team_execution_mode=plan_approval`。通过真实 IAM 登录，未启用认证旁路。
- 研究固定来源为 PostgreSQL 17 的 `sql-vacuum.html` 与 `sql-analyze.html`。本次运行关闭提供商搜索，唯一 Fetch 后端及允许的外部提取器均为 `deepseek`，实际访问 `https://api.deepseek.com/v1/tools/fetch`。本轮验证 DeepSeek Fetch，未单独验证 DeepSeek Search。

## 结果与验收证据

| 检查 | 实际结果 |
| --- | --- |
| Docker 就绪 | 迁移成功；API、前端、LiteLLM、Gateway、Controller、Publisher 就绪，代理入口可访问 |
| 团队与依赖 | 仅 `vacuum-checker`、`analyze-checker` 两名成员；两个独立研究任务加一个依赖前两项的汇总任务；汇总复用 `vacuum-checker` |
| 计划与收尾 | 三份计划均获 Lead 批准，三项任务完成，全部依赖解除，团队与成员关闭 |
| DeepSeek 实际抓取 | 两份已提交文档缓存的 `extractor` 均为 `deepseek`，正文分别为 14,227 与 9,815 字符 |
| 证据与工具纠偏 | 任务结果携带 60 条证据记录（含汇总复用记录）；三项任务各执行一次工具批次质量评估并获得接纳 |
| 临时门禁旁路 | 交接回执均为 `accepted_with_caveats`、`evaluation_status=skipped`；报告记录跳过 `handoff`、`supervisor_completion`，未伪造覆盖通过 |
| 报告审阅 | `decision=pass`、`degraded=false`，引用仅指向两个指定官方页面；刷新后报告与团队看板均可见 |
| 远程消息 | 本运行 outbox 的 24 条消息全部发布，无待发布消息；远程广播、团队事件和控制主题均有消息偏移增长 |
| 恢复账本 | 调用账本所有记录均为 `committed`，无未知结果；运行租约已释放 |
| 发布下载 | Publisher 完成 Markdown 发布，下载 2,463 字节；SHA-256 为 `8c27549b1a27b73e7fb1501fd2df9e893b74487a5f1dfe9e295d893c6882def3`，与最终报告一致 |
| 用量 | 53 次模型调用，记录费用 53,968 micro-USD（约 0.054 美元）；该数值不是 DeepSeek Fetch 的独立供应商账单核验 |

## 本次修复与检查

`docker-compose.yaml` 显式向 LiteLLM Proxy 注入当前静态模型路由需要的 `ZHIPU_BASE_URL`、`ZHIPU_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`。根 Compose 的该服务没有 `env_file`，仅在宿主 `.env` 设置这些变量不能让容器内的 `os.environ/...` 引用生效。真实值继续保留在未跟踪的本地配置中。

`frontend/e2e/agent-teams.spec.ts` 增加四个仅供验收使用的环境覆盖：`E2E_TEAMS_SEARCH_API`、`E2E_TEAMS_SEARCH_PROVIDERS`、`E2E_TEAMS_FETCH_BACKEND_ORDER`、`E2E_TEAMS_EXTERNAL_EXTRACT_BACKENDS`。未设置时沿用原有固定来源与本地抓取测试行为。

首次 DeepSeek 验收在网络审批断言失败：`external.extract` 的目标为 `api.deepseek.com:443`，旧断言只接受 PostgreSQL 来源域名。现在按审批的结构字段检查精确域名、端口和 capability；只有显式选择 DeepSeek 外部提取器时才接受该 Provider 审批，研究来源范围仍限于两个官方页面。

Compose 配置解析、`git diff --check`、测试文件 ESLint 和全前端 TypeScript 检查通过。宿主 `pnpm exec` 的版本自动下载遇到 `registry.npmmirror.com` 连接失败，最终代码检查直接使用仓库已安装的 ESLint 9.39.5 与 TypeScript 6.0.3。实际浏览器 E2E 已通过 pnpm 启动并执行。

## 运行产物与清理

本机详细证据位于 `.runs/teams-review-20261018/deepseek-current-20261020/`，包括 `verification.json`、`public-evidence.json`、`fetch-artifacts.json`、`mq-ledger.json`、`source-verification.json`、`cleanup.json`、`report.md`、团队/报告截图及日志。该目录保持忽略，不提交凭据、完整运行数据或截图到版本库。

验收后执行专用 Compose 项目的 `down --timeout 30`，核对无剩余本项目容器及 Teams 容器，宿主 `8097` 和 `4197` 端口均已释放。删除本轮创建的五个 `teams_e2e_20261018_` 主题：`coordination_events_v1`、`coordination_control_v1`、`broadcast_wake_v1`、`team_events_v2`、`team_control_v2`。已有验收数据库和共享卷保留供复查，清理命令仅针对本项目资源。

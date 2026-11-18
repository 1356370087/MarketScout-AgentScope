# MarketScout

MarketScout 是面向市场与竞品分析的研究工作台。你可以提出一个比较问题，限定可用的网站或内部资料，在浏览器中审批研究计划、查看取证进度，并取得带引用的报告。后端使用 AgentScope 组织研究，SQL 账本保存任务状态、审批、用量和工具回执。

## 一次竞品研究怎么走

例如，要用单 Agent 和 Skills 比较 Dify 与 FastGPT 的云服务，可以在 `/research/new` 选择「自适应研究」和 Web 来源，输入下面的研究要求。

> 只建一个主任务，依次核验来源、比较产品、综合证据。比较 Dify 和 FastGPT 的 RAG 能力、部署方式和入门付费云套餐。仅使用双方官网和官方文档。逐项列出套餐名称、币种、计费对象、月付实付价、年付实付价和核价日期。年付折算月价单独列出。每项关键结论附来源，资料缺失时说明缺口。

1. **确认计划。** 查看研究对象、来源范围、必答问题和预算。审批通过后，可在 `/research/[runId]` 跟踪执行。如果计划要求限制页数或只读指定页面，请在这一步核对。
2. **跟踪取证。** 单 Agent 可以依次读取 `source_verification`、`comparative_research` 和 `evidence_synthesis` 三项 Skills，核查来源、逐项比较，再把证据交给报告阶段。需要拆分的任务也支持委派给 Researcher。Web 抓取会记录来源地址、内容哈希和读取时间，便于区分核价日期与网页发布日期。
3. **审阅报告。** 核对大纲后进入正文生成、引用校验与审阅。运行完成状态和报告审阅结论分别记录。确认终稿时，可同时查看 `completion_status`、`report_review.decision` 及关键事实的引用。资料不足时保留草稿，补证或复核可产生新版本。

2026 年 11 月 18 日的一次 Docker 实网验收使用单 Agent 和上述三项 Skills，读取预选的 6 个官方页面，准入 37 条证据，覆盖 16 项要求。该次运行记录为 `success`，报告审阅为 `pass`，下载的 Markdown 与归档终稿哈希一致。验收记录也保留了较早一次开放发现运行的证据缺口。[查看场景、结果与复核记录](deploy/e2e-remediation-plan-20261118.md)

## 用 Docker 启动工作台

需要 Docker Compose v2、可访问的 PostgreSQL 和相应数据库权限。在仓库根目录执行以下 PowerShell 命令，已有 `.env` 的环境可以直接配置下一步。

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

按 [根配置模板](.env.example) 填写以下部署参数。

| 用途 | 需准备的配置 |
| --- | --- |
| 应用数据库 | `IAM_DATABASE_URL` 使用 `postgresql+asyncpg://`。文档与团队协调分别使用 `DOCUMENT_DATABASE_URL`、`COORDINATION_DATABASE_URL`，协议为 `postgresql://`。当前项目使用远程 PostgreSQL，文档检索需要 pgvector。 |
| 恢复与模型路由 | Gateway 模式使用 `AS_DATABASE_URL` 连接 PostgreSQL，框架表位于 `AS_DATABASE_SCHEMA`。`LITELLM_DATABASE_URL` 指向独立的 LiteLLM 数据库。验收使用独立命名的业务库。 |
| 首次初始化与身份 | 设置 `PLATFORM_SETUP_TOKEN` 和稳定的 `PLATFORM_SECRET_ENCRYPTION_KEY`，按模板配置 IAM 的 Access、Refresh 签名密钥。LiteLLM 和 Sandbox Gateway 的凭据按选用的拓扑设置。 |

启动基础工作台。Compose 会运行应用数据库迁移，并启动 API、前端、反向代理及后台 Worker。

```powershell
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
```

打开 [本地工作台](http://localhost:8080)。首次部署进入 `/setup` 初始化管理员。默认的平台数据库配置模式下，管理员在 `/admin/configurations` 导入、校验并发布模型、工具及预算配置。后端 `/healthz` 用于检查 API 存活，登录后可通过 `/capabilities` 的 `runtime_ready` 字段确认原生研究服务是否已装配。[平台初始化与配置发布](deploy/platform-management.md)

### 选择研究运行方式

| 场景 | 配置与入口 |
| --- | --- |
| 本机直连模型、使用本地工具 | 使用 `AS_NATIVE_RESOURCES=host`、`MODEL_BACKEND=legacy` 和 `provider:model` 模型名。关闭提供商搜索，设置 `SEARCH_API=none`、`SEARCH_PROVIDERS=[]`、`WEB_PIPELINE_MODE=legacy`、`SANDBOX_ENABLED=false`。 |
| 单 Agent 联网竞品分析 | 使用 `AS_NATIVE_RESOURCES=gateway`、LiteLLM、PostgreSQL 恢复账本和 Sandbox Gateway。Web 验收覆盖文件负责接线，需先构建 Worker 镜像，并将实际镜像摘要写入该次部署的沙箱策略。默认验收入口为 `http://localhost:8087`。[Web 部署与验证步骤](deploy/web-e2e.md) |
| 异步团队研究 | 启用异步研究并配置团队协调数据库、RocketMQ、可信 Worker、网络和共享卷。[原生团队部署与验证步骤](deploy/native-e2e.md) |

根模板同时提供多种场景的配置示例。复制后按所选模式调整资源、模型和搜索设置。文档摄取、知识库维护和报告文件发布各有独立 Worker。持续情报按需启用，可按计划生成观察快照并跟踪跨次研究的事项。[后台服务与观测](deploy/README.md) · [持续情报](docs/continuous-intelligence/README.md)

## 从源码开发

后端使用 Python 3.14、uv 和 FastAPI。前端使用 Next.js App Router、React 和 TypeScript。前端容器使用 Node.js 24，仓库指定 pnpm 11.9.0。以下命令从仓库根目录执行。

**后端**　准备好开发数据库与 `.env` 后，在第一个终端安装依赖、执行迁移并启动 API。

```powershell
uv sync --frozen
$env:PYTHONPATH = 'src;.'
uv run --env-file .env alembic upgrade head
uv run --env-file .env uvicorn open_deep_research.server:app --reload --host 127.0.0.1 --port 2024
```

Linux 和 macOS 使用 `export PYTHONPATH=src:.`。业务迁移和 Gateway 所需的框架存储迁移分别执行，后者的命令见 Web 部署说明。

**前端**　在第二个终端执行以下命令，然后打开 [本地开发页面](http://localhost:3000)。前端 BFF 通过 `RESEARCH_API_ORIGIN` 回源，开发默认值为 `http://127.0.0.1:2024`。

```powershell
if (-not (Test-Path -LiteralPath frontend/.env.local)) {
    Copy-Item -LiteralPath frontend/.env.example -Destination frontend/.env.local
}
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

## 代码与验证

| 入口 | 用途 |
| --- | --- |
| [AgentScope 运行时](src/open_deep_research/agentscope_runtime/) | 研究 Pipeline、Agent、Skills 装配、SQL 恢复与模型调用计账。 |
| [质量契约](src/open_deep_research/quality/) 与 [报告](src/open_deep_research/report/) | 按研究要求核对覆盖，验证证据与引用，审阅、归档并发布报告。 |
| [文档](src/open_deep_research/documents/) 与 [知识库](src/open_deep_research/knowledge/) | 摄取资料、发布版本、限定检索范围，支持仅文档或混合来源研究。 |
| [前端业务模块](frontend/src/features/) 与 [API](src/open_deep_research/api/) | 创建研究、查看 SSE 事件、处理审批、浏览证据与报告。 |

在已配置 `PYTHONPATH` 的终端中，可以运行以下检查。

```powershell
uv run python -m open_deep_research.migration_check --check all
uv run pytest tests/as_runtime/test_competitor_remediation.py
pnpm --dir frontend test
```

免费评估可用固定的正负参考产物验证评分器。真实模型试验需要单独配置模型凭据和预算，实网试验还需部署相应服务。数据库集成测试使用独立验收库。[Agent Evals 用法](src/open_deep_research/evaluation/README.md) · [开发与验证约定](AGENTS.md)

项目采用 [MIT 许可证](LICENSE)。

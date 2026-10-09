# AGENTS.md

## 用途与协作约定

本文件用于指导仓库内的开发、排障与验证。架构和命令以当前源码、`pyproject.toml`、`frontend/package.json`、示例配置及 Compose 文件为准；历史设计、迁移记录和 README 中的旧描述不能代替现行实现。迁移进度与验收证据另见 `docs/agentscope-migration/07-progress.md`，不要在这里维护完成比例或将组件检查等同于完整部署验收。

- 修改前先检查工作区现有改动，保持任务范围；发现范围外的问题先报告，不顺手重写或加固。
- 修改前端时同时遵守 `frontend/AGENTS.md`，按其要求查阅当前安装的 Next.js 文档。
- 回复不使用 Markdown 水平分割线，使用自然段落分隔内容。
- 示例配置的注释、分组标题、说明和操作提示统一使用清晰完整的中文；说明用途、单位、默认值、必填条件、格式及关联配置，区分服务接口与管理控制台端口。变量名、协议名和命令保留原文，示例值只使用占位符。
- 真实凭据只放在不纳入版本控制的本地配置中；不要提交 `.runs/`、文档存储、测试产物或其他运行数据。`.runs/` 已加入 `.gitignore`，其他生成目录仍需检查 Git 状态。

## 项目与代码导航

InsightForge 是面向市场与竞争情报的多 Agent 深度研究平台，使用 AgentScope 原生 Agent、研究 Pipeline 和 SQL 恢复账本。Python 项目名为 `marketscout-agentscope`，导入命名空间为 `open_deep_research`。

后端使用 Python 3.14（见 `.python-version` 和 `pyproject.toml`）、AgentScope 与 FastAPI；前端使用 Next.js App Router、React 和 TypeScript，统一使用 `frontend/package.json` 指定的 pnpm 版本，不生成 npm/yarn 锁文件。

下表中的后端子路径均相对于 `src/open_deep_research/`：

| 位置 | 职责 |
| --- | --- |
| `server.py`、`api/` | FastAPI 组合、研究 HTTP/SSE、审批、用量、发布和生命周期管理 |
| `agentscope_runtime/` | 原生 Agent、Pipeline、模型/工具装配、SQL 恢复、团队与 Gateway 接线 |
| `configuration.py`、`config_types.py` | 配置解析、冻结契约和项目自有 `RuntimeConfig` 映射 |
| `models/` | 模型目录、凭据与 Run Key、提供商解析、价格、Token 上限和熔断 |
| `tools/`、`web/`、`sandbox/` | 工具协议与治理、Web 证据算法、沙箱控制面及出网边界 |
| `quality/`、`evidence.py` | 覆盖与来源契约、证据、确定性门禁和 Judge 策略 |
| `documents/` | 文档上传、解析、版本、索引、检索与摄取 Worker |
| `knowledge/` | 工作空间、知识库、合集、统一检索、事实/Wiki、后台维护与备份 |
| `report/` | 报告编排、审阅、引用、CanonicalReport 与独立文件发布 Worker |
| `tasks/` | 团队状态、成员、消息、租约与 RocketMQ 协调；不是旧文件任务池 |
| `memory/`、`observability/`、`evaluation/` | 长期记忆、追踪/指标/用量、本地 Agent Evals |

其他主要目录：

- `src/security/rbac/`：自有 IAM/RBAC、HTTP 接口、数据库迁移；CLI 为 `src/security/cli.py`。`src/security/auth.py` 仅保留兼容别名。
- `frontend/src/app/`：页面与 BFF；业务功能位于 `frontend/src/features/`。
- `tests/as_runtime/`：原生运行时与集成验证；`tests/documents/`、`tests/knowledge/`、`tests/rbac/` 为对应领域测试，其他回归测试位于 `tests/`。
- `config/`：LiteLLM 路由和沙箱策略；`deploy/`：Compose 覆盖、代理与观测部署配置。

## 本地开发与部署

### 安装与启动

在仓库根目录运行 `uv sync`。当前项目采用 `src/` 布局且没有声明构建后端；从源码运行后端 CLI、测试前设置模块搜索路径：

```powershell
# Windows PowerShell，在仓库根目录执行
$env:PYTHONPATH = 'src;.'
```

```bash
# Linux / macOS，在仓库根目录执行
export PYTHONPATH=src:.
```

首次配置时复制根目录 `.env.example` 为 `.env`、`frontend/.env.example` 为 `frontend/.env.local`，不要覆盖已有本地配置。`server.py` 会加载 `.env`；其他 CLI 是否加载配置需看各自入口，不能假定迁移或 Worker 自动读取该文件。

```bash
# 后端开发服务
uv run uvicorn open_deep_research.server:app --reload --host 127.0.0.1 --port 2024

# 前端，在 frontend/ 目录执行
pnpm install
pnpm dev
```

前端默认端口为 3000，BFF 回源地址由 `RESEARCH_API_ORIGIN` 指定，默认 `http://127.0.0.1:2024`。浏览器通过 `/api/research`、`/api/auth`、`/api/iam` 访问后端，Access/Refresh Token 保存在 HttpOnly Cookie 中，由 BFF 注入认证头。`NEXT_PUBLIC_*` 在构建期进入客户端代码。

主要页面包括 `/research/new`、`/research/[runId]`、`/documents`、`/knowledge`、`/knowledge/ledger`、`/knowledge/health`、`/usage`、`/settings` 和 `/admin`。

### 运行模式与基础设施

`RESEARCH_ENGINE` 默认且仅接受 `native`。`AS_NATIVE_RESOURCES` 决定资源装配方式，和模型后端、认证旁路是不同设置：

| 模式 | 用途与前提 |
| --- | --- |
| `host`（默认） | 宿主模型及本地工具；研究恢复使用本地 SQL 存储。使用 `MODEL_BACKEND=legacy` 和 `provider:model` 直连配置；关闭提供商搜索（`SEARCH_API=none`，且不启用 `SEARCH_PROVIDERS`），设置 `WEB_PIPELINE_MODE=legacy`、`SANDBOX_ENABLED=false`。不满足时会拒绝运行 Web/Gateway 工具。 |
| `gateway` | Web/Gateway 研究；需要 `AS_DATABASE_URL` 指向 PostgreSQL、`MODEL_BACKEND=litellm`、模型目录与 Run Key 配置，以及 Sandbox Gateway/Controller 控制面。 |

根示例配置使用 LiteLLM 别名和 Web 搜索，但 `AS_NATIVE_RESOURCES` 仍为 `host`；复制模板后必须按目标模式调整，不能把服务启动成功当作研究链路已就绪。

- 框架表使用 `AS_DATABASE_SCHEMA`（默认 `agentscope_runtime`），与业务表隔离。生产保持 `AS_STORAGE_AUTO_CREATE=false`，框架表通过 `agentscope_runtime/storage.py:run_storage_migrations()` 显式迁移。
- `ENABLE_ASYNC_RESEARCH=true` 的团队协调还需 `COORDINATION_DATABASE_URL` 与 `ROCKETMQ_*`；框架广播使用另一组 `AS_ROCKETMQ_*` 配置。原生团队容器需要专用 Worker 环境文件、镜像、网络和共享卷。
- 完整 IAM 需要 PostgreSQL 与 JWT 配置；仅本地开发可同时设置 `APP_ENV=development` 和 `LOCAL_DEV_AUTH_BYPASS=true`。前端旁路需同步设置 `NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS`。
- 文档/知识库依赖 `DOCUMENT_RESEARCH_ENABLED`、PostgreSQL/pgvector、文档存储和相关 Worker；免 IAM 不等于免数据库。
- 部署保持单 Uvicorn worker；API 准入、SSE 连接限制等仍有进程内状态，不能假定增加 worker 就能获得一致的全局限制。

业务迁移位于 `src/security/rbac/migrations/`，除 IAM 外也包含文档、知识库与团队表。先向命令进程提供 `IAM_DATABASE_URL` 等所需环境变量，再执行：

```bash
uv run alembic upgrade head
uv run python -m security.cli bootstrap-admin --email admin@example.com --password '替换为管理员密码'
```

### 容器与后台任务

根 `docker-compose.yaml` 包含应用 PostgreSQL/pgvector、LiteLLM 的 PostgreSQL/Redis/Proxy、业务迁移、API、前端、Nginx、`document-worker`、`knowledge-worker` 和 `publisher-worker`。代理入口为 `http://localhost:8080`；默认 API/前端采用开发认证旁路。`sandbox`、`observability`、`backup` 等 profile 提供可选服务，Docling/OCR 和 RocketMQ 按部署配置接入。

`docker compose up --build` 使用根拓扑；完整原生研究的迁移、Gateway、团队 Worker 接线参考 `deploy/compose.native-e2e.yaml` 和 `deploy/compose.web-e2e.yaml`。这些覆盖文件用于对应验证场景，使用前核对环境、挂载与服务清单。观测、发布 Worker 和 OCR 部署说明见 `deploy/README.md`。

- `documents.worker` 处理文档摄取；`knowledge.maintenance` 处理知识库后台任务；`report.publisher_worker` 处理报告文件发布，三者职责不同，Python 模块前缀均为 `open_deep_research`。
- 报告发布 Worker 在 Compose 中无网络，和 API 共享运行工件卷；文件发布队列仍存在，不要与已删除的研究文件任务池混淆。
- 长期记忆维护可由系统定时器运行，或使用 `uv run python -m open_deep_research.memory.maintenance daily --loop --interval-hours 24`；循环使用运行目录下的维护锁防止重复执行。
- 知识库灾备由 `knowledge.backup` 与 `backup` profile 提供；初始化、备份和独立目标库恢复的配置见 `.env.example`。

## 运行时与数据边界

### 研究、恢复与团队

正式 HTTP 入口为 `server:app`，核心调用链为：

`NativeRuns.create → RunConfig.compile → RecoveryStore.create_from_config → RecoverySession → ProductionRunFactory → ResearchPipeline`

`agentscope_runtime/research_pipeline.py` 按顺序执行消息摘要、记忆召回、澄清、研究简报、计划审批、Supervisor 研究、大纲审批、报告生成和记忆提取；具体领域行为在 `research_stages.py`。`PendingDecision` 与阶段状态进入 SQL，在用户决定后继续。

- `research_agents.py` 中的 Supervisor/Researcher 使用 AgentScope `Agent`、`Toolkit` 和 Middleware；Researcher 在独立会话运行。同步委派与异步研究复用覆盖、证据和完成契约。异步模式包括 `collaborator` 与 `teams`，团队计划审批由 `team_execution_mode` 控制。
- `ResearchSnapshot` 保存研究状态，`RecoveryStore` 的租约与 fence 决定写入权；模型/工具操作回执、预算与公共事件持久化到同一恢复账本。未知外部执行结果必须隔离，不能自动当作未执行重试。
- `NativeResearchContext` 使用框架压缩与卸载，保留任务约束、证据 ID 和反馈；回读绑定所属会话。预算耗尽、取消、失租及未知调用结果不能被摘要逻辑吞掉。
- `api/admission.py` 管理创建频率和 SSE 限制，非终态并发数读取 SQL；启动恢复与清理由生命周期、`api/native_runs.py` 和 `api/retention.py` 管理。正在发布的运行拒绝删除。
- 旧 QueryEngine 检查点仅经 `api/history.py` 只读展示，不能跨引擎恢复。不要重新引入旧循环、文件任务池或 LangChain/LangGraph 执行依赖。

### 配置与模型

`Configuration` 是 Pydantic 模型。未冻结配置通常按环境变量、请求 `configurable`、代码默认值解析；内部边界类型为项目自有 `RuntimeConfig`，`from_runnable_config()` 保留的是方法名，不表示依赖 LangChain。

`RunConfig` 冻结影响恢复的非秘密配置与指纹；恢复时冻结字段优先，不能用新环境变量覆盖旧运行语义。添加配置字段时同步 `agentscope_runtime/config_fields.json`；改变冻结字段时同时检查契约版本、兼容恢复和相关测试。

| 配置范围 | 当前规则与入口 |
| --- | --- |
| 模型后端 | `.env.example` 与 Compose 使用 `MODEL_BACKEND=litellm` 和 `if-*-v1` 别名；`Configuration` 的代码默认仍为 `legacy`。直连后端使用 `provider:model`，`legacy` 在这里指模型接入方式，研究引擎仍是原生。 |
| 模型角色 | 研究、摘要、压缩、质量、报告审阅、知识检索、Web 证据等角色以 `agentscope_runtime/models.py:ROLES` 为准；离线 Judge 由 `evaluation/session.py` 装配，不能再假定仅有七个角色。 |
| 搜索 | 支持 Tavily、OpenAI、Anthropic、Bing、Brave；`search_providers` 可并行选择，未设置时沿用 `search_api`，显式 `[]` 关闭提供商搜索。OpenAI/Anthropic 搜索有独立模型配置。 |
| Web 管线 | `web_pipeline_mode` 为 `legacy`、`shadow`、`enforced`（默认）；抓取可配置 local、Playwright、Tavily Extract、Firecrawl，实际可用性受凭据、工具治理与来源范围约束。 |
| 执行与质量 | 并发、迭代、预算、效率策略、质量严格度和 Judge 输入预算见 `configuration.py`；质量门禁必须绑定 `NativeResearchQuality`，报告必须使用原生报告端口，不能以 `fail_open` 掩盖缺失运行时。 |

模型通过 `ModelFactory`、`model_policy.py` 与 `model_accounting.py` 执行，统一有限重试、回退、熔断和计账。直连凭据由 `models/resolution.py` 与 `CredentialBinding` 解析；LiteLLM 生产运行使用冻结目录及限模型/费用的 Run Key，离线 Judge 和摄取等服务任务使用各自的服务凭据。凭据不写入 SQL 检查点，模型、Run Key、Gateway 登记和沙箱由所属运行统一管理与关闭。

新增 LiteLLM 模型需维护 `config/litellm.yaml` 及目录中的能力、价格、上下文/输出上限，并进入运行允许列表；直连模型的静态 Token 上限维护在 `models/limits.py:MODEL_TOKEN_LIMITS`。Tavily 内容摘要使用独立 `summarization_model`。

### 工具、来源与安全

- `tools/base.py` 定义协议、执行区和副作用，`tools/registry.py:prepare_existing_toolset` 统一检查重名、启用状态、权限与描述预算；`agentscope_runtime/tools.py` 将工具装配到 AgentScope Toolkit，保留公开调用 ID、治理、回执和证据观察。
- `agentscope_runtime/search.py`、`search_providers.py`、`web_tools.py` 组织 Search → Fetch → Extract → Evidence；`sandbox_catalog.py` 装配 Gateway 工具及嵌套模型。MCP 使用 `agentscope_runtime/mcp.py`，保留信任校验与交互错误协议。
- `quality/contract.py` 与 `evidence.py` 管理需求覆盖和来源准入；显式 URL/域名限制、排除项和“仅官方来源”约束必须进入契约。受限来源无法确认时拒绝准入，覆盖门禁按任务归属需求评估，不能靠平均 Judge 分数抵消硬约束。
- 沙箱策略来自 `config/sandbox-policy.toml`，控制面、可信团队 Worker 与执行 shell/file 的沙箱各有边界。旧 `SANDBOX_SECRET_ENV_KEYS` 已被配置解析器明确拒绝，不得继续使用；提供商秘密保留在 API/Gateway 凭据边界。
- 身份入口为 `security.rbac` 的 `get_current_principal` 与 `require_permissions()`，运行/任务还需所有权检查；业务知识权限使用 `knowledge/authz.py`，不能仅凭通用 RBAC 角色绕过资料范围。
- IAM 使用 Argon2、Ed25519 Access/Refresh JWT、Refresh 轮换与重用检测、`authz_version` 吊销；系统角色为 viewer/researcher/developer/admin，并支持自定义角色。

### 文档、知识库与报告

`documents/` 管理上传、解析、结构单元、版本和索引；摄取 Worker 使用 PostgreSQL 任务租约。配置 Docling Serve 时，PDF/图片和 Office 预览走对应解析链，表格/文本保留本地结构化读取；降级会记录质量标记。Office 预览与 OCR 的外部依赖按 `DocumentSettings` 和部署配置核对。

`knowledge/` 管理个人/团队工作空间、知识库成员、合集、资料发布、事实台账、Wiki、同步去重、回收站与导入导出。`knowledge/search_service.py` 统一向量/全文/trigram 召回、融合、重排和上下文预算，访问与版本限制必须在数据库查询内生效。

研究来源契约位于 `documents/contracts.py`，支持 `web`、`documents`、`hybrid`、`specific`，来源可包含文档、知识库、合集、URL 或域名。`knowledge/run_scope.py` 在运行前冻结已发布文档 generation、检索参数和事实/Wiki 资产；检索时仍重验当前权限，不随新版本发布悄悄替换冻结资料。

报告经 `agentscope_runtime/report.py:NativeReportWriter` 进入 `report/`，执行证据预算、引用校验、审阅修订和 CanonicalReport 生成。Token 超限处理保留固定输入完整性，按整条证据预算，并使用有限重试；文件导出交给独立发布 Worker。

## 验证与评估

### 开发检查

以下命令在根目录、配置好 `PYTHONPATH` 后执行：

```bash
# 针对原生研究服务的示例回归，按改动选择其他测试文件
uv run pytest tests/as_runtime/test_research_service.py

# 原生导入边界和镜像/依赖静态检查，不代表完整部署验收
uv run python -m open_deep_research.migration_check --check all

# 后端全量测试，包含需要 Docker/PostgreSQL 等环境的集成测试
uv run pytest

# 类型检查；存在待收敛的类型债务，不预设全量零错误
uv run mypy src/open_deep_research
```

当前 `pyproject.toml` 未声明 Ruff 依赖或 Google docstring 规则，不把旧的 `uv run ruff check` 与规则描述当作已经配置好的仓库检查。

前端在 `frontend/` 目录执行：

```bash
pnpm lint
pnpm exec tsc --noEmit
pnpm test
pnpm build
pnpm test:e2e
```

`frontend/playwright.config.ts` 未设置 `PLAYWRIGHT_BASE_URL` 时会启动/复用本地 `pnpm dev`；设置后连接指定服务。后端或其他依赖按所选 E2E 用例准备。

- 每次 E2E 后关闭本次启动的前端、后端、Worker 与专用容器，确认端口和资源已释放；不关闭用户原先运行的服务。
- Docker 验证只启动本次需要的服务，不批量启动其他已停止的容器，不删除共享卷来“清理”。
- 区分单元测试、模拟服务、真实模型、数据库集成与完整部署的验证结论；报告实际执行范围及未满足的环境前提。

### Agent Evals

统一入口为 `uv run python -m open_deep_research.evaluation`，详细契约、预算与恢复规则见 `src/open_deep_research/evaluation/README.md`。

```bash
# 免费验证评分器与正负参考产物，不执行 Agent 或调用模型
uv run python -m open_deep_research.evaluation validate tests/fixtures/agent_evals/core.json --output .runs/evals/controls
uv run python -m open_deep_research.evaluation check .runs/evals/controls

# 固定工具环境中的真实模型试验；需要 LiteLLM 和有效身份，预算单位为美元
uv run python -m open_deep_research.evaluation --env-file .env run tests/fixtures/agent_evals/core.json --mode fixed --output .runs/evals/baseline --budget-usd 5
```

- `fixed` 使用真实原生 Agent 与版本化工具夹具；`live` 使用实际 Gateway、PostgreSQL、MQ/Controller 等环境。二者都不能与免费的 `validate` 混为一谈。
- `--trials` 重复研究，`--judge-repeats` 重复评分；`rescore`、`compare`、`review-export`、`review-import` 支持重评分、比较与人工校准。`check` 退出码为 0 通过、1 确定失败、2 证据不足或无法完成判定。
- 研究与 Judge 使用各自 SQL 账本，在实验预算内结算；未知调用结果保留未决状态与费用预留。真实联网研究遇到审批不自动放行，不把基础设施不可用当作研究质量失败。
- `tests/run_local_evaluate.py` 和 `tests/evaluators.py` 是兼容入口，实现在 `evaluation/legacy_cli.py`、`evaluation/evaluators.py`。
- LangSmith 入口仍为 `uv run --extra evaluation-langsmith python tests/run_evaluate.py`，运行前核对其硬编码模型、参数、数据集和远程凭据；该入口固定 Tavily，不代表所有本地评估都固定 Tavily。`tests/extract_langsmith_data.py` 导出基准提交数据，远程实验仅在显式运行入口时提交。

## 实现重量约束

实现重量必须与威胁模型和实际规模匹配，选择能解决任务的最轻实现：

- **任务清单是范围边界**：不为避免潜在评审意见扩展改造；额外加固确有必要时单独说明原因。
- **信任内部不变量**：服务端生成且未接受外部输入的 UUID、内部路由和枚举不重复校验；校验放在用户输入、外部返回及跨信任域边界。
- **使用最小并发原语**：单条条件 UPDATE/INSERT 能表达的守卫，不改成锁行、查询和多步写入；个位数凭据复用用普通字典，不引入 LRU、淘汰和异步关闭体系。
- **跟随邻近实现**：同类操作使用相同模式；“复用客户端”等意见按字面范围落实，不升级为通用缓存组件。
- **不改写已发布迁移凑幂等性**：未被要求时不把已有 `bulk_insert` 等操作重写为 `ON CONFLICT DO NOTHING`；必要变更需说明动机与影响。

负面参照：给内部文档/chunk UUID 加永不触发的正则白名单、为两三种 embedding 凭据加带锁 LRU、让 reindex 的在途守卫比 retry 更重，均属于应避免的无效复杂度。

# AGENTS.md

## 项目概述

InsightForge（仓库名与 Python 导入命名空间仍为 `open_deep_research`）是一个可配置的、完全开源的深度研究（Deep Research）多 Agent 平台，默认执行入口已切换为 AgentScope 原生 Agent、研究 Pipeline 与 SQL 恢复账本；旧 QueryEngine、文件任务池及 LangChain 模型/工具桥接已删除，旧测试迁移和完整部署验收仍按台账收口。它支持多模型提供商、多种搜索工具和 MCP（Model Context Protocol）服务器，实现自动化研究并生成带来源的结构化研究报告。在 [Deep Research Bench](https://huggingface.co/spaces/Ayanami0730/DeepResearch-Leaderboard) 排行榜上曾获得 #6 排名。

仓库包含三个主要部分：

- `src/open_deep_research/`：Python 研究运行时与 FastAPI 服务
- `src/security/`：自有身份与 RBAC 子系统（IAM，已完全移除 Supabase）
- `frontend/`：Next.js 研究控制台（包管理器为 pnpm）

## 常用命令

```bash
# 安装依赖
uv sync

# 启动 FastAPI 开发服务器
uv run uvicorn open_deep_research.server:app --reload --host 127.0.0.1 --port 2024

# 代码检查
uv run ruff check

# 类型检查（当前仍有待收敛的类型债务，不代表零错误）
uv run mypy src/open_deep_research

# 运行全部后端测试
uv run pytest

# 本地评估（AgentScope 原生 NativeRuns，不依赖旧 QueryEngine；默认一条内置问题）
uv run python tests/run_local_evaluate.py

# LangSmith 评估（原生研究与 Judge；需配置模型、参数及 LangSmith 凭据）
uv run --extra evaluation-langsmith python tests/run_evaluate.py

# 从 LangSmith 提取评估结果用于提交 Deep Research Bench
uv run python tests/extract_langsmith_data.py --project-name "实验名称" --model-name "模型名称" --dataset-name "deep_research_bench"
```

前端（`frontend/` 目录）：

```bash
cd frontend
pnpm install
pnpm dev          # http://localhost:3000，需先启动后端并复制 .env.example 为 .env.local
pnpm test         # Vitest 单元测试
pnpm test:e2e     # Playwright E2E
```

IAM（启用完整认证时需要 PostgreSQL）：

```bash
uv run alembic upgrade head      # 建表并 seed 权限目录与系统角色
uv run python -m security.cli bootstrap-admin --email admin@example.com --password '...'
```

容器化部署：`docker compose up --build` 启动 api + frontend + nginx 代理（宿主机 `http://localhost:8080`），默认为免 IAM 的本地演示模式。

环境配置：复制 `.env.example` 为 `.env`，填入所需的 API 密钥（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`TAVILY_API_KEY` 等）。样例中还包含 IAM/JWT/SMTP/限流等自有身份配置段。

## 核心架构

正式入口为 `server:app`，研究运行由 `api/native_runs.py` 的 `NativeRuns` 管理。`RESEARCH_ENGINE` 默认且仅支持 `native`；旧检查点由 `api/history.py` 提供只读历史，不能跨引擎恢复。`api_host` 旧 HTTP 执行宿主已经删除；`agents/` 旧循环及文件任务池已经删除，业务源码及基础依赖已无 LangChain/LangGraph，测试迁移和完整部署退出条件仍由 T081 跟踪。

### 1. 运行与阶段流程

`NativeRuns.create → RunConfig.compile → RecoveryStore.create_from_config → RecoverySession → ProductionRunFactory → ResearchPipeline`。

Pipeline 依次执行消息摘要、记忆召回、澄清、研究简报、计划审批、Supervisor 研究、大纲审批、报告生成以及记忆提取。`NativeResearchStages` 负责领域阶段，`PendingDecision` 在 SQL 中持久暂停并在用户决定后继续。报告接入 `NativeReportWriter`，保留各产品策略、完整证据预算、评审修订及 CanonicalReport 发布。

### 2. 研究 Agent

`agentscope_runtime/research_agents.py` 中的 Supervisor 和 Researcher 使用 AgentScope `Agent`、`Toolkit` 与公开 Middleware 扩展。工具仍通过项目协议、权限、出网审批和来源约束。同步委派及异步团队共用领域覆盖契约与完成判断；SQL/框架会话承载恢复状态，不能回退旧 QueryEngine。

`NativeResearchContext` 委托框架自动/主动压缩，将摘要模型调用与卸载工件接到同一恢复账本。保留任务约束、证据 ID 和反馈；回读工具绑定所属会话。预算、取消、失租及未知调用结果不能被普通内容摘要降级吞掉。

### 3. 资源与服务边界

`AS_NATIVE_RESOURCES=host` 用于宿主模型及本地工具；Web 与 Gateway 工具需要 `gateway` 模式、PostgreSQL、LiteLLM 和 Sandbox 控制面。`ProductionRunFactory` 重验身份、冻结配置和资料版本，`production_resources` 管理 Run Key、Gateway 登记、团队与沙箱生命周期。前端继续通过 Next.js BFF 访问正式 HTTP/SSE 路由。

### 4. 持久状态与运行管理

`ResearchSnapshot` 保存阶段完成、研究发现、证据、覆盖、审批及报告产物；`RecoveryStore` 的 SQL 租约和 fence 是业务写入权威。模型/工具操作回执及公共事件同账本持久化，未知外部执行结果隔离而非自动重试。

创建频率和 SSE 限流由 `api/admission.py` 统一管理，非终态并发数读取 SQL；当前支持单 Uvicorn Worker。启动扫描无有效租约的中断运行及已持久化审批。`api/retention.py` 在租约保护下清理原生终态运行、团队会话、资料引用、追踪及工件；正在发布的任务拒绝删除，历史归档保持只读。E2E 启动的进程和专用容器必须在验证后关闭。

### 5. 配置系统（configuration.py）

`Configuration` 是 Pydantic BaseModel，所有字段可通过以下方式配置（优先级从高到低）：
1. 环境变量（字段名大写，如 `RESEARCH_MODEL`）
2. `RunnableConfig` 中的 `configurable` 字典
3. 代码默认值

关键配置项：
- **模型**：`summarization_model`（摘要）、`research_model`（研究）、`compression_model`（压缩）、`final_report_model`（最终报告）、`quality_evaluation_model`（Judge），格式为 `provider:model_name`（如 `openai:gpt-4.1`）
- **搜索 API**：`search_api` 枚举（`tavily`/`openai`/`anthropic`/`none`）
- **Web 证据管线**：`web_pipeline_mode`（`legacy`/`shadow`/`enforced`，默认 `enforced`）
- **并发控制**：`max_concurrent_research_units`（默认 5）、`max_researcher_iterations`（默认 6）、`max_react_tool_calls`（默认 10）
- **质量门禁**：`quality_evaluation_enabled`、`quality_evaluation_rigor`（五档）、`quality_evaluation_min_sources`、`quality_evaluation_max_input_chars`（Judge 输入预算，默认 30000）
- **MCP**：`mcp_config`（URL + 工具列表 + 是否需要认证）、`mcp_prompt`（额外指令）

运行开始后，影响恢复一致性的关键配置会被冻结并写入运行清单。

### 6. 工具系统（tools/）

通用工具协议、治理和本地工具位于 `src/open_deep_research/tools/`。旧 LangChain 工具包装、Supervisor 工具目录及 `tools/utils.py` 已删除；模型不得调用旧任务池或旧团队 RPC。

- **统一协议与投影**：`tools/base.py` 定义 Tool、ToolContext、ToolResult、执行区、副作用及出网目标；`tools/registry.py:prepare_existing_toolset` 统一检查重名、启用状态、权限和模型描述预算。
- **原生执行**：`agentscope_runtime/tools.py` 将项目 Tool 装配到 AgentScope Toolkit，保留公开工具调用 ID、受治理派发、重试、证据观察及回执。
- **搜索与 Web**：`agentscope_runtime/search.py`、`web_tools.py` 负责提供商搜索、Search→Fetch→Extract→Evidence 流水线和来源边界；物理 Gateway 通过 `sandbox_catalog.py` 装配授权工具及嵌套模型。共享 `web/` 领域算法继续保留。
- **MCP 与浏览器**：`agentscope_runtime/mcp.py` 使用 AgentScope MCPClient，保留工具信任校验、stdio/HTTP/SSE、OAuth 和交互错误协议。领域技能提示词已接入 Researcher，报告技能上下文继续沿用领域组件。
- **Supervisor 与团队**：原生 `research_agents.py`、`teams_tools.py`、`team_worker.py` 管理委派、协作和完成判断；不再回到文件任务池。动态工具指导来自最终可用工具集。
- **token 限制检测**：`is_token_limit_exceeded()`（`models/errors.py`）根据模型提供商（OpenAI/Anthropic/Google）检测不同的 token 超限错误模式
- **模型族子包**：`models/` 保留 provider/凭据解析、错误检测、能力元数据、价格目录和 token 上限等公共契约；旧 codec、ModelGateway、invocation、fallback 实现已删除。模型执行由 `agentscope_runtime/models.py`、`model_policy.py`、`model_accounting.py` 管理。
- **模型解析层**：`models/resolution.py` 只负责 provider、API Key/base URL 和兼容参数，不创建 LangChain 模型。Native ModelFactory 从冻结目录和绑定凭据构建 AgentScope 模型。
- **模型回退与熔断**：`agentscope_runtime/model_policy.py` 统一候选链、有限重试、输出恢复和原生事件；共享 `models/circuit.py` 负责进程级 CLOSED/OPEN/HALF_OPEN 状态。
- **MODEL_TOKEN_LIMITS**：位于 `models/limits.py`，用于计算截断阈值；查找采用精确键优先、再按键长度降序的最长子串匹配。注意：此表需要手动维护

### 7. 质量与证据（quality/ 子包：gate/contract/policy / evidence.py）

- **覆盖契约**：从用户消息编译需求清单，每条需求有稳定 ID（`COV-NN-<hash>`）；门禁只对任务归属需求做硬覆盖检查
- **来源契约**：用户消息中的显式 URL 白/黑名单、"仅官方来源"约束会被编译为来源准入范围（`SourceScope`）；受限契约下证据准入 fail-closed
- **覆盖账本**：按需求 ID 跨任务合并 `supported`/`partial`/`unsupported` 状态（单调提升）
- **质量门禁**：`evaluate_tool_results`（内层，工具批次）与 `evaluate_subagent_handoff`（外层，交接）结合确定性硬门禁与 Judge 语义评分；Judge 输入受字符预算约束

### 8. 评估系统（tests/）

LangSmith 评估使用 `tests/run_evaluate.py`，本地评估使用 `tests/run_local_evaluate.py`，两者共用 `tests/evaluators.py` 的核心评估器：
- 本地研究入口通过 `evaluation/local_runtime.py` 调用原生 `NativeRuns`，读取 SQL 恢复状态并关闭运行资源；Web 评估默认使用 `AS_NATIVE_RESOURCES=gateway`，需要既有 PostgreSQL、Gateway、Controller 等依赖。不会回退到旧引擎。身份沿用合法开发旁路或本地 `EVALUATION_ACCESS_TOKEN`，默认单题等待 1800 秒；遇到审批不自动放行。`--resume` 仅复用结果文件，不恢复旧检查点。
- 本地 Judge 通过 `evaluation/session.py` 使用 AgentScope 原生模型与独立 SQL 评分账本，Service Key 的调用、费用与截止时间在本地受限；`--pair-dataset` 配合 `--pair-repeats` 对冻结历史产物进行重复评分。未知调用结果拒绝自动重试；历史产物配对不能代替新研究生成质量验收。
- LangSmith 入口复用同一 NativeRuns 生命周期及原生 Judge；`evaluation-langsmith` 是独立可选依赖，不要求安装旧引擎桥接包。它保留完整输入消息、参考答案和未评分状态；远程实验仅在显式运行入口时提交。
- 10 个评估器：`eval_overall_quality`、`eval_relevance`、`eval_structure`、`eval_correctness`、`eval_evidence_integrity`、`eval_groundedness`、`eval_completeness`、`eval_citation_accuracy`、`eval_tool_efficiency`、`eval_execution_compliance`
- 评估结果通过 `tests/extract_langsmith_data.py` 导出为 JSONL，提交至 Deep Research Bench
- 评估固定使用 Tavily 搜索以保持一致性

### 9. 安全认证（src/security/rbac/）

FastAPI 部署时的认证与授权（Supabase 已完全移除；`src/security/auth.py` 仅保留兼容别名）：
- 本地账号体系：Argon2 密码哈希、邮箱验证、注册审批、密码重置、会话管理
- JWT：EdDSA（Ed25519）签名的 Access/Refresh 双 Token，独立密钥与 kid，Refresh 一次性轮换并检测重用，`authz_version` 支持即时吊销
- RBAC：4 个系统角色（`viewer`/`researcher`/`developer`/`admin`）+ 自定义角色，基于封闭权限目录构建权限矩阵；入口为 `require_permissions()` / `get_current_principal`
- 运行与任务级所有权通过 `require_run_owner()` / `require_task_owner()` 校验
- `LOCAL_DEV_AUTH_BYPASS=true` 且 `APP_ENV=development` 时返回合成 researcher/developer 身份，跳过 JWT/DB
- 数据库迁移位于 `src/security/rbac/migrations/`；CLI 入口为 `python -m security.cli`

### 10. 前端（frontend/）

- Next.js（App Router）+ React + TypeScript，包管理器为 pnpm，端口 3000
- 浏览器只连接 Next.js BFF（`/api/research`、`/api/auth`、`/api/iam` 路由处理器）；Token 保存在 HttpOnly Cookie，由服务端代理注入 `Authorization` 头
- 主要页面：研究创建（`/research/new`）、运行工作区（SSE 实时进度 + HITL 审批 + Subagent 活动抽屉）、设置、登录/注册、管理后台（`/admin`）

## 实现重量约束（防止过度防御性编程）

修复缺陷或实现评审意见时，实现重量必须与威胁模型和实际规模匹配，选择能解决问题的最轻实现。起源：2026-08 Local Documents 修复轮中，AI 助手为"不被后续评审挑刺"普遍选择了偏重的实现（对服务端自生成的 ID 再做白名单校验、简单字典够用时引入 LRU 池、单条带 NOT EXISTS 的 UPDATE 够用时写成 FOR UPDATE 多步事务），虽不影响正确性，但增加了维护认知负担。具体约束：

- **只修清单内的项**：评审或任务给出的问题列表就是范围边界；顺手发现的其他问题先报告，未经确认不主动加固，不重写未被指出的代码。
- **信任上游不变量**：对服务端自己生成、从未接受外部输入的值（UUID、内部路由、枚举），不需要再做格式校验或防御性 re-validate；只有在数据边界（用户输入、外部 API 返回、跨信任域）才做校验。
- **选择最小并发原语**：能用单条条件 UPDATE/INSERT 表达的守卫，不要拆成 FOR UPDATE + 查询 + 多步写入；能被进程内字典覆盖的复用场景（凭据/键位数量为个位数），不要引入 LRU、淘汰清理等通用缓存机制。
- **优先与邻近代码同构**：同类操作（如 retry 与 reindex 的在途守卫）应采用同一模式；如果仓库里已有轻量先例，跟随它而不是发明更重的替代方案。
- **评审意见的字面范围就是意图**：意见说"建议复用客户端"，实现为按 key 的简单字典即可；不要自行升级为带锁、上限、异步淘汰的完整缓存组件。
- **额外加固需要声明**：若确有必要超出意见字面范围（例如安全边界处），在提交说明或回复中单独列出并给出理由，便于 reviewer 区分"被要求的"与"自行加的"。

负面参照（均为 2026-08 Local Documents 修复轮的真实案例，出现同类写法时应识别为坏味道并改用更轻实现）：

- **对内部不变量二次校验**：`canonical_local_source` 对服务端自己生成的文档/chunk UUID 再做正则白名单校验——这些值从未接触外部输入，校验永不触发，纯属死代码路径。
- **简单复用场景过度工程**：embedding 客户端只有"Run Key / 服务 Key"两三种凭据，却实现了带锁 LRU（32 上限）+ 驱逐时异步关闭 + 把常量 `id(AsyncOpenAI)` 塞进缓存键——一个按 `(base_url, api_key)` 的普通字典即可，且 LRU 驱逐引入了"被驱逐 client 可能仍被并发使用"的新风险。
- **同一语义两种重量**：`retry_document` 用单条带 `NOT EXISTS` 的条件 UPDATE 实现在途守卫，`reindex_document` 却写成 `SELECT ... FOR UPDATE` + 查询在途 job + 多步写入——后者应向前者看齐。
- **未被要求的幂等化改造**：把已发布的 `0002_sandbox_permissions.py` 迁移从 `bulk_insert` 改成 `ON CONFLICT DO NOTHING`——不在任何评审意见范围内，修改已发布迁移还需额外说明动机。

## 开发注意事项

- **示例配置说明统一使用中文**：新增或修改 `.env.example`、`.env.*.example` 及其他示例配置文件时，配置项的注释、分组标题、使用说明与操作提示必须使用清晰完整的中文。变量名、协议名和可执行命令保留原文；说明应包含用途、单位、默认值、必填条件、填写格式及关联配置，涉及端口时区分服务接口与管理控制台。示例值使用占位符，不填写真实凭据；真实配置仅保存在本地 `.env` 等不纳入版本控制的文件中。

- 每次执行完E2E验证后，需要关闭验证时启动的前端与后端工作进程，否则可能导致端口占用或资源泄漏
- 使用 docker 执行端到端（E2E）验证时，只启动与本次项目 E2E 运行相关的容器，避免一并启动原 docker 已经停止运行的容器
- 模型必须通过原生 ModelFactory 和执行策略调用；质量门禁必须提供 NativeResearchQuality，报告必须绑定原生报告端口，缺失运行时不能通过 fail_open 掩盖。
- Researcher 由 AgentScope Agent 在独立会话内运行；Supervisor 与异步团队共用原生研究和证据门禁，不调用 `ResearcherQueryEngine`
- API 密钥获取由 `models/resolution.resolve_api_key()` 和原生 CredentialBinding 管理；凭据不进入 SQL 检查点，模型和 Gateway 资源由所属运行统一关闭。
- 七个模型角色支持同名角色级 API Key 覆盖：`SUPERVISOR_API_KEY`、`RESEARCHER_API_KEY`、`SUMMARIZATION_API_KEY`、`MESSAGE_SUMMARY_API_KEY`、`COMPRESSION_API_KEY`、`FINAL_REPORT_API_KEY`、`QUALITY_EVALUATION_API_KEY`
- 模型 fallback：`model_fallbacks` 支持 `supervisor`、`researcher`、`summarization`、`message_summary`、`compression`、`final_report`、`quality_evaluation` 七个角色，仅对限流、瞬态错误和模型不可用切换
- Token 超限处理：研究上下文委托 AgentScope 压缩与卸载；报告保留完整固定输入并按整条证据预算，原生报告端口最多重试三次，真实固定输入超限时拒绝执行。
- 添加新模型时，需要在 `MODEL_TOKEN_LIMITS` 字典（`models/limits.py`）中注册其 token 限制
- Tavily 搜索的摘要模型独立于研究模型，由 `summarization_model` 配置
- 评估脚本 `run_evaluate.py` 中的模型和参数是硬编码的，每次运行前需要手动调整
- ruff 配置使用 Google 风格的 docstring 规范（`convention = "google"`），测试文件忽略 D 和 UP 规则
- `.runs` 目录包含运行数据与 Trace Store，已加入 `.gitignore`，不要提交
- 前端统一使用 pnpm（不要用 npm/yarn 生成锁文件）；`NEXT_PUBLIC_*` 变量在构建期烧入客户端代码
- 生产部署当前推荐单 Uvicorn worker；进程内 API 限流、SSE 连接数和运行表不会跨 worker 汇总，SQLite Trace 多 worker 写入仅为 best-effort
- 高级记忆维护建议使用系统定时器每天执行一次，或运行 `python -m open_deep_research.memory.maintenance daily --loop --interval-hours 24`；循环会持有 `.runs/memory-maintenance.lock` 防止重复执行
- 沙箱资源隔离不等于密钥隔离：容器内代码可读取 `SANDBOX_SECRET_ENV_KEYS` 指定的凭据；只注入最小权限密钥，设置 `SANDBOX_SECRET_ENV_KEYS=` 可禁用全部密钥注入

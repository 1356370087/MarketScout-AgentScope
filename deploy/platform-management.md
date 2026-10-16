# 平台管理与数据库配置

研究工作台与管理后台使用同一个前端应用及登录会话。拥有后台权限的账号可以通过侧栏顶部按钮切换；后台入口为 `/admin`，首次初始化入口为 `/setup`。

## 启动与初始化

真实服务默认使用 `PLATFORM_CONFIGURATION_MODE=database`。数据库连接、监听与网络、共享目录、JWT 签名密钥、Sandbox 签名根密钥和平台凭据加密主密钥由部署环境提供。业务设置从数据库中的生效版本读取。

启动前完成以下准备：

1. 配置 `IAM_DATABASE_URL`，通过现有业务迁移安装平台表及权限：`uv run alembic upgrade head`。从源码运行前按仓库说明配置 `PYTHONPATH`。
2. 设置 `PLATFORM_SETUP_TOKEN` 为随机初始化口令。首位管理员创建后，初始化接口关闭；已有管理员的部署直接登录。
3. 设置 `PLATFORM_SECRET_ENCRYPTION_KEY`。它必须是 Base64 编码的 32 字节随机密钥，用于数据库凭据的 AES-GCM 加密。保存凭据后应保持主密钥稳定，并与数据库备份分别保管。
4. 配置现有 IAM 签名密钥。开发环境允许临时生成 JWT 密钥，但这种会话会随进程重启失效；持续开发与生产应提供稳定密钥。
5. 启动 API 和前端，访问 `/setup` 创建管理员。管理员同时获得研究员角色，初始化完成后进入配置中心。

PostgreSQL 使用 AGENTS.md 指定的远程服务，Compose 不部署数据库容器。启动前提供 `IAM_DATABASE_URL`、`DOCUMENT_DATABASE_URL`、`COORDINATION_DATABASE_URL` 和独立库的 `LITELLM_DATABASE_URL`；gateway 模式还需 `AS_DATABASE_URL`。IAM 与 AgentScope 使用 `postgresql+asyncpg://`，文档、团队和 LiteLLM 使用 `postgresql://`。密码只保存在未跟踪的环境文件中，URL 中的密码需正确编码。E2E 使用远程专用数据库，迁移不覆盖已有业务库。

后台与本地调试均使用真实 IAM 账号。前后端认证旁路设置应为 `false`。`explicit` 模式用于离线测试、显式夹具和兼容验证。

根 Compose 已配置 `platform-config` 共享卷及真实登录。API、文档/知识库 Worker、发布 Worker 和 LiteLLM 使用该卷；沙箱控制面只读挂载。发布 Worker 保持无网络配置。

### 研究资源模式

平台配置系统需要 PostgreSQL；研究执行资源仍由 `AS_NATIVE_RESOURCES` 决定。

- `host` 使用直连模型和本地工具。按现有运行契约配置 `model_backend=legacy`、关闭提供商搜索、使用 `web_pipeline_mode=legacy` 并关闭沙箱。
- `gateway` 使用 LiteLLM 和 Sandbox Gateway/Controller。提供 `AS_DATABASE_URL` 并执行原生存储迁移；团队模式另需相应 MQ、Worker 与网络。具体拓扑沿用 `deploy/compose.native-e2e.yaml`、`deploy/compose.web-e2e.yaml`。

数据库配置发布与组件就绪是两个状态。平台概览还显示研究运行时的启动状态；组件检查不能替代真实研究链路验收。

## 导入、编辑与发布

配置中心支持 `.env`、LiteLLM YAML、沙箱 TOML 和项目 JSON 导出包。管理员还可读取服务器当前已配置的导入来源。导入预览展示字段数、凭据数、部署字段和未识别字段，确认后保存草稿。

导入的秘密存入加密凭据表。配置中使用 `credential:凭据编号` 引用；已发布版本将引用固定为 `credential:凭据编号@版本号`。JSON 导出包保留字段锁定与预算规则，导出内容不包含凭据明文。

草稿保存完整默认值。发布流程包括校验、与当前生效配置比较、填写变更原因和确认锁定。已发布版本不可原地编辑；调整时复制为新草稿。回滚也通过新的发布记录完成，保留完整审计。

管理员在字段旁决定用户是否可以调整，以及允许的范围。预算、并发和容量采用平台默认值加用户覆盖。普通研究请求、个人模板和直接 API 请求均经过服务端策略校验。

个人模型使用本人凭据，并受平台允许的提供商与端点限制。模型的物理部署名带内容摘要；运行引用具体部署版本，旧部署继续保留。其他用户不能通过猜测部署别名取得个人模型的使用权。

### 凭据轮换

替换凭据产生新版本。已发布配置保留原绑定；需要使用新凭据时，在配置草稿中更新对应引用并发布。停用凭据会阻止其后续调用，运行恢复时也会检查当前可用性。

OSS 记忆后端通过配置中的 `memory` 对象管理。模型或嵌入提供商的秘密使用凭据引用；数据库模式下不再在运行时读取 `MEM0_CONFIG_PATH` 的业务内容。

## 组件应用与重启

发布版本保存在 `PLATFORM_CONFIG_DIR`，默认目录为 `RUNS_DIR/platform/config`。数据库是权威来源，目录内文件是组件运行产物。

- API 在线读取生效配置；新研究保存平台版本、模板版本、有效参数和指纹。
- LiteLLM 模型部署通过控制接口同步。路由器全局设置需要重启代理，启动器从待应用版本生成临时配置并解密必要凭据。就绪后持续写入版本回执。
- 沙箱策略变更后重启 Controller/Gateway。管理 API 检查两个组件实际报告的策略摘要；历史运行可以使用保留的策略版本。
- 文档与知识库 Worker 在配置缺失时等待，并为任务绑定配置版本。涉及组件参数的变更通过 Worker 回执确认。
- 发布 Worker 从共享卷读取设置；每个新发布任务记录配置版本，执行时读取对应快照。

在 Compose 中按后台列出的组件安排重启，然后点击“重新检查生效状态”。同一版本的必要组件全部就绪后，才切换平台生效指针。

非容器沙箱组件可以使用以下入口加载待应用版本：

```bash
uv run python -m open_deep_research.platform.component_runner --component sandbox-controller
uv run python -m open_deep_research.platform.component_runner --component sandbox-gateway
```

需要指定版本时增加 `--revision 配置版本编号`。这些入口使用已发布的配置产物，不提供网页端任意命令执行。

## 用量、配额与维护

月度费用按 `Asia/Shanghai` 自然月统计，以整数微美元保存。费用包含已结算金额和在途预留；额度不足时拒绝新增付费调用，已发出的调用继续结算。

原生研究、Gateway 嵌套调用、嵌入、知识检索/生成、记忆维护和连接测试接入统一月度入口。具有可验证事件时间与费用的历史原生回执会幂等导入。缺少时间或价格的记录不按零费用编造账单。

未知调用结果保留预留。服务调用可以按 LiteLLM 请求编号核对账单；原生调用沿用原有恢复和费用核对机制。确认未执行的操作才释放预留，恢复或重放不会重复扣费。

第三方搜索、OCR、托管记忆等缺乏统一价格或账单接口的服务，其外部账单与本系统模型费用分别核对。

运营接口只投影用户、状态、费用、错误分类和关联编号。研究、报告和资料内容仍按业务权限读取。维护操作包括取消研究、失败文档重试、重索引、可重试发布任务及既有知识库维护任务；记录操作者、任务所有者和原因。

## 升级与验证

升级使用维护窗口完成业务配置切换：暂停新任务，正常结束或中断在途工作，运行迁移，导入现有配置并比较差异，发布并核对组件状态，再恢复准入。

历史运行保留原有所有权。开发认证旁路产生的运行不会自动转交新管理员；旧运行缺少可验证的配置或组件信息时，需要先确认其恢复条件。

针对性验证入口：

```bash
uv run pytest tests/platform
uv run pytest tests/as_runtime/test_model_accounting.py tests/as_runtime/test_recovery.py
uv run python -m open_deep_research.migration_check --check all
```

`tests/platform/test_postgres.py` 仅在设置 `PLATFORM_TEST_DATABASE_URL` 后连接专用测试库；`tests/platform/test_litellm.py` 使用专用 `PLATFORM_TEST_PROXY_URL` 验证控制接口，不调用真实模型。前端继续执行 lint、类型检查、单元测试、构建及对应 E2E。

### 本次验证记录（2026-10-14）

| 验证范围 | 结果 |
| --- | --- |
| 控制面、权限、计账、恢复、文档建议和报告发布针对性回归 | 177 项通过 |
| 平台新增测试复核 | 17 项通过，包含真实 PostgreSQL 与 LiteLLM 控制接口 |
| 前端单元测试 | 160 项通过、1 项跳过 |
| 桌面与移动端 IAM E2E | 2 项通过；2 项付费研究用例按配置跳过 |
| 前端类型检查、生产构建、本次改动 lint | 通过 |
| 新增平台后端模块类型检查、原生迁移静态检查、Compose 配置检查 | 通过 |

浏览器另验证了首次管理员创建、配置导入、锁定策略保留、未保存草稿提示、保存后切换、配置发布与研究启动确认。全量前端 lint 仍有知识库和审批页面中的 7 个既有错误。本次未调用真实付费模型，也未将组件接口验证计为完整生产部署验收。

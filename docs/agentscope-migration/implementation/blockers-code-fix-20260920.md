# 企业资料、报告交付与原生边界修复（2026-09-20）

本轮按用户要求实施代码修复，不执行生产切换、不删除 QueryEngine、不修改旧运行的 quarantined 操作。开始时工作区已有上下文与评估迁移改动，本轮在其上增量修复；未提交或推送。状态权威为 `../07-progress.md`。

## 企业资料 UnknownOperation

源码确认的缺陷链为：`production_resources.tools_for` 把 Gateway 目录中的 `search_documents` 替换为 HOST_CONTROL 本地实现；资料检索的 embedding 调用要求 `current_run_key()`，生产资源层没有给宿主/团队 Worker 绑定 Run Key；Gateway 的工具执行入口才绑定该凭据。检索错误返回后，`RecoverySession.tool` 仅把 READ_ONLY 或显式幂等工具视为可重放，把 SENSITIVE_READ 当成非幂等写入，再次 begin_operation 导致隔离与 UnknownOperation，掩盖原始读取错误。这条链已通过可控夹具复现；未从真实历史运行重新取回原始 embedding 错误，不把源码分析冒充历史日志归因。

修复内容：

- 删除本地替换，企业资料检索使用 Gateway 目录与已有代理，Worker 不接收 Run Key。
- 仅资料来源启用时为 Run Key 增加配置的 embedding 模型权限。聊天候选目录仍按聊天能力与价格契约验证，embedding 不伪装为聊天模型。
- 恢复层和 Gateway 工具预算将 SENSITIVE_READ 与既有工具治理中的读取重试语义对齐；执行仍经过权限与审批，非幂等外部写入继续隔离。已提交的错误回执按原结果重放，不重复调用，也不把原错误改成 UnknownOperation。
- 根 Compose 为 Gateway 接入与 API 相同的资料数据库及 document-internal 网络，显式提供 embedding 别名与维数；Gateway 关闭时释放资料连接池与 embedding 客户端。
- 默认沙箱策略精确允许 `search_documents`，没有开放全部 sensitive_read。新增管理员策略 `allow_tools`，顺序为工具名称拒绝、精确名称允许、效果策略；工具权限、资料选择和资料所有权照常校验。部署时 API、Gateway 与 Controller 应使用同一更新后的策略文件，保持策略摘要一致。

## API 宿主与原生边界

将十个同时服务旧执行路径/兼容宿主的模块迁至 `open_deep_research.api_host`：operations、run_access、run_admission、run_execution、run_interactions、run_recovery、run_registry、run_retention、run_start、security_routes。`server.py` 更新装配引用，原生 `api/` 不再直接包含这些旧执行依赖。

迁移前后十个文件的 AST 比较确认仅导入路径变化，业务逻辑相同。架构守卫新增禁止原生 runtime/api 导入 `api_host` 或 `server`，独立解释器检查扩大到 NativeRuns 和正式原生 research_router。未对白名单放宽旧依赖规则；retirement 仍不能通过，宿主、旧引擎与 LangChain 桥接保留到切换验收后处理。

## 长输入与正常报告交付

沿用工作区已实现的整条证据预算，本轮新增跨层验证：四种 source mode 都使用 100 条较长证据及约 51 万字符冗余交接文本，模型窗口为 24000；检查预算内输入、证据尾部完整、遗漏计数和原始证据不变。测试经过实际 NativeRuns、SQL 检查点、研究 Pipeline、计划/大纲审批、报告评审与发布 HTTP 路由。

该验证发现 `finalize_report` 在评审/修订后没有重新生成 CanonicalReport，导致原生最终产品丢失发布所需的结构化报告。已用最终评审正文和来源重新生成；证据受限恢复的 partial 状态优先于原始状态保留。新增正文摘要校验确保不会重新发布旧草稿。

四种模式均通过严格评审（fail_open=false，无 skipped/degraded），并实际生成及下载 Markdown 和 PDF。此处研究交接、模型和 Judge 使用确定性夹具；不是四来源真实搜索/检索/模型/浏览器 E2E，不代替生成质量验收。来源目录回归对齐 SDK 自带 CompressContext，保留原来源工具集合和隔离断言。

## 验证结果

| 批次 | 结果与范围 |
|---|---|
| [原生最终联合](../evidence/blockers-native-20260920.xml) | 163 passed / 4 warnings，77.63 秒。资料权限/代理/凭据/回执、预算恢复、报告预算/格式/强杀恢复、四来源交付、来源过滤、架构检查 |
| [扩大兼容回归](../evidence/blockers-compat-20260920.xml) | 150 passed / 5 failed / 91 warnings，19.77 秒。共享报告、发布、沙箱、兼容 API 与生命周期；不登记整批通过 |
| [原始 HEAD 对照](../evidence/blockers-head-baseline-20260920.xml) | 5 failed / 91 warnings，16.14 秒。独立导出的 HEAD 源码与测试中复现相同五项失败，未修改当前工作区来做对照 |
| [原生静态边界](../evidence/blockers-native-imports-20260920.json) | passed，无旧执行或宿主组合根导入 |
| [镜像静态配置](../evidence/blockers-deployment-20260920.json) | passed，仅解释器/依赖组检查，不代表镜像构建或部署验收 |

五项既有失败分别为：两个 publication SSE 测试仍 monkeypatch 已不存在的 `server.get_publisher_settings`；旧 sandbox payload 字段断言漏列 team_coordination_enabled；旧结构化 Gateway 夹具仍返回旧 ModelResult；取消测试未把既有 task-activity 回传计入调用序列。保留原断言及失败记录，未为本次收口修改这五项。

本轮新增/迁移模块及其余修改文件的 Ruff F 检查通过；扩大到 server.py 与 recovery.py 时另发现 14 条既有未使用导入/重复导入诊断，不宣称全仓 Ruff 通过。`git diff --check` 通过。早期夹具的权限、HTTP 受保护配置和创建状态码已对齐实际契约后重跑；原生总数以最终联合为准，不与前序批次累加。

本地测试因 Windows 沙箱临时目录权限问题在批准的沙箱外执行，仍仅使用隔离 SQLite、临时文件与可控模型。未启动前后端或业务容器，测试内启动的发布/恢复子进程由夹具关闭。

## 后续验收

T072/T064～T068/T077 本轮均不升级完成，整体保持 66/81。需要在更新镜像与配置后重新执行真实企业资料检索、四来源研究、长输入和正常报告交付；同时补 PostgreSQL/Gateway/Controller/浏览器联调、真实费用核对与视觉验收。旧 UnknownOperation 运行不自动解封或盲目重放；T080 切换演练及 T081 旧引擎物理清退仍未执行。

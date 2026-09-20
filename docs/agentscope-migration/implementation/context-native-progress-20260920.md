# 上下文原生化实施记录（2026-09-20）

本轮执行用户确认的 AgentScope 2.0.8 上下文统一迁移及旧引擎清退计划。用户随后明确：先继续代码变更，注明受影响功能，E2E 联调及错误修复后续统一执行。实际状态只维护于 `../07-progress.md`。

## 已确认边界

- M9 T064～T068、M11 T075～T081 均关联；T037 需要新增原生闭环证据，M6 预算恢复及 M10 已完成模块按影响复验。
- 保留报告完整证据选取预算、覆盖与来源准入规则、历史只读兼容；通用窗口管理迁移给框架。
- 本轮仅仓库与隔离部署，不操作实际生产运行或真实 `.env`。E2E 放行前保留旧引擎及默认选择，不把本批标为全迁移完成。

## 本批实现

- 补齐宿主 `RunContextOffloader.offload_tool_result`，支持 AgentScope 原生工具截断调用；保留卸载的 session ID 和完整框架消息块。
- 新归档放在运行目录的 `context/offloaded/`，与清单、凭据及报告存储隔离。既有文件不移动、不删除；旧归档的历史访问不转成任意文件读取工具。
- 增加仅当前 run/session 可读的分页读取入口，session 由调用侧绑定，模型不能指定身份；读写均检查当前租约，拒绝跨运行、跨会话和目录越界。
- `ReadContextArtifact` 经统一工具治理装配到 Lead 和 Researcher；回读内容不触发新的工具批次质量评估，也不会作为新证据准入。
- 沙箱 Workspace 已提供相同的分页读取接口，限制当前 workspace 的卸载目录及 session；既有 `recall` 内部接口保持原状。

## 仍待接线与验证

- 公共上下文配置及冻结映射、业务约束投影预算、外层阶段请求视图；研究与报告的大输入回归。单次写作的 `NativeContextCompactor` 调用者暂留。
- 宿主多模态 `offload_data_block` 接口及图片卸载验证；工具结果和历史消息文本卸载已经接线。
- 真实模型生成摘要后主动回读归档细节；组件测试已验证历史压缩和工具截断提示中的引用可以回读，尚未替代模型行为验收。
- 完整 E2E、M9/M11 关联验收及通过后的旧引擎清退。不得仅通过删除旧代码或测试关闭这些门禁。

## 第二批：原生压缩及恢复接线

- `NativeResearchContext` 使用公开 `on_compress_context` 调用 `next_handler`，由 AgentScope 2.0.8 决定阈值、完整消息分组、结构化摘要、近期窗口及降级；不复制框架私有压缩实现。Lead/Researcher 共用配置，自动阈值 0.8、保留比例 0.1、提示缓冲 0.2、主动工具开启，工具 token 限额为模型窗口 10% 与 8192 的较小值，图片上限 5，注入配置沿用 UTC。
- 摘要沿用所属 Agent 模型。模型代理仅在压缩作用域将公开结构化调用转交 `ResearchModels.context_summary`，经既有候选策略、物理计账及恢复模型回执处理；普通模型调用保持已有中间件责任链。
- 每次框架压缩检查作为独立恢复操作保存 AgentState，嵌套摘要使用独立稳定操作键；重放已提交状态不再次调用模型。保存结构化摘要调用回执、近期消息、卸载引用及压缩前后 context token 数、摘要失败/降级信息。
- 格式错误或已知模型瞬态失败保存失败摘要回执，再交框架执行裁剪回退；预算拒绝、取消、失租、认证及存储失败传播到研究边界。框架摘要与主动工具会捕获 Exception，因此使用仅限该边界的控制信号穿透，并在研究调用出口恢复原异常。
- 归档失败不提交压缩状态，恢复时在新租约下复用摘要回执。物理失败回执重放后的格式异常识别已经补齐，避免同一失败首次可降级、恢复后却终止。
- 任务初始约束与最近质量反馈单独保存在 middle_context，最新用户反馈从恢复快照更新，压缩提示要求保留任务/证据/需求 ID、来源、反馈和卸载位置。投影预算和更完整的领域状态更新仍待后续接线。

第二批验证：**125 passed / 5 warnings，34.25s**。新增 `test_native_context.py` 13 项专项，使用真实 SDK 压缩器和可控模型、临时 SQLite；涵盖自动/主动压缩、连续摘要、成功及失败摘要回执恢复、物理回执提交窗口、归档失败、预算拒绝、取消与失租。测试初期修正 tokenizer 调用必须传 tools 的接线错误，并按 SDK 无参数工具协议和新租约恢复协议修正测试夹具；未降低业务断言。

```powershell
$env:PYTHONPATH = 'src'
$contextTestTemp = Join-Path (Get-Location) ('.runs/context-native-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe -m pytest tests/as_runtime/test_native_context.py tests/as_runtime/test_context_offload.py tests/as_runtime/test_research_migration.py tests/as_runtime/test_recovery.py tests/as_runtime/test_e2e_closeout.py tests/as_runtime/test_sandbox_workspace_policy.py -q --disable-warnings --basetemp $contextTestTemp
```

新批次与首批有重叠，不累计通过数；5 个改动 Python 文件 AST 检查、`git diff --check` 通过。无生产运行 ID：本批为临时恢复库组件测试，未启动前后端、Worker 或容器。E2E 及真实依赖计账/质量验收保持待执行。

## 本批组件验证

- 首批卸载与生产边界组件：20 passed，4 warnings。
- 加入研究 Agent 和沙箱 Workspace 接线回归：**71 passed，5 warnings**。与首批重叠，不累加。覆盖四份测试：`test_context_offload.py`、`test_e2e_closeout.py`、`test_research_migration.py`、`test_sandbox_workspace_policy.py`。
- 新测试使用真实 AgentScope `Agent.reply`、工具截断与 Toolkit，以及临时 SQLite 恢复库，验证截断引用、完整尾部内容回读、分页重组、会话/目录隔离、失租读写拒绝和工具权限撤销；沙箱使用可控后端，没有启动 Docker。
- 首次调用缺少 `PYTHONPATH=src`，补齐后被 Windows 沙箱的 pytest 私有临时目录 ACL 阻断；改用受批准的沙箱外测试进程和工作区内独立临时目录后通过，未修改测试断言规避权限验证。
- 原生虚拟环境未安装 Ruff，不能登记 Ruff 通过；以组件回归、Python AST 语法检查和 `git diff --check` 核验本批代码。

复现组件批次（PowerShell，要求运行身份能访问 pytest 创建的私有临时目录）：

```powershell
$env:PYTHONPATH = 'src'
$contextTestTemp = Join-Path (Get-Location) ('.runs/context-native-tests-' + [guid]::NewGuid().ToString('N'))
.venv/Scripts/python.exe -m pytest tests/as_runtime/test_context_offload.py tests/as_runtime/test_e2e_closeout.py tests/as_runtime/test_research_migration.py tests/as_runtime/test_sandbox_workspace_policy.py -q --disable-warnings --basetemp $contextTestTemp
```

本批未执行 E2E，测试文件名称 `test_e2e_closeout.py` 不代表本次完成了浏览器或真实依赖联调。未连接真实模型、未启动前后端服务、未切换引擎、未删除旧引擎，也未升级 M9/M11 完成状态。

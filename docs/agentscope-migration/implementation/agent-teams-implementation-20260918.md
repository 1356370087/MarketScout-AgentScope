# Agent Teams 实施与验收记录

更新日期：2026-09-18。依据用户批准的修订方案实施；本记录区分编码、组件测试与真实 E2E，不改变原迁移任务总表的完成计数。

## 交付台账

| 阶段 | 编码 | 组件验证 | 真实验收 |
|---|---|---|---|
| AT-1 | collaborator 默认保留；teams 显式 TeamCreate；团队与成员执行方式冻结、继承及覆盖 | 创建权限、重复创建、恢复身份和混合模式通过 | 已实际启动混合团队 |
| AT-2 | 0017 新增迁移；依赖单边存储、双向投影；派发、CAS 认领、5 次抖动退避 | 20 并发仅一次成功；同成员单任务；环和失效租约拒绝；质量阻塞通过 | 两项上游 accepted 后解锁下游并由 mq 自主认领已验证 |
| AT-3 | SendMessage / ListAgents；文本与结构化消息；PG outbox、完整 MQ 信封、稳定消费组、回执与应用检查点 | 文本不执行控制、越权拒绝、重复投递和发布确认丢失重投通过 | 远程 RocketMQ 发布与接收、真实模型结构化计划响应通过 |
| AT-4 | 一个 Docker 对应一个稳定成员；连续多任务；30 秒租约、10 秒续约、失效代次隔离 | 同一成员连续处理两项、Worker 恢复与提交守卫专项通过；同运行 fence 的安全操作接管及 planned 窗口强杀回归通过 | 两容器、取消清理、空闲成员恢复通过；07c1a419 运行中活动压缩调用强杀后原任务恢复并 accepted |
| AT-5 | direct / planning / awaiting_plan_review；逐任务计划版本、Lead 理由、3 次自动修订后人工续修；质量准入与关闭 | 驳回、修订、显式批准、旧版本拒绝；规划阶段 Gateway 禁工具通过 | 真实门禁拒绝、Lead 补证和依赖调整已观察；完整审批和报告闭环待收口 |
| AT-6 | 前端模式选择、任务板、依赖、成员模式、计划历史、消息投递、人工续修；Prometheus / Grafana 团队指标 | TypeScript 通过；原生 HTTP 和兼容专项通过 | 可见 Playwright 浏览器使用公开网络运行；完整成功场景待收口 |

## 已执行的组件测试

- `tmp/teams-list-suite.log`：57 passed，覆盖紧凑 TaskList、完整 TaskGet 与团队/研究回归；原始失败为列表正文超过 30000 字符，后续任务被截断。
- `tmp/teams-early-feedback.log`：1 passed，TeamCreate 前的全局反馈通过运行检查点保存，不向尚不存在的 Lead 邮箱发送。
- `tmp/teams-judge-error2.log`：4 passed，格式修复反馈包含具体校验错误，重试次数与独立回执不变，最终错误仍拒绝。

- `tmp/teams-release-regression.log`：83 passed，覆盖团队、研究兼容、原生 HTTP、质量和观测。
- 最新工件复用修复后 `tmp/teams-final-artifact-tests.log`：35 passed，覆盖研究兼容、中文引用分隔符、压缩纠错与上游已接纳工件复用；拒绝和未完成工件不能进入证据。
- `tmp/teams-control-idempotency.log`：91 passed，覆盖最新显式创建参数、SQL 幂等消息错误回执、恢复与研究兼容。
- `tmp/teams-citation-all-boundaries.log`：8 passed，覆盖中文左右括号、反引号、真实未登记引用拒绝与工件复用。
- `tmp/teams-union-restored.log`：25 passed，覆盖团队域与 Worker。
- `tmp/teams-final-api.log`：33 passed / 1 skipped，覆盖团队域、原生 HTTP、团队兼容。
- `tmp/teams-regression4.log`：47 passed，研究兼容与观测专项。
- `tmp/teams-resumed-tests.log`：51 passed / 1 skipped，较早联合批次。
- 前端 `pnpm exec tsc --noEmit` 通过；上述批次有重叠，不相加为测试总数。
- 旧环境 `tests/test_tool_governance.py`：73 passed / 11 failed；失败集中于 legacy Tavily 适配器把注入参数 config 暴露为必填，未将该环境报告为全绿。新 SendMessage 字符串/结构化联合参数已有独立回归。

## 真实运行记录

部署项目仅为 `insightforge-teams-e2e`，前端入口 8088，模型网关入口 4188。使用本地 `.env` 与 `config/litellm.yaml`，未在文档保存凭据，未启动知识库服务。远程 RocketMQ 已为本次验证配置独立前缀 `insightforge_teams_e2e` 的 NORMAL topic：`broadcast_wake_v1`、`team_events_v2`、`team_control_v2`；完整投递使用后两者各自稳定 `_delivery` 消费组。其他项目容器不属于本次清理范围。

| 运行 | 观察与修复 | 结论 |
|---|---|---|
| `423ba35a1e1d5353b9d54396e06b64f5` | Lead 建队前读取 Inbox 被拒绝；修复尚未建队时的空 Inbox | 失败，不计通过 |
| `2f0895ea87ac5c0eaba7b4a4b8650c40` | 两成员、真实工具和 MQ 正常；成员计划错误扩大需求范围，改为运行时绑定已分配需求 | 已前端取消，无遗留成员容器 |
| `fdc4b954796c5620843e8e45b37d0d8f` | 发现治理校验只选择 anyOf 第一分支，拒绝结构化正文；修复联合类型校验 | 已前端取消，不计完整通过 |
| `460bcbcfa904586397856d4ed50e1bef` | direct 实际搜索与抓取，质量拒绝后保持依赖；Lead 创建补证。模型仍把计划控制消息发成文本；添加字段说明和对象示例。热更新后旧操作因工具 Schema 不同被重放一致性校验拒绝 | 失败；不将跨版本热更新当成同版本恢复成功 |
| `25a74f0f71875b4fbd1546dde3e1b5ef` | 模型仍将控制正文序列化为文本，后以扁平 Schema 修复 | 已前端取消，两成员 closed，无孤儿 |
| `87200843996850dfa4b9688445ccc0e4` | Lead 真实驳回 v1、批准 v2；PostgreSQL 任务 accepted；MQ 完整信封无积压。空闲 db 容器 SIGKILL 后发现消息处理输入包含动态任务板，重放冲突导致连续重启 | 已前端取消；故障已定位修复，恢复复验待执行 |
| `bac6e443c43d5ccbbdcf48213b6f8497` | v1 驳回、v2 批准；db 强杀后同一身份 epoch 2 恢复并执行第三项任务。另发现压缩引用误含中文右括号及输出未登记 URL，已修复分隔符并增加一次受限纠错 | 恢复通过；研究质量未通过，已前端取消，不计完整成功 |
| `1fc80949550458959b8665f3fea03db8` | 两个独立混合模式成员；v1 驳回、v2 明确批准；两项上游 accepted 后下游解锁，mq 自主认领并重新提交计划；直接成员消息、零积压。汇总被误分配 TeamCreate 事实需求导致门禁拒绝 | 已前端取消；新增团队流程约束分类，未放宽事实证据门禁 |
| `c2686df0a9a555089812593f0ab6ea73` | 流程约束分类正确；发现模型省略 TaskCreate 需求、负责人和依赖后，兜底范围扩大且创建后派发存在竞争；补证期间 SendMessage 错误触发 UnknownOperation | 已前端取消；创建工具要求显式填写三项调度字段；PG 幂等协调工具补恢复层声明，待新镜像复验 |
| `bf13562da8cf57199e0142e487381d6f` | 新镜像正确绑定单主题需求和成员，v1 驳回后批准 v2，文本广播与结构化消息正常；Judge 缺失必填字段耗尽三次格式修复。另定位合法 URL 后中文左括号误判并修复。成员强杀后 epoch 2 启动，但活动任务发生 RecoveryConflict；Lead 后续补证未在协调上限内完成 | failed：team_lead_stopped_before_team_completion；不计完整 E2E 通过，末次 SQL 证据保存于本地 tmp/teams-final-evidence.json |

补充：`25a74f0f71875b4fbd1546dde3e1b5ef` 已前端取消，两名成员均 closed，无孤儿容器。通过配置中的 `if-supervisor-v1` 做三个独立协议探针：原始联合 Schema、展开引用后 Schema 均输出字符串；扁平 object/string Schema 输出对象。因此只调整 SendMessage 模型参数投影，服务端仍使用 Pydantic 判别联合严格校验；不解析文本控制协议。记录 `tmp/teams-schema-probe.log`。

空闲消息恢复修复：先在成员 SQL 会话保存事件对应的完整模型输入，再执行模型；提交摘要与接收回执时一并清除待处理输入。即使中途任务板变化，恢复仍使用同一个输入。`tmp/teams-discussion-fix.log` 16 passed，包含该故障回归、失效计划代次、失败运行清理与 Schema 投影。研究失败现在会在同一运行检查点事务内终结尚未完成的团队任务、计划和成员租约。

重启另发现历史团队页依赖已启动的运行资源而返回 503；已改为按需打开 SQL 基础设施，历史读取不创建业务团队、不启动模型。

补齐获批计划与实际执行的连接：研究上下文明确携带当前计划正文、版本和 Lead 理由；规划提示词区分审批前禁止调用工具与获批后的执行步骤。`tmp/teams-approved-plan-tests.log` 28 passed。需求分类后的联合回归 `tmp/teams-release-final2.log` 105 passed；后续调度参数及幂等补丁另行验证，不将该批次冒充最终全部代码测试结果。

## 验收边界

2026-09-18 续验：`6ddd71b4…` 失败暴露活动补证期间的旧失败依赖使 WaitForTeamEvents 空转，已修复并通过等待专项；`b7df31ba…` 两项初始研究 accepted，汇总成员因 TaskList 载荷截断看不到第二项而失败，已按参考源码的摘要列表/详情读取职责修复。当前 `809657a6…` 使用新镜像验证列表修复；前端曾提供官方来源线索，并纠正 Lead 将补证依赖指向已拒绝任务的规划错误，必须登记为有人工方向反馈的 E2E。创建前反馈修复、补证提示词与具体 Judge 格式错误提示仅完成组件验证，尚未热更新到该运行。

已取得真实证据：混合成员、逐任务计划驳回再批准、成员点对点消息、两项上游 accepted 后依赖解锁与自主认领、取消后无孤儿、空闲成员恢复并继续新任务。尚未取得同一完整成功运行的报告与优雅关闭，不应把分散场景通过合并成一次完整 E2E 通过。

原 P0 恢复冲突已定位为成员接管没有改变运行级 fence，已在任务租约接管事务中仅放行该任务的 replay_safe 未完成操作；07c1a419 运行强杀活动压缩调用后，同一任务恢复并 accepted。另据原始回执修复空等待消耗 Lead 轮次、汇总任务参数及 V2 响应丢失后的回执查询；分析依据见 [原文与根因核对](agent-teams-root-cause-20260918.md)。最新回归分别为 70 passed、负责人参数 3 passed、V2 回执 62 passed（重叠批次不累加）。配置中的 Judge 仍可能耗尽格式修复，不能自动填评分或把拒绝结果当作 accepted；完整报告收尾由 6ddd71b4 新运行验证。

真实用量页面已展示预算名称、工具成功率、任务调用计数、Token 时间图及 supervisor/researcher/quality_evaluation 等角色统计，未出现 undefined；本地截图 output/playwright/teams-live-usage-roles.png。最新中文左括号解析修复已通过组件测试，并在两成员恢复时加载；由于本轮最终失败，不将其登记为完整报告复验通过。

同版本成员失租恢复与跨版本变更工具 Schema 是不同场景：后者目前会被重放输入一致性校验拒绝，部署前应排空活动团队。可观测性代码已提供统计和看板；本地 `.env` 的外部观测开关关闭，本次未声明 Langfuse / OTel Collector / Prometheus / Grafana 真实整栈验收通过。

本次启动资源的最终关闭情况将在 E2E 结束后补记。


2026-09-19 最新验收：质量修复后 `fd178735…` 三项任务附保留意见接纳，原 mq 拒绝后补证通过、成员优雅关闭，最终报告大纲因上下文超限失败。Lead 依赖替换另有调度缺口；不登记完整 E2E 通过。详见 [质量修复与回放记录](quality-closeout-20260919.md)。本次 Compose 资源已 down（保留数据卷），浏览器已关闭，未停止其他项目服务。

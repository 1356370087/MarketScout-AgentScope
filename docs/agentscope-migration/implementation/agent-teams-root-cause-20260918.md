# Agent Teams：原文、源码与失败证据核对

2026-09-18，按用户要求先核对原文和代码，再实施针对性修复与 E2E。这里区分已复现根因、源码推导和外部模型失败，不将容器重启等同于任务恢复。

## 依据与优先级

1. 用户批准的修订方案：RocketMQ、PostgreSQL CAS、独立 Docker 成员、Lead 显式审核、同身份租约恢复。与参考资料不同的地方以该方案为准。
2. [Agent Teams 官方说明](https://code.claude.com/docs/en/agent-teams)：重点核对 Assign and claim tasks、Have teammates plan before implementing、Shut down teammates、Limitations。当前网页明确存在计划自动批准和会话恢复不重建进程内队友的限制；本项目不能因此取消用户要求的显式审核或恢复能力。
3. 本地参考仓库 `D:/WorkSpace/webstorm/claude-code-sourcemap`：阅读指定的两篇分析，核对 `restored-src/src/utils/tasks.ts::claimTask`、`restored-src/src/tools/SendMessageTool/SendMessageTool.ts`。技术解析基于 2.1.88；理论文档对工作区隔离等有简化，不能把概念图当成实现保证。

原文明确区分成员执行、共享任务、消息投递与业务完成。参考源码在领取时重新检查 owner 和 blockedBy；SendMessage 按正文实际类型分流，字符串不成为结构化控制命令。迁移必须保留这些语义，不能只替换存储介质。

## 从原始失败定位到代码

| 现象与证据 | 对应代码与根因 | 修复及验证边界 |
|---|---|---|
| `bf13562...` 两个活动成员换代后任务记录 RecoveryConflict | `team_worker.py::_Worker.claim` 只换任务令牌；`recovery_store.py::begin_operation` 对未完成操作仍比较运行级 fence。同一 Lead 下成员重启不改变运行 fence，因此安全操作被视为仍在同代执行 | 取得新任务租约的同一事务，仅把该任务前缀下 replay_safe 的 started 操作移出旧执行代次；不改业务输入，不重放未知副作用。专项同时检查旧 Worker 无法提交、其他任务不受影响 |
| bf13562 运行有 61 次 supervisor 模型回执，其中 39 次 WaitForTeamEvents，最终 `team_lead_stopped_before_team_completion`；6ddd71b4 进一步暴露补证期间旧失败依赖持续唤醒 | 原 task_wait 每 30 秒空返回；另一个 failed_blockers 分支未区分“补证正在执行”和“没有工作可推进”，同样会立即重复返回 | 仅对真实任务/消息/审批变化唤醒；存在活动补证时不因旧失败边重复唤醒，仅无活动工作且有失败阻塞时要求 Lead 处理。期限和取消有效，不提高模型轮次上限 |
| 原始 TaskCreate 回执中自主认领的 owner 是字符串 `"null"` | `teams_tools.py::TaskCreateInput` 的 nullable anyOf 被配置模型输出成字符串，后续被当成员标识处理；同模型探针证实改成 string/null 数组也未解决 | 模型工具显式用空字符串表达未分配，在参数边界映射为 None/SQL NULL；派发仍填写成员 ID。探针已验证空字符串、上游事实 ID 和依赖正确输出；纯文本消息仍不解释为控制对象 |
| 汇总任务反复使用 COV-09/COV-20，数据库中最终没有该汇总任务 | 这些是 deliverable 需求，`Supervisor.assign` 仅允许 factual ID；原错误没有给出可选事实 ID，模型无法正确修复 | 单独提供 task_requirement_choices；错误返回可用完整 ID，并说明汇总复用上游事实 ID。质量门禁仍针对事实证据 |
| 压缩文本的合法 URL 后带中文括号，被当成未登记引用 | `research_agents.py::Researcher.run` 的 URL 正则漏掉中文左括号和其他文本分隔符；原始压缩回执可复现 | 补全边界；同时保留真实未知 URL 拒绝及有限纠错，不能自动登记模型编造来源 |
| Judge 报 relevance 必填字段缺失 | `gateway.py::SandboxChatModel.generate_structured_output` 已有三次 Schema 格式修复；失败发生在重试耗尽后，不是完全没有重试 | 保留拒绝，不自动补评分。真实研究质量是否最终通过仍由后续 E2E 决定 |
| 新运行 mq 在计划阶段报 sandbox_gateway_outcome_unknown，但 SQL 的 gateway:model:2c575b2665ea5bcbbdf274d9572c0360 已 committed/completed | `agentscope_runtime/gateway.py::_request` 的 120 秒 HTTP 超时直接抛错，缺少 V2 回执查询；已有 /v1/models/lookup 也不兼容 V2 摘要和返回类型 | 新增 V2 只读查询，复用同一鉴权链并核对请求摘要。客户端仅在超时/5xx 后查询原 operation_id，最多等待 60 秒，不重新派发 Provider 请求；未取得结果仍报不确定 |

原始证据保存在本地 `tmp/teams-final-evidence.json`、`tmp/teams-original-lead-responses.jsonl`、`tmp/teams-latest-compression.jsonl`，不提交运行正文或真实配置。SQL 回执是事实来源；源码对应关系是定位结论，最终以针对性回归和新镜像真实 E2E 交叉验证。

## 本轮验证顺序

### TaskList 工件截断的再次定位

真实运行 `b7df31ba3f76586d885a0922272e2cbf` 的两个上游任务均已 completed/accepted，汇总任务也已解锁、自主认领并获计划批准，但汇总成员声称看不到 RocketMQ 工件。原始工具回执 `tmp/teams-aggregate-tool-receipts.jsonl` 显示：typed output 有三项任务，模型实际可见 message 却只有 30024 字符，末尾为 `[truncated 39805 chars]`，其中没有 RocketMQ 任务 ID。原因是 TaskList 复用详情投影，将第一个任务的完整证据注册表与研究正文塞进列表，耗尽全局 30000 字符预算。

回查参考源码 `restored-src/src/tools/TaskListTool/TaskListTool.ts`，列表只返回 id、subject、status、owner、blockedBy，完整内容通过 TaskGet 获取。本项目据此增加紧凑摘要投影，用于成员、Lead 和空闲讨论的任务列表；保留依赖、质量准入、模式、版本及需求 ID，TaskGet 继续提供完整工件。Lead 的计划正文与审核反馈也移到对应任务的 TaskGet 历史，TaskList 只带计划状态。没有提高全局输出上限或降低质量门禁。

回归构造首项超过 9 万字符的真实工具输出，经同一治理序列化函数后验证所有任务 ID 可见且无截断，TaskGet 仍能读取完整证据；专项 1 passed，Agent Teams 与研究迁移联合回归 57 passed。新镜像已部署，Playwright 创建运行 `809657a6b57c5b72a2822edc1e2321a2`，完整 E2E 仍待结果。已通过前端提供官方来源线索，来源仍须由成员实际抓取及质量评估，不能计为完全无人干预验收。

续验另定位三处可操作缺口：

- `api/team_routes.py` 把基础设施中的 team 对象视为业务团队已存在，TeamCreate 前反馈返回 `message_has_no_recipients`。现在成员记录尚不存在时复用运行决策检查点保存，真实 PG HTTP 回归 1 passed。
- Lead 将补证任务依赖指向已拒绝原任务，形成业务上的永久阻塞。前端反馈后 Lead 正确移除，并原子替换汇总依赖；提示词补充明确规则，不修改数据库硬约束。
- RocketMQ 初始任务质量评估 accepted，但准入 rejected，原因是 `result.termination=exceed_max_iters`，不是历史 `protocol_errors`。任务摘要和详情显式投影终止及准入原因，回归 1 passed；仍不接纳超限任务。Judge 缺字段的格式修复现反馈具体校验错误，4 passed，重试次数不变，耗尽仍失败。

先运行同运行 fence 的安全操作恢复、真实子进程强杀、无变化等待、取消/期限、未分配负责人参数和事实需求范围回归；通过后再构建新镜像。真实 E2E 使用原有 `.env` 和 `config/litellm.yaml`、公开网络、无知识库，验证混合成员、计划审核、消息、依赖与报告，并单独记录取消清理。测试结果和剩余问题更新到实施台账，不能用旧失败运行证明新补丁通过。

当前结果：`tmp/teams-source-root-regression.log` 70 passed，含真实子进程在安全工具 planned 窗口强杀后恢复；负责人边界更新后 `tmp/teams-owner-wire-tests.log` 3 passed。同模型前后探针分别见 `tmp/teams-task-schema-probe.log`、`tmp/teams-task-schema-final.log`。Playwright 插件创建的新运行 `07c1a419adad5abfae992dfff18b8827` 正在验证，尚未登记通过。

该运行的真实恢复已通过：db 在 model:compression:0 的 started/replay_safe=1 窗口强杀，epoch 2 恢复同一任务 d6ac83b1a2cd50beb484621654070b31，原回执 committed，任务 completed/accepted。汇总任务亦已成功创建，owner 未分配，两条依赖齐全。mq 随后暴露上述响应丢失问题，本轮已通过前端取消，记录于 tmp/teams-root-recovery-evidence.json；不能登记完整成功。

V2 回执专项 `tmp/teams-v2-receipt-final.log` 62 passed，覆盖迟到回执、查询期限、同请求摘要、一次 Provider 派发，以及服务签名/任务令牌/失效代次/错误任务鉴权。新运行 `6ddd71b459fb573e8c541ea8fcb4b9ac` 使用更新后的成员、API 和 Gateway，继续验证完整报告闭环；不再在该运行中热更新工具或注入已验证的成员故障。

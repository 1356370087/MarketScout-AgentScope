# 质量门禁 P0/P1 修复与历史回放

日期：2026-09-19。依照用户要求，先编码、再历史集成回放、最后启动真实 E2E。

## 编码修复

- 已确定的 Judge 输出协议失败在恢复账本中保存为终态失败回执，重放不再次调用模型；异常在提交之后交给质量域的 fail-open/fail-closed 策略处理，不设置整个 RecoverySession 的 problem。失租、取消、预算及网关结果不明仍保留阻断行为。
- 来源范围和安全检查合格的候选证据先累积，不因单个批次佐证不足而删除。工具调用结束后、下一次 AgentScope 推理之前，把同轮结果和累计候选统一送入 Judge；按调用 ID/证据 ID 稳定排序，避免并发完成顺序改变恢复输入。
- Judge 的 decision、缺口、建议、确定性检查和可用性进入下一轮受信上下文。ResearchComplete 先登记请求，统一评估后才允许结束；fail-closed 评估不可用明确停止，fail-open 保留候选继续研究，不能把不可用结果当成质量通过。
- 外层交接仍执行原有覆盖、来源、语义与执行终态检查；仅 accepted/accepted_with_caveats 才解锁依赖。保留 accepted_with_caveats 投影，前端区分任务准入原因与 Judge 理由，超限不再被误读成评分失败。
- balanced 评分与三个来源阈值未降低；本次修复数据累积和控制闭环，不用修改阈值掩盖问题。

## 组件与历史集成测试

| 验证 | 结果 | 证据 |
|---|---|---|
| 原生恢复、团队、研究与新增质量闭环联合 | 153 passed / 1 skipped | tmp/quality-closeout-combined.log |
| 真实历史缺 relevance 回执，Gateway Schema → SQL 恢复回执 → NativeResearchQuality → fail-open/closed → 再恢复 | 两种策略通过，模型结果只读取一次 | tmp/quality-history-final.log（共四项） |
| 历史 mq 质量通过但 exceed_max_iters 的准入原因 | 正确投影执行超限拒绝 | 同上 |
| 历史 404 未采信 URL 的压缩上下文 | 404 诊断保留，未登记 URL 移除，有效引用保留 | 同上 |
| 历史 PostgreSQL 三次抓取，经新候选累积、统一评估和真实 LiteLLM Judge 重评 | 45 条候选保留，3 个来源，1 个批次、1 次真实 Judge 调用，complete/accepted=true | tmp/quality-history-pg-live.log |
| 前端类型与目标静态检查 | 通过 | tmp/quality-frontend-tsc.log |

真实历史回放的数据取自 `809657a6…` 的 `f3ac9cb9…` 失败任务；原始任务列状态是 failed（snapshot 内的 status 仍旧为 pending，回放日志中的 historical_status 仅反映旧 snapshot，不作为权威状态）。取证页面是 PostgreSQL 官方 sql-update、dml-returning、queries-with 三页。

自动审批曾拒绝将完整原始快照发给模型；随后先按字段白名单提取公开官方证据、剔除用户/运行/会话/配置/Worker 数据，再经审批执行真实 Judge 重评。历史错误回执测试完全本地执行。原运行记录未被修改，新 Judge 调用是集成重评，不冒充历史模型原始结果，也不等同于整个研究 E2E。

## 新 E2E

历史回放通过后，构建 API/成员和前端新镜像，通过可见 Playwright 浏览器创建 `fd1787357e965fb4bc6dd58be9161914`。公开网络、无知识库，使用 mixed direct/plan_approval、Lead 派发/自主认领、依赖、消息、计划驳回再批准和质量准入流程。TeamCreate 前通过前端给出六个官方页面线索，不注入工件或审批结论。

最终运行 failed，不能登记完整 E2E 通过。质量链路取得以下真实结果：

- PostgreSQL `2e07a886…` 正常完成、accepted_with_caveats，保留第三方 PG18 特性来源及中文资料不足意见。
- RocketMQ `bea5d51d…` 计划 v1 驳回、v2 批准；内层反馈从 continue/3 个来源推进到 complete/4 个来源。外层因“广播模式无重试”缺证据而 rejected，未出现 evaluator_error，也未因协议错误污染恢复会话。
- Lead 新建补证 `28ae63ff…`，重新生成并审批计划，最终 accepted_with_caveats；汇总 `ccfd88b1…` 同样 accepted_with_caveats。
- 两个持久成员均 graceful_shutdown，Lead closed；RocketMQ 发件记录 27 条，待发 0。成员容器已退出。

真实运行仍暴露两项独立收口，未通过降低评分阈值规避：

1. **报告输入预算**：团队结束后，`outline_approval:0:pipeline:model:final_report:0` 请求失败。LiteLLM 明确报 ContextWindowExceededError：139716 输入 token 超过当前 `zai/glm-5.3-flash` 的 131072 上限；Gateway 回执 `34d9aaee…` 为 failed/invalid_request，客户端表面错误为 GatewayCallError。不是质量 Judge 评分或状态异常，未生成最终报告。
2. **Lead 依赖替换**：Lead 先单独 removeBlockedBy 删除被拒任务，随后才 addBlockedBy 新补证；中间汇总被自主认领，不能再修改前置关系。汇总随后自行搜索补足材料并通过交接，而非严格等待补证任务接纳再解锁。SQL 没有将 rejected 当作 accepted，但本次不满足完整的“原子替换依赖后解锁”业务验收。

上述问题作为后续报告/调度专项登记，本轮质量 P0/P1 修复未扩展为报告系统重构。SQL 汇总证据：`tmp/quality-closeout-e2e-evidence.json`；浏览器截图：`tmp/quality-closeout-e2e-final.png`。这些本地运行产物不提交。


清理完成：仅关闭本次 insightforge-teams-e2e Compose 项目与验证浏览器，保留数据卷；无本次成员孤儿容器，其他预先运行项目保持运行。

## 后续修复：依赖分步替换

针对本轮 Lead 先删后加的问题，服务端 `edit_dependencies` 增加事务内守卫：移除尚未完成或未质量接纳的前置边时，必须在同一次调用中为同一下游加入真正的新前置边。单独删除、把旧边重新添加、添加已有边均不能满足替换条件；两个依赖方向统一转换后检查。失败回滚版本、任务内容与依赖，成员不会看到中间无阻塞状态。已完成且 accepted/accepted_with_caveats 的前置仍可单独移除。

Lead 应先创建补证任务，再对下游使用一次 `TaskUpdate(removeBlockedBy=[旧任务], addBlockedBy=[补证任务])`。本次不增加全局领取锁、不改 CAS 领取路径或既有迁移；校验由业务服务统一执行，模型工具与 HTTP 调用都不能绕过。

验证：真实 PostgreSQL 团队/任务联合回归 40 passed / 1 skipped；去除重复参数组合后，最终依赖专项 6 passed（两批重叠，不累加）。覆盖正反向删除、失败/取消/拒绝前置、回滚、无效替代边、10 个并发领取、重复请求和替代任务质量接纳后的解锁。测试 PostgreSQL 容器已清理；本次未重跑模型浏览器 E2E，报告输入超限仍待修复。

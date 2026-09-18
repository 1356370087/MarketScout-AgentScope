# Agent Teams 质量门禁接入与失败归因排查

日期：2026-09-18。以当前代码、冻结配置和真实运行 `809657a6b57c5b72a2822edc1e2321a2` 的 PostgreSQL 回执为依据。本文为定位结论，不把尚未修改的闭环缺口登记成已修复。

后续状态（2026-09-19）：用户已授权修复本文 P0/P1，编码、联合回归及真实历史回放已完成，新 E2E 正在执行。本文保留修复前诊断，最新状态以 [修复与回放记录](quality-closeout-20260919.md) 为准。

## 接入结论

已接入 AgentScope 原生研究运行时，但质量标准属于项目业务策略，并非 AgentScope 框架自带评分器。生产组装 `research.py` 和独立 Worker `team_worker.py` 均注入 `NativeResearchQuality`。AgentScope 工具执行后的 `GovernedToolkit.result_observer` 调用内层评估；任务交接调用外层评估。Judge 经 `ResearchModels.structured → AgentScope ChatModel.generate_structured_output → Sandbox Gateway V2 → LiteLLM`，并经过恢复账本、预算及模型调用策略。

`quality/gate.py` 的确定性规则、覆盖契约与 Judge 协议复用；这不等同于回到旧 LangChain Agent 执行路径。框架集成存在，但评估反馈、证据累积与错误恢复的业务闭环还没有完整收口。

## 本次运行实际冻结值

| 参数 | 值 |
|---|---|
| quality_evaluation_enabled | true |
| quality_evaluation_model | if-quality-v1 |
| quality_evaluation_rigor | balanced |
| quality_evaluation_min_sources | 3 |
| quality_evaluation_fail_open | true |
| max_structured_output_retries | 3 |
| max_react_tool_calls | 10 |
| max_researcher_iterations | 15；teams 代码另采用 max(60, 配置值) 协调上限 |

balanced 的内层语义评分要求四项最低分均至少 3，平均至少 3。外层另检查需求覆盖、来源、引用及交接语义。最终 SQL 准入还要求执行终止为 completed/research_complete，不能仅由 Judge accepted=true 决定。

## 定位结果

### P0：Judge 格式失败被恢复会话升级为整个任务失败

`54ee4d16…` 的 Judge 输出缺少 relevance，格式修复耗尽后抛出 jsonschema.ValidationError。`RecoverySession.operation` 对所有 BaseException 设置 `self.problem`。质量域虽然按 fail-open 捕获评估异常并生成降级结果，后续操作仍首先重抛该 problem；`Researcher.run` 的 finally 也会重新抛出。于是实际结果是任务 failed/ValidationError，与冻结的 fail-open=true 不一致。

这是错误分类与恢复状态问题，不是评分太低。不能简单清除所有 problem：失租、取消、预算、未知外部副作用仍必须阻断。应将已确定的模型输出协议失败作为持久终态回执处理，由质量策略决定继续研究或停止；不得伪造合格评分、直接准入证据。

### P1：内层研究反馈没有回到 AgentScope 推理循环

`_Observations.capture` 保存 assessment，并用 accepted 决定是否累积证据；`tools.py` 最终返回给 Agent 的仍然只是原始 `result.message.content`。Judge 的 decision=continue/retry、missing_information、suggested_queries 及准入状态没有作为本轮模型输入返回。`ResearchComplete` 也没有以这些评估结果为条件。

因此研究员看到抓取成功即可结束，但内部证据可能已经被评分丢弃；外层再拒绝，Lead 不得不创建新任务。应把受信质量反馈在工具或推理边界持久化后交给研究员，并保持恢复重放输入一致。

### P1：证据采集与最终质量准入混用

内层仅把本次 accepted 的 candidates 加入 `self.evidence`，下一次 Judge 输入只有此前 accepted 的证据加当前 candidates。并发 fetch 在各自评估前读取当前集合，可能各自只看到一个来源。

真实 `f3ac9cb9…` 同轮抓取三个官方页面，但三条 Judge 回执分别认为“目前只有一个来源”；其中 dml-returning 的 corroboration=2、queries-with 的 evidence_coverage=2，被内层丢弃。压缩阶段能看见原始工具正文却拿不到完整接纳证据，进一步触发引用拒绝。

需要分开“来源安全、可追溯的候选证据集合”和“完成任务时的质量准入”。建议按 Agent 一轮工具批次聚合后评估，允许未充分佐证但合法的候选继续累积；只有最终通过门禁的结果才能共享为已接纳工件及解锁依赖。不能通过降低来源数或忽略质量拒绝解决。

### P1：评分结果与任务执行状态缺少统一解释

`10c79a48…` 的最终 Judge accepted=true，四项评分均为 5，确定性检查通过；但 result.termination=exceed_max_iters，SQL admission_status=rejected。当前准入代码符合既定规则，历史 protocol_errors 中的 accepted_contains_follow_up_action 已修复，并非最终拒绝原因。

Lead 实际把历史协议错误误认为最终原因，安排了错误方向的补证。此前本轮已给 TaskList/TaskGet 增加 termination 和 admission_reason 投影，组件回归通过；该原因还应作为前端统一准入说明，区分“研究执行未正常结束”“证据不足”“Judge 不可用”。

### P1：来源数量策略与狭窄事实任务不完全匹配

`137aa72d…` 的 Judge 认为事实已充分支持，但硬门禁要求至少三个来源标识，实际只有两个，因此拒绝。这是配置策略在生效，不是 AgentScope 未调用门禁。三个来源标识也不等于三个独立机构的佐证；同一官方文档的不同页面可计为不同来源。

是否对狭窄官方事实允许更少来源需要作为明确质量策略决定，不能为了让 E2E 通过临时下调。本次没有修改该阈值。尚无人工标注基准集，不能仅凭这几个回执断言 Judge 的整体准确率。

### 压缩和任务展示是额外放大因素

此前 TaskList 携带完整工件，在 30000 字符处截断后续任务，导致模型以为上游不存在；已改摘要列表，完整内容走 TaskGet。

`238508ac…` 两次压缩都把返回 404 的 /docs/retry/ 写入“未采信”说明。输出校验按未登记 URL 拒绝，未误放行；但失败 URL 被原始工具日志持续带入压缩输入。已隔离压缩上下文中的未接纳 URL，同时保留 404 诊断和最终严格校验。研究回归 38 passed，后续调整的专项 5 passed；新补丁尚未完成真实 E2E。

## 修复顺序与验收边界

1. 优先收口质量协议错误与恢复会话的分类，验证 fail-open/fail-closed 各自行为以及失租、取消、未知结果仍阻断。
2. 拆分候选证据和最终准入，按工具批次聚合质量输入，消除单次抓取被要求满足整个任务佐证标准的问题。
3. 将质量决策和缺口持久投影到研究员下一轮输入，并协调 ResearchComplete、迭代上限、Lead 补证三种状态。
4. 统一前端及 Lead 的准入原因，随后用确定性故障回归和真实混合成员 E2E 验证。

当前完整 teams E2E 未通过，前述 100 项联合回归不覆盖这里新发现的全部闭环缺口，不能据此登记业务验收完成。最近运行已 failed，没有新的运行启动；本次排查保留原始回执于本地 tmp，不提交凭据或完整运行正文。


2026-09-19 更新：本文保留排查时证据。P0/P1 已修复并完成历史回放及新 E2E，最新结果见 [质量修复与回放记录](quality-closeout-20260919.md)。

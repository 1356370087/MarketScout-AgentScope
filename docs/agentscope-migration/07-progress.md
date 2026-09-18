# 07 工作进度台账

[目录](README.md) · 最后更新：2026-09-19。**本页是状态和实际工时的唯一维护入口。**

## 当前收口状态（2026-09-19）

2026-09-19 质量门禁专项排查：已确认接入 AgentScope 原生调用链，但发现 Judge 协议异常污染恢复会话、候选证据与准入混用、质量反馈未返回研究员等闭环缺口。排查时 teams 运行 `809657a6…` 已失败；后续修复及新运行结果见下一段。评分、状态与策略归因见 [质量门禁排查](implementation/agent-teams-quality-audit-20260918.md)，当前不登记完整 teams E2E 通过。

2026-09-19 后续修复：上述 P0/P1 已编码，153 passed/1 skipped 联合回归、4 项真实历史错误回放通过；公开 PostgreSQL 历史三来源经新批次聚合和真实 Judge 重评一次通过（45 条候选全部保留）。前端类型检查通过。已按顺序完成新 E2E `fd178735…`：三项任务 accepted_with_caveats，原 mq 缺证据断言被拒后完成补证；无 evaluator_error，两名成员优雅退出、消息无积压。最终在报告大纲阶段因 139716 输入 token 超过 131072 上限而 failed；另记录 Lead 分两次替换依赖导致汇总提前领取的问题。完整 E2E 未通过，本次容器与浏览器已清理。见 [质量修复与回放记录](implementation/quality-closeout-20260919.md)。

原生可观测性迁移：已补 AgentScope 生命周期内容最小化 OTel/Langfuse 导出、SQL 权威统计到 Prometheus 及专用 Grafana 看板；修复预算 undefined 标签、工具成功率、角色/任务分组、时间桶和任务级调用计数。当前本地 `.env` 的 `LANGFUSE_ENABLED / OTEL_ENABLED / PROMETHEUS_ENABLED` 均为 false，未擅自开启或声称生产平台验收通过。实施与验证边界见 [原生观测迁移记录](implementation/native-observability-20260918.md)。

Agent Teams 修订方案已获用户授权并实施 AT-1～AT-6：保留 collaborator，新增 Lead 显式创建、混合执行模式、SQL CAS 与双向依赖、RocketMQ 完整信封和事务发件、持久成员 Docker、版本化计划审核及前端投影。**编码和组件测试已推进，真实 E2E 未全量通过**。回查原文与原始回执后，已修复活动成员安全操作恢复、无变化等待消耗 Lead 轮次、V2 响应丢失的回执查询，以及 TaskList 携带完整工件导致后续任务被截断。最新批次分别为恢复与研究回归 70 passed、V2 回执 62 passed、任务列表与研究回归 57 passed、创建前反馈 1 passed（批次重叠，不累加）。活动任务强杀恢复在 `07c1a419…` 已真实通过；`b7df31ba…` 两项上游 accepted，但汇总因列表截断失败。`809657a6…` 已失败；最新 `fd178735…` 完成质量闭环与成员优雅退出，但报告超限失败，完整验收仍未通过。实施、运行证据和剩余限制见 [Agent Teams 实施记录](implementation/agent-teams-implementation-20260918.md) 与 [原文及根因核对](implementation/agent-teams-root-cause-20260918.md)。

后续 Docker 真实复验已通过 Playwright 弹出浏览器执行公开网络异步研究，知识库关闭。发现并修复镜像依赖、迁移 schema、Worker 装配、工具路由、冻结配置、Web 准入、审批租约、Controller 并发连接及任务事件等实际联调问题；原生统一专项 **210 passed**，其后上下文归档与生产装配专项分别 **47 / 4 passed**（存在重叠）。实际搜索、抓取、证据抽取与质量拒绝已取得真实回执，完整报告交付以 [Docker 真实复验记录](implementation/docker-web-live-20260918.md) 为准。以下表格保留上一轮编码提交时的验收快照，不代表这些 live 实验尚未尝试。

以当前工作区实现与本轮测试为准。正式实施完成数 **39/81**，加权约 **47.6%**，端到端业务验收仍为 **0/75**；这是原任务的验收计数，不是代码实现比例。此次用户授权对 E2E P0/P1 编码并执行一次本地提交；同日按用户要求完成 M5 T035/T036 复验验收（见 [M5 复验验收记录](implementation/m5-t035-t036-reacceptance.md)），完成数 37/81 → 39/81。

最新真实运行 `e290e2054b18532c94d93e89631d1ed4` 已 `completed`：异步双任务、动态出网审批、上下文归档、质量门禁降级、证据恢复报告、刷新和 Markdown/JSON 下载闭环完成。质量仍为 `degraded / research_incomplete`，不是完整技术简报验收通过；该运行发现的细粒度用量投影和预算标签缺口已在本轮编码修复，后端 138 项、前端 9 项回归及 TypeScript 检查通过，真实观测栈联调仍待验收。旧记录缺失的维度不伪造回填。

| 收口项 | 当前实现 | 验收状态 |
|---|---|---|
| P0 正式团队启动通道 | API 经签名 UDS 请求 Controller，Controller 用固定命令和管理员环境启动正式 team_executor；API 不持有 Docker socket | 签名、重放、固定命令和清理组件验证；正式生产容器 live 待执行 |
| P0 原生出网审批 | 公共路由与所有权查询使用 SQL 恢复运行、当前 fence；安全事件进入 SQL outbox，保留已有审批文件协议 | SQL 组件及公共 HTTP 验证；真实浏览器审批联调待执行 |
| P0 documents/hybrid/specific | 创建时授权和记录发布代次快照，执行/恢复绑定不可变来源；可信 Worker 提供受治理私有资料检索 | 授权/快照复用验证；四来源真实资料研究待执行 |
| P1 长运行/暂停恢复 | 动态任务令牌、Run Key 和 Vault 注册续期；冻结默认成本上限，恢复申请剩余额度；SQL 感知的 Key 清理及 Worker 失租退出 | 生命周期与真库 Worker 回归；长时真实代理实验待执行 |
| P1 一次计账/迟到用量 | 保留物理尝试账本，精确 run/operation 标签关联代理账单，幂等差额补回与后台扫描；原生用量 HTTP 投影 | 确定性回执验证；供应商真实账单和多笔代理重试核验仍未关闭 |
| P1 完整浏览器 E2E | 新增四来源真实 BFF/浏览器用例，等待报告、刷新、引用、用量、发布和下载；失败取消研究 | TypeScript、ESLint、用例收集通过；真实模型批次未执行 |
| P1 生产组合故障 | 专用 Compose 覆盖与执行/清理脚本；新增受限项目的 Worker、API/Gateway SIGKILL 注入，等待自然租约过期恢复 | 现有真实容器确定性故障矩阵通过；新增 live 注入和跨主机网络分区未执行 |

本轮完整原生回归 **515 passed / 2 skipped**（可选远程 MQ），随后最新专项 **51 passed**；两批有重叠，不能相加。共享凭据/账单/旧服务兼容 **32 passed**。最终资源专项 **14 passed**、公共 HTTP **1 passed**；各批次重叠。公共路由及最终检查见 [E2E 收口记录](implementation/e2e-p0-p1-closeout.md)。新加的真实浏览器/故障脚本不作为已经执行的证据。

原生和镜像静态守卫通过；全项目仍有 **72 处 LangChain / 12 处旧引擎导入**。默认入口仍为 legacy，未执行正式切换或旧路径删除。T023 已恢复实施，下面历史批次中的“暂缓”和“生产资源尚未实现”只描述当时状态。

## 历史批次交付与进度快照



| 本次文档工作 | 状态 | 证据 |
|---|---|---|
| 用户目标与迁移边界确认 | 已完成 | README 与 AS-D001～015 |
| 静态源码/配置/接口/版本盘点 | 已完成 | baseline、指纹、244 配置、191 路由附录 |
| 原生导入/状态/确认离线探针 | 已完成 | [probe-results.json](evidence/probe-results.json) |
| 九份主文档与任务定义落盘 | 已完成 | [文档验证通过](evidence/validation-results.json)：编号/链接/依赖/配置/源码依据均完整 |
| 旧系统回归基线及分阶段真实设施验证 | 已执行，覆盖仍有缺口 | M0 回归与 M1/M2、M3 Docker、M5/M6 分阶段记录见下表；不是整仓库无失败或生产切换通过 |

业务功能迁移 **0/75**；实施任务完成 **39/81**（M0/M1/M2、M4 全部；M3 的 T017～T022；M5 全部验收完成，其中 T035/T036 于 2026-09-18 复验关闭）。T021 原生组件已验收，T023 已按用户最新要求恢复实施。加权实施进度 **约 47.6%**。本地组件及真实模型部分成功不等同业务切换完成，实际人日未记录。

M6 八项任务均已推进并于 2026-09-17 在当前代码树完成组件验收复验：AS-A041～A048 全部通过（35 项故障矩阵 + 470 项全套件联合回归，含真实 PostgreSQL）；但按原定义的依赖与旧路径退出条件（T023 暂缓、默认入口仍为 QueryEngine、真实出网审批与部署故障矩阵未接线），无任务登记已完成，T041/T043/T044/T046 维持待验收、T042/T045/T047/T048 维持进行中，详见 [M6 验收复验记录](implementation/m6-acceptance-report.md)与 [M6 实施报告](implementation/m6-recovery-report.md)。

M7 已接通原生 Supervisor → 持久 Worker → TeamSay → 交接准入 → 工件与覆盖账本；五个独立 Worker 进程强杀窗口、leader 新租约重建、资源关闭与远程 MQ 通过。联合回归 114 passed/1 skipped，最终专项 22 passed，补充反馈 1 passed（批次重叠）。T049～T052 组件待验收、T053 进行中；旧 HTTP 路径退出和部署组合故障未关闭，详见 [M7 Worker 报告](implementation/m7-worker-report.md)。

计算规则：完成任务数仅计“已完成”；加权进度按任务估算区间中点加权，进行中不虚报固定百分比；功能完成需全部关联任务及跨功能验收通过。取消项只有经明确范围变更才从分母调整，并保留变更记录。

M9 T064～T068已推进：七类原生报告、评审修订、CanonicalReport、六格式发布及三窗口Worker强杀恢复；131项原生、153项兼容回归通过。T064～T067待验收，T068进行中，旧服务退出和部署组合故障尚未关闭，详见 [M9报告验收](implementation/m9-report-report.md)。

M10 首批已推进 T069/T070/T071/T074：共享请求与 SSE、前端领域类型、无 LangChain 的历史只读边界；11 项原生、79 项兼容、91 项前端通过（另 1 项跳过），类型检查通过。四项均进行中，T072/T073 已领取并进行页面重组，未切换生产运行入口，详见 [M10 实施记录](implementation/m10-api-report.md)。

M11 首批已推进 T077/T078/T079/T081：原生导入守卫、六应用镜像 Python 3.14、生命周期回归和清退盘点。Docker 已恢复，主镜像与原生组件探针通过；当前仍有 72 处 LangChain 与 12 处旧引擎导入，完整部署/默认入口切换未通过。最新见 [沙箱与宿主接线记录](implementation/m10-sandbox-team-host-report.md)。

本轮新增 Gateway V2 原生 provider、原生容器 Worker 调用链、NativeTeamHost 部署资源绑定及原生发布路由。53 项联合回归、42 项旧兼容通过，Docker 组件探针通过；生产 pipeline_factory、统一预算权威、Controller 团队分派/强杀恢复及完整业务路由尚未完成。默认入口保持旧引擎；详见 [执行与验收边界](implementation/m10-sandbox-team-host-report.md)。

2026-09-17 后续关键项：新增 SQL Gateway 模型账本（物理回执/结算原子提交、外层不重复计费、超限审批）、团队 Docker 分派与两窗口强杀恢复、原生运行工厂和团队/反馈/预算路由。联合 104 passed/1 skipped，补充预算接口 15 passed、宿主研究 39 passed（批次重叠）。生产资源提供器、正式容器命令、工具/fetch 预算及其余业务路由未闭环，默认入口未切换，完成计数保持 36/81。见 [本轮实现与证据](implementation/m10-ledger-container-factory-report.md)。

## 状态定义

M8 已推进 T055～T063 原生知识/记忆入口，并完成隔离真库8项、真实Docling补验1项、原生联合150项和旧记忆兼容71项验证（批次重叠）。T055/T056/T057/T058/T060待验收，T059/T061/T062/T063进行中；真实Mem0、备份恢复及部署接线尚未关闭。详见 [M8验收报告](implementation/m8-acceptance-report.md)。

| 状态 | 判定 |
|---|---|
| 待开始 | 尚未实施，普通前置依赖等待不算阻塞 |
| 进行中 | 已开始，有当前产物与下一步 |
| 阻塞 | 记录具体原因、解除条件和责任/依赖 |
| 待验收 | 实现完成，仍缺必要验证 |
| 已完成 | 验收通过，附测试或交付证据 |
| 已取消 | 记录范围决策、原因和替代任务 |

默认负责人为实施者（用户）配合编码 Agent，领取任务后填写具体负责人；不是自动启动并行 Agent。实际人日为空表示未记录，不代表已经实施但零工时。

## 实施任务状态

| 任务 | 阶段 | 负责人 | 预计人日 | 实际人日 | 状态 | 阻塞/验收证据 | 更新日期 |
|---|---|---|---|---|---|---|---|
| [AS-T001](05-migration-roadmap.md#as-t001) 封存实施前基线 | M0 | 用户+编码 Agent | 0.5～1 | — | 已完成 | [基线封存记录](implementation/m0-t001-baseline-seal.md)：646 指纹 645 未变（仅 KNOWLEDGE_BASE.md 外部删除）；Git 封存点 `a8fb8fb` | 2026-09-14 |
| [AS-T002](05-migration-roadmap.md#as-t002) 恢复旧系统隔离验证环境 | M0 | 用户+编码 Agent | 1～2 | — | 已完成 | [隔离环境报告](implementation/m0-t002-legacy-environment.md)：.venv-legacy（mcp 2.2.0，无 agentscope）；1990 项测试可收集 | 2026-09-14 |
| [AS-T003](05-migration-roadmap.md#as-t003) 冻结接口及配置契约 | M0 | 用户+编码 Agent | 1～2 | — | 已完成 | [契约冻结报告](implementation/m0-t003-contract-freeze.md)：191/191 路由运行时匹配（补遗 /metrics）；244/244 配置对齐；4 SSE 端点固化 | 2026-09-14 |
| [AS-T004](05-migration-roadmap.md#as-t004) 建立旧实现回归基线 | M0 | 用户+编码 Agent | 1～2 | — | 已完成 | [回归基线报告](implementation/m0-t004-regression-baseline.md)：后端 1891/1990 通过、15 项既有失败隔离；前端 91/92+tsc 零错误；E2E/live 明确未执行 | 2026-09-14 |
| [AS-T005](05-migration-roadmap.md#as-t005) 验证服务、SQL 与身份扩展 | M1 | 用户+编码 Agent | 1～2 | — | 已完成 | [探针报告](implementation/m1-t005-service-sql-identity.md) 14/14：生命周期/伪造头无效/schema 隔离（含负向对照） | 2026-09-14 |
| [AS-T006](05-migration-roadmap.md#as-t006) 验证 MessageBus 完整契约 | M1 | 用户+编码 Agent | 1～2 | — | 已完成 | [探针报告](implementation/m1-t006-message-bus-contract.md) 12/12；新风险 AS-R021（InMemory 锁无 TTL） | 2026-09-14 |
| [AS-T007](05-migration-roadmap.md#as-t007) 验证检查点与交互恢复边界 | M1 | 用户+编码 Agent | 1～2 | — | 已完成 | [探针报告](implementation/m1-t007-checkpoint-recovery.md) 7/7；W1 窗口缺口实证（snapshot+重发不足，M6 须配 AS-T042 账本） | 2026-09-14 |
| [AS-T008](05-migration-roadmap.md#as-t008) 验证现有沙箱接入 | M1 | 用户+编码 Agent | 1～2 | — | 已完成 | [探针报告](implementation/m1-t008-sandbox-workspace.md) 6/6（框架侧）；控制器深对接留 AS-T031；AS-R022（未声明依赖） | 2026-09-14 |
| [AS-T009](05-migration-roadmap.md#as-t009) 验证模型与版本组合 | M1 | 用户+编码 Agent | 1～2 | — | 已完成 | [探针报告](implementation/m1-t009-model-versions.md) 6/6：版本矩阵+流/结构化/凭据/取消；真实网关留 AS-T020 | 2026-09-14 |
| [AS-T010](05-migration-roadmap.md#as-t010) 建立应用及原生服务组合入口 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [M2 报告](implementation/m2-foundation-report.md)：ASRuntime 组合根（PG/演示双模式）+ 最小暴露；旧入口回归不受影响 | 2026-09-14 |
| [AS-T011](05-migration-roadmap.md#as-t011) 接入隔离的原生服务存储 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [M2 报告](implementation/m2-foundation-report.md)：schema 隔离工厂 + 显式迁移入口 + session 回程测试；生产默认禁自动建表 | 2026-09-14 |
| [AS-T012](05-migration-roadmap.md#as-t012) 实现总线队列及事件日志 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [M2 报告](implementation/m2-foundation-report.md)：PG queue/log 原语；原子 drain/TTL/多游标/trim 契约测试全过 | 2026-09-14 |
| [AS-T013](05-migration-roadmap.md#as-t013) 实现总线锁与注册表 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [M2 报告](implementation/m2-foundation-report.md)：真实 TTL 锁 + 过期持有者不可解锁新持有者 + CAS；跨实例持久测试 | 2026-09-14 |
| [AS-T014](05-migration-roadmap.md#as-t014) 接入 RocketMQ 唤醒及可靠交接 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [收尾验收](implementation/m2-closeout-report.md)：远程 RocketMQ 双实例收发、持久恢复/重投递、活跃消费者互斥与非幂等隔离通过 | 2026-09-14 |
| [AS-T015](05-migration-roadmap.md#as-t015) 接入 IAM 与资源访问策略 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [M2 报告](implementation/m2-foundation-report.md)：真实 EdDSA JWT 覆盖（伪造头 401）+ bypass 同款 + 最小暴露；完整 principal 仍在 API 层（分层说明见报告） | 2026-09-14 |
| [AS-T016](05-migration-roadmap.md#as-t016) 统一启动、drain 与关闭 | M2 | 用户+编码 Agent | 1～2 | — | 已完成 | [收尾验收](implementation/m2-closeout-report.md)：接收闸门、取消保存后解锁、原生 lifespan、启动失败清理/并发关闭通过；真实研究/Gateway 接入仍属 M5+ | 2026-09-14 |
| [AS-T017](05-migration-roadmap.md#as-t017) 迁移消息和工具块 codec | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [消息 codec 报告](implementation/m3-t017-message-codec.md)：23 项原生/历史协议测试 + 5 项旧 codec 回归通过；不加载 LangChain，旧历史只读 | 2026-09-14 |
| [AS-T018](05-migration-roadmap.md#as-t018) 拆分配置及冻结契约 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [报告](implementation/m3-t018-t019-report.md)：244 项映射、优先级/冻结冲突/指纹与秘密隔离验证通过；旧契约回归通过 | 2026-09-14 |
| [AS-T019](05-migration-roadmap.md#as-t019) 装配原生模型角色及凭据 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [重新验收报告](implementation/m3-t018-t019-report.md)：64 项全部通过；Gemini 2.23.0 原生构造、单次重试及同步/异步连接池关闭验证通过，依赖锁检查通过 | 2026-09-14 |
| [AS-T020](05-migration-roadmap.md#as-t020) 接入模型网关适配 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [Docker 验收](implementation/m3-docker-acceptance.md)：Docker LiteLLM 普通/结构化/SSE、Sandbox V2 双鉴权/真实预算拒绝/重放通过；后续调用方替换见 T033/T064/T081 | 2026-09-15 |
| [AS-T021](05-migration-roadmap.md#as-t021) 统一 fallback、重试和熔断 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [退出修复与验收](implementation/m3-t021-stream-closeout.md)：探针对齐生产 httpx；修复跨 Task ContextVar 关闭；137 项通过，三轮真实接口含取消退出通过；旧路径清退仍由 T081 跟踪 | 2026-09-17 |
| [AS-T022](05-migration-roadmap.md#as-t022) 迁移输出与上下文恢复 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [Docker 验收](implementation/m3-docker-acceptance.md)：真实 length→stop 有限升级通过，155 项原生回归通过；领域 Pipeline 接线与旧循环清退见 T037/T064/T081 | 2026-09-15 |
| [AS-T023](05-migration-roadmap.md#as-t023) 接入一次计账边界 | M3 | 用户+编码 Agent | 1～2 | — | 进行中 | [T023 实施记录](implementation/m3-t023-accounting-report.md)：物理尝试回执、失败流与幂等补回已落地；全量 500 passed/2 skipped，最终专项 26 passed、真实 PG 专项 1 passed；代理账单关联、Gateway 迟到账单及旧计账退出待关闭；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T024](05-migration-roadmap.md#as-t024) 迁移工具协议与装配 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [组件验收](implementation/m4-t024-t025-report.md)：原生 Toolkit/ToolBase、统一装配、描述和动态提示词、框架保留名称验证通过；旧连接器及循环清退见 T026～T033/T081 | 2026-09-15 |
| [AS-T025](05-migration-roadmap.md#as-t025) 迁移工具权限和执行治理 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [组件验收](implementation/m4-t024-t025-report.md)：26 项工具契约测试、181 项原生回归、118 项旧接口回归通过；副作用/出网/执行区/幂等/并发/取消验证完成，跨进程提交账本仍属 M6 | 2026-09-15 |
| [AS-T026](05-migration-roadmap.md#as-t026) 迁移 MCP stdio/HTTP | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：真实 stdio/HTTP/SSE fixture 完成 list/call/cancel，连接不泄漏、关闭不可复活，schema 嵌套约束与原始引用保留 | 2026-09-15 |
| [AS-T027](05-migration-roadmap.md#as-t027) 补齐 MCP OAuth 与 SSE | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：RFC 8693 exchange、令牌缓存过期/刷新隔离、错误码翻译与 URL 受验、日志脱敏、SSE 路径窄判定；未引入 MCP 2.x | 2026-09-15 |
| [AS-T028](05-migration-roadmap.md#as-t028) 接入 Browser MCP 与 Skills | M4 | 用户+编码 Agent | 0.5～1.5 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：浏览器信任边界复刻（enforced 只读过滤、HTTP surface stdio 阻断）；技能纯上下文不扩大权限，裁剪后提示词与目录一致 | 2026-09-15 |
| [AS-T029](05-migration-roadmap.md#as-t029) 迁移各搜索提供商 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：四 search_api 分支、并行/去重/输出格式与基线一致，原生摘要 120 秒预算 fail-closed 隔离 | 2026-09-15 |
| [AS-T030](05-migration-roadmap.md#as-t030) 接入 Web 证据流水线 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：确定性引擎接入原生工具；额度预留/归还/transport 退款、run 缓存、SPECIFIC 边界、robots、异常提取与证据锚点全部可验证 | 2026-09-15 |
| [AS-T031](05-migration-roadmap.md#as-t031) 实现沙箱 Workspace 适配 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：控制器生命周期（create/start/archive/stop + 工件回收）、路径根 /workspace/work、profile 限额、Offloader 往返；后端三原语只经受治理分发且 AST 守卫无 docker/subprocess 导入 | 2026-09-15 |
| [AS-T032](05-migration-roadmap.md#as-t032) 接入沙箱权限与网络网关 | M4 | 用户+编码 Agent | 1～2 | — | 已完成 | [M4 收尾报告](implementation/m4-t026-t032-report.md)：能力令牌签发/回验/过期、manual/auto/open 最窄合成与封顶、审批版本冲突与 allow/block、auto 分类器带缓存、Worker 密钥形状键拒绝 | 2026-09-15 |
| [AS-T033](05-migration-roadmap.md#as-t033) 实现完整研究阶段 Pipeline | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T034](05-migration-roadmap.md#as-t034) 迁移澄清与研究简报 | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T035](05-migration-roadmap.md#as-t035) 迁移 Supervisor | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 复验验收](implementation/m5-t035-t036-reacceptance.md)：AS-A035 组件标准通过；引擎路由缝、M6 三窗口强杀恢复、M7 持久异步团队与 2026-09-18 真实异步双任务运行覆盖原扣验项；旧 supervisor 循环物理删除由 T081 跟踪 | 2026-09-18 |
| [AS-T036](05-migration-roadmap.md#as-t036) 迁移 Researcher | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 复验验收](implementation/m5-t035-t036-reacceptance.md)：AS-A036 组件标准通过；每主题独立 Worker 上下文、真实工具轮次与证据抽取、压缩归档及错误终态经 2026-09-18 真实链路复核；旧 researcher 循环物理删除由 T081 跟踪 | 2026-09-18 |
| [AS-T037](05-migration-roadmap.md#as-t037) 迁移上下文压缩及外置 | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T038](05-migration-roadmap.md#as-t038) 接入覆盖与来源契约 | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T039](05-migration-roadmap.md#as-t039) 接入证据及内外质量门禁 | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T040](05-migration-roadmap.md#as-t040) 实现补研与业务完成策略 | M5 | 用户+编码 Agent | 1～2 | — | 已完成 | [M5 全阶段验收](implementation/m5-all-acceptance.md)：210 项目标回归通过；组件验收完成，业务切换边界见报告 | 2026-09-15 |
| [AS-T041](05-migration-roadmap.md#as-t041) 实现框架与业务检查点 | M6 | 用户+编码 Agent | 1～2 | — | 待验收 | [M6 验收复验](implementation/m6-acceptance-report.md)：组件通过（当前树复验）；整项仍依赖旧 QueryLoopState 写入退出（T036 验收已于 2026-09-18 满足）| 2026-09-17 |
| [AS-T042](05-migration-roadmap.md#as-t042) 接入模型及工具提交账本 | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [T048 部署与账本记录](implementation/m6-t048-deployment-closeout.md)：扩展 SQL 工具回执、批量 fetch 实测量和模型失败尝试计费；T023 进行中、生产资源提供器及全链路未知结果对账仍未关闭；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T043](05-migration-roadmap.md#as-t043) 实现领域 outbox 和事件重放 | M6 | 用户+编码 Agent | 1～2 | — | 待验收 | [M6 验收复验](implementation/m6-acceptance-report.md)：组件通过（当前树复验）；等 T042 全调用链与旧事件入口退出 | 2026-09-17 |
| [AS-T044](05-migration-roadmap.md#as-t044) 接入运行租约与 fencing | M6 | 用户+编码 Agent | 1～2 | — | 待验收 | [M6 验收复验](implementation/m6-acceptance-report.md)：组件通过（当前树复验）；旧运行路由/锁退出仍待集成 | 2026-09-17 |
| [AS-T045](05-migration-roadmap.md#as-t045) 实现全部审批与暂停 | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [M6 验收复验](implementation/m6-acceptance-report.md)：组件通过（当前树复验）；真实出网审批服务仍为注入接口、未部署接线；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T046](05-migration-roadmap.md#as-t046) 实现决策幂等及反馈消费 | M6 | 用户+编码 Agent | 1～2 | — | 待验收 | [M6 验收复验](implementation/m6-acceptance-report.md)：组件通过（当前树复验）；等 T045 真实审批与旧进程内审批状态退出 | 2026-09-17 |
| [AS-T047](05-migration-roadmap.md#as-t047) 接入跨任务预算与 deadline | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [T048 部署与账本记录](implementation/m6-t048-deployment-closeout.md)：远区工具外层不重复记账、SQL 工具结算及失败已计费 usage 已扩展；完整生产六维预算闭环仍待验收；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T048](05-migration-roadmap.md#as-t048) 执行恢复与取消故障矩阵 | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [容器部署矩阵](implementation/m6-t048-deployment-closeout.md)：API+uvicorn+PG 三窗口强杀/自毁恢复已执行；等待真实租约过期、审批 ID 保留、外部副作用与账本对齐；跨主机及 Gateway/API 同故障仍未关闭；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T049](05-migration-roadmap.md#as-t049) 接入团队及成员会话 | M7 | 用户+编码 Agent | 1～2 | — | 待验收 | [Worker 报告](implementation/m7-worker-report.md)：真实原生分派、任务独立会话和独立消费者通过；旧 TeammatePool 生命周期退出待服务切换；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T050](05-migration-roadmap.md#as-t050) 接入任务依赖与认领 | M7 | 用户+编码 Agent | 1～2 | — | 待验收 | [Worker 报告](implementation/m7-worker-report.md)：持久认领、令牌心跳、执行中取消和迟到回执拒绝通过；完整暂停审批部署闭环依赖 M6 | 2026-09-16 |
| [AS-T051](05-migration-roadmap.md#as-t051) 接入团队消息和控制优先 | M7 | 用户+编码 Agent | 1～2 | — | 待验收 | [Worker 报告](implementation/m7-worker-report.md)：TeamSay 工具、双向普通消息消费、去重反馈进入模型、Worker 控制打断和远程 MQ 通过；旧转发层退出待服务切换 | 2026-09-16 |
| [AS-T052](05-migration-roadmap.md#as-t052) 接入交接及结果准入 | M7 | 用户+编码 Agent | 1～2 | — | 待验收 | [Worker 报告](implementation/m7-worker-report.md)：真实 Researcher/质量门禁/工件校验/Supervisor 覆盖汇合通过；整项关闭受前置 T050/T051 验收约束 | 2026-09-16 |
| [AS-T053](05-migration-roadmap.md#as-t053) 验证团队恢复及资源回收 | M7 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮关键项报告](implementation/m10-ledger-container-factory-report.md)：Docker 分派、工具提交后及交接提交后强杀恢复通过；正式 Worker 宿主、leader/跨主机组合故障与旧路径退出待验收；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T054](05-migration-roadmap.md#as-t054) 迁移文档上传与解析适配 | M8 | 用户+编码 Agent | 1～2 | — | 进行中 | [M8 文档适配](implementation/m8-document-report.md)：原生 Parser/Chunker、Worker 接线及35项回归通过；上传配额、真库/Docling/Office恢复验收待完成 | 2026-09-16 |
| [AS-T055](05-migration-roadmap.md#as-t055) 接入文档版本与发布 | M8 | 用户+编码 Agent | 1～2 | — | 待验收 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T056](05-migration-roadmap.md#as-t056) 接入纠错与局部重解析 | M8 | 用户+编码 Agent | 1～2 | — | 待验收 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T057](05-migration-roadmap.md#as-t057) 接入统一检索与问答 | M8 | 用户+编码 Agent | 1～2 | — | 待验收 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T058](05-migration-roadmap.md#as-t058) 接入知识授权及工作空间 | M8 | 用户+编码 Agent | 1～2 | — | 待验收 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T059](05-migration-roadmap.md#as-t059) 接入批量、回收与同步 | M8 | 用户+编码 Agent | 1～2 | — | 进行中 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T060](05-migration-roadmap.md#as-t060) 接入事实与 Wiki | M8 | 用户+编码 Agent | 1～2 | — | 待验收 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T061](05-migration-roadmap.md#as-t061) 接入健康、导出与备份 | M8 | 用户+编码 Agent | 1～2 | — | 进行中 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T062](05-migration-roadmap.md#as-t062) 接入基础记忆 | M8 | 用户+编码 Agent | 1～2 | — | 进行中 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T063](05-migration-roadmap.md#as-t063) 接入高级记忆与维护 | M8 | 用户+编码 Agent | 1～2 | — | 进行中 | [M8 验收报告](implementation/m8-acceptance-report.md)：原生适配/真库与回归证据已记录，剩余部署/专项验收详见报告 | 2026-09-16 |
| [AS-T064](05-migration-roadmap.md#as-t064) 迁移报告产品及写作 | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [M9报告验收](implementation/m9-report-report.md)：原生写作/评审、产品与发布组件已接线，131项原生及153项兼容回归通过；旧服务退出及部署边界详见报告 | 2026-09-16 |
| [AS-T065](05-migration-roadmap.md#as-t065) 接入引用与 CanonicalReport | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [M9报告验收](implementation/m9-report-report.md)：原生写作/评审、产品与发布组件已接线，131项原生及153项兼容回归通过；旧服务退出及部署边界详见报告 | 2026-09-16 |
| [AS-T066](05-migration-roadmap.md#as-t066) 迁移报告评审及修订 | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [M9报告验收](implementation/m9-report-report.md)：原生写作/评审、产品与发布组件已接线，131项原生及153项兼容回归通过；旧服务退出及部署边界详见报告 | 2026-09-16 |
| [AS-T067](05-migration-roadmap.md#as-t067) 接入发布 Worker 与格式 | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [M9报告验收](implementation/m9-report-report.md)：原生写作/评审、产品与发布组件已接线，131项原生及153项兼容回归通过；旧服务退出及部署边界详见报告 | 2026-09-16 |
| [AS-T068](05-migration-roadmap.md#as-t068) 验证报告及发布恢复 | M9 | 用户+编码 Agent | 1～2 | — | 进行中 | [M9报告验收](implementation/m9-report-report.md)：原生写作/评审、产品与发布组件已接线，131项原生及153项兼容回归通过；旧服务退出及部署边界详见报告 | 2026-09-16 |
| [AS-T069](05-migration-roadmap.md#as-t069) 重组兼容 API 与 BFF | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮关键项报告](implementation/m10-ledger-container-factory-report.md)：运行工厂、团队/反馈/预算及签名账本路由已接入原生组合；生产资源提供器、其余路由和默认入口切换未完成；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T070](05-migration-roadmap.md#as-t070) 重组 SSE 与任务活动投影 | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮关键项报告](implementation/m10-ledger-container-factory-report.md)：团队 HTTP 所有权、消息幂等、SQL 权威任务投影与原生宿主路由挂载通过；完整 SSE/业务契约及前端联合回归待验收；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T071](05-migration-roadmap.md#as-t071) 重组前端共享类型与状态 | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [M10 首批记录](implementation/m10-api-report.md)：组件验证通过，应用接线和整项验收待完成 | 2026-09-17 |
| [AS-T072](05-migration-roadmap.md#as-t072) 重组研究创建及运行工作区 | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [领取与实施报告](implementation/m10-t072-t073-report.md)：功能模块/路由拆分已实现，类型检查通过，91 项组件与10项桌面浏览器通过；完整业务 E2E 与继承 lint 债务待验收；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T073](05-migration-roadmap.md#as-t073) 重组知识库、管理及账户页面 | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [领取与实施报告](implementation/m10-t072-t073-report.md)：功能模块/路由拆分已实现，类型检查通过，91 项组件与10项桌面浏览器通过；完整业务 E2E 与继承 lint 债务待验收 | 2026-09-17 |
| [AS-T074](05-migration-roadmap.md#as-t074) 实现历史只读兼容 | M10 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮执行记录](implementation/m10-sandbox-team-host-report.md)：原生沙箱/团队绑定/发布组件通过，默认宿主与完整切换未完成 | 2026-09-17 |
| [AS-T075](05-migration-roadmap.md#as-t075) 统一 tracing 与运营指标 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | 原生 SQL 用量 HTTP 投影与代理账单关联已实施；完整 tracing/sink 与实际账单核验待验收，见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T076](05-migration-roadmap.md#as-t076) 迁移研究与知识评估 | M11 | 待领取 | 1～2 | — | 待开始 | — | 2026-09-14 |
| [AS-T077](05-migration-roadmap.md#as-t077) 重构架构检查及移植测试 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | [M11 首批记录](implementation/m11-readiness-report.md)：架构/镜像改造及清退盘点，完整验收未通过 | 2026-09-17 |
| [AS-T078](05-migration-roadmap.md#as-t078) 统一应用 Python 与镜像 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | [M11 首批记录](implementation/m11-readiness-report.md)：架构/镜像改造及清退盘点，完整验收未通过；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T079](05-migration-roadmap.md#as-t079) 验证运维模式和 Worker 清理 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | [M11 首批记录](implementation/m11-readiness-report.md)：架构/镜像改造及清退盘点，完整验收未通过；2026-09-18 最新边界见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T080](05-migration-roadmap.md#as-t080) 执行历史迁移及切换演练 | M11 | 待领取 | 1～2 | — | 待开始 | — | 2026-09-14 |
| [AS-T081](05-migration-roadmap.md#as-t081) 清退旧循环和过渡依赖 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | [M11 首批记录](implementation/m11-readiness-report.md)：架构/镜像改造及清退盘点，完整验收未通过 | 2026-09-17 |

## 更新模板

任务开始时记录具体负责人和当前产物；遇到阻塞记录“事实原因、解除条件、关联 AS-R、下一步”。完成时链接对应 AS-A 的实际结果、代码修订或源码指纹，说明实际工时与估算差异。只跑静态检查的任务不能以“已通过 E2E”关闭。

## 变更记录

| 日期 | 变更 | 实施进度影响 |
|---|---|---|
| 2026-09-14 | 完成功能/任务设计及证据盘点，初始化 81 项实施任务 | 业务迁移保持 0；文档交付单独验收 |
| 2026-09-14 | 九份主文档交付检查通过；646 份基线文件未变化，81 项任务依赖无环 | 文档交付完成；实施任务仍全部待开始 |
| 2026-09-14 | M0 完成：基线以 Git 提交 `a8fb8fb` 封存（646 指纹核对 645 未变）；`.venv-legacy` 隔离旧环境建立（mcp 2.2.0 与新环境 mcp 1.30.0 互斥）；191 路由运行时全量匹配并冻结（补遗 `/metrics`）；244 配置对齐；回归基线固化（后端 1891/1990，15 项既有失败隔离登记；前端单测/tsc 全绿）。证据见 [implementation/](implementation/) | 实施任务 4/81；业务迁移仍 0/75；M1 可开始 |
| 2026-09-14 | M1 完成：五个框架适配探针全部通过（T005 14/14、T006 12/12、T007 7/7、T008 6/6、T009 6/6，共 45 项断言）。关键发现：X-User-ID 默认盲信但 dependency_overrides 可覆盖（AS-D010 成立）；schema 隔离必要且可行（默认同名静默跳过为负向实证）；MessageBus 契约与设计假设一致（drain=at-most-once）；"模型后/工具前"崩溃窗口 snapshot+重发不足 → M6 必须配 AS-T042 操作账本；ASK 暂停恢复可用；新风险 AS-R021（InMemory 锁 TTL）、AS-R022（框架未声明依赖 apscheduler/alembic/aiodocker，探针环境已补装）。无阻塞性发现，M2 可开始 | 实施任务 9/81；业务迁移仍 0/75；M2 服务底座可开始 |
| 2026-09-14 | M2 第一批完成：新增 `../../src/open_deep_research/agentscope_runtime`（组合根 ASRuntime、schema 隔离存储工厂与显式迁移入口、PostgreSQL 持久 MessageBus 20 原语、IAM JWT 身份覆盖）；`tests/as_runtime/` 13 项真库测试全绿（原子 drain/TTL/fencing/CAS/跨实例持久/伪造头 401/有效 token 通过）。旧回归基线不受影响（1990 项收集零错误+抽样通过，双环境测试边界已隔离）。报告见 [m2-foundation-report.md](implementation/m2-foundation-report.md)。余项：T014 RocketMQ 桥接（本机镜像已就绪）、T016 完整 drain 编排（依赖 M5+ 运行路径） | 实施任务 14/81；T016 进行中；业务迁移仍 0/75 |
| 2026-09-14 | M2 收尾：T014/T016 实际接线与并发/关闭缺口修复；32 项测试全部通过，RocketMQ 使用用户指定 172.22.121.109 服务器，未启动本地 RocketMQ；见 [收尾报告](implementation/m2-closeout-report.md) | 实施任务 16/81；业务迁移仍 0/75；后续业务接入不计为本次完成 |
| 2026-09-14 | M3 第一批 T017 完成：原生 Msg 版本化 codec 与无 LangChain 的历史读取器；23 项协议测试和 5 项旧网关 codec 回归通过；见 [报告](implementation/m3-t017-message-codec.md)。下一依赖为 T018 配置拆分与冻结契约 | 实施任务 17/81；M3 为 1/7；业务迁移仍 0/75 |
| 2026-09-14 | T018 完成；T019 原生工厂与凭据绑定已实现，Gemini 依赖安装遭自动审批拦截，保留待验收。新回归 63 passed/1 deselected，旧契约 48 passed，见 [报告](implementation/m3-t018-t019-report.md) | 实施任务 18/81；M3 为 2/7 完成，T019 待验收；业务迁移仍 0/75 |
| 2026-09-14 | T019 重新验收通过：google-genai 2.23.0 安装/锁文件一致；补齐 Gemini 异步客户端关闭，完整目标回归 64 passed，无排除项；见 [报告](implementation/m3-t018-t019-report.md) | 实施任务 19/81；M3 为 3/7 完成；业务迁移仍 0/75 |
| 2026-09-14 | T020～T022 执行：新增原生网关和调用策略，90 项新回归、73 项旧回归通过；真实服务联调、跨提供商与领域压缩接线尚未完成，见 [执行记录](implementation/m3-t020-t022-report.md) | 完成任务仍 19/81；不把部分实现计为完成 |
| 2026-09-15 | T020～T022 再验收：原生回归 149、真实路由鉴权 6、旧回归 73 项通过；补齐服务签名、提供商停止原因/结构化策略、原生裁剪及恢复持久化，修复 LiteLLM 重试叠加。真实 LiteLLM 连接失败，见 [再验收记录](implementation/m3-t020-t022-reacceptance.md) | 实施任务保持 19/81；T020 阻塞，T021/T022 待验收；业务迁移仍 0/75 |
| 2026-09-15 | Docker 实测完成 LiteLLM/Sandbox V2 真实调用、预算拒绝与输出恢复；修复通用输出参数及思考截断升级。T021 保留退出异步生成器异常，详情见 [Docker 验收](implementation/m3-docker-acceptance.md)；临时资源清理完成 | 实施任务 21/81；T020/T022 组件验收完成，T021 待验收；业务迁移仍 0/75 |
| 2026-09-15 | 按用户要求暂缓 T023，交付 T024/T025 原生工具与治理组件；共享治理核心保留旧消息兼容入口，真实 Agent 普通/结构化输出离线验证通过；提示词回归保留 M0 已登记的 1 项失败。见 [M4 交付记录](implementation/m4-t024-t025-report.md) | 实施任务 23/81，加权约 28.1%；T021 仍待验收，T023 待开始；业务迁移仍 0/75 |
| 2026-09-15 | M4 收尾：T026～T032 全部交付——原生 MCP 连接器（stdio/HTTP/SSE + OAuth/错误码翻译 + Browser/Skills）、四分支搜索与原生摘要、Web 证据流水线适配（确定性引擎保留）、沙箱 Workspace/权限网关桥接。新增 49 项测试、原生回归 230 项、旧接口回归 168 项通过；补齐 pymupdf/bs4/markdownify/tavily 及 AS-R022 缺口依赖并更新锁文件；真实服务联调留 M5+。见 [M4 收尾报告](implementation/m4-t026-t032-report.md) | 实施任务 30/81，加权约 36.4%；M4 任务全部完成（T023 属 M3 仍暂缓）；T021 待验收；业务迁移仍 0/75 |

| 2026-09-15 | 交付 T033～T037 原生阶段、Agent 与上下文组件；最终目标测试 42 项、PostgreSQL 重验 6 项、旧接口抽样 37 项通过，批次有重叠；见 [M5 实施记录](implementation/m5-t033-t037-report.md) | T033/T034/T037 待验收，T035/T036 进行中；完成数仍 30/81，不将组件通过计作旧路径已退出 |

| 2026-09-15 | M5 补齐 T038～T040 来源契约、完整领域门禁及原生有限补研；210 项目标回归、37 项旧协议测试通过，旧门禁 166 通过/1 既有失败；真实模型生成部分成功报告，报告长度仍有缺口；见 [全阶段验收](implementation/m5-all-acceptance.md) | 36/81；T033/T034/T037～T040 组件已完成；T035/T036 待旧执行路径退出验收；业务迁移仍 0/75 |

| 2026-09-15 | 先提交 `1c97431` 封存 M5 和命名空间调整，再交付 M6 检查点、回执、outbox、fencing、审批决策与预算组件；原生目标回归 218、最终恢复矩阵 35、旧回归 30 项通过（批次重叠）；见 [M6 报告](implementation/m6-recovery-report.md) | M6 整项尚未关闭；T023 继续暂缓，36/81、业务 0/75 不变；M6 代码尚未提交 |

| 2026-09-16 | 按用户要求先完成本地提交 `7d7f0f3`（M6 组件），再推进 M7；47 项联合回归、10 项最终 PG/远程 MQ 专项、7 项旧事务测试通过，批次重叠；新增固定 RocketMQ SDK 依赖并通过锁/兼容检查 | T049～T053 进行中，尚无 M7 整项关闭，完成计数仍 36/81；M7 改动尚未提交 |
| 2026-09-16 | 接通真实原生 Worker、TeamSay/反馈、交接工件与覆盖汇合；独立进程五窗口强杀、leader 新租约重建、关闭清理通过；114 passed/1 skipped 联合回归、22 passed PG/远程 MQ 专项及1项补充反馈（批次重叠） | T049～T052 组件待验收，T053 进行中；保留部署组合故障与旧路径退出边界，完成数仍36/81；证据见 M7 Worker 报告 |

| 2026-09-16 | M8 第一批接入原生文档 Parser/Chunker 与实际 Worker；抽离共享密钥上下文和包导入边界，补齐监控/Excel 依赖；35项原生文档、67项旧网关与观测回归通过 | T054 进行中，T055～T063 未计完成；完成任务仍36/81，证据见 M8 文档适配报告 |

| 2026-09-16 | M8 隔离 PG 真库8项通过，Docling补验1项通过；原生联合150项、旧记忆兼容71项通过（批次重叠） | T055～T063 状态按验收边界更新；剩余真实记忆/备份和部署接线未关闭，完成计数36/81 |

| 2026-09-16 | M9接入原生报告产品与模型账本；七类报告、六格式、PDF/Office视觉及三个真实发布Worker强杀窗口通过；131项原生、153项兼容回归通过 | T064～T067待验收、T068进行中；保留旧路径退出及M6/T023边界，完成计数36/81；本轮未提交 |

- 2026-09-17：推进 M10 首批协议、SSE、类型和历史边界；完成计数保持 36/81，未将组件通过记为全阶段完成。

- 2026-09-17：推进 M11 首批架构与部署守卫；Docker 停止及前置验收缺口已记录，不执行旧循环删除，完成计数维持 36/81。

- 2026-09-17：Docker 已恢复，主镜像构建及原生 create_app 合成流程探针通过；移除 41 处 LangChain 导入，仍剩 74 处及 13 处旧引擎导入。最终原生边界 31 项、旧兼容 63 项通过；默认入口未切换，完成计数维持 36/81。详见 [接入记录](implementation/m10-native-cutover-progress.md)。

- 2026-09-17：推进原生 Gateway V2、Worker、生产团队资源绑定与发布路由；53 passed/1 skipped 联合回归，42 项旧兼容及 Docker 组件探针通过。导入盘点为 72+12；默认入口切换未完成，36/81 完成数不变。

- 2026-09-17：SQL 模型预算、Docker 团队分派/强杀恢复、运行工厂与补充业务路由推进；104 passed/1 skipped，15 项与39 项补充专项通过（重叠）。完整生产闭环及默认切换尚未完成，36/81 不变；详见本轮关键项报告。

- 2026-09-17：领取 T072/T073，负责人用户+编码 Agent，状态改为进行中。抽出16个页面实现及研究组件，拆分知识库面板/管理API/报告展示；91 passed/1 skipped 组件回归、10 passed 桌面 Playwright、TypeScript 通过。真实后端联合验收、角色矩阵和继承 lint 债务仍待推进，36/81 完成数不变。见 [实施报告](implementation/m10-t072-t073-report.md)。

- 2026-09-17：T021 完成原生组件验收。对齐探针与生产传输，修复跨 Task 关闭时 ContextVar token 归属错误；137 项通过，最终三轮真实接口共 15 次逻辑调用正常退出。完成数更新为 37/81、加权约 45.1%；历史段落的 36/81 保留当轮事实。T023 继续暂缓。见 [修复报告](implementation/m3-t021-stream-closeout.md)。

- 2026-09-17：工作区按职责分四批本地提交（`acf3946` 后端 M7～M11 原生组件、`0fa225e` 前端领域重组、`8746d07` 镜像 Python 3.14、`4994d76` IDE 配置），未推送远程。随后对 M6 全部八项任务执行组件验收复验：M6 核心文件自 `1c97431` 后已被 M7～M11 修改，在当前代码树重跑故障矩阵 35 passed、`tests/as_runtime` 全套件 470 passed/2 skipped（真实 PostgreSQL 容器，自清理，无残留），AS-A041～A048 组件标准全部通过；但 T023 暂缓、旧入口未退出、真实审批与部署故障矩阵未接线，无任务登记完成，状态与 37/81 完成数不变。见 [M6 验收复验记录](implementation/m6-acceptance-report.md)。

- 2026-09-17：T048 部署矩阵续作：按数据库时间等待崩溃租约过期，真实容器三窗口全部完成恢复，plan/outline 暂停与审批 ID 保留通过；最终原生全量 489 passed/2 skipped，旧环境 47 passed/2 项 HEAD 已有失败。修复原生内部账本鉴权绕过与失败模型尝试漏计数。跨主机和 Gateway/API 同故障仍未覆盖，M6 状态及 37/81 完成数保持不变。见 [部署与回归报告](implementation/m6-t048-deployment-closeout.md)。

- 2026-09-17：本轮代码分批本地提交完成：cfac047（预算/回执）、7c52d9b（原生宿主）、09e8e33（部署矩阵/架构守卫）。未推送；docs/ 按既有忽略规则保留本地验收交付。

- 2026-09-17：按用户要求恢复 T023，接入原生逐次模型预留/结算与重放、缓存和失败用量明细、可信回执幂等补回；修复 Gateway 部分缺失用量错误归零。原生全量 500 passed/2 skipped，最终专项 26 passed，详见 [T023 实施记录](implementation/m3-t023-accounting-report.md)。任务保持进行中，37/81 完成数不变，本轮未提交。

- 2026-09-17：推进生产资源提供器、Web Gateway 显式任务凭据及正式 team_executor 入口；内部出网端点接原生 SQL 租约。38 项早期联合、19 项真实 PG/容器专项通过；真实 LiteLLM 冻结 10 个目录并创建/封禁测试 Key。完整真实研究 E2E 未通过，状态不升级，详见 [生产接线记录](implementation/m10-production-resources-report.md)。

- 生产接线补验：原生全量 508 passed/1 failed/2 skipped；新 Worker 测试夹具的冻结指纹错误修正后真库专项 1 passed。真实研究生成、正式 Worker 命令的容器联调及长时凭据续期仍未关闭，不升级任务完成状态。

- 2026-09-18：按用户要求对 M5 T035/T036 执行复验验收：2026-09-15 扣验的三项（旧研究入口退出、工具级确认/外部执行恢复、持久异步团队）分别被引擎路由缝与 migration_check 守卫、M6 部署矩阵三窗口强杀恢复（489/515 全量）、M7 Worker 与正式 Controller 团队通道，及当日真实公开网络异步双任务运行（`e290e205…`，`completed`）覆盖；两项登记已完成，完成数 39/81、加权约 47.6%。旧循环物理删除仍由 T081 跟踪，业务验收 0/75 不变。见 [M5 复验验收记录](implementation/m5-t035-t036-reacceptance.md)。

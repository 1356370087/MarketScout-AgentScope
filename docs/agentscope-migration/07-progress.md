# 07 工作进度台账

[目录](README.md) · 最后更新：2026-09-20。**本页是状态和实际工时的唯一维护入口。**

## 来源契约、资料研究与四来源交互修复完成（2026-09-20）

**本轮三项运行问题已修复并验证，T072 已完成，整体更新为 67/81。** 冻结来源选择进入覆盖契约，统一来源计数与有限范围下的要求，配置下限仍为 3；修复中文标点造成的 URL 越界误判、资料定位字段在证据及评估快照中的类型错误，以及 `ApprovalPending` 被误判为 Worker 失败。报告引用格式与准入来源字段的指导也已补齐，未放宽引用校验。

原生相关回归 **159 passed**、兼容领域 **173 passed**，另有来源契约专项 15 项、抓取边界专项 3 项（批次有重叠）。修改前即失败的旧工具 schema 枚举用例保留基线证据。Web、Documents、Hybrid、Specific 均已取得真实报告，完成 Markdown/JSON 正式发布、HTTP 200 下载及浏览器下载哈希核对；批准、拒绝、待审批取消和刷新恢复已验证。内容质量评价不作为本轮完成/阻塞条件，探索与失败记录保留。见 [修复与验收记录](implementation/source-closure-20260920.md) 和 [机器可读证据](evidence/source-closure-20260920.json)。

本次验收前端、浏览器和四个验收服务已关闭。原有 Docker 容器在最终检查中也显示退出，原因未确定，已在验收记录单独注明。下方历史段落保留各轮当时的完成计数。

## 报告恢复评审预算修复完成（2026-09-20）

**报告恢复评审预算缺陷已修复并验证。** 原 Web 运行六份报告回执重放复现：冻结窗口为 131072，但沙箱候选模型漏传窗口而使用 32768，导致完整恢复草稿的固定输入被误判超限。现已绑定冻结目录窗口，保留完整草稿、整条证据与预算拒绝规则。最终联合 **89 passed**；真实长输入（19984 字符草稿、88 条证据）完成 **4 次评审、3 次修订**，运行 completed，Markdown/JSON 正式发布及下载均成功，费用 0.032683 美元，预算预留归零。本次验收服务及发布进程已关闭。见 [修复与验收记录](implementation/report-recovery-budget-20260920.md) 和 [机器可读证据](evidence/report-budget-live-20260920.json)。

按用户确认，**内容质量评价不纳入台账完成/阻塞判定，只有质量门禁底层运行错误才计入问题**。本次评审、修订和发布链路正常，不因内容评价保留预算缺陷。原三个研究交接及恢复草稿作为固定输入复用，未重新执行搜索、浏览器/Office 视觉或跨主机故障验收；其余技术验收范围单独跟踪，整体仍 **66/81**。

## 固定版本联合验收执行（2026-09-20）

上轮固定版本冻结 767 文件，指纹 `260c81a8…d1b7f3`；验收镜像内 741 文件核对一致，结束时源码未变化。联合回归 **153 passed**，真实 PostgreSQL/API 容器故障矩阵 **7 passed**。四来源真实新研究主批次成功 **0/4**，资料独立复验 **0/1**：Web 报告恢复评审触发 `ReportInputBudgetExceeded`；指定单 URL 与最少 3 来源门禁冲突；资料工具真实成功但研究迭代耗尽；混合研究进入第三方出网审批。失败发布均返回 409。固定小型知识集真实调用 **16/16** 通过，Recall/nDCG 均 1.0，中位延迟 7.566 秒，仅覆盖一份四单元合成语料。研究 Run Key 总费用 0.237391 美元，未发布草稿辅助 Judge 0.019031 美元，知识服务 Key 窗口增量 0.00324429 美元（共享 Key，非逐请求精确归因）。本轮验收服务及 Worker 已关闭，原服务保留。联合放行未通过，整体仍 **66/81**，见 [完整结果与边界](implementation/joint-acceptance-20260920.md) 和 [机器可读汇总](evidence/joint-acceptance-20260920.json)。

## 企业资料与报告交付阻断项代码修复（2026-09-20）

已修复 `search_documents` 被替换为无 Run Key 的宿主实现、敏感读取错误被误判为未知写入，以及严格评审后 CanonicalReport 丢失的问题；补齐 Gateway 资料库/embedding 接线与精确工具策略。兼容宿主十个模块迁至 `api_host`，原生导入守卫重新通过并禁止反向依赖。最终原生联合 **163 passed**，四来源长输入的计划/大纲审批、严格评审、Markdown/PDF 发布下载在确定性夹具下闭环。扩大兼容批次 **150 passed / 5 failed**，五项失败均在独立 HEAD 副本复现，保留证据。真实模型、PostgreSQL/Gateway/浏览器联合 E2E 尚未重跑，不登记生产放行或旧引擎清退，整体仍 **66/81**。见 [修复与验证记录](implementation/blockers-code-fix-20260920.md)。

## T076 原生 Judge 与配对评估接入（2026-09-20）

原生评分链路、十类指标、固定历史配对集和重复比较 CLI 已接入；独立 SQL 账本负责回执恢复及 Service Key 费用上限。联合 24 项通过，原有评估兼容 69 项通过（批次不累计）。首轮真实实验中断，未知模型操作按设计拒绝重试；修正评分独立费用上限后，新实验完成 4 个样本、32 次真实调用、0.095776 美元，同目录重放零新增调用。十类指标覆盖、19 个细分键及波动已记录；引用准确性低分与未评分项如实保留，不判定质量门禁通过。T076 仍进行中，整体 **66/81**，不把历史产物重评分视为新原生研究质量验收。见 [实施与证据](implementation/t076-native-judge-20260920.md)。

## T076 本地评估入口迁移（2026-09-20）

T076 已领取并进行中。按用户要求先迁移业务入口：本地研究改为 AgentScope NativeRuns/SQL，不导入或运行旧 QueryEngine；身份、冻结配置、失败状态、等待审批、超时取消和资源释放均沿用原生边界。原生 6 项、兼容 69 项通过，独立进程导入无 LangChain。原生 Judge/固定集/真实配对比较继续实施，不登记 T076 完成，整体仍 **66/81**。见 [实现与验证](implementation/t076-local-native-20260920.md)。

## M9 原生上下文适配推进（2026-09-20）

本轮推进 T064～T068 受影响链路：报告使用冻结模型窗口，候选模型调用前重新预算；原生评审/修订退出逐字段字符裁剪，保留完整草稿与整条证据，累计遗漏计数，预算/失租/未知回执等控制错误不被 fail_open 吞掉。最终原生 82 项、共享领域 105 项通过（分批，不累计为业务验收）；既有配置盘点 244/246 差异另有失败记录。真实长输入、归档回读、视觉及部署联调未执行，不升级完成数，仍 **66/81**。见 [本轮实现与证据](implementation/m9-context-adaptation-20260920.md)。

## 上下文原生化与清退影响登记（2026-09-20）

用户授权按上下文统一迁移计划继续编码；受影响功能先登记，E2E 联调与错误修复后续统一执行。本批不执行实际生产切换，也不提前删除仍被使用的旧引擎。任务总完成数保持 **66/81**，既有完成证据仍保留；本轮新增变更没有被那些历史批次验证。

| 关联任务 | 本轮影响与后续复验要求 | 本轮状态 |
|---|---|---|
| T037（M5） | 原生压缩、工具截断、上下文卸载及回读；旧组件验收不能代表完整原生闭环 | 增量改造进行中，待补验；原完成记录保留 |
| T064（M9） | 写作与大纲输入、研究交接信息；保留现有完整证据预算 | 真实长输入预算已复验通过，其余适配范围另验 |
| T065/T066（M9） | 摘要与回读后的来源、证据 ID、CanonicalReport、评审及修订反馈 | 真实报告评审/修订及 CanonicalReport 交付已复验；归档回读范围另验 |
| T067/T068（M9） | 归档与发布存储依赖、恢复后的文件引用、模型与发布回执 | Markdown/JSON 真实发布下载已复验；视觉与组合恢复另验 |
| T075/T076（M11） | 原生压缩的模型调用计账、追踪、降级记录与配对质量评估 | 待本轮联合复验 |
| T077/T078/T079（M11） | 清退导入守卫、原生测试迁移、依赖镜像、维护与资源关闭 | 待本轮联合复验 |
| T080/T081（M11） | 隔离切换演练、历史只读及旧引擎/适配代码删除；真实生产切换仍不在本轮范围 | 保留 E2E 放行条件，尚未清退 |
| T069/T070/T071/T073/T074（M10） | 正式接口、事件/状态、权限与历史读取受影响回归；不重复实施已完成模块 | 已完成记录保留，待本轮受影响回归 |
| T047/T048（M6） | 压缩与归档增加模型/文件操作，须验证预算、失租及崩溃恢复 | 待本轮联合复验 |

实现边界与本批实际验证见 [上下文原生化实施记录](implementation/context-native-progress-20260920.md)。代码完成、组件检查与 E2E 通过分开登记，不因本批代码变更升级 M9/M11 任务。

首批已补宿主工具结果卸载、受治理的 Lead/Researcher 分页回读工具与沙箱回读接口。第二批已将 Lead、Researcher（含复用 Researcher 的团队成员）切换到 AgentScope 自动/主动压缩：公开 `on_compress_context` 委托框架执行，退出研究循环的字符裁剪接管。摘要使用所属 Agent 模型并接入模型策略、物理计账及恢复账本；压缩状态独立提交，归档失败回滚内存状态，预算/取消/失租不允许降级吞掉。

第二批联合组件回归 **125 passed / 5 warnings（34.25s，AgentScope 2.0.8）**；范围为 `test_native_context.py`、`test_context_offload.py`、`test_research_migration.py`、`test_recovery.py`、`test_e2e_closeout.py`、`test_sandbox_workspace_policy.py`，与首批重叠、不累加。新增 13 项专项覆盖真实 SDK 自动/主动压缩、连续摘要、摘要回执及物理回执提交后的故障恢复、格式失败降级、归档失败恢复、预算拒绝、取消和失租；模型为可控夹具，恢复库为临时 SQLite。AST 语法检查及 `git diff --check` 通过，Ruff 未安装，未登记通过。

仍待公共配置映射与冻结、外层阶段请求视图、领域投影预算、宿主多模态卸载及真实模型/Gateway/PostgreSQL/浏览器 E2E；单次写作的旧字符裁剪调用者尚未退出。压缩增加模型费用和持久化操作，M6、M9/M11 及 M10 的影响登记保持待联合复验；旧引擎未删除，实际生产切换未执行。

## 当前收口状态（2026-09-19）

**T069 已完成，M10 已完成 5/6，整体实施完成 66/81。** 入口及领域模块拆分完成；149 个 OpenAPI 路径的请求/响应/schema/状态码与权限契约核对通过，74 项最终回归通过。T072 保持进行中：真实 Web 审批、反馈与降级 PDF 下载已通过，企业资料运行因 `UnknownOperation` 失败，四来源及活动/发现投影的真实补验仍待完成。见 [T069 收口](implementation/t069-api-closeout-20260919.md) 与 [推进记录](implementation/m9-m10-progress-20260919.md)。

T072 完整隔离栈已走通真实 Web 研究、计划/大纲审批、方向反馈及降级报告 PDF 发布下载。修正验收 Worker 的 MQ 主题前缀后，3 个子任务完成；真实补验同时发现任务活动 404，已修复原生 SQL 所有权与 Gateway 活动回传接线，回归通过，尚待更新镜像补验。正常质量报告、四来源模式和其余 M9/M10 门禁未齐，整体仍 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T068 评审提交后的容器恢复补验通过：前次失败确认为合成模型遗漏需求覆盖，补齐夹具后评审仅调用一次、最终通过且账本一致。产品门禁保持不变，容器已清理，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T068 报告模型提交后的真实 API 容器退出/新容器恢复通过，使用 PostgreSQL 与 HTTP，报告模型仅调用一次且完成事件/SQL 账本一致。本轮容器已清理；其余验收继续核对，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T068 修复失败运行残留 output.status=success 的 HTTP 投影问题，新增已提交正文后失败的真实 SQL/HTTP 验证；最终后端 13、前端 2 项通过，类型检查通过。部署组合验收继续，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T068 新增写作模型提交后的独立进程强杀恢复，确认模型不重复调用；原生报告专项 26 项通过，含发布 Worker 三个强杀窗口。部署组合验收仍待补齐，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 服务入口已收敛为装配及兼容引用（312 行），72 项联合回归通过，170/170 实际方法路径与基线一致。完整契约验收与 T068 前置继续核对，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出应用生命周期，48 passed / 1 skipped 及新增生命周期直接验证 1 passed。server.py 现 419 行，后续完成入口整理与验收核对，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出运行权限、配置和沙箱上下文，57 项专项回归通过；扩大旧工具安全批次因本地 MCP SDK 缺少 Client 导出而收集失败，已保留证据。server.py 现 546 行，生命周期继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出健康/就绪/指标端点及 HTTP 中间件，48 passed / 1 skipped 与 40 passed 两批回归通过（重叠）。server.py 现 753 行，启动生命周期与权限上下文继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出恢复扫描与请求/SSE 准入管理，61 passed / 1 skipped 与 51 passed 两批回归通过（重叠）。server.py 尚有 974 行启动检查和权限上下文等职责，继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出后台执行、恢复执行与控制命令监听，68 项回归通过。server.py 尚有 1262 行恢复扫描和启动等逻辑，继续推进，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出运行注册表、容量/延迟驱逐与关闭排空，53 passed / 1 Windows 既有 skipped。server.py 尚有 1381 行后台执行及启动逻辑，继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出创建、流式创建与恢复路由，61 项相关回归通过，170/170 方法路径保持。研究业务路由已分域，server.py 尚有 1615 行运行时管理与基础设施逻辑，继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出运行删除与保留策略，40 passed / 1 skipped，170/170 方法路径保持一致。server.py 仍有 1820 行，创建/恢复及运行时生命周期继续拆分，整体 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已进一步迁出运行列表/快照与审批/反馈/团队/取消接口；74、38、13 项相关回归批次通过（重叠），170/170 方法路径保持。server.py 尚有 2179 行，创建、恢复和生命周期继续拆分，完成数仍 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已迁出运行用量领域路由与对账，相关 81 项回归通过，170/170 公共方法路径保持一致。server.py 仍有 2487 行，研究与生命周期拆分继续；扩大旧 Supervisor 回归另有 5 项失败已记录，T069 不提前完成，总计 65/81。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

**T071 已完成，当前实施完成 65/81。** 领域 DTO/client/reducer/store 与真实 HTTP/SSE 样例核对通过；修复可空字段差异，前端 130 passed / 1 既有 skipped，类型检查通过。T069、T072 及 M9 剩余任务继续推进；见 [T071 验收](implementation/t071-state-contracts-closeout-20260919.md)。

**T070 已完成，当前实施完成 64/81。** 六阶段投影与三类 SSE 恢复、心跳、终态和权限矩阵通过；原生后端 88 passed，前端 128 passed / 1 既有 skipped。M9/M10 其余原范围继续推进；见 [T070 验收](implementation/t070-sse-closeout-20260919.md)。

T070/T071 已修复 SSE 断流、认证刷新、游标恢复及终态重连，并用服务端真实序列化样例对齐可空字段。前端 127 passed / 1 既有 skipped，后端 83 passed，样例一致性 1 passed，类型检查通过；完整投影/协议核验继续，63/81 完成数不变。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T071 已拆分七个领域 API 客户端与共享传输层，31 个请求方法及三类事件订阅保留原行为；前端 98 passed / 1 既有 skipped，类型检查通过。跨语言契约和状态层退出条件继续核对，完成数保持 63/81；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

**T073 已按原页面与场景验收完成，当前实施完成 63/81。** 真实角色/资料隔离、上传审核、重排问答、会话刷新和其余页面接线通过；前端 98 passed / 1 既有 skipped，类型检查与生产构建通过。M9/M10 其余任务继续推进；见 [T073 验收报告](implementation/t073-pages-closeout-20260919.md)。

T073 已补齐当前部署的真实浏览器会话刷新/退出及四角色矩阵：CSRF、Cookie 安全标志、viewer 禁止创建、管理权限和私有运行隔离均通过。上传审核、资料可见性与问答流程尚待补齐，完成数保持 62/81；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T073 已建立当前版本的隔离认证部署，并修复容器端口映射下 BFF 将同源登录误判为跨站请求的问题。11 项认证回归、类型检查和生产构建通过，真实浏览器登录成功；角色、刷新和资料流程尚待补验，62/81 完成数不变。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

**T074 历史只读兼容已验收完成，当前实施完成 62/81。** 修复用量补写/对账与发布事件尾部修补副作用，83 项联合回归通过；历史工件下载、所有权、旧 resume 409、无 LangChain 导入和禁止双向转换均有当前证据。M9/M10 其余任务继续推进；见 [T074 验收](implementation/t074-history-closeout-20260919.md)。

T074 历史用量的补写/对账副作用已修复，72 项历史、原生 HTTP、计账及观测回归通过；独立进程确认 Reader 无 LangChain 导入且存档内容不变。历史工件下载只读证据及 M9/M10 其他验收仍待完成，任务状态不提前升级；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已拆出活动流与能力/模型配置领域路由，40/24 项相关回归通过（批次重叠），170/170 公共方法和路径继续一致。研究 HTTP 与生命周期拆分、M9/M10 完整验收仍未完成，状态不提前升级；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 已进一步拆出发布与安全审批领域路由，67/20/54 项相关批次全部通过（重叠）；170 个公共接口清单仍一致。研究生命周期等拆分和 M9/M10 完整验收继续推进，状态不提前升级；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

T069 继续拆分用量与观测领域路由：75 项联合、10 项活动接口回归通过（重叠批次），实际 OpenAPI 与原盘点 170/170 方法及路径一致。server.py 仍有约 3789 行，研究生命周期等拆分及完整 E2E 尚未完成，M9/M10 目标继续进行；见 [推进记录](implementation/m9-m10-progress-20260919.md)。

M9/M10 继续推进：T064 大纲审批绕过证据预算的问题已修复，报告/审批/写作 87 项回归通过；前端 94 passed / 1 skipped，类型检查通过。正式路由拆分、真实长输入与完整浏览器验收仍在本次目标内，T064～T074 尚未升级完成。见 [推进记录](implementation/m9-m10-progress-20260919.md)。

**M7、M8 的全部单项任务已完成。** 本轮新增关闭 T053～T063 共十一项，当前完成 **61/81**。真实 PG/远程 MQ 54 项、M8 真库/Docling 69 项、记忆组件 78 项、文档与维护锁 10 项通过（批次重叠），真实 Office 4 种组合及备份恢复通过。T057 日期错误已修复；按用户授权恢复现有知识服务 Key 登记后，两次真实重排/问答调用及引用校验通过。Mem0 按用户确认登记组件完成，明确尚未实际业务使用。此结论不覆盖 T048 跨主机组合故障、M10 全量 BFF/浏览器及 M11 生产切换。见 [本轮报告](implementation/m7-m8-closeout-20260919.md)。

T047/T048 本轮继续推进：修复恢复申请 Key 的余额读取竞态，六维 PostgreSQL 双客户端预算验收通过，容器矩阵补齐审批提交后与完成事件入库后崩溃窗口。首批 17 passed，相关回归 58 passed / 1 skipped（批次重叠）；长时真实代理与跨主机/API+Gateway 组合故障仍待验证，两项维持进行中。见 [推进记录](implementation/t047-t048-progress-20260919.md)。

T043/T046 当前版本验收通过并登记已完成：联合 98 passed / 1 skipped，新增真 PostgreSQL 故障专项 2 passed。验证领域事件/游标原子性、公共重放去重、决策失败不唤醒、新旧审批隔离及任务反馈恢复。M6 仍有 T047/T048 未完成；见 [验收记录](implementation/t043-t046-acceptance-20260919.md)。

T023/T042 已完成原生实施收口：核验结果/计账/审计单事务、冲突账单隔离及 Gateway 已报告 token 校验已修复；联合 133 passed、最终专项 15 passed（重叠批次）。历史 77 条真实代理账单与 77 个 Gateway 操作唯一对应，隔离补回两次与原 SQL 一致。详见 [收口报告](implementation/t023-t042-closeout-20260919.md)；不据此宣称代理内部每次上游尝试已完成财务审计，T047/T048/T080/T081 边界保留。

M6～M11 已按原验收条目复核：T041/T044/T045/T049～T052 七项更新为已完成；T064 因真实报告输入超限从待验收退回进行中。当前版本联合复验 151 passed / 1 skipped，原生静态守卫通过。其余任务不凭无知识库的部分成功运行升级；逐项证据与剩余条件见 [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)。

Lead 分步替换依赖问题已编码修复：服务端拒绝单独删除未解除前置，要求同事务加入同一下游的新前置；回滚保留版本与依赖，正反向接口统一守卫。真实 PostgreSQL 联合回归 40 passed / 1 skipped，去重后的最终专项 6 passed。尚未重跑完整浏览器 E2E，报告输入超限仍待修复，详见 [依赖修复记录](implementation/quality-closeout-20260919.md)。

2026-09-19 质量门禁专项排查：已确认接入 AgentScope 原生调用链，但发现 Judge 协议异常污染恢复会话、候选证据与准入混用、质量反馈未返回研究员等闭环缺口。排查时 teams 运行 `809657a6…` 已失败；后续修复及新运行结果见下一段。评分、状态与策略归因见 [质量门禁排查](implementation/agent-teams-quality-audit-20260918.md)，当前不登记完整 teams E2E 通过。

2026-09-19 后续修复：上述 P0/P1 已编码，153 passed/1 skipped 联合回归、4 项真实历史错误回放通过；公开 PostgreSQL 历史三来源经新批次聚合和真实 Judge 重评一次通过（45 条候选全部保留）。前端类型检查通过。已按顺序完成新 E2E `fd178735…`：三项任务 accepted_with_caveats，原 mq 缺证据断言被拒后完成补证；无 evaluator_error，两名成员优雅退出、消息无积压。最终在报告大纲阶段因 139716 输入 token 超过 131072 上限而 failed；另记录 Lead 分两次替换依赖导致汇总提前领取的问题。完整 E2E 未通过，本次容器与浏览器已清理。见 [质量修复与回放记录](implementation/quality-closeout-20260919.md)。

原生可观测性迁移：已补 AgentScope 生命周期内容最小化 OTel/Langfuse 导出、SQL 权威统计到 Prometheus 及专用 Grafana 看板；修复预算 undefined 标签、工具成功率、角色/任务分组、时间桶和任务级调用计数。当前本地 `.env` 的 `LANGFUSE_ENABLED / OTEL_ENABLED / PROMETHEUS_ENABLED` 均为 false，未擅自开启或声称生产平台验收通过。实施与验证边界见 [原生观测迁移记录](implementation/native-observability-20260918.md)。

Agent Teams 修订方案已获用户授权并实施 AT-1～AT-6：保留 collaborator，新增 Lead 显式创建、混合执行模式、SQL CAS 与双向依赖、RocketMQ 完整信封和事务发件、持久成员 Docker、版本化计划审核及前端投影。**编码和组件测试已推进，真实 E2E 未全量通过**。回查原文与原始回执后，已修复活动成员安全操作恢复、无变化等待消耗 Lead 轮次、V2 响应丢失的回执查询，以及 TaskList 携带完整工件导致后续任务被截断。最新批次分别为恢复与研究回归 70 passed、V2 回执 62 passed、任务列表与研究回归 57 passed、创建前反馈 1 passed（批次重叠，不累加）。活动任务强杀恢复在 `07c1a419…` 已真实通过；`b7df31ba…` 两项上游 accepted，但汇总因列表截断失败。`809657a6…` 已失败；最新 `fd178735…` 完成质量闭环与成员优雅退出，但报告超限失败，完整验收仍未通过。实施、运行证据和剩余限制见 [Agent Teams 实施记录](implementation/agent-teams-implementation-20260918.md) 与 [原文及根因核对](implementation/agent-teams-root-cause-20260918.md)。

后续 Docker 真实复验已通过 Playwright 弹出浏览器执行公开网络异步研究，知识库关闭。发现并修复镜像依赖、迁移 schema、Worker 装配、工具路由、冻结配置、Web 准入、审批租约、Controller 并发连接及任务事件等实际联调问题；原生统一专项 **210 passed**，其后上下文归档与生产装配专项分别 **47 / 4 passed**（存在重叠）。实际搜索、抓取、证据抽取与质量拒绝已取得真实回执，完整报告交付以 [Docker 真实复验记录](implementation/docker-web-live-20260918.md) 为准。以下表格保留上一轮编码提交时的验收快照，不代表这些 live 实验尚未尝试。

以当前工作区实现与本轮测试为准。正式实施完成数 **50/81**，加权约 **61.3%**，端到端业务验收仍为 **0/75**；这是原任务的验收计数，不是代码实现比例。此次用户授权对 E2E P0/P1 编码并执行一次本地提交；此前 M5 T035/T036 复验使完成数 37/81 → 39/81；M6/M7 七项复核使完成数 39/81 → 46/81；T023/T042 收口后为 48/81；T043/T046 验收后为 50/81，阶段退出和业务验收仍单独跟踪。

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

业务功能迁移 **0/75**；实施任务完成 **61/81**（原 50 项，加上本轮 T053～T063 十一项）。M7、M8 单项验收已完成，M6 跨主机组合故障与 M10/M11 发布门禁仍待关闭。加权实施进度 **约 75.1%**（完成估算中点 90.25 / 总中点 120.25 人日）。本地组件及真实模型部分成功不等同业务切换完成，实际人日未记录。

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
| [AS-T023](05-migration-roadmap.md#as-t023) 接入一次计账边界 | M3 | 用户+编码 Agent | 1～2 | — | 已完成 | [T023/T042 收口](implementation/t023-t042-closeout-20260919.md)：应用可见物理尝试一次计账、缓存/重试/流式失败与缺失值处理通过；历史 77 条代理账单两次补回与 SQL 完全一致；代理内部逐次审计边界见报告 | 2026-09-19 |
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
| [AS-T041](05-migration-roadmap.md#as-t041) 实现框架与业务检查点 | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：检查点版本/身份/凭据引用及强杀重建通过；原生路径使用 SQL RecoveryStore，旧 QueryLoopState 物理清退归 T081 | 2026-09-19 |
| [AS-T042](05-migration-roadmap.md#as-t042) 接入模型及工具提交账本 | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [T023/T042 收口](implementation/t023-t042-closeout-20260919.md)：已提交工具不重做、未知非幂等隔离、回执先于消费通过；核验结果/结算/审计现为单事务，SQLite/PG 故障回滚和幂等复验通过 | 2026-09-19 |
| [AS-T043](05-migration-roadmap.md#as-t043) 实现领域 outbox 和事件重放 | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [T043/T046 验收](implementation/t043-t046-acceptance-20260919.md)：领域更新/outbox 与投影/游标同事务；真 PG 发布后崩溃重放保持完成及用量事件不重复；原生 SQL 公共事件接线通过 | 2026-09-19 |
| [AS-T044](05-migration-roadmap.md#as-t044) 接入运行租约与 fencing | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：SQL 过期接管、跨实例旧 fence 拒绝、并发恢复和迟到回执通过；正式 Docker 成员失租恢复已有实证 | 2026-09-19 |
| [AS-T045](05-migration-roadmap.md#as-t045) 实现全部审批与暂停 | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：持久审批/部分确认/预算与敏感工具恢复通过；真实出网批准拒绝已接线；计划及成员审批实测，大纲输入超限另归 T064 | 2026-09-19 |
| [AS-T046](05-migration-roadmap.md#as-t046) 实现决策幂等及反馈消费 | M6 | 用户+编码 Agent | 1～2 | — | 已完成 | [T043/T046 验收](implementation/t043-t046-acceptance-20260919.md)：真 PG 决策插入后失败完整回滚且不唤醒；重复旧决策不影响新审批；任务反馈隔离及恢复消费通过 | 2026-09-19 |
| [AS-T047](05-migration-roadmap.md#as-t047) 接入跨任务预算与 deadline | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [最新推进](implementation/t047-t048-progress-20260919.md)：修复创建 Key 的余额读取竞态；六维 PostgreSQL 双客户端竞争、接管、未知结果预留与幂等结算通过；长时真实代理续期及账单持续联动仍待验证 | 2026-09-19 |
| [AS-T048](05-migration-roadmap.md#as-t048) 执行恢复与取消故障矩阵 | M6 | 用户+编码 Agent | 1～2 | — | 进行中 | [最新推进](implementation/t047-t048-progress-20260919.md)：真实 API+PG 容器矩阵扩展到五窗口，新增审批消费提交后、完成事件入库后强制退出恢复；跨主机及 Gateway/API 同故障仍未关闭 | 2026-09-19 |
| [AS-T049](05-migration-roadmap.md#as-t049) 接入团队及成员会话 | M7 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：成员复用、任务/运行会话隔离、正式独立 Docker Worker 多任务与重新绑定通过；原生路径退出旧 TeammatePool | 2026-09-19 |
| [AS-T050](05-migration-roadmap.md#as-t050) 接入任务依赖与认领 | M7 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：单次领取、所有权/取消/幂等、接纳后解锁通过；分步替换守卫已补真库并发回归，不再暴露无阻塞窗口 | 2026-09-19 |
| [AS-T051](05-migration-roadmap.md#as-t051) 接入团队消息和控制优先 | M7 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：双通道控制优先、普通消息持久去重和死 Worker 回执恢复通过；真实 RocketMQ 完整信封与 27 条发件零积压 | 2026-09-19 |
| [AS-T052](05-migration-roadmap.md#as-t052) 接入交接及结果准入 | M7 | 用户+编码 Agent | 1～2 | — | 已完成 | [M6～M11 复核](implementation/m6-m11-reacceptance-20260919.md)：委派需求/工件校验/质量准入通过；真实正常退出但无证据断言被拒，补证接纳；失败或拒绝不冒充完成接纳 | 2026-09-19 |
| [AS-T053](05-migration-roadmap.md#as-t053) 验证团队恢复及资源回收 | M7 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：真实 PG/远程 MQ 54 项通过；prepare/commit/replay、提交失败、Worker 强杀与 leader 新租约重建通过；跨主机组合故障仍由 T048 跟踪 | 2026-09-19 |
| [AS-T054](05-migration-roadmap.md#as-t054) 迁移文档上传与解析适配 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：HTTP 上传配额/去重、真实租约接管和并发重试通过；真实 Office/Docling 4 种组合及无 Docling 降级通过 | 2026-09-19 |
| [AS-T055](05-migration-roadmap.md#as-t055) 接入文档版本与发布 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：真库待审、发布、撤回及运行代次冻结通过，T054 前置验收已补齐 | 2026-09-19 |
| [AS-T056](05-migration-roadmap.md#as-t056) 接入纠错与局部重解析 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：真实 Docling、HTTP 修订冲突 409、发布代次不可变与草稿检索隔离通过 | 2026-09-19 |
| [AS-T057](05-migration-roadmap.md#as-t057) 接入统一检索与问答 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：as_of 日期类型错误已修复；真库检索/配额/拒答/引用回归与两次真实 LiteLLM 重排/问答通过；按用户授权恢复现有服务 Key 登记 | 2026-09-19 |
| [AS-T058](05-migration-roadmap.md#as-t058) 接入知识授权及工作空间 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：启动安装动态 ResourceAccessPolicy、领域知识库只读投影接线通过；成员/可见性/撤权/owner 转移/SQL 过滤通过 | 2026-09-19 |
| [AS-T059](05-migration-roadmap.md#as-t059) 接入批量、回收与同步 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：批量并发认领、逐项撤权、取消及中断恢复已修复并通过；引用保护/恢复/快照去重与宿主逐跳出网审核通过 | 2026-09-19 |
| [AS-T060](05-migration-roadmap.md#as-t060) 接入事实与 Wiki | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：真库事实审核/撤回、Wiki 版本、旧引用与过期检查通过，原生事实发布工具实调用通过 | 2026-09-19 |
| [AS-T061](05-migration-roadmap.md#as-t061) 接入健康、导出与备份 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：导入预检/导出/健康及实际 restic/pg_dump/pg_restore 演练通过，恢复文件校验和与运行引用链一致 | 2026-09-19 |
| [AS-T062](05-migration-roadmap.md#as-t062) 接入基础记忆 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：按用户确认完成 Mem0 组件验收；Platform/OSS/noop、身份隔离、合格证据及故障回归通过，尚未实际业务使用 | 2026-09-19 |
| [AS-T063](05-migration-roadmap.md#as-t063) 接入高级记忆与维护 | M8 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/m7-m8-closeout-20260919.md)：高级记忆固定时间/遗忘/并发回归及维护退出真实锁释放通过；Mem0 按用户确认的组件接入范围完成 | 2026-09-19 |
| [AS-T064](05-migration-roadmap.md#as-t064) 迁移报告产品及写作 | M9 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮修复验收](implementation/source-closure-20260920.md)：修复沙箱候选窗口漏传；真实长输入预算及四来源报告交付通过，资料定位字段/引用范围指导已补齐；其他写作适配范围另验 | 2026-09-20 |
| [AS-T065](05-migration-roadmap.md#as-t065) 接入引用与 CanonicalReport | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [本轮修复验收](implementation/source-closure-20260920.md)：完整证据、引用及四来源 CanonicalReport 实际交付通过；资料结构化定位信息保留，原生归档回读范围另验 | 2026-09-20 |
| [AS-T066](05-migration-roadmap.md#as-t066) 迁移报告评审及修订 | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [本轮修复验收](implementation/source-closure-20260920.md)：真实评审/修订及四来源交付闭环；预算、来源约束和引用目标格式已修复验证，内容质量不扣验；其他迁移范围另验 | 2026-09-20 |
| [AS-T067](05-migration-roadmap.md#as-t067) 接入发布 Worker 与格式 | M9 | 用户+编码 Agent | 1～2 | — | 待验收 | [本轮修复验收](implementation/source-closure-20260920.md)：四来源 Markdown/JSON 经正式 Worker 发布，HTTP/浏览器下载 200 且哈希一致；新版本 PDF/DOCX/PPTX 视觉仍待验收 | 2026-09-20 |
| [AS-T068](05-migration-roadmap.md#as-t068) 验证报告及发布恢复 | M9 | 用户+编码 Agent | 1～2 | — | 进行中 | [本轮修复验收](implementation/source-closure-20260920.md)：六份原始回执重放定位预算错误并修复，真实后续评审/修订/发布闭环；共享存储清退适配和跨主机组合恢复仍待验收 | 2026-09-20 |
| [AS-T069](05-migration-roadmap.md#as-t069) 重组兼容 API 与 BFF | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [收口报告](implementation/t069-api-closeout-20260919.md)：入口及领域 routers/use cases/BFF 拆分；170/170 公共路径保持，149 个 OpenAPI 路径契约及权限闭包核对通过；最终 74 passed，所有权、错误状态和发布下载兼容 | 2026-09-19 |
| [AS-T070](05-migration-roadmap.md#as-t070) 重组 SSE 与任务活动投影 | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/t070-sse-closeout-20260919.md)：六阶段原生映射、三类 schema/游标/重连/终态/心跳/权限矩阵通过；后端 88 passed，前端 128 passed / 1 既有 skipped，类型检查通过 | 2026-09-19 |
| [AS-T071](05-migration-roadmap.md#as-t071) 重组前端共享类型与状态 | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [本轮验收](implementation/t071-state-contracts-closeout-20260919.md)：领域 DTO/client/reducer/store 拆分、真实 HTTP/SSE 样例对齐与唯一实现核对通过；前端 130 passed / 1 既有 skipped，类型检查通过 | 2026-09-19 |
| [AS-T072](05-migration-roadmap.md#as-t072) 重组研究创建及运行工作区 | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [四来源修复验收](implementation/source-closure-20260920.md)：四模式真实研究报告、SSE、审批/拒绝/取消、刷新恢复、发布及浏览器下载与历史查看通过；保留此前反馈交互证据，内容质量不扣验 | 2026-09-20 |
| [AS-T073](05-migration-roadmap.md#as-t073) 重组知识库、管理及账户页面 | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [验收报告](implementation/t073-pages-closeout-20260919.md)：真实角色/资料可见性、上传审核、重排问答、会话刷新及页面接线；前端 98 passed / 1 既有 skipped | 2026-09-19 |
| [AS-T074](05-migration-roadmap.md#as-t074) 实现历史只读兼容 | M10 | 用户+编码 Agent | 1～2 | — | 已完成 | [验收报告](implementation/t074-history-closeout-20260919.md)：83 项联合通过；历史用量与发布事件无写入、工件下载、旧 resume 409、独立进程无 LangChain 与禁止双向转换 | 2026-09-19 |
| [AS-T075](05-migration-roadmap.md#as-t075) 统一 tracing 与运营指标 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | 原生 SQL 用量 HTTP 投影与代理账单关联已实施；完整 tracing/sink 与实际账单核验待验收，见 [E2E收口](implementation/e2e-p0-p1-closeout.md) | 2026-09-18 |
| [AS-T076](05-migration-roadmap.md#as-t076) 迁移研究与知识评估 | M11 | 用户+编码 Agent | 1～2 | — | 进行中 | [原生入口实施](implementation/t076-local-native-20260920.md)：本地研究 NativeRuns/SQL 接线及生命周期完成，6 项原生、69 项兼容通过；原生 Judge、十类指标及固定历史配对 CLI 已接入，联合 24 项通过；真实重评分结果见 [本轮记录](implementation/t076-native-judge-20260920.md)，知识集和新研究生成配对待验收 | 2026-09-20 |
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

- 2026-09-19：M6～M11 逐项复核，当前代码联合复验 151 passed/1 skipped，原生导入守卫通过。T041/T044/T045/T049～T052 单项实施验收关闭，T064 因大纲输入超限退回进行中；实施完成 46/81、加权 56.3%，业务 0/75 与阶段整体验收边界不变。测试容器已清理；本轮未启动新浏览器 E2E。

- 2026-09-19：T023/T042 原生实施收口，修复未知操作核验的两事务窗口、账单冲突覆盖和已报告 token 校验；133 项联合与 15 项最终专项通过，77 条真实历史代理账单在隔离账本幂等复核通过。完成数 48/81、加权 58.8%，业务 0/75 不变；本次数据库和测试容器已清理。

- 2026-09-19：T043/T046 验收通过，新增 PG 决策事务失败及新旧审批隔离、投影/日志重放专项，联合 98 passed/1 skipped、真库 2 passed。未修改生产代码。完成数 50/81、加权 61.3%，业务 0/75 不变，测试容器已回收。

| 2026-09-19 | M7/M8 补齐上传恢复、真实 Office、动态权限投影、批量并发/中断、备份恢复与维护锁；按用户确认关闭 Mem0 组件验收 | 新增十一项完成，61/81；T057 日期修复与真实重排/问答通过，知识服务 Key 经用户授权恢复登记，见本轮报告 |

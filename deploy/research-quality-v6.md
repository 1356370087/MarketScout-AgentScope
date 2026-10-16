# 资料研究质量策略 v6

本策略修复“原文已经抓取，正确事实却在加工中丢失；随后评审要求继续补证或软化正确结论”的问题。新运行冻结 `quality-gate-v6`；既有 v5 及更早版本保留原有提取、准入和审阅分支。运行冻结契约仍为版本 19，质量策略本身已经进入配置指纹。

实际结果见 [多主题验收记录](research-quality-v6-results.md) 和 [同题独立研究对照](independent-research-comparison-20261016.md)。三个主题已完成运行与发布，但独立复核仍发现语义漏判，不能将本策略描述为所有报告已经完全准确。

## 工程依据

- [OpenAI Retrieval](https://developers.openai.com/api/docs/guides/retrieval)：语义相关的信息可能几乎不共享关键词。实现中不再用中英文主张与原文的词汇交集代替事实支持判断；证据事实以已回查的原文为准。
- [OpenAI Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)：使用具体任务、参考答案、正常/边界/对抗样本，结合过程与结果进行持续评估。多主题夹具预先列出必答事实、错误对照和官方来源，参考事实不注入研究 Agent。
- [Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)：孤立片段会失去实体、条件和上下文。提取保留参数定义、段落、表格表头与行、代码块，优先正文区域并去除导航噪声。
- [Anthropic Research engineering](https://www.anthropic.com/engineering/multi-agent-research-system)：按具体缺口收窄查询，明确任务边界，通过持久工件减少转述损失。定向补读使用同一已下载网页，对不同目标分别记录已检查片段；相同目标不会反复抽取同一段。
- [Anthropic Agent evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)：结合 groundedness、coverage、source quality 与执行轨迹检查，并审查评分器是否误拒绝有效答案。原始失败样本、审阅意见与源文保持可追踪，不以单个高分作为通过依据。

这些是实现取舍的依据。外部文章中的性能提升数字不是本项目的测量结果；本实现没有声称复现整套厂商检索架构。

## 证据保真与定向补读

`web/passages.py` 对原文段落、参数说明、表格和完整代码块进行有界处理。模型输出的摘录必须能在对应 chunk 中逐字回查；规范证据的 claim 使用该原文，而不直接采纳可能过度扩写的模型释义。伪造摘录、未知 chunk、导航和指令性内容继续被拒绝。

HTML 提取优先标准正文节点及文档正文容器；新的分块尽量沿完整块边界，不从表格中间的 `Max.` 或代码空行处截断。超出片段预算的内容仍受界限控制，不能将一次已读取等同于全文已验证。

正文 article/main 内的 aside 可能是必要提示或限制，予以保留；正文之外的导航侧栏仍移除。Qdrant 的“摄取前建索引”建议及过滤限制在此路径中曾被删除，已经使用同一份官网 HTML 做了修复前后对照。

模型只引用定义列表的释义时，提取器从同一有界原文块恢复其参数名和定义，避免 `This parameter ... default off` 脱离所属参数。若同一释义对应多个不同定义项，拒绝猜测绑定，要求更明确的原文。参数标识符允许排版或括注差异，未知标识符仍不能获得证据支持。

缓存区分总体访问进度和具体问题的检查进度。`focus_inspections` 随运行保存并在并发更新时合并。新目标可以重新检查已有网页的相关段落，提取缓存包含目标与策略版本；同一目标重试复用原结果，下载缓存和现有预算/未知结果规则继续生效。

长文档选段使用已有 `web_rerank` 模型角色，在有界的章节标题与片段预览上进行跨语言语义选择。超过 80 块时分批选择并对候选统一排序，避免总选文档前部；当全部片段可以放下时无需新增模型调用。选段缓存同时绑定阅读目标、需求 ID、策略及实际候选块，不能让一个任务替另一个任务宣称已经检查。

若交接摘要与证据矛盾，但质量评估已确认全部所属事实均有支持、无事实缺口且确定性检查通过，运行时仅进行一次结构化证据交接重建，再提交同一质量门禁。初次摘要和判定保留在 `summary_repair` 中。重建后仍不合格则继续拒绝，不以该路径绕过缺口或重新发起宽泛搜索。

研究门禁与写作均明确：用户询问如何操作时，必要输入、开启条件、步骤顺序、结果和适用限制属于必答事实；完整运维脚本、性能调优或范围外扩展不因此变成必答要求。资料中没有被提取的内容不能直接被判成“官方未说明”。

仅靠提示语仍有漏判，因此 v6 对明确的操作问题增加 `procedure_checks`：逐项记录必需输入、启用与默认值、步骤与结果、适用限制，并绑定真实证据 ID。仅说“需要某个摘要或服务”不能证明怎样取得或开启它。缺少任何必需项都会形成事实缺口并把覆盖状态降为 partial，即使模型同时给出高分和 accepted。无关的原理说明不自动扩大成安装操作题；共享需求仍遵循各任务实际归属。

其中每个需单独配置的前置功能，还通过 `activation_controls` 分别记录原文中的启用参数/命令及默认值，各自绑定证据。未知值必须为 null，不能把“服务需要该功能”推定成“默认已开启”；参数和值必须在对应摘录中按完整词项找到。缺失会产生定向补证要求，模型的总分不能抵消它。

已有有效证据 ID 但其原文不支持所声称的前提，归为带具体检索方向的事实缺口；未知 ID、缺少必填字段仍归为协议错误。通用认证、连通性和共享容量的完整部署配置不会自动扩张成用户必答范围。

需求拆分保留用户原话中的条件、否定和待核实说法。例如“摄取后才建索引，若要利用新边是否需要重建”不能拆成“索引只应在摄取后创建”的事实要求。条件片段合并时保留原始消息偏移；历史策略的需求 ID 不重写。

## 审阅问题闭环

每条问题由服务端分配 `issue_id` 和 `blocking`。后续 Reviewer 必须解释前轮阻塞问题已经解决还是仍然存在；同一缺陷保留 ID，新增缺陷必须是新的具体问题。缺少解决记录、未知问题 ID 或自相矛盾的解决状态属于协议错误，有限修复后仍失败则拒绝通过。

质量评估与 Reviewer 新配置默认输出上限统一为 8192 Token，为逐项覆盖、前提核验和问题解决记录提供空间。此前数据库初稿采用代码的 2048/3072 默认值，与质量评估示例推荐 8192 不一致，真实回执出现多次恰好用满 2048 后缺少字段的情况。Provider 没有一致返回 length，因此仅凭终止标签不能确认判定完整。旧运行保持冻结的输出上限，评分阈值及任务/月度费用上限不因此放宽。

低严重度的纯冗余/可读性建议，在不对应明确用户要求时保留为建议。任何事实、引用或覆盖缺陷仍可阻塞，即使被模型标为 low；绑定用户要求的可读性问题仍阻塞。原有分数阈值、引用/来源边界、未知 ID、关键事实缺失和模型不可用规则保持执行。

Reviewer 接收正文引用与来源列表的区别、前轮问题及原稿摘要。Revisor 应做最小完整修正，核对原文相邻句与表格上下文，不因评审转述遗漏了原文就删除正确结论。

正文引用识别覆盖“参考来源”“参考文献”等标题；仅在文末列来源不能满足正文引用。审阅结构化输出使用实际需求 ID 的枚举，原生 `ev_` 引用与写作端使用相同契约，未知 ID 仍然拒绝。

Supervisor 到达轮次上限时，若所有事实需求已经由门禁验证、来源与证据条件满足、没有活动任务、冲突、取消或其他预算停止，则以 `coverage_complete_at_iteration_limit` 正常进入写作。尚有真实缺口时继续按原规则部分交付或失败，不把“最后一轮没有再调用 ResearchComplete”误判为证据不足。

同步 Supervisor 的协调轮次至少足以逐一委派已冻结的范围并提交完成；单个 Researcher 的搜索深度保持原限额。每个任务结果同时列出未覆盖范围及尚未委派的范围，避免任务标题写了某个问题，却没有实际认领对应需求 ID。预算、截止时间、补证上限和门禁阈值继续执行。

报告适配器统一解包 `completion_decision` 的 override 更新协议，部分交付保持 partial、停止原因和缺口，避免包装对象没有顶层 action 时误显示 success。

写作与审阅都要求默认行为有相应部署范围的直接证据；配置选项存在不等于默认值已知。资料只支持条件结论时保留该条件，不凭空补全默认行为。该规则是模型指令，最终验收仍需独立核查其实际遵守情况。

## 验证入口

离线针对性回归（不调用模型）：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHON_DOTENV_DISABLED = '1'
uv run pytest tests/as_runtime/test_evidence_fidelity.py tests/as_runtime/test_review_convergence.py tests/as_runtime/test_procedural_coverage.py tests/as_runtime/test_qualified_requirements.py tests/as_runtime/test_efficiency.py tests/test_web_pipeline.py tests/test_report_reviewer_integration.py tests/as_runtime/test_research_quality.py tests/as_runtime/test_e2e_stability.py tests/as_runtime/test_gateway_ledger.py tests/as_runtime/test_research_migration.py
```

真实研究使用 `tests/fixtures/research_quality/topics.json` 的三个主题：PostgreSQL 17 增量备份、Qdrant payload/HNSW 过滤索引、Python 3.14 TaskGroup 取消语义。每题的 `required_facts`、`negative_controls` 和官方 `reference_sources` 用于独立复核，不能当作 Agent 已自行发现的证据。

在已初始化、配置生效的专用部署执行（真实模型会产生费用）：

```powershell
uv run python tests/as_runtime/run_quality_e2e.py --base-url http://127.0.0.1:20249 --credentials-file .runs/platform-e2e-20261015/account.json --output .runs/quality-optimization-20261015/live-v6 --total-budget-micro-usd 3000000
```

账号文件包含 email/password，仅保存在未跟踪目录。默认每题模型预算上限为 1,000,000 微美元；实验上限为 3,000,000 微美元，还受平台与用户月度上限约束。第三方搜索费用不包括在模型账本中。审批仅面向本题研究计划、大纲与明确允许的公共官方域名。

测试保存运行 ID、执行状态、审批、任务活动、报告、审阅、用量和发布下载结果。`runtime_quality_passed` 只表示在线门禁通过；独立内容核对初始为 pending，必须逐项对照原文后填写，不能自动等价为内容正确。失败与降级样本继续保留。

若完成检查点已经可读、执行器租约尚未释放，发布创建可能短暂返回明确的租约占用 409。验收脚本仅对该已知状态与 run_not_completed 有界重试幂等发布，并保存冲突；其他发布错误保留为失败。恢复收集不会覆盖已有审批记录，也不会把恢复后短暂的观察时长当作完整研究耗时。

修改实现后必须使用新的输出目录；同目录恢复只复用同一实现与请求的已有运行。所有数据库使用 AGENTS.md 指定的远程服务，不启动本地 PostgreSQL。验证完毕后停止本次专用容器，保留原始工件与远程数据库，不影响其他项目服务。

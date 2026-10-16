# 深度研究 E2E 降级原因诊断（2026-10-15）

本次确认的主要问题在证据加工与质量判断之间：部分已经抓取、已经由模型正确提取的事实，在进入研究交接前被丢弃；研究门禁随后把某些实质缺口改判为可选建议；最终 Reviewer 又针对不完整的证据片段提出修改。任何非 info 意见都触发 revise，修订次数耗尽后统一标记 degraded，使真实缺失、证据误删和轻微编辑意见呈现相同的交付结果。

诊断使用本轮 PostgreSQL 17 真实研究的 SQL 快照、原始网页缓存、模型提取缓存、三轮报告审阅、离线代码复算，以及历史 Qdrant/Milvus 运行。另独立联网读取了五篇 PostgreSQL 17 官方文档和 Qdrant 官方索引文档。没有修改本轮原始报告、证据、门禁结论或阈值；没有为本次诊断新增付费模型调用。

## 1. 已复现：中文主张与英文摘录的词汇匹配误删正确证据

位置：`src/open_deep_research/web/pipeline.py` 的 `_complete_model_evidence()`，尤其是词汇交集比例必须达到 0.5 的条件。`NativeEvidenceExtractor` 此前已经验证摘录存在于对应网页 chunk 中，但合并阶段又用词面重合率判断模型主张与摘录是否匹配。

本轮 8 批模型提取共返回 51 条记录，经过该条件只保留 9 条，过滤 42 条。51 是批次记录数，包含重复来源或重复证据，不代表 51 个独立事实；42 条也不能全部直接认定为误删。44 条中文主张中仅 3 条保留，7 条英文主张中保留 6 条。

已逐字核对的明确误删案例：

- 主张：合并时按从旧到新顺序列出备份，第一个是全量备份，最后是要恢复的增量备份。
- 原文：`Specify all of the required backups on the command line from oldest to newest. That is, the first backup directory should be the path to the full backup, and the last should be the path to the final incremental backup that you wish to restore.`
- 证据 ID：`ev_494698ae2a7ccfb21467`。
- 模型两次提取出了这条事实，词汇重合率均为 0，合并时两次丢弃。
- 对应 chunk `chk_9ecffe900751964a21a3` 已记录为 processed，整篇 pg_combinebackup 文档 3/3 chunk 被标记处理完成。

离线反事实复算中，把证据主张规范为其已经验证的原文摘录，51 条均通过此过滤条件，原流程只通过 9 条。这证明过滤条件与语言形式相关；并不证明所有模型原始主张都正确。可优先考虑保留锚定原文作为事实载体，将翻译或释义与原文分开，而不是取消摘录回查、来源边界和事实支持校验。

## 2. 已复现：关键步骤漏提取，噪声却进入证据库

网页正文已经包含以下事实，但本轮证据链没有完整保留：

| 事实 | 原始抓取与处理情况 | 后续情况 |
| --- | --- | --- |
| `--incremental` 必须提供同一服务器先前备份的 manifest | continuous-archiving 对应 chunk 已处理；pg_basebackup 对应选项 chunk 尚未处理 | 未进入候选证据与最终报告 |
| 合并顺序从旧到新，第一项是全量备份 | 已处理、模型已正确提取 | 被词汇匹配过滤删除 |
| `summarize_wal` 默认 off，必须启用 | 对应 WAL 配置 chunk 已处理 | 摘取了 minimal 冲突和关闭后的清理行为，漏掉默认值及启用前提 |
| 合并后仍需适用的 WAL 与恢复流程 | 原文已抓取，证据库有保留 WAL 的陈述 | 压缩交接与最终报告未展开 |
| standby 缺少新 restartpoint 时增量备份可能失败 | 原文已抓取，对应 chunk 未处理 | 未进入最终报告 |

`WebPipelineSettings` 默认每文档选 3 个 chunk；缓存进度在模型提取调用成功后即把 chunk 标记为 processed，并在后续选段中排除。处理完成不等于所需事实已提取，当前语义会限制再次针对同一段补证。

此外，HTML 提取没有优先识别 PostgreSQL 页面的 `#docContent`，会退回整个 body；确定性证据兜底每段仅选 3 个句子，且没有与模型证据使用一致的完整性检查。本轮 74 条去重候选里有 19 条没有句末完整句标点，其中明确包含版本导航、Prev/Up/Next 导航表格和章节标题。这里的 19 不是独立事实错误率，但这些实例证明证据数量可以被非事实内容抬高。

对应代码：`web/extraction.py:extract_html`、`web/pipeline.py:_safe_evidence_sentences`、`_select_evidence_sentences`、`select_chunks` 及 processed_chunks 更新逻辑。

## 3. 已确认：研究门禁前后尺度不一致

同一个研究任务的前两次质量反馈明确指出缺少生成方法、manifest、合并输入顺序和 WAL 恢复衔接，要求继续取证。后续反馈却把完整命令语法概括为 advisory，并将相关需求全部标为 supported。

“不必给出完整运维脚本”可以成立；但 manifest 是必填输入，不只是命令排版或额外示例。用户问“如何生成、如何合并”，这类必要条件应保留为待回答事项。粗粒度的 requirement ID 被标为 supported，不能代替其内部步骤与前提的覆盖。

本轮两个交接均为 `accepted_with_caveats`，最终运行 `completion_outcome.gaps=[]`。这和独立核查发现的必要步骤遗漏并不一致。

报告写作和每轮审阅实际均收到全部 74 条已准入证据，未发生报告阶段按 Token 预算裁掉已准入记录的情况。因此，本轮核心事实的消失首先发生在写作之前；WAL 恢复要求则还存在交接/写作遗漏。

## 4. 评审意见需要逐条复核

| 意见或缺口 | 独立判断 | 依据 |
| --- | --- | --- |
| 第一轮 Reviewer：首次全量备份缺乏直接依据，只能写成推断 | 对最终提供的片段显得谨慎，但对已抓取原文的判断不成立；关键原文此前被误删 | pg_combinebackup 明确要求第一输入目录为 full backup |
| 最后一轮 Reviewer：报告声称引用 5 页，正文实质引用只有 4 页 | 成立，但属于轻微引用计数问题；仍满足用户至少 3 页要求 | backup.html 只在 Sources 出现，正文没有实质引用 |
| 最后一轮 Reviewer：已经使用“由此可推断”，还建议再强调推断性质 | 编辑建议，不能单独代表研究内容不可用 | 最后标注为 info，无硬失败 |
| 缺 manifest、启用 WAL 摘要、合并顺序及恢复衔接 | 是更值得优先修复的真实覆盖问题，Reviewer 最终没有抓住 | 官方操作说明；原始缓存与证据流转复算 |
| “平台限制” | 应明确仅指可选 `--clone` 加速；普通复制是默认路径 | pg_combinebackup 的 `--copy` 与 `--clone` 说明 |

最后一轮六项分数为 0.90–0.95，`hard_failures=[]`，仅剩一个 low 和一个 info 问题。当前 `report/reviewer.py` 把任何非 info 问题映射为 revise；`report/orchestrator.py` 在次数耗尽且仍为 revise 时标记 degraded。因此，低严重度编辑问题也会决定整个 E2E 是否严格通过。

三轮问题发生变化：第一轮是“全量前提的推断、版本信息重复”，第二轮是“引用冗余、引号改为概述”，第三轮是“5 页/4 页计数、推断措辞”。修订使用本轮 review，下一次重新全稿审阅；缺少稳定的问题身份、已解决问题回执和新增意见的实质性约束，容易持续发现新小问题直到耗尽次数。

## 5. 跨议题历史证据

历史 Qdrant/Milvus 样本 `914d985e6f7458c9a7c1aef017c21ba7` 的 Reviewer 认为“后建 payload index 后需重建 HNSW”超出原文。实际该次运行保存的官方网页缓存明确写着：`If you create a payload index after data has already been ingested, you need to rebuild the HNSW index to take advantage of the new payload indexes.`

该次传给评审的 `ev_ab8d776811c65c165d13` 摘录只保留了前两句，漏掉紧接着的明确说明。当前联网读取也确认该说明仍在官网。这是跨议题的“原文有依据，证据片段缺失，评审要求软化正确结论”的直接例子。

历史缓存：`output/e2e-repair-20261008/candidate-documents/9cef3029776a06d539e85590f01fe32a843b754aeffb0e99de37691d1e4f7ba3.md` 第 122 行。原 Reviewer 记录和 evidence_id 见同目录上层对应运行的 `report-reviews/` 与 `evaluation-snapshot.json`。

同一历史样本的 Milvus 多租户表格证据 `ev_1a6ef4f1e1baf81fecf6` 被截断在 `Max.`。Reviewer 指出无法验证具体上限是正确的，但根因在表格证据提取而非正文凭空遗漏。此次独立请求 Milvus 官网返回 403，因此没有声称已重新核实其当前上限数值。

历史样本不能直接混作当前版本通过率：旧 PostgreSQL 对照使用 quality-gate-v4，本轮为 v5；部分历史 Web 组关闭了 Reviewer。历史记录也包含实际通过的窄问题，不能概括为所有议题都必定降级。

## 6. 建议修复顺序与验收口径

1. 先修证据保真：锚定原文、跨语言主张不靠词面重合率丢弃；表格与代码参数按结构保留，去除导航噪声。用本轮原始缓存重放，确认 manifest、合并顺序等目标事实到达 Writer/Reviewer。
2. 拆分“已访问、已提取、已覆盖”：反馈定位到具体遗漏事实和原文区域，允许定向重读；不能把成功跑过抽取器等同于完成覆盖，也不能用反复扩大搜索代替恢复现成证据。
3. 统一研究与报告门禁的必答标准：程序性问题应核对输入、前置条件、步骤、输出与限制；必填参数不能因为某轮没有证据就自动变成 advisory。
4. 修订闭环要跟踪稳定问题及解决状态；只有可定位、可验证、有明确修法的意见才能阻塞。保留真实事实/引用/覆盖硬门禁，分别记录非阻塞编辑建议。先复核意见有效性，再决定接受标准，不能简单降低分数阈值。
5. 建立独立人工核验的小型多议题夹具，固定源文、必要事实、已知错误与可接受改写。分别衡量事实丢失、门禁误报、漏报及修订收敛，再做同版本真实 E2E 对照。

本节记录最初的诊断阶段。当时只完成诊断和部署接线修复，未调整质量管线。后续优化见 [资料研究质量策略 v6](research-quality-v6.md)，各轮原始失败结果仍保留；同一个模型给出的高分不能充当独立事实正确率。

## 7. 本轮部署与 E2E 事实

- 容器连接用户指定的远程 PostgreSQL `172.22.208.30:5432`，版本 18.6；远程专用应用库和 LiteLLM 库与既有业务库分离。AGENTS.md 与 Compose 已改为不部署本地数据库。密码只保存在未跟踪配置。
- 已修复构建上下文遗漏、BFF/Nginx 接线、首次初始化的沙箱依赖、PostgreSQL 历史费用分组查询，以及组件启动器的 Pydantic 模块解析。
- 远程 PostgreSQL 集成测试 7 项通过；桌面/移动端 IAM E2E 2 项通过；前端类型检查与相关 lint 通过。
- 研究 `d913d9f1e16a4d898fc1cbf68ed9f214` 完成，3 个任务完成，5 个来源列表条目，正文实质引用 4 页；报告 3244 字符（含 Markdown 和 URL），不是 3244 个中文字。
- 42 次真实模型调用，实报覆盖 100%，费用 76,646 微美元（$0.076646），全部预留归零，未知调用 0。连接测试另计 4 微美元；总月度记账 76,650 微美元。搜索费和基础设施费不在此模型账本内。
- 严格研究 E2E 在最终 Reviewer 的 `revise / degraded` 断言处失败，保留为失败。
- 随后独立验证了已完成报告的 Markdown 发布、下载与正文内容。发布任务 `pub-724d353e8103ce0b8be60279415c5d70b6970208` 完成，下载 4686 字节；Publisher 容器的 NetworkMode 为 none。
- 研究中实际通过按钮切入后台并切回同一详情页，任务持续执行；保留实时页与最终报告截图。

本轮证据目录：`.runs/platform-e2e-20261015/`。关键文件：`recovery-snapshot.json`、`raw-research-cache/`、`extraction-filter-audit.json`、`extraction-counterfactual.json`、`critical-fact-paths.json`、`review-history.json`、`publication-evidence.json`、`deployment-wiring.json`。这些运行数据不纳入版本控制。

独立官方来源（2026-10-15 联网读取）：

- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/app-pgcombinebackup.html
- https://www.postgresql.org/docs/17/continuous-archiving.html#BACKUP-INCREMENTAL-BACKUP
- https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-SUMMARIZATION
- https://www.postgresql.org/docs/17/backup.html
- https://qdrant.tech/documentation/manage-data/indexing/

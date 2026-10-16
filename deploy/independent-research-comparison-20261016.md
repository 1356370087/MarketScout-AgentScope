# 同题独立研究与系统结果对照

独立检索可以用限定范围内的官方资料回答三个主题的关键问题。已确认的反复缺口包含检索停止过早、正文提示框被删、条件问句被改写、需求未委派和评估输出不完整，不能统一解释成“官方没有资料”。

本对照沿用原题及来源边界：PostgreSQL 使用版本 17 官方文档；Qdrant、Python 仅使用原题指定的两页资料。PostgreSQL 先执行官方站内检索 `PostgreSQL 17 incremental backup`，读到 WAL 摘要前提后，再检索 `PostgreSQL 17 WAL summarization`。第二次检索直接返回 17 的 WAL 配置页。其余两题直接阅读固定资料。共读取八篇官方页面，保留正文中的提示框、代码和限制。

原始 HTML、独立正文、检索结果与摘要保存在 `.runs/quality-optimization-20261015/independent-research/`。这次阅读没有调用项目的提取器、研究摘要或 Judge；对照答案仅用于事后核验，没有提供给在途研究 Agent。

## PostgreSQL 17：增量备份如何生成和恢复

PostgreSQL 17 支持原生增量基础备份。`pg_basebackup --incremental=old_manifest_file` 要求提供参考备份的 manifest；该参考备份必须来自同一服务器。服务器据此生成增量备份：非关系文件完整保存，部分关系文件由变更块和重建元数据组成，不能将其当作独立全量备份直接恢复。[pg_basebackup](https://www.postgresql.org/docs/17/app-pgbasebackup.html)、[增量备份说明](https://www.postgresql.org/docs/17/continuous-archiving.html#BACKUP-INCREMENTAL-BACKUP)

必须先满足 WAL 摘要条件。官方配置页明确称 WAL summarization 是进行增量备份必须启用的功能；`summarize_wal` 默认 **off**，可通过 `postgresql.conf` 或服务器命令行开启，主库和备库均可使用。`wal_level=minimal` 不满足其正常工作条件。只写“依赖 WAL 摘要”不足以指导启用此功能。[WAL Summarization](https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-SUMMARIZATION)

摘要位于 `pg_wal/summaries`，须覆盖参考备份起始 LSN 到本次备份起始 LSN 的完整区间。摘要被清理或 summarizer 未及时跟上时，增量备份会失败。`wal_summary_keep_time` 默认 10 天，保留策略应覆盖实际依赖区间；开启开关本身不能替代检查所需摘要是否齐备。[增量备份说明](https://www.postgresql.org/docs/17/continuous-archiving.html#BACKUP-INCREMENTAL-BACKUP)、[WAL 配置](https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-SUMMARIZATION)

恢复前用 `pg_combinebackup` 合并完整备份链，输入按**从旧到新**排列，第一项是全量备份，最后一项是目标增量备份，`-o` 指定输出目录。结果为“合成全量备份”，以后也可代替该备份链作为合并输入。官方原文是 “the first backup directory should be the path to the full backup”，因此“首项全量”有直接依据。[pg_combinebackup](https://www.postgresql.org/docs/17/app-pgcombinebackup.html)

合并不会免除常规恢复要求：仍需适用的 WAL 段和历史文件，并按恢复流程设置 `recovery.signal` 或 `standby.signal`。备份链依赖由用户维护；`pg_combinebackup` 校验备份之间的关系，单个备份完整性另用 `pg_verifybackup` 检查。备库缺少新的 restartpoint、备份间 checksum 状态变化也可能限制增量备份或合并。[增量备份说明](https://www.postgresql.org/docs/17/continuous-archiving.html#BACKUP-INCREMENTAL-BACKUP)、[合并与限制](https://www.postgresql.org/docs/17/app-pgcombinebackup.html)

`--clone` 的 Linux/Btrfs/XFS、macOS/APFS 条件仅约束可选的文件克隆加速。普通文件复制 `--copy` 是默认方式，不能据此宣称整个增量备份功能只适用于这些文件系统。[pg_combinebackup 选项](https://www.postgresql.org/docs/17/app-pgcombinebackup.html)

## Qdrant：索引分工、创建顺序和过滤限制

两种索引职责不同。官方原文：“a vector index speeds up vector search, and payload indexes speed up filtering.” Payload 索引按字段和类型建立，既加速对应过滤条件的点检索，也估算过滤基数，帮助查询规划选择策略；HNSW 向量索引加速向量近邻搜索。[Indexing](https://qdrant.tech/documentation/manage-data/indexing/)

过滤检索需要两者配合。Qdrant 通过 “extending the HNSW graph with additional edges based on indexed payload values” 增加过滤感知边。边依赖已建索引的 payload 值；Filtering 页直接建议：“For best results, create payload indexes before ingesting data.” 因而摄取前创建 payload 索引有官方明确建议，不能只因某些 segment 按需建索引，就推断 payload 索引应在摄取后创建。[Indexing](https://qdrant.tech/documentation/manage-data/indexing/)、[Filtering](https://qdrant.tech/documentation/search/filtering/)

对于题设“已摄取数据，之后新建 payload 索引，并希望利用新 filter-aware 边”，需要重建 HNSW。原文明确列出这一情形：“when you create a new payload index and want to take advantage of filter-aware edges in the HNSW graph.” 这是一条有直接依据的条件结论。[Indexing：Rebuilding HNSW](https://qdrant.tech/documentation/manage-data/indexing/)

官方给出的办法是小幅修改 HNSW 配置，例如读取当前 `ef_construct` 后加 1：“This forces the optimizer to re-index all segments.” 文档还要求不要立即恢复原值：“Keep it set to the new value.” 这是重建触发方式，不是吞吐量提升数字，也不意味着每次查询或每个索引都必须无条件重建。[Indexing](https://qdrant.tech/documentation/manage-data/indexing/)

缺少 payload 索引不等于没有过滤能力。Indexing 页明确说，不启用 strict mode 时，缺索引的查询仍能返回结果，只是较慢。若开启 strict mode 并将 `unindexed_filtering_retrieve` 设为 `false`，对未索引字段的过滤查询会报错；Qdrant Cloud 默认对集合应用这些设置。因此需要区分索引带来的性能改善和部署策略对请求的拒绝，不能简单写成“无索引绝对不能过滤”或“所有部署默认都允许”。[Indexing：Block Queries That Filter on Unindexed Fields](https://qdrant.tech/documentation/manage-data/indexing/)

Filtering 页的前缀过滤还给出更具体的例子：未启用 prefix 索引时可按点检查并返回正确结果，但相应 strict mode 设置会拒绝缺少 prefix-enabled keyword 索引的请求。这个局部限制不能替代对整个过滤机制的解释。[Filtering](https://qdrant.tech/documentation/search/filtering/)

## Python 3.14：TaskGroup 的异常与取消

`asyncio.TaskGroup` 在上下文退出时等待组内任务结束。首次有子任务以非 `CancelledError` 的异常失败时，其余任务会被取消，之后不能继续向组内添加任务。如果 `async with` 的主体仍在执行，包含它的外层任务也会收到取消；这次内部 `CancelledError` 会中断主体中的等待，但不会直接冒出该 `async with` 语句。[Task groups](https://docs.python.org/3.14/library/asyncio-task.html#task-groups)

等所有任务结束后，非取消异常会合并为 `ExceptionGroup` 或适用的 `BaseExceptionGroup` 再抛出。主体自身带异常退出也按相同流程处理：取消并等待其余任务，通常把主体的非取消异常一起合并。应区分“发起取消”“等待清理完成”和“最终传播异常组”，不能只写成一个任务失败后立即返回。[Task groups](https://docs.python.org/3.14/library/asyncio-task.html#task-groups)

`CancelledError` 表示取消。自 Python 3.8 起，它直接继承 `BaseException`，不是普通 `Exception` 子类，`except Exception` 因而不会捕获它。任务取消会在下一个可处理取消的位置抛出该异常。协程应以 `try/finally` 保证清理；若捕获取消异常做清理，通常应在清理完成后重新抛出。[Exceptions](https://docs.python.org/3.14/library/asyncio-exceptions.html#asyncio.CancelledError)、[Task cancellation](https://docs.python.org/3.14/library/asyncio-task.html#task-cancellation)

“取消异常可以安全吞掉而不影响 TaskGroup”是不成立的。文档指出 TaskGroup、`asyncio.timeout()` 内部使用取消机制，吞掉 `CancelledError` 可能使它们 “misbehave”。确有抑制取消的特殊需求时，还须调用 `uncancel()` 清除取消状态；这并非推荐给一般业务代码的默认做法，不能把特殊处理解释成吞掉取消始终安全。[Task cancellation](https://docs.python.org/3.14/library/asyncio-task.html#task-cancellation)

`KeyboardInterrupt` 和 `SystemExit` 另有明确规则：TaskGroup 仍取消并等待其他任务，随后重新抛出最初的该异常，**不按通常方式封装进异常组**。主体自身抛出这两类异常也适用相同特殊处理。它们与取消异常、普通异常必须分别说明。[Task groups](https://docs.python.org/3.14/library/asyncio-task.html#task-groups)

## 与系统执行轨迹的比较

| 核查点 | 独立研究所见 | 系统历史轨迹 | 定位结果 |
| --- | --- | --- | --- |
| PostgreSQL 启用条件 | 第一次检索找到操作页；从 WAL 摘要前提追加定向检索，即找到版本 17 配置页及 default off | 多轮只读取操作/合并/归档页，达到来源数量后停止；Judge 仍判全面覆盖 | 必要前提发现与补证闭环不足；不是官方缺少说明 |
| 合并顺序与第一份全量备份 | 合并页直接说明 oldest-to-newest、first full backup | 基线正确摘录被中英文词汇交集规则丢弃，评审随后要求软化结论 | 证据加工误删，不能据此认定原结论缺乏来源 |
| 参数名与定义的绑定 | WAL 页在同一定义块列出 summarize_wal、boolean、启用含义和 default off | 后续定向补证已读到正确页面，但模型只引用释义，参数名仍被留在摘录之外，门禁无法绑定 | 恢复完整定义块；不能把 This parameter 的裸释义当成已证明任意具名参数 |
| Qdrant 摄取前建议 | 原文直接给出 before ingesting data | 旧 HTML 路径删除正文 aside，使建议从提取正文消失 | 提取器误删提示框；同一份 HTML 对照已复现并修复 |
| 无索引过滤与 strict mode | 两页分别给出一般规则与前缀条件的特殊例子 | 部分报告只看到局部 prefix 限制，或只依据配置项名称推断默认行为 | 应保留提示框，并区分一般规则与局部例子；一般规则在 Indexing 页 |
| 摄取后建索引的条件 | 题设场景下，新边需要重建 HNSW | 需求拆分曾把“摄取后才建”当成必须证明的恒真命题 | 需求语义失真，反向诱导出错误结论 |
| Python 异常传播 | 两页足以闭合全部问题，无需新增来源 | 证据已在兄弟任务中，却漏委派一个需求 ID，账本仍标缺口 | 协调与覆盖账本问题；不是资料不足 |
| 评分协议失败 | 不属于研究内容的真假判断 | 输出上限附近出现缺失必填字段；错误重试耗尽后交接被拒绝 | 应记录评估器故障，不能把协议错误当作官方证据不足 |

三个主题的最新系统运行、各版本失败保留、费用和发布状态见 [多主题验收记录](research-quality-v6-results.md)。这份对照先建立可核验答案，再定位信息在具体哪一层消失，不以系统自报高分替代事实核查。

最终比对仍发现两条在线门禁漏判。Qdrant 的一般未索引字段拒绝策略不能被缩成 prefix 条件，也不能声称整页没有更一般的限制；上面的 Indexing 原文已直接提供反证。Python 报告额外写出“捕获时需使用 except*”，将示例变成了唯一方式；本地 Python 3.14.5 用普通 `except ExceptionGroup` 成功捕获了 TaskGroup 产生的异常组。后者是独立的程序反例验证，未作为研究 Agent 的新增资料。两条问题均保留为需要修订，最终验收没有把在线通过写成语义全部通过。

# 研究质量 v6 多主题验收记录

本轮围绕 PostgreSQL 17 增量备份、Qdrant 过滤索引和 Python 3.14 TaskGroup 取消语义进行真实研究。验收同时检查研究行为、在线门禁、报告正文和文件发布。题目、15 项必答事实及 8 条错误对照在 `tests/fixtures/research_quality/topics.json` 中预先固定，参考答案未注入研究 Agent。

最终三个主题均完成研究、通过在线门禁并发布文件，私有覆盖账本和公开状态一致、无剩余缺口。但独立内容复核仍发现 Qdrant 的政策范围遗漏及 Python 的额外过强断言，**完整语义质量验收尚未全部通过**。流程跑通和内容完全准确分别记录。

实现依据与设计见 [资料研究质量策略 v6](research-quality-v6.md)，最初缺陷证据见 [降级原因诊断](e2e-quality-diagnosis-20261015.md)。OpenAI/Anthropic 官方资料是工程实践依据；本次实际付费调用使用部署中已有的 GLM 路由，没有声称使用了 OpenAI 或 Claude 模型进行实验。

## 实际验证范围

- Docker 专用项目 `insightforge-platform-e2e-20261015`，真实 IAM 登录、API、Gateway、Controller、LiteLLM、SQL 恢复账本与无网络的 Publisher Worker。
- 全部数据库连接远程 `172.22.208.30:5432`，未启动本地数据库容器。凭据仅保存在未跟踪配置。
- `quality-gate-v6`，冻结契约版本 19，现有模型路由 `if-e2e-research--58fb58777c91`。质量严格度沿用 `balanced`：运行维度下限 3、均分下限 3.0、报告聚合下限 0.75、关键维度下限 0.70。没有降低阈值；研究与报告 fail-open 均关闭。
- 每题费用上限 1,000,000 微美元；用户月上限 5,000,000 微美元，最多并发两项研究。末轮按这个并发限制执行，原题与每题预算保持不变。
- 独立复核直接对照另外下载的官方原文，包含正常回答、范围/版本限制和错误说法。错误对照用于人工核验及相关代码反例；未将它们冒充为 8 次独立的付费 Judge 实验。

## 各候选实际发现

每轮使用独立输出目录，保留失败、降级、协议错误和原始在线评分。以下是修复过程中的样本结果，不是跨模型的大型稳定性基准。

| 候选 | PostgreSQL | Qdrant | Python | 完成时模型费用 |
| --- | --- | --- | --- | --- |
| 1：证据保真与修订闭环 | 事实已补齐，但 WAL 配置引用 current 版本 | 事实齐，但只有文末引用被误计为正文引用 | 合法 ev_ 引用与需求 ID 协议问题导致失败 | $0.206844 |
| 2：引用与完成边界 | 在线 pass，独立复核仍缺 WAL 摘要启用/default off | 未可靠召回重建方法，失败 | 五项事实、引用、完成与发布均通过 | $0.264384 |
| 3：语义选段 | 仍有同一必要前提缺口 | 重建方法已召回，但条件被拆成必须证明的假断言，报告被带偏 | 一个需求范围未委派，随后审阅解决记录协议失败 | $0.275302 |
| 4：条件、分工与四项操作核验 | 门禁把“摘要必须存在”误当“启用/default 已核实” | 条件错误消失，需重建及方法已正确；摄取前建索引的建议未明确落入正文 | 五个任务覆盖全部需求，完整通过 | $0.257848 |
| 5：独立启用控制项与部分结果状态 | 多次评分输出达到 2048 Token，协议失败，运行 partial | 交接及审阅协议失败 | 核心事实完整 | $0.237768 |
| 6：评估输出容量统一 8192 | 源文不支持控制项被归为协议错误，补证细节丢失，仍 partial | 正文 aside 丢失导致重要条件遗漏 | 完整完成并发布 | $0.212415 |
| 7：正文提示框与具体补证反馈 | 已找到配置页，但定义参数名和释义分离，门禁仍 partial；正文关键事实已较完整 | 五项事实均进入正文，完整完成 | 完整完成 | $0.224087 |
| 8：参数定义上下文 | 真正完成缺口→定向补读→具名证据准入→完整报告 | 运行通过，但默认行为被过度概括 | 核心事实完整，运行通过 | $0.244086 |
| 9：默认陈述范围与最终补验 | 完整完成并发布 | 首次上游限流失败；单独补验完成，但仍有政策范围漏判 | 完整完成，但额外 except* 必须性断言错误 | $0.278771 |

基线 PostgreSQL 研究另计 $0.076646，模型连接测试 $0.000004。28 个优化研究加 1 个基线，完成时运行回执累计 **$2.278155**，包含全部失败和补验。结束后再次读取的逐运行结算与月度投影均为 **$2.278121**，费用预留和费用未知操作均为 0；34 微美元的时间点差异及逐运行变化保留在 `final-per-run-accounting.json`，原始回执未改写。搜索服务和基础设施费用不包括在内。

候选 4 的 Qdrant 首次发布请求遇到 409，原脚本保存了异常日志但没有保存响应体。后续幂等发布、下载成功，研究未重跑；因此该次总耗时标为未知，未用恢复收集的短时长冒充完整研究耗时。验收脚本现保存发布错误，并仅对明确的租约占用或完成状态转换进行有界重试。

## 最终运行与独立判断

最终配置版本为 `c11a7f73-6122-4451-b0ee-a021d8991f2f`，API、LiteLLM、Sandbox 应用状态均已确认；四个运行组件的全部 Python 源码与工作区逐一核对一致。Publisher 与 Controller 的网络模式为 none。

| 主题 | 运行与发布 | 模型调用 / 完成时费用 | 耗时 | 独立内容结论 |
| --- | --- | --- | --- | --- |
| PostgreSQL 17 | complete、review pass、未降级、发布成功 | 62 / $0.097843 | 728.96 秒 | 五项必答事实及四篇固定版本文档链接通过核查 |
| Qdrant | complete、review pass、未降级、发布成功 | 57 / $0.085200 | 596.43 秒 | 职责、顺序和重建方法正确；一般 strict-mode 规则被错误收窄，需要修订 |
| Python 3.14 | complete、review pass、未降级、发布成功 | 52 / $0.074706 | 468.80 秒 | 五项必答正确；额外“捕获需使用 except*”断言过强，需要修订 |

对应运行 ID：PostgreSQL `05ebac95a6724761b01ab83175bfefb1`；Qdrant `2ff184e1ed0e469da02d46a4939683f8`；Python `540eb12639784bca893f87dc81ed34a5`。三个运行的 SQL 完成状态均为 complete，事实覆盖账本均 supported，公开 gaps/uncovered 均为空；源 Markdown 摘要与发布任务一致，下载文件摘要与产物记录一致，费用预留均为 0。

Qdrant 本批首次运行 `da05bfb5090246c79729247367a8fa37` 因 ZAI HTTP 429/1302（账户并发限制）在研究阶段失败，18 次模型调用共 $0.021022；被拒绝的那次物理调用为 0 Token/0 费用。该运行及日志完整保留。待另外两题结束后使用相同实现、配置、题目、来源、阈值和每题预算单独补验，取得上表结果。上游并发约束仍是实际部署限制，单独补验成功不等于该可用性问题已解决。

PostgreSQL 的行为证据尤其明确：运行先拒绝“只证明摘要存在、未证明如何启用”的交接，再定向读取 `runtime-config-wal.html`，把 `summarize_wal` 与 default off 的完整定义作为证据，通过复核后进入完整报告。此路径没有放宽评分阈值，也没有把研究答案注入任务。

Qdrant 的剩余问题是：所选 Indexing 页明确说明一般的未索引字段阻断策略及 Cloud 默认设置，报告却把“唯一接近的限制”收窄为 prefix 条件。相关一般规则没有进入本次所用摘录，Reviewer 把摘录的范围当成整个页面的范围。独立比较因此仍判需要修订。

Python 的剩余问题是：官方示例使用 `except*`，不意味着它是唯一捕获方式。独立运行的 Python 3.14.5 反例通过普通 `except ExceptionGroup` 成功捕获 TaskGroup 的 ValueError 异常组，证据保存在 `independent-research/exceptiongroup-counterexample.json`。在线 Reviewer 未识别这个过度概括。

独立答案与原文对照见 [同题独立研究](independent-research-comparison-20261016.md)。这些残余问题表明：已修复的证据保真、覆盖和协议机制不能代替模型对完整语义与条件范围的判断；当前不能把在线 pass 当成报告完全准确的证明。

输出目录：

- `.runs/quality-optimization-20261015/live-v6-acceptance-postgres/`
- `.runs/quality-optimization-20261015/live-v6-acceptance-fixed/`
- `.runs/quality-optimization-20261015/live-v6-acceptance-retry/`

## 离线检查与复现

完整相关回归 **360 项通过，1 项排除**：排除项是仍依赖已删除 QueryEngine 的旧测试。最后的默认陈述范围调整另执行 86 项写作/审阅回归，全部通过；六个核心模块的定向 mypy 和静态迁移检查通过。另一个旧 `test_research_quality_regressions.py` 因仍导入已移除的 `langchain_core` 无法收集，不计入通过数。本轮是后端相关回归与真实 API 全流程验证，静态检查不代替完整部署验收。

测试覆盖跨语言摘录、段落/表格/代码保真、长文后部语义选段、相同下载的不同问题补读、条件问句、未覆盖范围协调、程序性前提、真实缺口不被高分覆盖、引用边界、修订问题闭环、部分结果状态与 v5 恢复行为。

可在已初始化且配置生效的专用部署，分别在两个终端执行下列命令。三个主题合计模型费用上限为 3 美元，仍受账号月度硬额度限制；配置或实现改变时使用新的输出目录。

```powershell
.venv/Scripts/python.exe tests/as_runtime/run_quality_e2e.py --credentials-file .runs/platform-e2e-20261015/account.json --output .runs/quality-optimization-20261015/new-postgres-trial --cases postgres-incremental-backup --total-budget-micro-usd 1000000
```

```powershell
.venv/Scripts/python.exe tests/as_runtime/run_quality_e2e.py --credentials-file .runs/platform-e2e-20261015/account.json --output .runs/quality-optimization-20261015/new-fixed-trials --cases qdrant-filterable-index python-taskgroup-cancellation --total-budget-micro-usd 2000000
```

运行数据保留在 `.runs/quality-optimization-20261015/`，不纳入版本控制。任务终态、预算预留和发布工件摘要已核对。本次 8 个专用容器已全部停止，宿主端口 4109、8089、20249 无监听；远程数据库、共享卷、镜像和实验数据保留。原有 xxl-job-admin、rustfs、nacos、seata-server 均保持运行。清理证据为 `cleanup.json`、`cleanup-ports.json`。

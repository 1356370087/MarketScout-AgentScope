# MarketScout 与 Codex 同题研究对照：Kubernetes 原生 Sidecar

本次使用全新主题“将传统日志 Sidecar 迁入 Kubernetes 原生 Sidecar，并用于 Deployment 和 Job”。研究前固定相同问题、`kubernetes.io` 官方来源范围、六类事实检查和四个错误对照。Codex 独立报告先保存并记录 SHA-256，再启动项目真实研究，未读取项目答案来改写自己的答案。项目验收参考事实没有注入 Agent 提示词。

**项目最终失败，未生成可发布的报告；Codex 独立报告完成。** 项目运行约 25 分钟，119 次模型调用，最终结算 214,375 微美元（$0.214375），费用预留与平台未知计费操作均为 0。网页端实际显示“研究失败，未生成报告”，没有把中间送审稿标为已完成报告。

**本题定位到三处具体问题：完整公式在评审输入筛选中丢失；报告清理误改 YAML 字段；Reviewer 将 XML 转义误当成 Markdown 错误。** 前者造成重复补证与不完整覆盖，后两者造成报告修订。最后的时间限制又被转换成笼统的 GatewayCallError，使已有草稿无法交付。此外，独立复核发现一个未被 Reviewer 抓住的 Recreate 条件遗漏。这些问题不能靠放松质量标准统一解决。

## 部署与试验边界

使用仓库 Dockerfile 实际执行 `docker compose build`，构建了 API、前端、LiteLLM Proxy、Sandbox Controller、Gateway、Sandbox Worker 和 Publisher Worker 共 7 个镜像。构建允许复用基础层缓存；没有使用旧镜像改标签或仅复制源码的替代构建。前端实际执行 `pnpm build`。第三方 Redis/Nginx 沿用官方镜像。

Compose 项目为 `marketscout-compare-20261016`；应用镜像与容器均采用 `marketscout` 前缀，Worker 镜像为 `marketscout-sandbox-worker:local`。Docker 镜像名称按格式要求使用小写。仓库默认 Compose 名、Gateway 容器名及相关 Worker 镜像示例也已改为 MarketScout。

全部数据库连接指定的远程 `172.22.208.30:5432`。复用原有独立验收数据库、加密配置及历史工件卷，未启动本地 PostgreSQL，未清空历史数据。旧数据库中的沙箱逻辑 `deployment_id` 属于不可变恢复契约，因此保留；它与新容器、镜像名称不同。产品界面原有的 InsightForge 文案没有在本次 Docker 命名修改中一并替换。

构建后的配置版本 `fcb9e213-c5a9-4dae-9167-6ee20ca93d13` 已生效，API/LiteLLM/Sandbox 均报告 applied。核对了四个 Python 运行容器的全部源码哈希与工作区一致；Controller 和 Publisher 保持 `network_mode: none`。第一次启动遇到旧项目网段冲突，已为本次项目使用独立控制网段，没有删除旧网络。

项目使用真实 IAM、AgentScope 运行链路、SQL 恢复账本和 GLM-5.3-Flash 模型；研究及报告评审使用原有 `quality-gate-v6`、balanced 严格度，fail-open 关闭。本次一条研究，模型费用上限 1 美元，不进行追求通过的反复重跑。研究 ID：`0fac6012d3504254ba6ec50c9a873e44`。

两者使用不同模型、工具及计算方式。本对照用于诊断工程链路和内容，不是同模型、同预算的受控性能排名。双方都没有执行 Kubernetes 集群测试，题目也明确不要求这一点。

## 独立检索与证据差异

Codex 先进行了两次 Bing 定向查询，两次都返回相同的非官方页面集合，全部未用作证据；随后执行两次 Tavily 官方域名定向查询，并通过官方页面关联链接核对资料。共独立读取 7 篇官方页面，保存 HTML、正文、查询、获取时间与 SHA-256。[Codex 独立报告](marketscout-sidecar-codex-20261016.md)在项目启动前已经冻结。

官方资料明确区分：Sidecar 的 startupProbe 成功可以形成启动屏障，readinessProbe 影响 Pod Ready；原生 Sidecar 不阻塞 Job 完成；正常终止中主应用先退出、Sidecar 逆序退出，但宽限期耗尽时仍可强杀；常驻 Sidecar 必须计入资源需求，不能把所有普通 init 和应用简单相加。

特别核对了 [2025 年官方启动顺序专题](https://kubernetes.io/blog/2025/06/03/start-sidecar-first/)及 [Sidecar 概念文档](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)。2023 年 Alpha 博客只用于版本背景，不能把其当年的限制套用为 v1.33 稳定版行为。

项目本题主要直接使用 `fetch_url` 读取已知官方 URL，先后形成 6 个子任务，其中资源计算有一次补证。直接访问已知官方页面本身合理；本题的失败点不能归咎于未使用搜索引擎。中间账本已有完整公式，后续应优先从已有证据恢复，而不是继续获取相同网页。

## 公式丢失的可重放证据

第一次资源任务 `0275bfa29f0e5ccebfe00da7790decc3` 的原始交接包含 96 条证据，其中三条完整保留 `higher of` 公式：

| 证据 ID | 原始来源与内容 |
| --- | --- |
| `ev_91a00277127cc3a380f7` | Init Containers：Pod 有效资源取应用容器之和与有效 init 资源之间的较大值 |
| `ev_d845ce3459f6db1436c5` | Sidecar Containers：包括 app + sidecar、effective init 与 Pod overhead 的完整列表 |
| `ev_6f71cd90b006490e113b` | 同一 Sidecar 公式的另一个片段定位 |

用保存的交接、覆盖契约和冻结质量配置，调用现行评审输入构造逻辑并在模型调用前截获 payload，**没有调用模型，也没有更改运行中的研究**。离线只关闭无关的沙箱装配验证；质量配置保持冻结值。结果如下：

| 步骤 | 实际结果 |
| --- | --- |
| 原始交接 | 96 条证据；完整公式 3 条 |
| 评审总输入上限 | 30,000 字符 |
| 最终送评输入 | 51 条证据；完整公式 0 条 |
| 已引用证据 | 8 条，全部保留 |
| 裁剪标记 | `truncated=true` |
| 评审反馈 | “51 条 included evidence 中没有完整公式”，归类为 factual gap，建议重读资源章节 |

关键逻辑位于 `quality/gate.py:_bounded_evidence_records`、`_bounded_quality_payload` 和 `evaluate_subagent_handoff`。投影优先保留交接引用、匹配文本，再依据来源与 confidence 排序。完整公式段落的 confidence 为 0.7，而较短事实通常为 0.95；交接摘要又未引用公式 ID，公式因而落到后面并被排除。

交接摘要自身已经声称“当前证据中没有直接出现完整公式”。但保存的完整交接证明该说法错误。`agentscope_runtime/efficiency.py:handoff_prompt` 按来源轮转后取能装入预算的前缀，没有按当前缺口优先保留决定性证据。当前证据可以确定摘要与原始材料不一致；没有另行声称已恢复并检验过摘要模型的完整实际输入。

相同证据预算下的离线选段检查表明：优先保留一条现有公式 ID 即可装入公式，无须增加网页数量。这只是输入容量验证，没有执行修改后的 Judge，也不代表修复已经完成或重验通过。

```mermaid
flowchart LR
    A[官方页面有完整公式] --> B[抓取与提取成功\n原始交接含 3 条公式]
    B --> C[摘要声称缺失\n未引用公式 ID]
    C --> D[评审输入从 96 条裁到 51 条\n公式全部被排除]
    D --> E[门禁归类为事实缺口]
    E --> F[重读相同页面\n再次拒绝资源交接]
```

## 如何判断门禁意见

需要分三层判断，不能简单说门禁全对或全错。

1. **对可见输入的判断：** 资源公式确实不在本次送给 Judge 的 51 条证据中。拒绝把缺乏可见证据的公式当成已证实事实，有合理依据。
2. **对缺口原因和补救的判断：** 把它当成官网/检索层的事实缺口并建议重抓，是错误归因。完整交接里已经有公式，而且裁剪标志明确为真。应先扩大查看同一任务的已有证据，修复摘要和输入选择。
3. **对推理要求的判断：** 首次评审认为“顺序执行、不同时存活”不足以解释为什么不能简单相加，要求原文直接说出因果；第二次评审使用其中相同的生命周期证据接受了这个推导。首次要求过强，也暴露了同一问题的评判不稳定。合理做法是区分有依据的推导与无依据的补全。

此外，第二次资源交接中“不能简单相加”的单项已被 Judge 标为 supported，但另一单项未通过，整份交接被拒绝后，这两项都未形成有效的运行覆盖。`research_agents.py` 在交接被拒时清空该交接证据并跳过覆盖合并，这会放大局部问题。改动这里时应保留引用安全和任务归属约束，不能直接把所有被拒绝的材料当作已接纳证据。

## 报告审阅：一项真实缺陷和一项重复误报

报告阶段保存了送审 Markdown 及 SHA-256。检查原始模型响应、实际草稿和评审意见后，发现两个不同问题，不能都归咎于模型写错。

| 轮次 | 实际 Markdown | Reviewer 判断 | 独立核验 |
| --- | --- | --- | --- |
| 初稿 | `metadata:` 变成 `metablocked:`；重定向仍是 `>` | 两者都要求修复 | 字段损坏属实；`&gt;` 指责不属实 |
| 第一次修订 | `metadata:` 和 `>` 均正确 | 仍称 `&gt;` 未修复，revise | YAML 可解析，Job/Sidecar restartPolicy 正确，没有该字符序列 |
| 第二次修订 | `metadata:` 和 `>` 均正确 | 模型改判 pass；运行时因覆盖缺口与问题闭环校验改判 fail | 重定向误报消失，但解释错误地猜测先前存在双重转义；资源内容仍然缺失 |

三个草稿文件名分别以 `1b6e4da4`、`467f7c11`、`c736700e` 开头，全部哈希与内容匹配。后二份都成功解析为 Job，Pod 策略为 Never、Sidecar 策略为 Always，命令为 `echo "logging" > /opt/logs.txt`。这只验证结构与关键字段，不等同于 Kubernetes 实际执行。

**真实字段损坏来自渲染清理。** 原始 writer 响应中的字段是正确的 `metadata:`。`security/content.py` 的危险协议正则匹配任意位置的 `data:`，因此 `sanitize_report_markdown('metadata:')` 可确定性重放出 `metablocked:`。正常报告和修订路径已有代码块保护，但 `report/evidence_synthesis.py:_render_draft` 直接对整段调用清理，受限证据报告仍会触发。初稿的代码还以普通 claim 文本承载，没有形成规范的 YAML fence。

**重定向误报来自表示层混淆。** 三份真实草稿均无 `&gt;`，而 `quality/context.py:research_context_xml` 将 `>` 序列化为 XML 的 `&gt;`。离线编码后再解析，原始 Markdown 可无损恢复。前两次 Reviewer 把转义形式当作实际代码错误，修订模型则返回正确 `>`。第三次模型终于判通过，却称此前存在 `&amp;gt;` 双重转义，原始草稿并不支持这个解释。这是评审对表示层不稳定的理解，不能要求 writer 不断改写本来正确的正文。正文安全边界需要保留；应明确区分传输编码和真实报告文本，并对“具体字符/代码错误”的评审意见用实际草稿验证。

## 内容对照与遗漏

项目没有正式最终报告，下表比较的是最后一份完整送审稿 `c736700e…`，明确不将它当成成功交付。Codex 一栏是基于独立原文和预先固定检查项的复核，不是第三方盲评。

| 检查项 | 项目完整送审稿 | Codex 独立报告 |
| --- | --- | --- |
| 声明、版本与前提 | 正确区分容器级/Pod 级 restartPolicy，列明主要版本；未细查节点与 Webhook 兼容 | 包含 API Server/节点版本、旧版本边界和字段被 Webhook 丢弃的检查 |
| 启动、就绪与探针 | 说清 started 不等于 Ready，但没有写出“配置 startupProbe 时须等成功”的核心条件；Reviewer 仍判 covered | 明确区分 startupProbe 启动屏障、readinessProbe 的 Pod Ready 作用及持续可用限制 |
| Job 完成 | 正确说明原生 Sidecar 不阻止 Job 完成 | 相同结论，并限制在原生 Sidecar 场景 |
| 终止、宽限期与日志 | 正确说明主容器先退出、Sidecar 逆序退出、宽限期可导致强杀，不承诺日志零丢失 | 相同核心结论，并明确缓冲/确认/重试为工程建议 |
| CPU/内存有效资源 | 被标记为证据缺口，没有提供所需计算规则 | 给出初始化峰值/运行合计/overhead 的计算思路，区分官方规则与按生命周期展开的推导 |
| YAML 与迁移清单 | 修订后的 YAML 关键字段正确；清单缺少 startupProbe 失败/等待和资源验证 | 有可解析的 Job 示例，明确 tail 示例的局限，并包含探针、崩溃、完成、强制终止、资源核验 |

**另有一个真实的语义错误被漏判。** 项目送审稿声称“Deployment 升级时旧修订 Pod 会被立即终止，只有升级场景保证先终止后创建”，随后把它与滚动更新风险关联。实际引用的 `ev_0b66b0bf2f1374cff839` 来自官方 [Recreate Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#recreate-deployment) 段落；它遗漏了前文 `.spec.strategy.type==Recreate` 的条件。默认 `RollingUpdate` 是另一种策略，不能套用先全部停止再创建的规则。

为核验此项，Codex 在冻结独立报告之后额外读取了 Deployment 官方全文，保存为 `codex/posthoc-deployment.*`；没有据此改写先前答案。可见门禁同时存在“输入裁剪造成错误缺口”“表示层造成误报”和“章节条件丢失造成漏报”，不是单一的严格度问题。

## 最终执行与交付失败

研究完成 6 个子任务，其中两个资源任务交接被拒；研究阶段以 `complete_partial / iteration_limit` 结束，资源要求 COV-13/14 未形成有效覆盖。随后生成受限证据报告并进行两次修订。第三轮模型评分虽为 pass，但仍将资源要求标为 partial，且问题闭环记录不符合运行时校验，确定性结果为 `coverage_requirement_partial`、`review_issue_resolution_missing_or_invalid`。拒绝将部分覆盖当作完整报告，这一约束本身合理；上游输入丢失才是首先需要修复的地方。

系统接着尝试生成证据恢复报告。第四份送审稿已落盘，尚未获得评审结果时运行超过 1,500 秒。脱敏日志确认调用预留抛出 `DeadlineExceeded: run deadline exceeded`，随后出现 `gateway operation was not reserved`，对外最终变成 `GatewayCallError("sandbox_gateway_operation_not_completed")`。因此正式输出为空，未进入 Markdown 发布/下载验收。不能声称离线 Publisher 的发布链路在本题完成了端到端验证；本次只确认其新容器健康且保持无网络模式。

最终统计：5 个官方页面、7 次物理抓取、27 次 fetch_url 工具调用、1 次 ReadResearchEvidence、5 次 ResearchComplete；共 33 次工具调用。研究缓存命中 20 次、评审缓存命中 4 次。没有发生 429 或模型传输重试。22/76 个片段被检查；这个数字是选段覆盖，不代表 5 个页面都全文读完。

初次终态用量尚含 1 次估算，记录 $0.231428；等待异步对账后，119 次调用全部获得供应商用量，最终为 **$0.214375**，与平台月度增量一致。初始快照保留，`final-usage.json` 是收敛值。模型费用不包含 Tavily 搜索费用及 Codex 会话费用，不能用这个数字做两者全成本比较。平台计费未知操作与预留均为 0；恢复账本仍有一条未开始物理调用的宿主评审操作处于 started，不能将它误称为未结算的付费调用。

## 后续优化的优先顺序

首先修复两类确定性内容传递问题：为代码内容保留结构并统一安全清理路径，防止 `metadata:` 被当成 URL；让 Reviewer 明确处理 XML 转义，并把具体代码指责与实际 Markdown 校验。使用本次三份草稿重放，确认真实字段错误会被发现，已修好的 `>` 不会继续被误报。

再改证据传递与诊断，不降低原有门禁阈值：按当前 requirement/缺口给摘要和 Judge 保留决定性完整证据；对当前要求使用无关材料去重与降权；当 Judge 报缺口且输入发生裁剪时，先检查被省略的已有证据，再决定是否联网补证。复验应固定本题、原始来源与阈值，验证两次资源交接的公式能进入输入，以及不存在重复补抓和虚假的 factual gap。

其次校准 Judge：允许明确标注、由多条官方事实推出的有限工程结论；当证据能支持计算规则时，不额外要求官网逐字提供同一句解释。再检查局部通过事实如何安全保留到运行覆盖账本。

同时保留摘录所属章节和条件：例如 Recreate 的段落不能丢掉策略限定词再交给写作。最后，将 deadline 作为正常运行控制结果传过 Gateway，提前保留报告恢复所需时间；在时限内给用户可审计的部分产物或明确失败原因，不能在超时后继续发起模型审阅并丢失已生成的草稿。

本次保留现行研究实现的原始结果，未修改评分阈值或反复重跑。Docker 命名修改已落盘，问题修复建议尚未实施，不能将离线诊断等同于修复验收。

## 验证和工件

已经执行仓库 Dockerfile 构建（含前端生产构建）、远程数据库与迁移核验、组件应用检查、容器源码哈希核验、真实 IAM/API 研究 E2E、浏览器登录/前后台切换/返回原研究/事件流重连/终态刷新，以及两类不调用模型的故障重放。未执行全量后端或前端测试，因为本次没有修改研究代码；不复用上轮测试数字宣称本轮通过。

本次 Playwright 会话已关闭，8 个 MarketScout 服务容器已停止；`xxl-job-admin`、`rustfs`、`nacos`、`seata-server` 保持原状。新建镜像、远程数据库和工件卷保留，供复验使用。

主要证据位于 `.runs/marketscout-comparison-20261016/`，包含本地私密配置，整个目录不纳入 Git。仅使用下列不含凭据的工件做审阅：

- `dataset.json`、`protocol.json`：冻结题目、来源边界和验收项。
- `codex/report-frozen.md`、`codex/report-freeze.json`：先于项目运行保存的独立报告。
- `build.log`、`build-manifest.json`、`verified-deployment.json`：实际重建与容器源码核验。
- `project/kubernetes-native-sidecar-migration/`：原始结果、活动、SQL 快照、完整/被拒交接、所有送审稿。
- `projection-diagnosis.json`、`replayed-resource-judge-input.json`、`report-representation-diagnosis.json`：位于上述项目运行目录内的离线重放证据。
- `failure-api-redacted.log`、`final-usage.json`、`cleanup.json`：最终失败原因、费用与清理。
- `output/playwright/marketscout-compare-20261016/`：真实页面快照与截图。

# MarketScout 结构化输出与工具回执修复验证

本次针对 [Sidecar 同题复验](research-quality-sidecar-fixes-20261016.md) 暴露的两类运行故障。两类协议/回执修复及验证已完成；完整研究内容验收仍未通过，原因和证据见下文。原始失败样本保留在 `.runs/quality-fixes-20261016/`，本次工件单独保存到 `.runs/protocol-receipt-fixes-20261016/`。

## 根因与处理

### 结构化输出

原运行 `79114c7f44394a038a140d003212c175` 的一次质量评审返回了重复 1,010 次的 `evidence_coverage` 字段，且 JSON 未闭合。之后两笔响应没有内容、工具调用或结束原因，却被标记为 completed。

在实际安装的 AgentScope 2.0.8 中，`ChatModelBase.__call__` 会捕获取消并返回 `FinishedReason.INTERRUPTED` 的空响应。项目的 `NativeGatewayProvider` 原先没有检查这个状态。真实 SDK 加 HTTP 夹具已复现：取消或超时会被适配层误记成正常完成。历史空回执没有保存框架中断状态，因此不能仅凭旧回执逐笔证明它们的中断来源。

修复后，适配层重新抛出取消信号；调用后的未知结果仍隔离并保留保守费用，不会以零用量成功响应进入格式修复。结构化 JSON 在 Gateway 与调用侧均拒绝重复属性，不再使用最后一个同名评分覆盖前面的值。格式重试提示要求每个字段仅出现一次、完整闭合 JSON、保持字段值简短；只有已完成但格式不合法的响应进入原有的有限修复预算。每次物理尝试保留独立操作 ID 和记账，缺失分数不自动填入。

第一轮在线复验继续观察到合法 JSON 中重复评分字段、缺失必填评分字段；连续使用强制工具格式的修复仍不稳定。因此最终实现会在首次格式失败后切换为同模型的 JSON response mode，保持原 schema、最大输出和物理尝试总量。该请求不提供可执行工具，JSON 正文仍经过重复键、完整性、JSON Schema 和 Pydantic 校验。若提供商明确返回不支持的 400，仅在原有剩余尝试内退回工具格式；不支持且预算耗尽时仍返回原格式失败。

此处理依据 [OpenAI 官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)：JSON mode 不保证 schema adherence，应用必须继续校验并处理不完整输出。文档不能证明第三方模型具有 OpenAI strict schema 能力，所以没有对现用模型虚构这种保证，也没有更换模型。

### 工具未知回执

原运行 `d0da3aab7328488497412569dff81e55` 的 `fetch_url` 已完成网页抓取。它在证据提取模型的预算预留阶段遇到 PostgreSQL `ConnectionDoesNotExistError`，API 返回 500；当时尚未调用模型。

原处理继续发送队列中的旧签名请求，触发 401，又尝试转换不存在的模型回执，触发第二个 500。Gateway 返回 failed，但调用侧把普通模型的所有未完成结果统一转为 uncertain。该误分类传播至提取缓存与工具层，最终成为 `UnknownOperation`，整次研究停止。

修复内容：

- 在真正进入模型提供商前后区分派发状态；调用前失败返回 `model_not_dispatched`。
- 清除已失败的待发请求，避免重用签名 nonce。通过新的只读请求核对是否已有预留，只对存在的预留写终态；明确未派发时按零调用、零费用结算。
- 调用侧保留 failed 与 uncertain 的区别。明确失败可以进入既有的提取失败处理；真实未知、失租与取消继续向恢复层传播。
- 新增经过任务身份与 fence 校验的工具回执查询。工具调用遇到网络错误或服务端 5xx 后，在 60 秒内查询原操作；不会重发工具执行 POST。查到成功或确定失败回执时复用，查不到时继续隔离。
- 查询沿用历史工具预留的操作 ID、任务、阶段与工具名契约，兼容已保存的回执。未完成操作的查询不改变其状态，也不触发新的预算预留。

本次未修改数据库迁移、质量阈值、模型路由、前端页面或来源准入规则。

## 自动化验证

最终 SDK、结构化协议、SQL Gateway 与工具传输针对性回归共 92 项通过，包含调用前/后取消、JSON 格式修复、提供商拒绝 JSON mode、尝试预算耗尽和回执只读恢复。

加入 JSON 修复后的扩展回归结果为 429 passed、2 failed、1 skipped、7 deselected；随后新增的两项调用前取消测试另行通过。模型错误分类、凭据隔离、出网与审批边界另有 49 项通过，合计覆盖 480 个不同的通过用例。两项失败分别为 `test_fetch_url_rejects_out_of_boundary_url` 和 `test_native_web_tools_pair_and_enabled_gates`：现行代码使用结构化来源拒绝错误并增加了 `source_discovery`，测试仍期待旧异常和两个工具。对 HEAD `356b0c4` 的独立源码快照执行这两项，得到相同失败；没有为本次修复修改这些范围外预期。一个历史账单夹具测试因未提供数据跳过；7 个已删除旧引擎的测试明确排除。

远程 PostgreSQL 的预算接管与结算恢复测试使用 `172.22.208.30:5432` 上的独立 `platform_test_e2e_20261015` 库。未启动本地数据库容器。静态迁移与冻结契约检查通过。

对 7 个相关模块运行 mypy，得到 19 个既有诊断；对 HEAD 源码执行相同检查，诊断内容一致，没有新增项。此结果不代表全量类型检查通过。

## 真实研究复验

API、Gateway、Controller、Sandbox Worker 与 Publisher 的 5 个 MarketScout 镜像已重新构建。每次运行前均将四个 Python 服务源码哈希与对应工作区逐文件匹配；首候选平台版本为 `5e3330b8-3734-455c-972e-3b371871a480`，最终版本为 `6890e094-3819-430b-a632-95a15d9ef87a`。Publisher 与 Controller 仍为无网络模式。

复验使用原 Kubernetes 原生 Sidecar 迁移题、仅 kubernetes.io 官方来源、原 `zai/glm-5.3-flash` 路由、quality-gate-v6/balanced、fail-open=false，期限 1,500 秒，单轮费用上限 1 美元。通过实际 IAM、研究 HTTP 状态轮询、审批和发布接口进行验证；本次未重新执行浏览器 UI 或 SSE 断线重连测试。运行 ID：`92588e10a10d49009ebd4d38974e60b7`。

第一轮运行约 1,262 秒，99 次物理模型调用，20 次工具调用，所有操作均 committed；没有空 completed 模型回执、unknown_failed 或剩余预留。报告成功发布并下载，文本一致；模型报告审阅为 pass，但研究标记 partial/iteration_limit。独立复核发现终止宽限期仍缺失、资源计算未完整覆盖 Pod overhead 和先启动 sidecar 与后续 init 的并发阶段，因此没有将此结果计为完整内容通过。最终读取的费用为 141,981 微美元。

第二轮使用最终 JSON 修复代码，平台版本 `6890e094-3819-430b-a632-95a15d9ef87a`，研究运行 `b55ee807455b4e70bfffa6339a97a3a4`。原题、模型、门禁、期限和费用上限全部保持不变，工件单独保存在 `.runs/protocol-json-fixes-20261016/`。最终结果如下。


| 验证项 | 最终结果 |
| --- | --- |
| 运行时间 | 约 1,146 秒 |
| 物理模型调用 | 54 次，全部 provider_reported，无估算或缺失 |
| 工具调用 | 13 条宿主回执、12 条 Gateway 回执，全部 committed；差额为宿主控制工具 |
| 协议结果 | 7 条 JSON 正文修复响应；包括重复评分字段后切换 JSON 并成功完成交接的实例；最终无 invalid-judge 回执 |
| 恢复账本 | 全部 169 条操作 committed，无 started/quarantined 遗留，无空 completed 模型回执 |
| 费用 | 128,235 微美元，剩余预留为 0 |
| 完整研究结果 | failed，最终异常为 report/orchestrator.py 的 `insufficient_evidence`，未交付或发布报告 |

本轮没有重现 `UnknownOperation`，没有通过零用量空响应掩盖中断。故障注入测试验证了调用前数据库故障、取消和响应丢失的处理；真实运行验证了第三方模型实际触发 JSON 修复后能够继续研究与审阅。两轮是单次样本，不据此推断统计意义上的性能或整体质量提升。

## 独立内容复核与剩余问题

最终失败的 5 次报告审阅均为合法结构化响应。独立检查原始模型输出和证据后，以下问题仍需另行处理：

1. **来源数量不足。** 已接受资料只有 Sidecar Containers 与 Pod Lifecycle 两个官方页面，原题要求至少三个不同页面。审阅对此的反馈正确，没有依据放行。
2. **恢复写作遗漏代码。** 首份结构化草稿包含完整官方 Job YAML；恢复草稿的 `code_language=yaml` 项只有“以下为官方文档给出的……”这一句。缺失已经存在于原始模型输出中，发生在渲染之前，审阅的代码缺失反馈正确。
3. **需求重复与覆盖聚合。** 本轮 COV-03 同时包含原题第 1–4 项，COV-04～COV-07 又分别表示其子项。交接将合并需求记为 partial 后，已经 supported 的 COV-04/COV-05 也未进入最终覆盖账本，形成与正文证据矛盾的“未覆盖”提示。该问题属于需求拆分/准入聚合，不是 JSON 格式或工具回执丢失。

没有修改质量阈值、放开 fail-open、凭空补分、伪造第三来源或将未知调用重发，以获取通过结果。本次只修复用户指定的模型结构化协议和工具回执问题；不能宣称完整研究报告 E2E 已通过。

## 费用与清理

本次两轮实际结算 141,981 + 128,235 = **270,216 微美元（$0.270216）**，均已取得完整提供商用量，且无本轮剩余预留。历史取消运行 `0ff8f6c92f8d472d9a64f53780be3b8f` 仍有 14,648 微美元预留，未擅自释放或重试。

8 个专用 MarketScout 服务容器已停止；4109、8089、20249 端口已释放。用户原有 `xxl-job-admin`、`rustfs`、`nacos`、`seata-server` 保持运行；数据库、镜像、共享卷及原始失败工件保留。官方文档浏览器会话已关闭。没有 Git 提交。

最终证据位于 `.runs/protocol-json-fixes-20261016/`：`verified-deployment.json`、`protocol-proof.json`、`final-costs.json`、`final-api-redacted.log`、`cleanup.json`、`cleanup-ports.json`，以及 `project/kubernetes-native-sidecar-migration/` 下的完整操作账本、评审、初稿、恢复稿和独立内容检查。回归日志与修改前基线对照位于 `.runs/protocol-receipt-fixes-20261016/`。这些目录含私密配置，不应整体提交或分享。

# 报告恢复评审预算修复（2026-09-20）

## 根因与固定输入复现

上轮 Web 运行 `09ac26e79e6750ae8c8cca9b2e53855c` 已完成三个研究交接。使用其 SQL 快照和六份已提交报告回执重放初稿、引用修复、评审、修订及证据受限草稿，在 `lead.report_review:2` 稳定复现 `ReportInputBudgetExceeded: report_fixed_context_exceeds_budget`。

冻结模型目录中的 `if-report-review-v1` 窗口为 **131072**；`ModelFactory.build_sandbox()` 创建候选实例时没有传入窗口，`SandboxChatModel` 因而使用 **32768** 默认值。初次预算通过，候选预算却缩到 32K。固定部分预算为 **28620**，误用窗口时可用输入为 `32768 - 3072 - 1638 = 28058`，尚未选入证据就已拒绝。该固定部分包含 **19984 字符**的完整恢复草稿；可复用输入还有 **88 条**完整证据。

复现产物从原始回执派生，仅提交报告领域字段到 `tests/fixtures/report-recovery-review-input.json`，不含身份、会话、Run Key 或原始工具回执。原运行状态和账本没有修改。

## 最小修复

`ModelFactory.build_sandbox()` 按最终解析的角色模型名读取冻结目录，并把 `context_window` 传给沙箱模型实例；没有目录条目的兼容调用仍沿用原先 32768 默认值。修复覆盖评审和修订使用的同一工厂入口。

保留完整草稿、整条证据、候选模型预算检查、输出和安全余量预留，以及控制错误的 fail-closed 行为。没有扩大模型目录窗口，没有启用 fail-open，没有截短草稿或绕过评审/发布门禁。

## 回归验证

- 修复前，新增长输入评审及小窗口测试均失败：前者复现相同调用栈，后者证明实例也未采用较小的冻结窗口。
- 修复后，真实输入通过原生模型工厂、候选策略与 Sandbox V2 序列化进入评审/修订；检查完整草稿、88 条证据、修订意见尾部，遗漏数为零。
- 24000 窗口仍在模型调用前拒绝上述输入；断言没有物理网络请求。
- 最终联合回归 **89 passed / 4 warnings（41.13 秒）**，覆盖报告上下文、七类报告、发布恢复、四来源 HTTP 下载、模型调用与生产资源生命周期。见 [JUnit](../evidence/report-budget-regression-20260920.xml)。四项警告为已有 Pydantic 弃用提示；本地环境未安装 Ruff，不登记 Ruff 通过。

复跑命令（PowerShell，仓库根目录）：

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m pytest tests/as_runtime/test_report_sandbox_budget.py tests/as_runtime/test_report_context_budget.py tests/as_runtime/test_report_native.py tests/as_runtime/test_source_report_delivery.py tests/as_runtime/test_model_execution.py tests/as_runtime/test_production_resources.py -q --tb=short -p no:cacheprovider
```

## 真实评审、修订与发布

独立验收运行 `a04ca3c0db7a4c91ac97931a3695974e` 复用原来的三个研究交接及上述完整恢复草稿。仅研究阶段和初稿输入替换为已保存产物；后续正式 API、PostgreSQL、原生报告编排、Gateway、真实模型和 PublisherWorker 均执行实际代码。配置 `report_review_fail_open=false`，修订上限为 3，不通过放宽门禁规避错误。

- **运行 completed，252.942 秒**。4 次评审、3 次修订全部提交回执，没有再次触发预算异常；最终没有评审硬失败或确定性协议失败。
- 7 次真实模型调用，175617 输入 token、12689 输出 token、SQL 计费 **0.032683 美元**；终态预留全部归零。评审及写作均使用冻结目录中的 `zai/glm-5.3-flash` 别名。
- 两次正式发布请求返回 202；仅认领本次运行的两个任务，经正式 PublisherWorker 处理后状态均为 completed，Markdown 和 JSON 下载均返回 **200**。
- Markdown 8515 字节，下载 SHA-256 为 `8113b1bbd999eea428eeeb1ba9e5bca3abb982b3ec9a47a8094f87bb048a6b28`，与最终报告一致；JSON 39280 字节，保留 CanonicalReport 及来源结构。
- 本次 API、Gateway、Controller、验收数据库四个服务均已停止；定向发布进程已退出，既有服务保留。

按用户确认的台账口径，**内容质量评价不影响本预算缺陷的关闭，只有质量门禁底层执行错误才计入运行阻塞**。本次质量门禁正常执行，最终因内容意见保留 `degraded / revise`，CanonicalReport 为 partial；这些原始语义结果仅作为证据保存，不登记为本次修复未完成。机器可读结果、原始评审及源码指纹见 [真实验收证据](../evidence/report-budget-live-20260920.json)。

## 验收边界

本次报告恢复窗口预算缺陷登记为 **已修复并验证**。上轮固定版本联合验收的历史记录保持原样。本次没有重新发起研究/搜索，没有执行浏览器、Office/PDF 视觉或跨主机故障验收，不把它们登记为通过；T064～T068 的其他技术验收范围仍单独跟踪，任务总完成数为 **66/81**，不因内容质量扣验。

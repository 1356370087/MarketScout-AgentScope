# M9 与原生上下文方案适配（2026-09-20）

以本轮未提交的原生压缩/卸载实现为基线，按用户指定的 AgentScope 2.0.8 方案推进 M9。状态权威为 `../07-progress.md`；本轮未连接真实模型，未更改 `.env`，未启动服务容器，未执行生产切换或旧引擎删除。

## 本轮变更

| 能力/任务 | 本轮结果 | 剩余边界 |
|---|---|---|
| T064 写作及大纲预算 | 原生报告优先使用冻结模型目录窗口；候选模型调用前按实际窗口与输出额度再次选择整条证据，覆盖回退与输出续写预算变化 | 真实长输入、回退模型完整部署链路待验收 |
| T065 引用与完整性 | 重复预算保留累计遗漏条数，完整证据不逐字段裁剪、不修改原始注册表；共享引用与 CanonicalReport 回归通过 | 摘要后真实模型回读、引用闭环待验收 |
| T066 评审及修订 | 原生输入保留完整草稿、校验后的问题及准入证据，通过统一报告证据预算选择整条记录；不再走旧字段/JSON 字符裁剪路径 | 超大固定草稿仍明确失败，尚未实现分节评审；不以部分草稿冒充完整评审 |
| T066 控制错误 | 预算、失租、未知操作、恢复冲突、审批等待、超时及已登记恢复问题传播；完整报告输入无法容纳时不能被 fail_open 当成普通评审不可用 | 普通模型不可用的既有显式 fail_open 行为保留 |
| T067/T068 发布及恢复 | 七类报告、发布格式、模型提交重放与发布 Worker 崩溃回归在修改后重新执行 | 本轮无新版视觉检查、真实 PostgreSQL/容器组合恢复证据，不升级完成 |

单次报告/Judge 的证据预算属于领域逻辑，继续保留；本轮没有为其重写会话压缩算法。原生预算使用保守 UTF-8 字节上界估算而非字符数冒充精确 token，可能低估可用容量；模型窗口来自冻结目录，候选调用前还检查实际模型窗口。记录无法完整容纳则整条省略，固定协议或全部证据无法容纳则明确失败。来源与需求的权威记录不受影响。

旧引擎尚被其他调用者使用，其旧报告分支和只读存储能力本轮保留。`RunContextStore` 的报告/发布共享存储仍待拆分，研究阶段 `NativeContextCompactor` 调用者也尚未全部退出；不能据此声明业务源码零 LangChain 或清退完成。

## 验证与失败记录

- Python 原生环境，AgentScope **2.0.8**；确定性模型、临时 SQLite/文件，未使用真实业务运行 ID。
- [最终原生联合](../evidence/m9-native-report-final-verified-20260920.xml)：**82 passed / 4 warnings，29.80 秒**。覆盖新增 9 项报告预算/完整性/控制错误专项、七类报告、现有实际格式生成、恢复/强杀、原生上下文与模型策略。
- [共享领域回归](../evidence/m9-domain-regression-20260920.xml)：**105 passed / 90 warnings**。保留引用、CanonicalReport、评审修订、装配、七类报告与默认兼容业务断言。该批使用原生解释器，追加现有 `.venv-legacy/Lib/site-packages` 以运行旧桥接测试，未安装或改动依赖。
- [首次扩大批次](../evidence/m9-native-report-final-20260920.xml)：118 passed / 2 failed。一项新夹具的 issue description 超过既有领域验证长度，导致尾部在模型构造时被限制；修正为合法领域输入后，仍验证提示词不二次裁剪。另一项既有配置盘点写死 244 项，当前 FIELD_MAP 与 Configuration 均为 246，新增 `async_research_mode`、`team_execution_mode` 未录入旧 CSV；本轮记录该配置盘点债务，未删除或跳过断言冒充全绿。
- 前置原生 39 项及中间 82 项通过与最终批次重叠，不累计。
- 修改文件 Ruff F 检查、`git diff --check` 通过；不是全仓库 Ruff/类型检查通过声明。

复现原生批次：

```powershell
$env:PYTHONPATH='src;.'
.venv/Scripts/python.exe -m pytest tests/as_runtime/test_report_context_budget.py tests/as_runtime/test_report_native.py tests/as_runtime/test_native_context.py tests/as_runtime/test_model_execution.py -q --disable-warnings --basetemp .runs/m9-native-report-20260920d --junitxml docs/agentscope-migration/evidence/m9-native-report-final-verified-20260920.xml
```

共享批次为 `test_report_writing.py`、`test_report_reviewer_integration.py`、`test_report_canonical.py`、`test_report_references.py`、`test_report_assembly.py`、`test_report_genres.py`、`test_report_default_parity.py`，通过 `pytest.main` 执行；独立临时目录 `.runs/m9-domain-20260920a`。

未启动前后端、Worker 或 Docker 服务，无本轮服务清理项。未提交或推送。M9 五项继续按原验收与本轮增量影响跟踪，总完成数保持 66/81。

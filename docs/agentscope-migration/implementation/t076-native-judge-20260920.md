# T076 原生 Judge 与配对评估接入

本轮在 AgentScope 2.0.8 原生本地研究入口上继续接入评分和配对实验。原研究检查点保持只读；评分使用独立的 `manifest.json`、`judge.db`、`budget.json`。不切换生产、不修改真实 `.env`，也不删除旧引擎。

## 业务链路

`tests/run_local_evaluate.py` 的研究后评分与只读重评分，现在通过 `evaluation/session.py` → `ResearchModels.structured` → `ModelFactory` → 原生 LiteLLM 模型执行。同步评估器通过受作用域限制的适配器使用这一通道，系统评分规则和用户数据消息分开传递；不再走旧 ModelGateway 的评分默认路径。

复用原十类指标：总体质量、相关性、结构、正确性、证据完整性、事实支撑、完整性、引用准确性、工具效率、执行合规。没有参考答案、轨迹或适用约束时保留未评分，不改为零分，不改指标定义或通过阈值。多个细分分数保留原键。

Judge 使用既有受限 `LITELLM_SERVICE_KEY`，通过 `/model/info` 冻结 `EVALUATION_MODEL` 的窗口、输出上限和价格，温度固定 0、固定单模型以避免配对中改变裁判。独立评估预算上限为 60 次物理调用、1000000 微美元（1 美元）、1800 秒，已有运行预算更严格时采用较小值；SDK 重试为零，应用模型策略及物理尝试账本负责治理。凭据只在内存绑定，不写入评估清单。

每个问题/侧/重复/指标有稳定任务范围，模型回执与最终指标分别持久化。相同输入恢复复用已提交结果；改变输入或评分实现时拒绝混用。复用现有 RecoveryStages 的租约续期与控制错误传播，预算不足、失租、未知回执或取消不能被评分器当成普通错误后继续消耗预算。退出关闭模型客户端、租约、数据库；已知评分不可用仍按原规则输出 evaluator_error。

## 命令行与冻结集

```powershell
$env:PYTHONPATH='src;.'
.venv/Scripts/python.exe tests/run_local_evaluate.py --pair-dataset tests/fixtures/t076-paired-research.json --pair-repeats 2 --output-dir .runs/paired-evaluation
```

配对清单字段为 `schema_version: 1`、`cases`；每项包括 `id`、`question`、`kind`、`baseline`、`candidate`、两侧文件的 `baseline_sha256`/`candidate_sha256` 和可选 `reference_outputs`。路径相对于清单目录。知识类还要求 `corpus_refs` 为非空列表，每条固定 `artifact_version`、`generation`、`unit`，不能使用 TBD。

提交的新固定清单选取仓库两份真实历史成功报告，问题逐字相同，哈希锁定。这是历史产物重评分样例，不是新原生研究与旧引擎的生成质量对比，也不是完整知识评估集。

每侧至少重复两次，重复间交替先后顺序；冻结评分日期、Judge、原评分代码和提示词哈希。输出 `dataset.json`、`samples.json`、`comparison.json`、`comparison.md`。统计要求 case/repeat/指标集合及三类指纹严格配对，拒绝重复、缺失、非有限分数和条件不一致。报告总对数、双方均可评分对数、状态组合、均值、样本标准差、差值范围；两次重复不代表统计显著性。

使用同一输出目录再次执行会读取评分账本。仅重生成报告不会重复调用已提交模型；输入、重复次数或原评分代码改变时应使用新的实验目录，不能覆盖旧实验条件。单份历史档案 `--rescore-json` 继续生成派生文件，评分的原生 run_id 与哈希记录在 `evaluation_provenance`。

## 确定性验证

- 首批新夹具遗漏 AgentScope ChatUsage 的 time 字段；补齐后发现空列表不产生执行合规类别，已改为显式未评分。首次失败 XML 保留，没有删除指标断言。
- 原生评分、配对、生命周期、物理计账联合 **22 passed / 4 warnings**；新增回执提交中断与重放专项 **5 passed / 4 warnings**，批次重叠。
- 原有本地评估与 Judge 协议兼容 **69 passed / 90 warnings**，保留原业务断言。该兼容批次追加已有旧环境依赖目录以运行旧消息夹具，不代表新入口需要 LangChain。
- 证据：`../evidence/t076-native-judge-complete-20260920.xml`、`t076-native-judge-crash-20260920.xml`、`t076-native-judge-compat-20260920.xml`。

原生联合命令：

```powershell
$env:PYTHONPATH='src;.'
.venv/Scripts/python.exe -m pytest tests/as_runtime/test_native_evaluation.py tests/as_runtime/test_local_evaluation.py tests/as_runtime/test_model_accounting.py -q --disable-warnings --basetemp .runs/t076-judge-20260920d --junitxml docs/agentscope-migration/evidence/t076-native-judge-complete-20260920.xml
```

## 真实验证环境及范围

沿用已经运行的隔离 LiteLLM 服务，未启停原部署。初次目录探针因测试进程继承 SOCKS 代理但缺少 socksio 而失败；仅清除本次测试子进程的代理变量后，既有受限 Key 读取目录成功，`if-evaluation-v1` 存在。未修改 `.env`、未重登记 Key、未提升权限。

真实实验目录为 `.runs/t076-real-paired`；逐次调用在独立 SQLite 账本计账。最终真实结果与实测费用见下方完成记录。即便此历史配对通过，T076 的知识资料绑定、完整确定性质量契约、原生研究与基线的生成配对、可选导出仍按原门禁继续跟踪，不升级整个任务完成。

## 中断恢复与费用接线补验

首轮真实实验 `7b543ae26bed41a8963f59d2d92f1e9d` 在会话中断前提交 2 个完整样本、16 次物理调用；已提交费用为 48192 微美元。恢复发现未知调用，返回 `UnknownOperation`，没有自动重试。账本保留 32 个 committed 操作、1 个 quarantined、1 个 started；操作含逻辑调用与物理尝试，不能把操作数当作模型调用数。未知尝试的费用尚不能确定，48192 微美元不是该实验最终总费用。见 `../evidence/t076-real-paired-interrupted-20260920.json`。

本次核对也发现通用 LiteLLM 预算策略把费用限额交给 Run Key，而离线 Judge 使用 Service Key。已在独立评分建单处显式设置 SQL 费用限额，仍复用原生物理调用预留与结算；旧评分账本缺少此限制时拒绝继续，要求新实验目录。未改写旧实验、未清除未知操作或费用预留。

新增 SQL 费用拒绝测试验证调用开始前预算生效、拒绝后没有预留泄漏；最新联合结果 **24 passed / 4 warnings**，见 `../evidence/t076-native-judge-closeout-20260920.xml`。独立进程导入 `evaluation.session/experiment` 未加载 LangChain 或 QueryEngine，Ruff F 检查及配对 CLI `--help` 通过。新实验目录为 `.runs/t076-real-paired-bounded`，实测 SQL 限额为 60 次物理调用及 1000000 微美元。

## 真实配对完成记录

- 环境：Python 3.14.5、AgentScope 2.0.8，既有隔离 LiteLLM `if-evaluation-v1`；既有真实配置仅读取，未修改。
- 运行 ID：`787524e7c44741c499d63b5bf9a55fc2`；执行 `evaluate_pairs("tests/fixtures/t076-paired-research.json", ".runs/t076-real-paired-bounded", repeats=2)`，与上述 CLI 调用同一入口。子进程加载本地配置，清除继承的代理变量后执行。
- 结果：4 个完整评分样本（基线/候选各 2 次），覆盖十类指标、19 个细分键；15 个键双方均可评分，4 个键保留未评分，无 evaluator_error。32 次物理模型调用、513770 输入 token、37390 输出 token、95776 微美元（0.095776 美元）。逻辑/物理操作共 64 条 committed，无 started/quarantined，预留全部为零。
- 同目录恢复重放完成，前后完整预算快照一致，新增调用和费用均为零；评分进程退出、模型客户端与数据库关闭。辅助证据提取第一次遇到 Windows 默认 GBK 解码错误，显式 UTF-8 后完成，不影响已提交模型回执。
- [完整统计与预算](../evidence/t076-real-paired-20260920.json)、[可读配对表](../evidence/t076-real-paired-20260920.md)。源报告文件 SHA256 在每次执行入口复核，未改写源产物。

部分结果（归一化 0～1，差值为候选减基线）：相关性均值 1.0 → 0.9、完整性 0.9 → 0.8，两者差值样本标准差均为 0.1414；写作质量 0.9 → 1.0，差值标准差 0.1414；来源权威性 0.9181 → 0.5362，差值标准差 0.1080。引用准确性双方均为 0，不能称为质量通过。正确性缺少参考答案、执行合规没有适用约束，保持未评分；两次重复不足以推断统计显著性。

本轮完成的是原生 Judge 接入及真实历史产物配对评分闭环。T076 保持进行中：知识集版本绑定、完整质量契约门禁、新原生研究生成结果与基线配对、可选导出仍需后续验收。没有实际生产切换，也没有启动需关闭的前后端或容器；原有部署保持原状。

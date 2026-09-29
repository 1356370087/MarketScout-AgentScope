# 本地 Agent Evals

统一入口为 `python -m open_deep_research.evaluation`。研究使用 NativeRuns，Judge 使用独立 SQL 账本。代码、模型和人工评分分别记录，最终任务判定与运行终态、报告质量分开保存。旧的 `tests/run_local_evaluate.py` 仍可使用。

从源码目录运行且尚未安装本项目时，先设置模块路径：PowerShell 使用 `$env:PYTHONPATH = 'src;.'`，Linux/macOS 使用 `export PYTHONPATH=src:.`。下列命令中的 `uv run python` 也可替换为已有项目虚拟环境的 Python。

## 免费验证

```powershell
uv run python -m open_deep_research.evaluation validate tests/fixtures/agent_evals/core.json --output .runs/evals/controls
uv run python -m open_deep_research.evaluation check .runs/evals/controls
```

内置 24 个任务，每题提供正、负参考产物。`validate` 只验证评分器的判定，不调用 Agent、模型或搜索。报告明确标记为 reference，这些结果不会进入 Agent 成功率统计。修改任务集后运行 `python tests/fixtures/agent_evals/build_dataset.py` 可重建冻结 JSON。

## 固定环境与真实联网

```powershell
uv run python -m open_deep_research.evaluation --env-file .env run tests/fixtures/agent_evals/core.json --mode fixed --output .runs/evals/baseline --budget-usd 20
uv run python -m open_deep_research.evaluation --env-file .env run tests/fixtures/agent_evals/core.json --mode fixed --case revenue --case tool-arguments --trials 3 --output .runs/evals/reliability --budget-usd 5
```

`fixed` 使用真实模型、原生 supervisor/researcher 和治理链路；工具提供方返回版本化的冻结资料，独立 SQLite 接收端和隔离文件目录记录实际效果。不会访问夹具 URL，也不会在夹具未匹配时回退到联网服务。此模式验证受控环境中的 Agent 行为；真实沙箱和网络设施的防护仍需集成验证。

研究沿用合法 IAM 身份或已有开发旁路，不自行开启认证旁路。研究模型通过现有 LiteLLM 管理接口获得限模型、限费用的 Run Key，退出即封禁；Judge 继续使用已有 Service Key。需要本地配置中的 `LITELLM_BASE_URL`、`LITELLM_MASTER_KEY`、Run Key 加密配置及 `LITELLM_SERVICE_KEY`，沿用项目现有配置说明。密钥不进入实验清单或评分产物。

`live` 调用既有原生 Gateway 运行环境，需要已部署的 PostgreSQL、Gateway、Controller 及相应身份配置。联网数据集使用真实 URL；合成资料集的 `.test` 地址只适用于 fixed 模式。基础设施不可用时记录无法完成判定，不把缺少环境记为 Agent 质量失败。

独立 CLI 的 Gateway 回调必须到达同一评估进程。设置 `EVALUATION_CALLBACK_HOST` 和 `EVALUATION_CALLBACK_PORT` 启动内部签名回调监听器，并使 Gateway 的 `SANDBOX_API_INTERNAL_URL` 指向它。监听器随本次运行关闭。若只验证联网研究而不验证 Docker Worker 隔离，可显式设置 `EVALUATION_LOCAL_WORKERS=true`，使用可信进程内 collaborator；仍需要 PostgreSQL、远程 RocketMQ 和 Gateway，保留生产的模型、工具与权限治理。

```powershell
uv run python -m open_deep_research.evaluation --env-file .env run tests/fixtures/agent_evals/live.json --mode live --output .runs/evals/live --budget-usd 5
```

每个实验默认串行执行、每题一次研究、每个产物一次 Judge 评分。`--trials` 创建独立研究与环境；`--judge-repeats` 只重复评分，不能代替重复研究。固定任务可以声明澄清、计划修订、取消或精确工具审批脚本；真实联网运行不按这些脚本自动审批。

## 费用与恢复

`--budget-usd` 是整个实验的研究与 Judge 共享模型费用上限，单位为美元，默认 20。每次研究最多分配 1 美元，每次 Judge 最多分配 0.25 美元，均受剩余额度和已有更严格配置限制。调用开始前预留，结束按 SQL 账本结算；未知结果保留预留，不能记为免费。研究 Key 也带同样的单次费用上限。

实验同时冻结评分日期；更换日期必须生成新的评分产物。`rescore --dataset` 可以更新参考答案或评分规则，但不能改变原始问题、工具、交互脚本或执行环境。

冻结环境没有收费搜索调用。真实联网的第三方搜索费用若不进入现有模型账本，需要通过服务侧配额另行约束；报告中的模型费用不冒充全部第三方账单。

同目录再次 `run` 会复用条件一致的已保存试验，不重新执行研究。更改任务集、模型、目录价格、配置、实现或重复次数须换新目录。`RUNNING` 文件阻止同时写入同一实验；异常退出遗留该文件时，先核对进程、SQL 未决操作和费用预留，再清除本实验锁，不能自动抹掉未知操作。

研究产物会在 Judge 前保存。评分中断可对已有产物执行 `rescore`，输出新目录；这是一项新的评分操作，使用新的独立预算。旧产物及原生检查点保持只读。

```powershell
uv run python -m open_deep_research.evaluation --env-file .env rescore .runs/evals/baseline --output .runs/evals/rejudged --judge-repeats 2 --budget-usd 5
uv run python -m open_deep_research.evaluation compare .runs/evals/baseline .runs/evals/candidate --output .runs/evals/comparison.json
```

比较要求任务和重复编号对应、数据集/评分规则/环境/Judge 指纹一致。Agent 或提示词变化可作为被比较变量。统计提供各任务的成功率、`pass@k`、`pass^k`、配对指标差值、成本与延迟差值；成功率差异使用以任务为单位的 bootstrap 95% 区间。样本不足、未知结果、评分覆盖不足会显式显示，少于 20 个完整任务对不宣称统计上的改进。

## 人工审阅

```powershell
uv run python -m open_deep_research.evaluation review-export .runs/evals/baseline --output .runs/evals/review --limit 20 --seed 0
uv run python -m open_deep_research.evaluation review-import .runs/evals/review .runs/evals/annotations.jsonl --output .runs/evals/calibrated
```

审阅包随机化样本顺序，隐藏实验身份、模型分数和评分理由；包含问题、报告、证据、工具事实及独立结果。`private-index.json` 用于关联与完整性校验，评分时不阅读该文件。

复制 `annotations-template.jsonl` 填写真实的评分者、判定、分数和理由。默认标准包括决策帮助、可操作性、不确定性表达和报告质量；每项显示通过阈值。单人可完成评分，有多位评分者时保留所有独立记录；分歧不会被悄悄平均为通过，人工裁决使用 `adjudication: true`。

导入前核对源产物 SHA256。导入产生完整的派生实验，保留未审阅的试验和原有模型分数；校准报告给出有效样本量、人与模型一致率、绝对分差及多人一致率。未录入真实人工标签时，校准保持待完成。

## 契约与门禁

- `EvalCase`：输入、成功条件、参考答案、工具夹具、交互脚本和评分器。
- `EvalTrial`：一次独立执行；保存运行 ID、实际输出、观察状态、轨迹、费用与评分。
- `GraderResult`：评分器、对象、分数、证据引用和 `pass/fail/not_applicable/unknown/error` 判定。
- 原生快照 v2 从 SQL 操作回执和私有评估事件生成；支持完成、失败、取消、等待。v1 历史快照可读，但缺少观察事实不会补造分数。

代码评分器仅从注册表选择，任务文件不能嵌入任意 Python。支持数值容差、引用、工具参数、必要顺序、来源范围、独立数据库/文件结果和安全断言。整体质量使用 balanced 阈值；安全和必需断言不能被平均分抵消。已确认的违规优先于缺失证据，不适用项退出评分分母。

实验输出包括 `manifest.json`、`dataset.json`、`trials/`、`spend.json`、`summary.json`、`report.md`、`junit.xml`。`check` 适合流水线调用：退出码 0 为通过，1 为存在确定失败，2 为必需证据不足、基础设施失败或未完成试验。所有生成产物放入已忽略的 `.runs/`。

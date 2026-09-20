# T076 本地评估原生入口实施

2026-09-20 按用户最新要求优先完成业务入口适配，真实模型配对评估后置。T076 已领取，状态为进行中，整体完成数保持 66/81。

## 已接入

- `tests/run_local_evaluate.py` 不再导入或实例化 QueryEngine。新建研究直接调用 `evaluation/local_runtime.py` → `build_native_research_service` → `NativeRuns` → AgentScope 原生研究管线，SQL 是状态权威。
- Web 评估默认选择 Gateway 资源提供器，保留显式 `AS_NATIVE_RESOURCES` 设置；缺少基础设施时记录失败，不退回旧执行器或放宽沙箱约束。生产默认入口及真实 `.env` 均未修改。
- 沿用现有 IAM Access Token 或合法开发旁路，检查研究创建权限；后续身份重验继续由生产工厂执行。研究原文、证据、覆盖、报告及 evaluation_snapshot 从原生状态投影，失败终态优先于残留报告。
- 持久结果记录原生 run_id 与 engine，能取得冻结值的配置采用实际冻结值；不写入认证 Token。超时保留 run_id 并记录错误，只取消本次新建运行；调用者取消继续传播；等待审批不自动批准。
- finally 关闭服务持有的执行器、模型、数据库和运行时。保留 JSON/Markdown、质量门禁、只读重评分和结果文件复用协议；旧检查点没有被转换或恢复。
- 评估器与 Judge 的旧依赖改为兼容路径惰性加载，独立进程导入本地入口不加载 LangChain 或旧 QueryEngine。现有离线 Judge 评分仍保留原调用路径；预备的 `evaluation/native.py` 十类指标适配和 `paired.py` 配对统计尚未接入本地评分入口，不宣称评分链路已全部原生化。

## 验证

- [原生生命周期](../evidence/t076-local-native-20260920.xml)：**6 passed / 4 warnings**，真实 SQLite + NativeRuns + ResearchPipeline，可控阶段；覆盖完成、失败、等待审批、超时、调用者取消、资源关闭，以及独立进程无 LangChain/QueryEngine 导入。未调用真实模型。
- [兼容业务回归](../evidence/t076-local-compat-final-20260920.xml)：**69 passed / 90 warnings**，结果状态、报告不可撤回、只读派生评分、原 Judge 协议等断言保留。测试追加已有旧环境依赖目录以运行旧消息兼容夹具，不代表入口依赖旧引擎。
- [首次兼容批次](../evidence/t076-local-compat-20260920.xml) 在收集时因旧测试从 tools.utils 间接引入 MCP Client 失败；改为直接引用既有连接配置兼容函数，未修改断言或 MCP 实现，最终通过。
- CLI `--help` 成功；变更文件 Ruff F 检查通过。未执行真实模型配对、知识固定资料版本绑定、十类指标全链路或 LangSmith/benchmark 新版验收，AS-A076 尚未关闭。

## 本地运行

使用已配置的隔离测试依赖，不会自动拉起 Docker：

```powershell
$env:PYTHONPATH='src;.'
.venv/Scripts/python.exe tests/run_local_evaluate.py --question "你的研究问题" --output-dir .runs/local-evaluation
```

运行读取本地 `.env`，不修改它。新增 `EVALUATION_RESEARCH_TIMEOUT_SECONDS` 默认为 1800 秒；`EVALUATION_ACCESS_TOKEN` 仅非开发旁路时需要，只配置到本地。`AS_NATIVE_RESOURCES=host` 仍受原宿主资源能力限制，不支持此入口默认启用的 Web 管线。

本轮未启动前后端或容器，无服务清理项；未提交推送。下一步继续原生 Judge 生命周期与独立评估计账接线，再进行固定集、十类指标和真实模型配对验收。

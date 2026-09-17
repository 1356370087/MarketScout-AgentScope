# 原生生产链路 E2E

此部署显式使用 `RESEARCH_ENGINE=native`、Gateway、SQL 恢复与正式团队 Worker。Docker socket 只由 Controller 持有；API 经已签名 Unix socket 请求固定的 Worker 命令。

准备条件：

1. 本地 `.env` 配置真实 LiteLLM、搜索、IAM 和共享签名密钥。不要提交凭据。
2. 从 `deploy/native-team.env.example` 创建专用 Worker 环境文件，并设置 `AS_TEAM_WORKER_ENV_FILE` 为绝对路径。Worker 的 PG、MQ、身份和签名配置必须与 API 一致。资料检索需要可用的检索服务配置。
3. 设置 `AS_ROCKETMQ_ENDPOINT` 为远程 gRPC Proxy 地址，不是 NameServer 的 9876 端口。使用独立消费组，不启动本地 RocketMQ。
4. 设置 `E2E_ADMIN_EMAIL`、`E2E_ADMIN_PASSWORD` 为已初始化的测试账号。完整认证数据库需要 IAM 初始化与管理员引导；不要把跳过登录当作完整认证验收。
5. 设置 `E2E_SOURCE_SELECTIONS` JSON，至少包含 documents、hybrid、specific 三项；每项遵循 `SourceSelection`，例如 `{"documents":{"mode":"documents","sources":[{"type":"document","id":"已发布资料ID"}]},"hybrid":{"mode":"hybrid","sources":[{"type":"document","id":"已发布资料ID"}]},"specific":{"mode":"specific","sources":[{"type":"url","url":"https://www.postgresql.org/docs/17/release-17.html"}]}}`。资料必须属于测试用户且已有发布代次。

运行 `python tests/as_runtime/run_native_e2e.py`。脚本要求没有运行中容器的专用 Compose 项目（可复用已初始化的验收数据卷），执行显式 schema 迁移、启动本次需要的服务，并在成功或失败后停止本项目容器。它保留数据卷和报告，不清理其他项目，不自动恢复原先已停止的容器。首次验收可先手动准备同配置环境并初始化账号/资料，再设置 `PLAYWRIGHT_BASE_URL`、`E2E_NATIVE_FULL=true`，执行 `pnpm --dir frontend exec playwright test native-research.spec.ts --project=desktop --workers=1`；手动启动的服务由操作者在验收后停止。

测试使用真实 BFF 请求创建四类研究，浏览器执行审批并检查报告刷新，核验原生 engine、引用、用量、发布和下载。未完成任务在 finally 取消。E2E 需要真实模型费用，默认不自动启用。

运行中续期 Run Key 和 Gateway 注册；模型每次请求取当前任务令牌。暂停结束后封禁本执行段 Key，恢复时按 SQL 已用及预留量申请剩余额度。后台按准确 run/operation 标签核对迟到代理账单；多笔代理重试或缺失账单保留未决，不能冒充上游逐次账单核验已通过。管理员也可执行 `python -m open_deep_research.agentscope_runtime.spend_reconciliation RUN_ID OWNER_ID` 补核对。未知执行结果不会因账单存在而自动重放。

故障验收使用 `tests/as_runtime/test_deployment_fault_matrix.py` 和 `test_team_container.py` 的真实 PG/容器测试，模型仍为确定性夹具。真实供应商未知结果、跨主机网络分区和 API/Gateway 同故障必须另留实验记录；上述测试通过不能替代这些证据。

故障批次可在同一专用项目中设置 `E2E_FAULT_SCENARIO=worker-kill` 或 `api-gateway-kill` 后重跑。脚本通过固定标签定位本次 run 的团队容器或本项目 API/Gateway，使用真实 SIGKILL；API/Gateway 重启后等待旧 SQL 租约自然过期再恢复。未触发窗口直接判为失败。代理已执行但结果未知时仍会隔离并使验收失败，需要可信核验，不会通过盲目重试制造成功。

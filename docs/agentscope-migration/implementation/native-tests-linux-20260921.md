# 业务测试迁移与 Linux 原生 API 镜像（2026-09-21）

本批保留报告、模型解析、来源、资料和公共事件的业务断言，把测试注入点迁到 Native ReportContext、SQL 恢复、原生工具与 HTTP 边界；未重新引入旧执行实现。

## 验证

| 证据 | 结果 |
|---|---|
| [报告业务回归](../evidence/native-report-domain-verified-20260921.xml) | 95 passed / 91 warnings；六文件覆盖组装、默认产品、非默认类型、评审/修订、完整消息预算、受限证据。评审和修订共享 SQL model_calls 上限，超限转原生审批等待，不能继续调用模型 |
| [模型与事件](../evidence/native-domain-contracts-20260921.xml) | 53 passed / 91 warnings；provider/key/base URL 解析、共享熔断状态机和配置、历史事件排序/去重/最后游标回放 |
| [企业资料](../evidence/native-doc-contracts-20260921.xml) | 24 passed / 5 warnings；模型可见工具集的四来源过滤、真实原生发现函数的域名查询范围、URL identity、资料版本与权限等领域断言 |
| [来源及别名](../evidence/native-scope-alignment-20260921.xml) | 11 passed / 5 warnings；排除条款不产生正向需求、Proxy 的普通/上下文回退目标都在原生 Run Key 模型白名单内 |
| [旧专用测试处置](../evidence/retired-runtime-tests-20260921.json) | 17 个旧执行/适配器专用文件退出，保存旧用例名称和原生覆盖文件映射；不是逐项私有 API 兼容声明 |
| [最新收集审计](../evidence/native-test-collection-20260921.json) | 1654 collected / 45 collection errors；剩余质量、工具、发布等旧测试仍需继续迁移，不能视为全量通过 |

报告测试中的字符窗口假设改为原生 UTF-8 预算，继续断言完整固定输入和整条证据；旧字段级裁剪不再作为正确行为。原生窗口不足会拒绝，预算不足会等待授权；本批没有降低产品门槛。

## Linux 构建和实际启动

构建命令为 `docker build -f Dockerfile -t insightforge-native-retirement:20260921 .`。安装仅使用基础原生依赖。第一次实际容器就绪检查失败，诊断定位为 trace_store 的 PermissionError，见 [原始诊断](../evidence/native-linux-readiness-diagnostic-20260921.json)。

Dockerfile 原来只设置 RUNS_DIR，TraceStore 仍默认写 `/app/.runs`，非 root 用户不能创建该目录。现将 TRACE_STORE_PATH 默认指向已有可写目录 `/data/runs/traces.sqlite3`。

修复后 [Linux 冒烟证据](../evidence/native-linux-api-smoke-20260921.json) 记录镜像 ID、Python 3.14.7、health/ready、原生运行列表、capabilities 和 OpenAPI 的 HTTP 200。容器中再次确认没有 LangChain/LangGraph 包，全部专用容器均已移除。其他既有服务没有启动。

该验证使用合法开发旁路、host 资源、关闭外部搜索；没有执行付费模型调用或替代完整认证、Gateway、PostgreSQL 与浏览器 E2E。完整目标继续进行，整体台账保持 67/81。

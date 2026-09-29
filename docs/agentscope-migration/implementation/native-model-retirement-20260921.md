# 模型、质量与记忆的旧框架退出（2026-09-21）

本批实现了业务源码和基础安装的零 LangChain/LangGraph，但测试迁移与完整 E2E 尚未完成，不能据此宣布整个迁移结束。

## 实际变更

- 删除旧模型 codec/gateway/invocation/fallback、旧可观察回调 core、手写 runtime 及沙箱模型/管理器/假提供商，共 11 个模块。清退前确认清单外无源码模块引用；公共 provider、凭据解析、价格、错误、熔断和领域 tracing 保留。
- 记忆策略仅调用原生 structured 模型端口；恢复分类提示、advisory 提示和缺失画像指标，非字典外部 metadata 不再导致整批召回失败。类别仅允许已有 MemoryCategory，避免新标签入口把外部指令带入上下文。
- 独立评分与同步评估接口使用原生 Judge 会话、SQL 预算和回执，不再直接创建旧网关。原生调用边界专项覆盖作用域、消息、独立费用/调用上限、缺失 Service Key、SDK 重试禁用和资源关闭。
- 质量门禁默认无绑定评估器时明确报错，不能按 fail_open 继续；Native Msg 与 HTTP 消息编译出的覆盖契约一致。
- 历史 RunManifest/SessionJournalRecord 与报告工件存储保留；旧 Query replay、消息对象反序列化、查询状态写入及上下文投影退出。新运行恢复仍由 SQL/AgentScope 负责。
- legacy-bridge 和 API/Gateway 镜像的旧依赖安装项删除。uv 锁文件移除 LangChain、LangChain Core/OpenAI/Protocol、LangGraph 及其旧支持包；独立安装发现 SMTP 依赖未声明，补充 aiosmtplib 5.1.3 范围。

## 证据

| 证据 | 结果与边界 |
|---|---|
| [源码与镜像静态检查](../evidence/native-retirement-static-20260921.json) | all 检查通过，业务源码无 LangChain/旧引擎导入，镜像的 Python/extra 声明有效；不代表镜像构建或运行通过 |
| [独立安装](../evidence/native-install-20260921.json) | `.venv-native-verify`，171 个基础包，AgentScope 2.0.8、MCP 1.30.0、aiosmtplib 5.1.3，未安装 LangChain/LangGraph；缺失缓存 wheel 后从锁定地址下载完成 |
| [原生边界回归](../evidence/native-model-boundaries-20260921.xml) | 130 passed / 6 warnings；质量、工具、评分、来源、报告与 HTTP |
| [记忆与 Judge](../evidence/native-memory-judge-verified-20260921.xml) | 86 passed / 5 warnings；原生记忆、维护、现有领域策略及独立 Judge |
| [独立环境联合](../evidence/native-clean-install-final-20260921.xml) | 149 passed / 7 warnings；全部原生/API 模块导入、公共发现、来源边界、质量与记忆。修复了清退中误删的共享 PublicFindingsSummary DTO，保留原生摘要与回执测试 |
| [消息与来源契约](../evidence/native-contract-20260921.xml) | 5 passed / 5 warnings；Native Msg 与 HTTP 契约一致、未绑定评估器拒绝、网络授权不扩大指定 URL 范围 |

各批次有重叠，不累计为独立用例总数。AST 和 Ruff F 检查通过。

## 未完成项

全量测试收集首次得到 1344 项及 78 个收集错误，多数是旧框架专用测试仍引用已删除代码；也发现缺失邮件依赖及共享 DTO，已修正。其余旧测试需要按业务职责迁移或在原生替代覆盖明确后退出，不能通过重新引入旧执行代码维持它们。

完整镜像构建、真实模型/Gateway/PostgreSQL 联合运行及浏览器验收尚待重跑。默认 Compose 配置接线、历史切换处置和最终全要求审计继续跟踪，整体保持 67/81。

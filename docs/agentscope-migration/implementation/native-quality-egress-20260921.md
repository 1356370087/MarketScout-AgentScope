# 质量、评估轨迹与出网迁移补齐（2026-09-21）

本批继续处理旧测试依赖，并把测试暴露的运行时缺口修到原生路径。没有恢复旧引擎、模型网关或 LangChain 包。

## 变更

- 评估快照以前只读旧 tool_calls/ToolMessage，漏掉原生 AgentState 中已有的工具记录。现在读取原生 object/JSON 工具块，投影有界脱敏参数、名称、ID 和状态；报告及本地评估从持久 supervisor/研究交接状态接线。只声明保留上下文的可观察部分，不假装覆盖已卸载历史。
- 原生 Judge 的 groundedness/citation_accuracy 复用 evidence_integrity 的规范结果，保留十类指标与状态，不重复生成两份可能矛盾的证据判断。独立原生会话回放验证一份 EvidenceIntegrityScore 且无额外派生 Judge 调用。
- 质量提示词的固定规则与不可信研究 JSON 改为 system/user 分离；修订协议反馈也保持独立。模型返回的 runtime diagnostics 仍不能覆盖服务端门禁结果。
- 任务校验恢复共享的事实 ID、数量和父维度约束；唯一 ordinal 可修复哈希拼写错误，未知序号仍拒绝。模型 schema 展示允许的事实 ID，团队 TaskCreate 原有必填约束保留。
- ProductionRunFactory 提供从用户消息生成的 500 字符出网意图，不包含 assistant/检索内容。外部 Tavily/Firecrawl 提取前独立查询 external.extract 许可，普通只读许可不足时不构造/调用外部客户端。
- 原生 fetch_url 在允许联网的来源范围内保持可用；完整 web_research 仅在 enforced 模式展示，legacy/shadow 模式沿用提供商搜索工具。测试保留离线模式隐藏联网工具、来源边界及描述预算断言。
- 出网模式 API 测试改用真实 NativeRuns/SQL 状态，验证收窄、基线禁止扩大、失效租约与目标版本冲突。测试夹具补齐非冻结策略路径，使其与实际原生配置投影一致。

## 验证

| 证据 | 结果 |
|---|---|
| [质量与评估联合](../evidence/native-quality-trace-final-20260921.xml) | 224 passed / 91 warnings；覆盖门禁、覆盖契约、协议修复、模型 metadata 隔离、消息角色与原生轨迹 |
| [出网调用接线](../evidence/native-egress-wiring-20260921.xml) | 34 passed / 92 warnings；V2 分类器、用户意图、OAuth 脱敏、并发 Run Key 作用域与资源释放 |
| [工具可用性](../evidence/native-tools-availability-20260921.xml) | 47 passed / 92 warnings；各 Web 模式、离线禁用、Web/搜索及来源契约 |
| [出网/API 安全](../evidence/native-egress-safety-20260921.xml) | 78 passed / 6 warnings；撤销、批准/拒绝、版本与租约、重绑定/重定向、外部提取的独立许可 |
| [测试收集](../evidence/native-test-collection-20260921.json) | 1908 collected / 30 errors；未算全量通过 |
| [静态检查](../evidence/native-quality-egress-static-20260921.json) | all 检查通过；仍需完整运行验收 |

各批次有重叠。Ruff F 与 AST 检查通过。旧查询/浏览器包装/搜索包装三份专用测试的处置已补入 [覆盖映射](../evidence/retired-runtime-tests-20260921.json)。

全量剩余测试、完整镜像与真实模型/Gateway/浏览器 E2E 继续进行。此前 Linux API 冒烟属于上一批版本，不能代替本轮新增运行时变更的部署验收。整体保持 67/81。

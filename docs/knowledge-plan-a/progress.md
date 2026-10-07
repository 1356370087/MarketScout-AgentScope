# 知识库方案 A 实施记录

用户于 2026-10-03 批准 P0-1～P0-6、P1-1～P1-5，现已完成代码实施和工程回归。实施前代码基线：`d1739f6`（72 个文件，一次本地提交）。本轮实现保留在工作区，未另行提交或推送。P2 未纳入范围。

| 工作包 | 已完成行为 |
| --- | --- |
| P0-1 权限与来源 | 团队成员可检索共享发布资料；SQL 内校验实时权限、范围与代次，撤权后拒绝搜索、选择和引用回读 |
| P0-2 检索契约 | limit 生效；语义等级优先排序后应用配额；记录实际三路召回量、生效 profile 和重排状态 |
| P0-3 用量与评估 | scope=service 的原生 ModelFactory；每次 embedding/rerank/answer/repair 独立预占并记账；汇总回写查询；修复 nDCG、回答评估和延迟 |
| P0-4 嵌入身份 | generation 保存模型/维度/revision；不匹配明确拒绝；部分重解析保留身份；新增 0018 迁移回填旧索引 |
| P0-5 引用回读 | 精确 chunk 返回真实代次、版本和位置；历史正文分页，可定位第 206 个片段；草稿仍走独立审核入口 |
| P0-6 同步治理 | 创建/刷新验证资料库与文档归属；逐跳出网授权、实际 DNS/peer 检查和大小限制；保留草稿审核流程 |
| P1-1 共用检索 | REST、问答和受治理的 search_documents 共用管线与证据投影 |
| P1-2 冻结与作用域 | 冻结代次/profile/filters/资产；Run Key 与 Service Key 隔离；原生角色授权；旧 v13/v14 运行不引入新环境模型 |
| P1-3 回读与证据 | 父单元、表头/单位/脚注与邻居回读受预算约束；最多 3 个查询融合；RRF 不再冒充事实置信 |
| P1-4 Facts/Wiki | 范围内已发布资产只读复用；固定事实/原文关系与 Wiki revision；历史时点和撤权/撤回生效 |
| P1-5 前端与公开过程 | 可搜索库/集合、历史时点/有效日期/profile；范围带入研究；SubAgent 展示检索/重排/代次；解析期间继续显示已发布正文 |

实现继续采用既有 PostgreSQL/pgvector、审核版本、原生 AgentScope 与恢复账本。原工作区 IDE、资料与工具缓存未纳入基线提交。

## 验证结果

- 知识库/文档回归 187 passed；独占空队列的 ingestion 验收另行通过。
- 原生全量 878 passed、7 skipped；14 个失败集中于测试库表冲突和 Windows 长路径。短路径/干净库重跑 17 passed、1 个子进程夹具失败，该用例独立诊断后通过。不能把原生全量记作一次全绿。
- 新增 Gateway 权限/回执与方案 A 单测 19 passed；配置、旧运行恢复和生产工厂补充回归 58 passed。
- 前端 TypeScript、生产构建通过；144 passed、1 skipped；新联动 E2E 桌面/平板/手机共 9 passed，原有 UI/审批/用量/冒烟回归 20 passed。
- 本次涉及的 64 个 Python 文件 Ruff 诊断由基线 277 项降至 238 项，没有新增诊断。ESLint 仍有 5 个既有错误、1 个既有警告，位于 health-page、ledger-page、approval-center。
- `git diff --check` 通过。构建生成的 next-env.d.ts 和临时诊断的旧 Worker 测试均已恢复。

Next.js 测试服务（3031）已关闭，专用知识库 PostgreSQL 容器已删除；原生 PostgreSQL/Worker 容器由夹具关闭。用户原有容器未调整。

详细日志、范围与部署步骤见 [acceptance.md](acceptance.md)。原审批方案见 [design.md](design.md)，其现状分析描述实施前代码。

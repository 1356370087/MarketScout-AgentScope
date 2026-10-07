# 知识库方案 A 验收与部署

日期：2026-10-03。基线提交：`d1739f6`。

## 部署步骤

1. 使用既有流程备份资料数据库。将迁移进程的 `IAM_DATABASE_URL` 指向待升级实例，执行 `uv run alembic upgrade head`。如果 `DOCUMENT_DATABASE_URL` 使用独立实例，确认它同样完成项目资料表迁移。
2. 确认版本为 `0018_knowledge_execution`。新增 generation 的 `index_profile`、`knowledge_usage_daily`、`knowledge_model_attempts`；未修改历史迁移和既有向量列维度。
3. API、document-worker、knowledge-worker 和 Gateway 配置一致的 `DOCUMENT_EMBEDDING_MODEL`、`DOCUMENT_EMBEDDING_DIMENSIONS=1536`、`DOCUMENT_EMBEDDING_REVISION`。旧代次按 segment 的唯一模型/维度回填 revision=v1；无法确定身份时保持空 profile，检索明确拒绝，需完整重建并审核发布。
4. 独立知识检索/问答配置 `LITELLM_SERVICE_KEY`，授权嵌入、重排、问答模型和 `/model/info` 能力目录。配置 `KNOWLEDGE_RERANK_MODEL`、`KNOWLEDGE_ANSWER_MODEL`；`KNOWLEDGE_MODEL_CALLS_PER_USER_DAY` 按 UTC 日界统计物理尝试，0 表示不限。
5. 研究资料工具保持 Gateway 执行区，部署 `AS_NATIVE_RESOURCES=gateway` 和既有 PostgreSQL、LiteLLM、Gateway/Controller。宿主模式不会将其改成本地执行，不会借用 Service Key。运行知识模型经原生角色和允许模型集合授权。
6. 默认研究策略已命名允许 `search_documents`、`knowledge_facts`、`knowledge_wiki`。自定义策略按需加入这些只读工具；保留 deny_tools 优先级及其他敏感工具限制。运行中的冻结策略仍有摘要校验，部署时不要替换活跃运行绑定的策略文件。
7. 网页同步遵循管理员出网策略；未允许的目标或重定向返回不可执行状态。继续沿用 ETag/hash、待审草稿与人工发布。
8. 使用 pnpm 构建前端，再按现有部署流程启动服务。以团队成员账号验证“资料库/集合 → 历史时点 → 研究 → SubAgent 来源 → 报告引用”。

更换嵌入别名背后的模型时必须递增 revision、完整重建并审核发布。已有研究绑定原 generation；部署嵌入身份与它不匹配时明确报错，不混用向量。旧 schema 13/14 运行使用原资料快照与固定 RRF 配置，不从当前环境增加知识模型授权。

## 验证范围

真实 SQL 验收使用专用 pgvector/pg17 和合成定价语料，覆盖共享读取/撤权、limit 与语义排序、真实候选数、固定代次与索引身份、pinned 范围防扩大、父单元表头/单位/脚注与预算、部分重解析、并发日额度、answer/repair 独立用量及汇总持久化、Facts/Wiki 原始证据链与历史版本、HTTP 同步的跨库拒绝。

模型测试使用真实 AgentScope SDK、原生策略和 MockTransport，验证服务凭据、目录与 provider usage；Gateway 测试经过真实工具目录、权限及 SQL 回执链，领域资料读取使用确定性替身。测试确认宿主保持 Gateway 边界，三种资料工具均可正确授权、拒绝和重放。原生套件还覆盖文档解析适配、四种来源模式报告审批/下载、SQL 恢复和团队协作；Worker kill/rejoin 使用实际 Docker 容器。

所有日志保存在 `output/knowledge-review-20261003/`：

| 日志 | 结果 |
| --- | --- |
| backend-final.txt | 知识库/文档 187 passed；排除真实 Docling 两组和需独占空队列的 ingestion |
| migration-clean.txt、migration-ingest.txt | 专用测试库迁移至 head；ingestion 在独立空队列库另行通过 |
| native-fixes.txt | 原生接口、冻结、协议样本、服务及报告定向回归 69 passed |
| native-final.txt | 原生全量 878 passed、7 skipped、14 failed；失败为测试库共享表冲突、Windows 长路径 |
| native-isolated.txt | 迁移/团队组以短路径、干净库重跑：17 passed、1 failed；剩余为测试调用 kill 时子进程已退出 |
| native-child-diagnostic.txt | 剩余 artifact_written 用例单独通过；临时诊断调整已恢复 |
| gateway-final-2.txt | 新 Gateway 工具与方案 A 单测 19 passed |
| compat-final.txt | 配置、生产工厂、旧运行兼容回归 58 passed |
| frontend-unit-final.txt | 144 passed、1 skipped |
| frontend-build-final.txt | Next.js 生产构建通过；另已通过 TypeScript 检查 |
| frontend-e2e-final.txt | 新知识联动的桌面/平板/手机 9 passed |
| frontend-lint-final.json | 5 个既有错误、1 个既有警告，无新增错误 |
| ruff-delta-final.json | 本次 64 个 Python 文件：基线 277 项、当前 238 项、无新增诊断 |

原生全量单次执行仍有既有夹具相互污染和 Windows 路径限制。复验没有放宽业务权限、删除冻结校验或启用 fail-open。Windows 复验建议用短 `--basetemp tmp/ka-<本次唯一名称>`；固定名称的原生容器夹具同一时刻只运行一组；迁移隔离测试使用干净实例。本机代理导致 SDK 构造异常的测试需在该测试进程中移除无关代理变量。

## 前端验收

- 库/集合、历史发布截止、有效日期、profile 带入新研究；编辑未提交问题后，继续研究仍沿用已提交问题和范围。
- 第 206 个旧版本片段按精确 segment 读取，按其 generation 分页，展示实际版本/位置，并正确聚焦。
- 新版本解析期间，已发布正文继续可读、可选为研究来源。

截图在 `output/knowledge-plan-a/`，包括 scope 和 citation 的 desktop/tablet/mobile 六张图。已检查 1440/1024/390px；手机范围场景为深色主题，引用测试校验焦点和无横向溢出。

## 未覆盖的生产条件

没有调用真实收费模型或真实 Docling/Office/OCR 服务，没有进行生产语料的人工语义评审或大容量基准。不能据此宣称 Recall/nDCG、P95 或费用改善；默认增强的生产放量仍需以指定语料与模型验证。历史评估集的 TBD 资料未在本轮补齐。

外部连接器、周期竞情、报告自动沉淀、完整框架 RAG 数据迁移、图谱和图像向量检索属于未批准的 P2。

本次 Next.js、专用 PostgreSQL 和 Worker 测试资源已清理。数据库迁移只执行于隔离测试库，未更改用户业务数据库。

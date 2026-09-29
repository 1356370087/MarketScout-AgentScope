# QueryEngine、文件任务池及旧工具包装清退（2026-09-21）

本轮实际删除 112 个源码文件，不是仅隐藏默认入口。此前已经删除的 11 个 `api_host` 模块不计入本轮数量。

## 清退边界

- 删除 `agents/` 的 QueryEngine、ResearcherQueryEngine、手写 query、状态/检查点、上下文编译、模型恢复与工具协议实现。
- 删除旧 `tools/supervisor/`、`tools/team/`、`tools/utils.py`，以及文件任务池、executor、team_bridge、team_pool、team_inbox、async_tools、coordination。原生领域团队服务和共享 TaskSnapshot 等数据契约保留。
- 删除旧 Web/搜索/MCP 包装目录及 LangChain 工具适配器、legacy_shims；原生 Web、提供商搜索、MCP、权限治理及共享 `web/` 算法保留。工具注册入口只负责原生已装配 Tool 的权限、描述和重名投影。
- 报告 assembly、Reviewer、Revisor 和受限证据综合统一调用原生报告端口。删除旧模型构建、重试/预算分支与逐字段字符裁剪，保留完整草稿和整条证据按冻结窗口预算；未绑定原生运行时不能借 fail_open 转成一次“成功评审”。纯渲染使用不依赖 LangChain 的领域追踪。
- 删除 Gateway 的旧 V1 模型执行/流/查询及旧团队 RPC。当前 SandboxChatModel 使用 V2 的原生模型调用、SQL 回执及预算；出网分类器也统一走 V2。
- 修复原生 Researcher 没有附加已配置领域技能上下文的缺口，medical/legal/finance 三类均有实际模型输入检查。

删除前检查了清单外源码调用者；删除后再次检查相对/绝对本地模块导入。架构守卫仍保留已清退模块名作为禁止项，而不是从规则中删除它们。

## 验证

| 证据 | 结果和范围 |
|---|---|
| [最终原生联合](../evidence/query-engine-retirement-final-20260921.xml) | 236 passed / 8 warnings；研究阶段、来源契约、完成判断、领域技能、工具治理、Web、搜索、真实 stdio/HTTP/SSE MCP、生产资源、七类报告及发布/恢复、原生导入守卫 |
| [Gateway 专项](../evidence/native-gateway-retirement-20260921.xml) | 71 passed / 7 warnings；V2 Gateway 预算/回执、Web/MCP/搜索及真实长草稿预算回归，和联合批次有重叠 |
| [报告与评估专项](../evidence/report-evaluation-retirement-20260921.xml) | 51 passed / 5 warnings；完整输入预算、缺失运行时拒绝、原生本地/LangSmith 评分入口及领域评估兼容 |
| [源码模块链接](../evidence/query-engine-module-links-20260921.json) | 静态本地模块导入未指向已删除模块；不覆盖任意动态模块名、符号级反射或全部旧测试 |
| [剩余清退盘点](../evidence/retirement-after-query-engine-20260921.json) | 旧引擎导入为 0，仍有 33 处 LangChain 导入，未达到最终退出条件 |

第一轮联合结果为 234 passed / 2 failed，两项均因本机 shell 的 SOCKS 代理与缺少 socksio 影响本机 MCP 夹具。只在本机 MCP 测试中隔离代理环境后复跑通过，产品网络策略和源限制未放宽。Ruff F 检查、AST 解析和 diff 空白检查通过。

测试使用确定性提供商或本机协议服务；本轮没有执行新的真实付费 Web 研究或前端浏览器验收。完整部署与浏览器验证将在残余模型/工具依赖收口后继续。

## 仍需推进

源级剩余 33 处 LangChain 引用、旧模型/追踪/质量兼容分支及与其耦合的测试仍需迁移；已清退引擎的旧专用测试不能再作为新入口覆盖证据。过渡依赖与镜像安装项尚未移除。整体 67/81 及 T081 进行中状态保持，不把物理删除主循环等同于全部目标完成。

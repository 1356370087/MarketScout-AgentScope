# 前端功能模块

`app` 保留 Next.js 路由、布局和服务端 BFF；本目录承载页面交互。浏览器继续通过 BFF 访问业务契约，不连接 AgentScope 内部身份、会话或工具接口。

| 模块 | 职责 |
|---|---|
| research | 研究创建、运行进度与反馈、审批、团队、任务抽屉、报告展示和发布 |
| documents | 资料列表、上传与文档详情 |
| knowledge | 检索、问答、审核、事实/Wiki 与健康看板 |
| iam | 登录注册、验证/重置、账户会话与身份管理；管理 API 独立收纳 |
| settings | 模型与研究配置 |
| usage | 用量页面 |

跨功能 UI 留在 `components`；共享 BFF 客户端与传输类型留在 `lib`、`lib/contracts`。研究 SSE hooks、Zustand store 和 reducers 继续作为唯一前端状态实现，不在功能目录复制状态机。

页面直接引用功能模块，已退出原 `components` 下的六个研究组件入口。研究审批与知识库健康 CSS 跟随其功能模块。测试 mock 应指向实际功能模块路径。

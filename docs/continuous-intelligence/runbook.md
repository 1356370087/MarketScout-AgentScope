# MarketScout 持续情报运行手册

## 部署准备

保持单 Uvicorn worker。所有 PostgreSQL 连接使用指定远程服务 `172.22.208.30:5432`，业务、恢复账本及 LiteLLM 控制库按现有配置隔离。不启动本地 PostgreSQL；验收使用独立命名数据库，凭据放未跟踪环境文件或平台凭据管理。

持续任务要求数据库纳管的配置与个人配额：`PLATFORM_CONFIGURATION_MODE=database`。人工登录会话不被后台长期保存。本地免认证模式不能替代负责人长期授权。

1. 备份业务数据库、文档共享卷和平台配置。确认目标数据库及扩展后执行 `uv run alembic upgrade head`，预期迁移头为 `0023_continuous_intelligence`。命令进程须显式获得 `IAM_DATABASE_URL`，不能假定 Alembic 自动读取 `.env`。
2. 按既有部署流程迁移 AgentScope 恢复 schema，并部署支持 v22、v23、v24 的 API。先保持 `INTELLIGENCE_ENABLED=false` 和 `FEISHU_NOTIFICATIONS_ENABLED=false`。
3. 启动共享文档卷及平台配置卷的 Worker：`docker compose --profile intelligence up -d --build intelligence-worker`。摄取及知识审阅沿用 `document-worker`、`knowledge-worker`；报告文件发布仍用 `publisher-worker`。
4. 在平台配置中设置文档服务、研究模型、质量模型、报告审阅模型、个人月度硬配额和运行额度。持续计划须解析为启用质量门禁、关闭 fail-open 和个人记忆；平台策略不能强制相反配置。实际研究所用资源模式必须满足现有 host/Gateway 条件，LiteLLM 健康并不代表完整 Gateway 研究已经就绪。
5. 发布启用 `INTELLIGENCE_ENABLED=true` 的配置，检查 API、`intelligence-worker` 及相关组件生效回执。`GET /intelligence/health` 应有新鲜 Worker 心跳，队列应能推进。
6. 负责人正常登录，建立 `observe` 计划并确认来源、预算、授权有效期。完成观察验收后再在授权预览中切换 `auto`。

`INTELLIGENCE_MAX_CONCURRENT_RUNS` 默认 4，`INTELLIGENCE_POLL_SECONDS` 默认 5 秒，`INTELLIGENCE_SNAPSHOT_RETENTION_DAYS` 默认 90 天。计划默认每日检查，支持手动、每小时、每周；计划时区默认 `Asia/Shanghai`，时间戳以 UTC 保存。

## 首个试点

选择三个竞品的静态公开定价页和产品更新页，先检查每个页面能提取出有效正文。已有对比结论通过“关联已有结论”绑定到计划；没有基线时允许先完成一次基线研究。

试点至少持续一整周，保留以下人工验收记录：

1. 首次抓取、无变化检查、真实正文变化各自生成的快照、事件及研判结果。
2. 用冻结的新旧证据形成变化简报，确认套餐、地区、时间和限制条件没有被省略。
3. 通过“准备资料审阅”提交原始快照，完成资料元数据确认与人工发布；不得重新抓网页替代证据。
4. 生成 Wiki 草稿，核对来源，再由现有 `review_publish` 权限用户发布；回到事项确认发布并更新基线。
5. 再次变化时验证引用的是已发布基线，且自身知识发布不会重复启动同计划研究。
6. 验证暂停、授权撤销、负责人转移、成员移除、额度耗尽和进程重启，记录草稿、费用预留和原 Run 恢复结果。

## 飞书接入与用户需要做的操作

管理员创建企业自建应用，启用机器人，申请以应用身份发送消息所需权限 `im:message:send_as_bot`，配置用户基本身份授权，并将需要接收通知的人加入应用可用范围、发布生效。管理员配置 `FEISHU_APP_ID`，将 App Secret 放入服务端凭据管理的 `FEISHU_APP_SECRET` 引用中。

`INTELLIGENCE_PUBLIC_ORIGIN` 填 MarketScout 的 HTTPS 源地址，不带路径。OAuth 回调地址为：`https://<MarketScout域名>/intelligence/feishu/callback`。本地调试可以使用 localhost/127.0.0.1，但实际回调必须能由执行授权的浏览器访问，且与飞书后台登记完全匹配。

每位接收者需要登录 MarketScout，点击“绑定本人飞书账号”，在飞书页面完成一次身份授权，返回后主动开启通知。此操作只绑定通知目标，不授予知识库权限，也不替代 MarketScout 登录。正常投递不要求每条消息重新验证；解绑或应用身份变化后需重新绑定。

管理员最后开启 `FEISHU_NOTIFICATIONS_ENABLED`。由本人发起一项试点研究并确认收到“待审阅”提醒，再检查消息链接回站后仍要求正常登录和当前权限。团队计划只给负责人及有权限且已绑定、已开启通知的指定审阅者投递。

实现采用 OAuth v3 token 端点、一次性 state、有效期内的应用凭证缓存和稳定通知 UUID；不保存用户 Access/Refresh Token，不订阅群消息，不处理卡片审批或业务指令。说明以飞书现行 [应用凭证文档](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)、[网页身份授权文档](https://open.feishu.cn/document/qr-code-scanning-login-for-web-app/introduction) 和 [发送消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/create) 为依据。

## 故障处理与回滚

先读站内计划原因、来源错误、事项质量状态和 `/intelligence/health`。来源越界需编辑来源并重新授权；持续解析失败先确认静态正文是否可用，再显式检查。不要把动态页面壳记为无变化。

对于 `unknown`/`unresolved` 模型费用，使用计划“按运行回执核对费用”；没有确定回执时保留额度。恢复执行故障使用原事项“重试”，业务问题或输入版本变化使用“补充研究”。后者新增关联 Run，保留历史成果与费用。

失败且仍等待重试的事项保留原 Run 的额度；取消、忽略或进入新的研究轮次后，再根据确定回执结算。预留和结算归属最初预留的月份，月份切换不会清空旧未决记录。自动 Run 从工作项入口重试，与首次派工共用并发限制；普通 Run 恢复入口不能绕过这些检查。原生引擎不允许恢复的终态仍需明确补充研究，未知外部结果不会自动重试。预算审批不能突破授权版本的研究上限，扩大额度或来源须更新计划授权。

飞书 `unknown` 投递不能创建新通知 ID 后盲目重发；保留回执并通过站内待办处理。已知限流遵守等待时间，通知失败不回滚研究结果。通知全文默认不含证据正文。

回滚先关闭 `FEISHU_NOTIFICATIONS_ENABLED`，暂停自动计划或关闭持续情报开关。需要停止已有研究时明确撤销授权或取消事项。已经产生 v24 Run 时，回滚代码必须能够恢复 v24；不通过删数据、删共享卷或降级迁移回滚。

无引用且属于非活动计划的快照默认保留 90 天；当前来源快照、待处理事件、事项、历史 Run 及知识引用的材料继续保留。文档共享卷的备份必须覆盖 `observations/`，恢复到独立目标后同时核对数据库引用、原始内容与正文哈希。删除工作空间后禁止继续读取或外部投递。

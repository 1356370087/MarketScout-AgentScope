"""平台控制面权限及系统角色授权。"""

CATALOG = {
    "platform.config.read": "查看平台配置",
    "platform.config.write": "编辑与导入平台配置",
    "platform.config.publish": "发布和回滚平台配置",
    "platform.config.personal": "管理个人调试配置",
    "platform.credentials.manage": "管理平台模型和凭据",
    "platform.credentials.personal": "管理个人模型和凭据",
    "platform.operations.read": "查看平台运行与 Worker 状态",
    "platform.operations.write": "执行平台维护操作",
    "platform.usage.read": "查看平台用量",
    "platform.quotas.write": "管理用户配额",
    "platform.audit.read": "查看平台审计",
}
ADMIN_GRANTS = tuple(CATALOG)
DEVELOPER_GRANTS = ("platform.config.personal", "platform.credentials.personal")

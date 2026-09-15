"""IAM 身份覆盖（M2/AS-T015，实现 AS-D010）。

用现有 IAM（``src/security/rbac``）的 EdDSA JWT 覆盖框架原生的
``agentscope.app.deps.get_current_user_id``（后者默认盲信 ``X-User-ID`` 头，
M1/T005 已实证伪造风险 AS-R004）。

分层设计：

- 本模块提供 **JWT 验签身份层**（零数据库）：验 access token 的签名/kid/受众/
  有效期，取 ``sub`` 作为 user_id。适用于框架内部端点的最小身份语义。
- 完整 principal（会话吊销、authz_version、RBAC 权限）由 API 层继续使用
  ``security.rbac.dependencies.get_current_principal``（需数据库会话）——
  框架原生路由默认不对浏览器暴露（BFF 不代理），身份覆盖是纵深防御而非
  唯一防线。
- ``LOCAL_DEV_AUTH_BYPASS=true`` 且 ``APP_ENV=development`` 时返回合成身份，
  与旧契约一致。
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from agentscope.app.deps import get_current_user_id


def _bearer_token(authorization: str) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


async def iam_jwt_user_id(authorization: str = Header(default="")) -> str:
    """框架身份依赖的 IAM 覆盖：JWT 验签取 ``sub``，忽略 X-User-ID。"""
    from security.rbac.dependencies import local_dev_bypass_enabled

    if local_dev_bypass_enabled():
        from security.rbac.principal import synthetic_dev_principal

        return synthetic_dev_principal().user_id

    from security.rbac.jwt_service import TokenError, decode_access_token

    token = _bearer_token(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = decode_access_token(token).claims
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid_token:{exc}") from exc
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_token:no_subject")
    return str(user_id)


def install_identity_overrides(app) -> None:
    """在框架应用上安装 IAM 身份覆盖（伪造 X-User-ID 自此无效）。"""
    app.dependency_overrides[get_current_user_id] = iam_jwt_user_id

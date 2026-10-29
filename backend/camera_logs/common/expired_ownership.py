"""收敛额外权限到期后仅限本人对象的读取和写入例外。"""

from fastapi import HTTPException

from camera_logs.common.security import authorize


def _ip_allows(identity: dict, scope: str) -> bool:
    """来源 IP 未启用策略时不限制；启用后例外也必须命中所需作用域。"""
    allowed = identity.get("ipAllowedScopes")
    return allowed is None or "*" in allowed or scope in allowed


def authorize_expired_owned(identity: dict, scope: str, document: dict) -> None:
    """先执行常规 scope 校验，失败时仅允许已到期用户操作本人创建的对象。"""
    try:
        authorize(identity, scope)
    except HTTPException:
        if (identity.get("permissionsExpired") and document.get("createdBy") == identity.get("id")
                and _ip_allows(identity, scope)):
            return
        raise


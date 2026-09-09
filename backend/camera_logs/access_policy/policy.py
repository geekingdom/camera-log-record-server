"""计算平台客户端 IP 的规则权限，并将其与既有身份权限做只减不增的交集。"""

import ipaddress

from fastapi import HTTPException
from pydantic import ValidationError

from camera_logs.access_policy.models import IpRule

POLICY_ID = "platform-ip-policy"


def client_ip(request):
    """只使用 ASGI 连接端点，绝不信任可由客户端伪造的 X-Forwarded-For。"""
    return request.client.host if request.client else None


def matching_scopes(policy, address):
    """合并命中 CIDR 的权限；无命中或非法连接地址以空集合表示拒绝。"""
    if not address:
        return set()
    try:
        source = ipaddress.ip_address(address)
    except ValueError:
        return set()
    scopes = set()
    for item in policy.get("rules", []):
        try:
            rule = IpRule.model_validate(item)
            network = ipaddress.ip_network(rule.network)
        except (ValidationError, ValueError):
            continue
        if source in network:
            scopes.update(rule.scopes)
    return scopes


async def enforce_ip(repo, request):
    """返回命中规则权限；停用策略返回 None，启用后无命中立即拒绝。"""
    policy = await repo.db.ip_policy.find_one({"id": POLICY_ID})
    if not policy or not policy.get("enabled", False):
        return None
    scopes = matching_scopes(policy, client_ip(request))
    if not scopes:
        raise HTTPException(403, "当前客户端 IP 不在平台访问白名单内")
    return scopes


def restrict_identity(identity, policy_scopes):
    """将账号权限与命中规则求交集；规则绝不向身份增加任何权限。"""
    if policy_scopes is None:
        return identity
    current = set(identity.get("scopes", []))
    if "*" in policy_scopes:
        scopes = current
    elif "*" in current:
        scopes = set(policy_scopes)
    else:
        scopes = current & set(policy_scopes)
    return {**identity, "scopes": sorted(scopes), "isAdmin": "*" in scopes or "admin" in scopes}


async def apply_ip_permissions(repo, request, identity):
    """为 HTTP 或 WebSocket 的本轮认证收窄身份，调用方可每次重新计算。"""
    return restrict_identity(identity, await enforce_ip(repo, request))

"""提供管理员维护的平台客户端 IP 白名单接口，候选规则写入前防止当前管理员自锁。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pymongo import ReturnDocument

from camera_logs.access_policy.models import IpPolicyPatch
from camera_logs.access_policy.policy import POLICY_ID, client_ip, matching_scopes, restrict_identity
from camera_logs.common.database import now


async def _policy(repo):
    """首次读取建立默认停用策略，版本字段支持后续乐观锁更新。"""
    return await repo.db.ip_policy.find_one_and_update(
        {"id": POLICY_ID},
        {"$setOnInsert": {"id": POLICY_ID, "enabled": False, "rules": [], "version": 1,
                          "createdAt": now(), "updatedAt": now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def _public(policy, request):
    """仅返回可管理字段和实际连接端点，避免泄露 Mongo 内部字段。"""
    return {"version": policy["version"], "enabled": policy["enabled"], "rules": policy["rules"],
            "clientIp": client_ip(request)}


def install_ip_policy_routes(app):
    """安装只允许当前有效管理员访问的平台来源规则接口。"""
    from camera_logs.common.security import actor, authorize

    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/admin/ip-policy")
    async def get_ip_policy(request: Request, user: User):
        """读取版本化规则，来源地址只取当前 ASGI 连接端点。"""
        authorize(user, "admin")
        return _public(await _policy(request.app.state.repo), request)

    @app.patch("/api/v1/admin/ip-policy")
    async def update_ip_policy(body: IpPolicyPatch, request: Request, user: User):
        """原子替换规则；候选启用策略必须保留当前管理员的管理权限。"""
        authorize(user, "admin")
        repo = request.app.state.repo
        await _policy(repo)
        candidate = {"enabled": body.enabled, "rules": [rule.model_dump() for rule in body.rules]}
        candidate_identity = restrict_identity(user, matching_scopes(candidate, client_ip(request)) if body.enabled else None)
        try:
            authorize(candidate_identity, "admin")
        except HTTPException as exc:
            raise HTTPException(422, "候选规则会使当前管理员失去管理权限") from exc
        changed = await repo.db.ip_policy.find_one_and_update(
            {"id": POLICY_ID, "version": body.version},
            {"$set": candidate | {"updatedAt": now()}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not changed:
            raise HTTPException(409, "IP 白名单版本已变化，请刷新")
        await repo.audit(user["id"], "update_ip_policy", POLICY_ID)
        return _public(changed, request)

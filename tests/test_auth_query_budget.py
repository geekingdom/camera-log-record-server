"""鉴权高频路径只读取必要身份数据，展示资格查询不放大实时订阅负载。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.common.security import authenticate
from camera_logs.users.sessions import session_identity
from fastapi import HTTPException


def repository():
    """构造可观测查询次数的身份存储，不模拟索引或事务。"""
    user = {"id": "user", "enabled": True, "role": "USER", "scopes": ["tasks:create"], "authVersion": 1}
    return SimpleNamespace(settings=SimpleNamespace(bootstrap_token=""), db=SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value=user)),
        tokens=SimpleNamespace(find_one=AsyncMock(return_value={"id": "token", "userId": "user", "revoked": False})),
        user_sessions=SimpleNamespace(find_one=AsyncMock(return_value={"userId": "user", "authVersion": 1})),
    ))


async def test_bearer_reuses_just_verified_token_for_display_qualification():
    """本次刚验证的服务令牌足以证明资格，不能再按用户重复查询令牌目录。"""
    repo = repository()
    identity = await authenticate(repo, "synthetic")
    assert identity["hasActiveServiceToken"] is True
    assert repo.db.tokens.find_one.await_count == 1
    assert repo.db.users.find_one.await_count == 1
    repo.db.tokens.find_one.return_value = None
    with pytest.raises(HTTPException) as revoked:
        await authenticate(repo, "synthetic")
    assert revoked.value.status_code == 401


async def test_live_cookie_identity_omits_only_unused_display_query():
    """实时鉴权仍查会话与用户；省略UI专属资格不影响下轮账号禁用。"""
    repo = repository()
    identity = await session_identity(repo, "synthetic", include_service_token=False)
    assert "tasks:create" in identity["scopes"]
    assert "hasActiveServiceToken" not in identity
    repo.db.tokens.find_one.assert_not_awaited()
    repo.db.users.find_one.return_value = None
    with pytest.raises(HTTPException) as disabled:
        await session_identity(repo, "synthetic", include_service_token=False)
    assert disabled.value.status_code == 401


async def test_current_user_still_queries_service_account_qualification():
    """当前用户接口默认保留实时资格判定，服务账号撤销后入口可及时隐藏。"""
    repo = repository()
    assert (await session_identity(repo, "synthetic"))["hasActiveServiceToken"] is True
    repo.db.tokens.find_one.return_value = None
    assert (await session_identity(repo, "synthetic"))["hasActiveServiceToken"] is False
    assert repo.db.tokens.find_one.await_count == 2


async def test_ip_policy_is_read_once_per_http_request_but_rechecked_next_request():
    """同一HTTP的中间件和权限依赖共享快照，后续请求仍立即看到规则撤销。"""
    from camera_logs.access_policy.policy import apply_ip_permissions, enforce_ip
    from starlette.requests import Request

    policy = AsyncMock(return_value={"enabled": False})
    repo = SimpleNamespace(db=SimpleNamespace(ip_policy=SimpleNamespace(find_one=policy)))
    scope = {"type": "http", "client": ("127.0.0.1", 1234), "headers": []}
    request = Request(scope)
    assert await enforce_ip(repo, request) is None
    await apply_ip_permissions(repo, request, {"scopes": ["tasks:read"]})
    assert policy.await_count == 1
    policy.return_value = {"enabled": True, "rules": []}
    with pytest.raises(HTTPException) as denied:
        await enforce_ip(repo, Request({**scope, "state": {}}))
    assert denied.value.status_code == 403
    assert policy.await_count == 2


async def test_websocket_ip_policy_is_not_cached_for_connection_lifetime():
    """长连接每轮授权重查IP策略，不能复用HTTP专属快照。"""
    from camera_logs.access_policy.policy import enforce_ip

    policy = AsyncMock(return_value={"enabled": False})
    repo = SimpleNamespace(db=SimpleNamespace(ip_policy=SimpleNamespace(find_one=policy)))
    socket = SimpleNamespace(scope={"type": "websocket"}, client=SimpleNamespace(host="127.0.0.1"))
    assert await enforce_ip(repo, socket) is None
    policy.return_value = {"enabled": True, "rules": []}
    with pytest.raises(HTTPException):
        await enforce_ip(repo, socket)
    assert policy.await_count == 2

"""验证服务账号令牌的管理列表只向管理员暴露非敏感元数据。"""

from datetime import timedelta

from camera_logs.common.database import now
from test_api import client  # noqa: F401


def test_service_token_list_is_paginated_and_hides_hash(client):  # noqa: F811
    """管理员可查看令牌元数据；哈希和明文均不得出现在列表响应中。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tokens.insert_many, [
        {"id": "older", "name": "旧令牌", "scopes": ["tasks:read"], "taskIds": None,
         "tokenHash": "secret-hash-older", "revoked": False,
         "expiresAt": now() + timedelta(days=10), "createdAt": now() - timedelta(days=1)},
        {"id": "newer", "name": "新令牌", "scopes": ["logs:download"], "taskIds": ["task-1"],
         "tokenHash": "secret-hash-newer", "revoked": True,
         "expiresAt": now() + timedelta(days=20), "createdAt": now()},
    ])

    response = client.get("/api/v1/service-tokens?page=1&pageSize=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["pageSize"] == 1
    assert body["items"][0]["id"] == "newer"
    assert body["items"][0]["revoked"] is True
    assert "tokenHash" not in body["items"][0]
    assert "token" not in body["items"][0]


def test_service_token_list_requires_admin_scope(client):  # noqa: F811
    """普通服务令牌没有管理作用域时不能枚举其他账号元数据。"""
    created = client.post("/api/v1/service-tokens", json={
        "name": "只读账号", "scopes": ["tasks:read"], "expiresInDays": 30,
    }).json()

    response = client.get(
        "/api/v1/service-tokens",
        headers={"Authorization": f"Bearer {created['token']}"},
    )

    assert response.status_code == 403


def test_service_token_rejects_unknown_scope(client):  # noqa: F811
    """创建接口拒绝拼写错误的作用域，防止生成看似可用却没有任何权限的账号。"""
    response = client.post("/api/v1/service-tokens", json={
        "name": "拼写错误", "scopes": ["tasks:reed"], "expiresInDays": 30,
    })

    assert response.status_code == 422

"""验证服务账号令牌的管理列表只向管理员暴露非敏感元数据。"""

from datetime import timedelta

from camera_logs.common import audited_mutations
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


def test_token_creation_uses_audited_transaction(client, monkeypatch):  # noqa: F811
    """创建令牌必须通过事务入口，不能先提交凭据后单独写审计。"""
    original = audited_mutations.mutation_transaction
    calls = []

    async def tracked(repo, callback):
        calls.append(True)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", tracked)
    response = client.post("/api/v1/service-tokens", json={
        "name": "审计事务", "scopes": ["tasks:read"], "expiresInDays": 1,
    })
    assert response.status_code == 201
    assert calls == [True]


def test_repeated_revoke_has_one_transition_audit(client):  # noqa: F811
    """撤销重试保持幂等，成功审计只表示实际发生的状态变化。"""
    response = client.post("/api/v1/service-tokens", json={
        "name": "撤销重试", "scopes": ["tasks:read"], "expiresInDays": 1,
    })
    identifier = response.json()["id"]
    for _ in range(2):
        assert client.delete(f"/api/v1/service-tokens/{identifier}").status_code == 204
    assert client.portal.call(client.app.state.repo.db.audit.count_documents,
                              {"action": "revoke_token", "targetId": identifier}) == 1

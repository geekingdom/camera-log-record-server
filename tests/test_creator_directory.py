"""创建人筛选包含历史账号，但不能借筛选目录访问敏感账户字段。"""
from test_api import client  # noqa: F401


def test_creator_directory_keeps_deleted_users_and_only_minimal_fields(client):  # noqa: F811
    repo = client.app.state.repo
    client.portal.call(repo.db.users.insert_many, [
        {"id": "creator-old", "username": "old.creator", "displayName": "历史创建者", "enabled": False,
         "deletedAt": "2026-09-09", "passwordHash": "never-return", "scopes": ["admin"]},
        {"id": "creator-new", "username": "new-creator", "displayName": "新创建者", "enabled": True},
    ])
    result = client.get("/api/v1/users/creators", params={"search": "old.", "pageSize": 1})
    assert result.status_code == 200
    assert result.json() == {"items": [{"id": "creator-old", "username": "old.creator", "displayName": "历史创建者"}],
                             "page": 1, "pageSize": 1, "total": 1}
    assert client.get("/api/v1/users/creators", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_creator_directory_is_available_to_ordinary_user_without_account_management(client):  # noqa: F811
    user = client.post("/api/v1/users", json={
        "username": "creator-reader", "displayName": "创建人筛选用户", "password": "creator-password-123",
    }).json()
    token = client.post("/api/v1/service-tokens", json={"name": "筛选调用", "userId": user["id"]}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    response = client.get("/api/v1/users/creators", headers=headers)
    assert response.status_code == 200
    assert all(set(row) == {"id", "username", "displayName"} for row in response.json()["items"])
    assert client.get("/api/v1/users", headers=headers).status_code == 403

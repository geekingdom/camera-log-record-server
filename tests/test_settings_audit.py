"""验证平台设置和节点配置的用户变更与审计使用同一事务会话。

MongoMock 不支持事务回滚；本文件仅验证路由会话传递和 HTTP 状态语义，
真实副本集故障回滚由独立验证脚本覆盖。
"""
# ruff: noqa: F811

from test_api import client  # noqa: F401


def _audit_count(client, action, target):
    """按动作和目标读取审计数，避免初始化或其它操作影响断言。"""
    return client.portal.call(
        client.app.state.repo.db.audit.count_documents,
        {"action": action, "targetId": target},
    )


def _record_user_mutation_sessions(client, monkeypatch):
    """给 MongoMock 注入唯一事务会话，并记录写入和审计接收的会话。"""
    from camera_logs.common import audited_mutations

    marker = object()
    seen = []

    async def transaction(_repo, callback):
        return await callback(marker)

    collection_type = type(client.app.state.repo.db.audit)
    insert_one = collection_type.insert_one
    find_one_and_update = collection_type.find_one_and_update

    async def tracked_insert_one(self, *args, session=None, **kwargs):
        if self.name in {"audit", "node_configs"}:
            seen.append(session)
        return await insert_one(self, *args, **kwargs)

    async def tracked_find_one_and_update(self, *args, session=None, **kwargs):
        if self.name in {"platform_settings", "node_configs"}:
            seen.append(session)
        return await find_one_and_update(self, *args, **kwargs)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", transaction)
    monkeypatch.setattr(collection_type, "insert_one", tracked_insert_one)
    monkeypatch.setattr(collection_type, "find_one_and_update", tracked_find_one_and_update)
    return marker, seen


def test_platform_and_node_user_mutations_pass_one_session_to_write_and_audit(client, monkeypatch):
    """保留期、登记和节点配置更新均把业务写入与审计交给同一会话。"""
    assert client.get("/api/v1/platform-settings").status_code == 200
    marker, seen = _record_user_mutation_sessions(client, monkeypatch)

    settings = client.patch("/api/v1/platform-settings", json={"retentionDays": 14, "version": 1})
    assert settings.status_code == 200, settings.text
    registered = client.post("/api/v1/admin/nodes", json={
        "id": "audit-edge", "url": "https://audit-edge.example.test", "capacity": 8,
    })
    assert registered.status_code == 201, registered.text
    updated = client.patch("/api/v1/admin/nodes/audit-edge", json={"version": 1, "capacity": 9})
    assert updated.status_code == 200, updated.text

    # 保留期 PATCH 先在同一事务中保障默认记录，再更新业务值并追加审计。
    assert seen == [marker] * 7


def test_stale_versions_and_duplicate_node_do_not_write_success_audit(client):
    """旧版本和重名登记继续返回 409，且失败请求不追加成功审计。"""
    assert client.get("/api/v1/platform-settings").status_code == 200
    assert client.patch("/api/v1/platform-settings", json={"retentionDays": 14, "version": 1}).status_code == 200
    assert client.patch("/api/v1/platform-settings", json={"retentionDays": 30, "version": 1}).status_code == 409
    assert _audit_count(client, "update_platform_settings", "platform") == 1

    body = {"id": "duplicate-edge", "url": "https://duplicate.example.test", "capacity": 8}
    assert client.post("/api/v1/admin/nodes", json=body).status_code == 201
    assert client.post("/api/v1/admin/nodes", json=body).status_code == 409
    assert _audit_count(client, "register_node", "duplicate-edge") == 1

    assert client.patch("/api/v1/admin/nodes/duplicate-edge", json={"version": 2, "capacity": 9}).status_code == 409
    assert _audit_count(client, "update_node_config", "duplicate-edge") == 0

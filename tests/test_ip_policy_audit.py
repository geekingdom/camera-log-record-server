"""验证来源白名单更新使用审计事务；真实回滚由副本集 API 验证脚本证明。"""

from camera_logs.common import audited_mutations
from fastapi.testclient import TestClient
from test_ip_policy_integration import SOURCE_V4, app, policy_body  # noqa: F401


def test_policy_write_and_audit_share_transaction_session(app, monkeypatch):  # noqa: F811
    """记录同一会话是否同时传给配置更新和审计，拒绝事务外补记。"""
    with TestClient(app, client=(SOURCE_V4, 45000)) as client:
        repo = app.state.repo
        headers = {"Authorization": "Bearer ip-integration-bootstrap"}
        assert client.get("/api/v1/admin/ip-policy", headers=headers).status_code == 200
        session = object()
        seen = []
        collection_type = type(repo.db.ip_policy)
        original_update = collection_type.find_one_and_update
        original_audit = repo.audit

        async def transaction(_repo, callback):
            seen.append("transaction")
            return await callback(session)

        async def update(collection, *args, **kwargs):
            # MongoMock 不能接收真实 session；此处仅记录接口传递，不模拟提交或回滚。
            if collection.name == "ip_policy":
                passed_session = kwargs.pop("session", None)
                seen.append(("write" if "$set" in args[1] else "defaults", passed_session))
            return await original_update(collection, *args, **kwargs)

        async def audit(actor, action, target, *, session=None):
            seen.append(("audit", session))
            await original_audit(actor, action, target)

        monkeypatch.setattr(audited_mutations, "mutation_transaction", transaction)
        monkeypatch.setattr(collection_type, "find_one_and_update", update)
        monkeypatch.setattr(repo, "audit", audit)
        changed = client.patch("/api/v1/admin/ip-policy", headers=headers, json=policy_body())
        assert changed.status_code == 200, changed.text
        assert seen == ["transaction", ("defaults", session), ("write", session), ("audit", session)]


def test_policy_rejections_do_not_record_success(app):  # noqa: F811
    """自锁校验和版本冲突不能留下成功审计，已有策略和版本保持不变。"""
    with TestClient(app, client=(SOURCE_V4, 45001)) as client:
        headers = {"Authorization": "Bearer ip-integration-bootstrap"}
        path = "/api/v1/admin/ip-policy"
        assert client.patch(path, headers=headers, json=policy_body()).status_code == 200
        assert client.patch(path, headers=headers, json=policy_body()).status_code == 409
        unsafe = {"version": 2, "enabled": True, "rules": [
            {"label": "other", "network": "203.0.113.0/24", "scopes": ["admin"]},
        ]}
        assert client.patch(path, headers=headers, json=unsafe).status_code == 422
        assert client.get(path, headers=headers).json()["version"] == 2
        assert client.portal.call(app.state.repo.db.audit.count_documents, {"action": "update_ip_policy"}) == 1

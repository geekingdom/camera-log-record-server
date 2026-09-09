"""验证会话轮换使用当前账号状态及与审计一致的事务入口。"""
# ruff: noqa: F811

from camera_logs.common import audited_mutations
from camera_logs.users import api
from test_user_auth import CSRF, client, login  # noqa: F401


def test_login_rejects_account_disabled_during_password_verification(client, monkeypatch):
    """密码计算期间发生的停用必须在签发会话前重新检查。"""
    original = api.password_work

    async def disable_after_verification(repo, function, *values):
        result = await original(repo, function, *values)
        if function is api.verify_password:
            await repo.db.users.update_one({"id": "builtin-admin"}, {
                "$set": {"enabled": False}, "$inc": {"authVersion": 1},
            })
        return result

    monkeypatch.setattr(api, "password_work", disable_after_verification)
    response = login(client)
    assert response.status_code == 409
    assert "set-cookie" not in response.headers
    assert client.portal.call(client.app.state.repo.db.user_sessions.count_documents, {}) == 0


def test_login_and_logout_use_transaction_boundary(client, monkeypatch):
    """内存替身只证明入口使用事务；真实副本集脚本负责回滚证据。"""
    original = audited_mutations.mutation_transaction
    calls = []

    async def transaction(repo, callback):
        calls.append(callback)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", transaction)
    response = login(client)
    assert response.status_code == 200
    assert len(calls) == 1
    response = client.post("/api/v1/auth/logout", headers=CSRF)
    assert response.status_code == 204
    assert len(calls) == 2


def test_relogin_rotates_cookie_and_records_one_audit_per_login(client):
    """浏览器再次登录删除旧凭证，审计按成功登录次数计数。"""
    assert login(client).status_code == 200
    previous = client.cookies.get("camera_session")
    assert login(client).status_code == 200
    assert client.cookies.get("camera_session") != previous
    repo = client.app.state.repo
    assert client.portal.call(repo.db.user_sessions.count_documents, {}) == 1
    assert client.portal.call(repo.db.audit.count_documents, {"action": "login"}) == 2


def test_password_rejects_session_revoked_after_authentication(client, monkeypatch):
    """已通过路由鉴权的请求也不能在原会话随后撤销后继续改密。"""
    assert login(client).status_code == 200
    original = api.password_work

    async def revoke_after_verification(repo, function, *values):
        result = await original(repo, function, *values)
        if function is api.verify_password:
            await repo.db.user_sessions.delete_many({"userId": "builtin-admin"})
        return result

    monkeypatch.setattr(api, "password_work", revoke_after_verification)
    response = client.post("/api/v1/auth/password", headers=CSRF, json={
        "currentPassword": "initial-admin-password", "newPassword": "a-new-password-value",
    })
    assert response.status_code == 409
    assert "set-cookie" not in response.headers

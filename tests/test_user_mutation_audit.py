"""验证账户变更与审计同一提交边界，且响应 Cookie 故障不伪装为密码回滚。"""
# ruff: noqa: F811

from camera_logs.users.passwords import verify_password
from fastapi import HTTPException
from test_user_auth import CSRF, change_initial_password, client, create_operator, login  # noqa: F401


def _audit_count(client, action, target):
    """按动作与目标查询审计数，避免别的账户操作掩盖本次断言。"""
    return client.portal.call(
        client.app.state.repo.db.audit.count_documents,
        {"action": action, "targetId": target},
    )


def test_account_mutations_write_expected_audit_and_cas_miss_writes_none(client):
    """创建、修改、重置和删除均有审计；过期版本不会留下虚假编辑事件。"""
    change_initial_password(client)
    created = create_operator(client, username="mutation-operator")
    identifier = created["id"]

    assert _audit_count(client, "create_user", identifier) == 1
    stale = client.patch(
        f"/api/v1/users/{identifier}",
        json={"version": created["version"] + 1, "displayName": "不应写入"},
        headers=CSRF,
    )
    assert stale.status_code == 409
    assert _audit_count(client, "edit_user", identifier) == 0

    edited = client.patch(
        f"/api/v1/users/{identifier}",
        json={"version": created["version"], "displayName": "已审计操作员"},
        headers=CSRF,
    )
    assert edited.status_code == 200, edited.text
    assert _audit_count(client, "edit_user", identifier) == 1

    reset = client.post(
        f"/api/v1/users/{identifier}/reset-password",
        json={"version": edited.json()["version"], "password": "reset-password-next"},
        headers=CSRF,
    )
    assert reset.status_code == 200, reset.text
    assert _audit_count(client, "reset_user_password", identifier) == 1

    deleted = client.delete(
        f"/api/v1/users/{identifier}?version={reset.json()['version']}",
        headers=CSRF,
    )
    assert deleted.status_code == 204, deleted.text
    assert _audit_count(client, "delete_user", identifier) == 1


def test_password_commit_and_audit_precede_cookie_response(client, monkeypatch):
    """Cookie 响应故障发生在提交后，密码、会话和审计仍是已确认事实。"""
    logged_in = login(client)
    assert logged_in.status_code == 200, logged_in.text
    repo = client.app.state.repo
    user_id = logged_in.json()["user"]["id"]

    def cookie_failure(_repo, _token, _request, _response):
        raise HTTPException(503, "Cookie 响应暂时失败")

    monkeypatch.setattr("camera_logs.users.api.set_session_cookie", cookie_failure)
    changed = client.post(
        "/api/v1/auth/password",
        json={"currentPassword": "initial-admin-password", "newPassword": "committed-password-next"},
        headers=CSRF,
    )

    assert changed.status_code == 503
    stored = client.portal.call(repo.db.users.find_one, {"id": user_id})
    assert verify_password("committed-password-next", stored["passwordHash"])
    assert not verify_password("initial-admin-password", stored["passwordHash"])
    assert _audit_count(client, "change_password", user_id) == 1
    assert client.portal.call(repo.db.user_sessions.count_documents,
                              {"userId": user_id, "authVersion": stored["authVersion"]}) == 1

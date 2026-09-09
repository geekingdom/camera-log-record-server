"""验证浏览器带着另一账号 Cookie 登录时不会撤销原账号会话。"""
# ruff: noqa: F811

import hashlib

from test_user_auth import change_initial_password, client, create_operator, login  # noqa: F401


def test_cross_account_login_keeps_cookie_owner_session(client):
    """同一浏览器切换到 B 账号时，登录事务只能删除 B 自己的旧会话。"""
    change_initial_password(client)
    create_operator(client, username="session-operator")
    admin_token = client.cookies.get("camera_session")
    repo = client.app.state.repo
    admin_hash = hashlib.sha256(admin_token.encode()).hexdigest()
    assert client.portal.call(repo.db.user_sessions.find_one, {"tokenHash": admin_hash}) is not None

    response = login(client, "session-operator", "operator-password")

    assert response.status_code == 200
    assert client.portal.call(repo.db.user_sessions.find_one, {"tokenHash": admin_hash}) is not None

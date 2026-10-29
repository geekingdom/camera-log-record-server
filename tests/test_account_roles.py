"""三级角色、北京时间权限截止日及跨所有者手工命令回归。"""
# ruff: noqa: F811
from datetime import UTC, datetime

from test_user_auth import CSRF, client  # noqa: F401


def create(client, name, **values):
    """通过管理接口建立独立账户，不连接任何设备。"""
    response = client.post('/api/v1/users', json={
        'username': name, 'displayName': name, 'password': 'role-password', **values,
    }, headers={'Authorization': 'Bearer test-bootstrap'})
    assert response.status_code == 201, response.text
    return response.json()


def sign_in(client, name):
    """普通浏览器登录并完成初始密码修改。"""
    assert client.post('/api/v1/auth/login', json={'username': name, 'password': 'role-password'}, headers=CSRF).status_code == 200
    response = client.post('/api/v1/auth/password', json={
        'currentPassword': 'role-password', 'newPassword': 'role-password-next',
    }, headers=CSRF)
    assert response.status_code == 200, response.text
    return response.json()['user']


def test_roles_protect_other_admins_and_allow_ordinary_management(client):
    first = create(client, 'admin-one', role='ADMIN')
    other = create(client, 'admin-two', role='ADMIN')
    normal = create(client, 'ordinary')
    assert normal['role'] == 'USER'
    identity = sign_in(client, 'admin-one')
    assert identity['isAdmin'] and identity['effectiveRole'] == 'ADMIN'
    for target in (other, {'id': 'builtin-admin', 'version': 1}):
        path = f"/api/v1/users/{target['id']}"
        assert client.patch(path, json={'version': target['version'], 'enabled': False}, headers=CSRF).status_code == 403
        assert client.post(path + '/reset-password', json={'version': target['version'], 'password': 'new-password'}, headers=CSRF).status_code == 403
        assert client.delete(path, params={'version': target['version']}, headers=CSRF).status_code == 403
    result = client.patch(f"/api/v1/users/{normal['id']}", json={'version': normal['version'], 'role': 'ADMIN'}, headers=CSRF)
    assert result.status_code == 200, result.text
    assert result.json()['role'] == 'ADMIN'
    own = client.get('/api/v1/auth/me').json()['user']
    assert own['id'] == first['id']


def test_permission_expiry_shanghai_day_and_legacy_role():
    from camera_logs.users.roles import permission_state
    user = {'id': 'u', 'isAdmin': True, 'permissionsExpiresOn': '2026-10-13', 'scopes': ['tasks:create']}
    active = permission_state(user, instant=datetime(2026, 10, 13, 15, 59, 59, tzinfo=UTC))
    expired = permission_state(user, instant=datetime(2026, 10, 13, 16, 0, tzinfo=UTC))
    assert active['effectiveRole'] == 'ADMIN' and active['isAdmin']
    assert expired['role'] == 'ADMIN' and expired['effectiveRole'] == 'USER'
    assert expired['permissionsExpired'] and not expired['isAdmin']
    assert permission_state({'id': 'builtin-admin', 'builtin': True, 'isAdmin': True}, instant=datetime.now(UTC))['role'] == 'SUPER_ADMIN'


def test_every_user_can_send_to_other_owners_task_and_records_username(client):
    created = create(client, 'sender')
    identity = sign_in(client, 'sender')
    assert 'commands:send' in identity['scopes']
    db = client.app.state.repo.db
    client.portal.call(db.tasks.insert_one, {'id': 'foreign', 'createdBy': 'someone-else',
        'status': 'COLLECTING', 'desiredState': 'RUNNING', 'runId': 'r', 'sessionId': 's'})
    response = client.post('/api/v1/tasks/foreign/commands', json={'command': 'ifconfig'},
        headers=CSRF | {'Idempotency-Key': 'role-command'})
    assert response.status_code == 202, response.text
    command = response.json()
    assert command['actor'] == created['id'] and command['actorUsername'] == 'sender'
    assert client.portal.call(db.audit.count_documents, {'actor': created['id'], 'action': 'command:foreign'}) == 1


def test_expiry_reduces_session_and_service_token_without_disabling_account(client, monkeypatch):
    """同一会话和令牌跨截止日实时降权；期限到期不等于账号停用。"""
    from camera_logs.users import roles
    monkeypatch.setattr(roles, 'utc_now', lambda: datetime(2026, 10, 13, 15, 59, 59, tzinfo=UTC))
    user = create(client, 'temporary', role='ADMIN', permissionsExpiresOn='2026-10-13')
    token = client.post('/api/v1/service-tokens', json={'name': '期限验证', 'userId': user['id']},
        headers={'Authorization': 'Bearer test-bootstrap'}).json()['token']
    assert sign_in(client, 'temporary')['isAdmin']
    assert client.get('/api/v1/users').status_code == 200
    monkeypatch.setattr(roles, 'utc_now', lambda: datetime(2026, 10, 13, 16, 0, tzinfo=UTC))
    current = client.get('/api/v1/auth/me').json()['user']
    assert current['permissionsExpired'] and current['enabled'] and not current['isAdmin']
    assert set(current['scopes']) == roles.FIXED_SCOPES
    assert client.get('/api/v1/users').status_code == 403
    assert client.get('/api/v1/users', headers={'Authorization': 'Bearer ' + token}).status_code == 403
    assert client.get('/api/v1/tasks', headers={'Authorization': 'Bearer ' + token}).status_code == 200
    assert client.post('/api/v1/tasks', json={'name': 'forbidden', 'resourceId': 'unused', 'ip': '192.0.2.1', 'protocol': 'SSH', 'port': 22, 'username': 'u', 'password': 'test-password'},
        headers=CSRF | {'Idempotency-Key': 'expired-create'}).status_code == 403


def test_admin_cannot_obtain_other_admin_service_credentials(client):
    """不能借新增或查看其他管理员令牌绕过账户保护。"""
    create(client, 'manager', role='ADMIN')
    target = create(client, 'protected', role='ADMIN')
    token = client.post('/api/v1/service-tokens', json={'name': 'protected', 'userId': target['id']},
        headers={'Authorization': 'Bearer test-bootstrap'}).json()
    sign_in(client, 'manager')
    assert client.post('/api/v1/service-tokens', json={'name': 'bypass', 'userId': target['id']}, headers=CSRF).status_code == 403
    assert client.post(f"/api/v1/service-tokens/{token['id']}/reveal", headers=CSRF).status_code == 403
    assert client.delete(f"/api/v1/service-tokens/{token['id']}", headers=CSRF).status_code == 403


def test_invalid_role_and_expiry_are_rejected_and_default_is_long_term(client):
    """日期只接受规范自然日；客户端不能创建或提升超级管理员。"""
    user = create(client, 'permanent')
    assert user['permissionsExpiresOn'] is None and not user['permissionsExpired']
    for values in ({'role': 'SUPER_ADMIN'}, {'permissionsExpiresOn': '2026-10-13T00:00:00'},
                   {'permissionsExpiresOn': '2026-02-30'}, {'permissionsExpiresOn': '2026-1-2'},
                   {'role': 'ADMIN', 'isAdmin': False}):
        response = client.post('/api/v1/users', json={'username': 'invalid', 'displayName': 'invalid',
            'password': 'valid-password', **values}, headers={'Authorization': 'Bearer test-bootstrap'})
        assert response.status_code == 422, response.text


def test_admin_can_edit_reset_and_delete_own_account(client):
    """其他管理员保护不误伤管理员本人；自助修改仍按版本和会话撤销规则执行。"""
    user = create(client, 'self-admin', role='ADMIN')
    sign_in(client, 'self-admin')
    current = client.get('/api/v1/auth/me').json()['user']
    changed = client.patch(f"/api/v1/users/{user['id']}", json={
        'version': current['version'], 'displayName': '自己更新',
    }, headers=CSRF)
    assert changed.status_code == 200, changed.text
    # 配置变更撤销旧会话；重新登录后才能继续执行下一次操作。
    assert client.post('/api/v1/auth/login', json={'username': 'self-admin', 'password': 'role-password-next'}, headers=CSRF).status_code == 200
    reset = client.post(f"/api/v1/users/{user['id']}/reset-password", json={
        'version': changed.json()['version'], 'password': 'self-reset-password',
    }, headers=CSRF)
    assert reset.status_code == 200, reset.text
    assert client.post('/api/v1/auth/login', json={'username': 'self-admin', 'password': 'self-reset-password'}, headers=CSRF).status_code == 200
    updated = client.post('/api/v1/auth/password', json={
        'currentPassword': 'self-reset-password', 'newPassword': 'self-final-password',
    }, headers=CSRF)
    assert updated.status_code == 200, updated.text
    assert client.delete(f"/api/v1/users/{user['id']}", params={'version': updated.json()['user']['version']}, headers=CSRF).status_code == 204
    assert client.get('/api/v1/auth/me').status_code == 401

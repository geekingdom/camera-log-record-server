"""显式资源共享授予操作资格，但不能绕过平台权限或资源删除限制。"""
import asyncio

from camera_logs.common.security import actor
from test_api import client  # noqa: F401
from test_resource_ownership import as_user, task


def test_explicit_resource_share_and_revocation(client):  # noqa: F811
    repo = client.app.state.repo
    asyncio.run(repo.db.users.insert_one({'id': 'bob', 'username': 'bob', 'enabled': True}))
    as_user(client, 'alice')
    response = client.post('/api/v1/resources', json={
        'name': '共享设备', 'kind': 'SERIAL_SERVER', 'ip': '192.0.2.71', 'sharedUserIds': ['bob'],
    }, headers={'Idempotency-Key': 'shared-resource'})
    assert response.status_code == 201, response.text
    resource = response.json()
    owned_task = task(client, resource['id'])
    as_user(client, 'bob')
    patch = {'version': resource['version'], 'name': '协作者编辑', 'kind': 'SERIAL_SERVER', 'ip': resource['ip']}
    changed = client.patch(f'/api/v1/resources/{resource["id"]}', json=patch)
    assert changed.status_code == 200, changed.text
    assert changed.json()['sharedUserIds'] == ['bob']
    assert client.post(f'/api/v1/tasks/{owned_task["id"]}/stop').status_code == 202
    assert client.delete(f'/api/v1/resources/{resource["id"]}?version=2').status_code == 403
    client.app.dependency_overrides[actor] = lambda: {'id': 'bob', 'isAdmin': False, 'scopes': ['tasks:read']}
    assert client.patch(f'/api/v1/resources/{resource["id"]}', json=patch | {'version': 2}).status_code == 403
    assert client.post(f'/api/v1/tasks/{owned_task["id"]}/stop').status_code == 403
    as_user(client, 'alice')
    assert client.patch(f'/api/v1/resources/{resource["id"]}', json=patch | {'version': 2, 'sharedUserIds': []}).status_code == 200
    as_user(client, 'bob')
    assert client.patch(f'/api/v1/resources/{resource["id"]}', json=patch | {'version': 3}).status_code == 403
    assert client.post(f'/api/v1/tasks/{owned_task["id"]}/stop').status_code == 403


def test_sharing_defaults_empty_and_rejects_invalid_users(client):  # noqa: F811
    as_user(client, 'alice')
    body = {'name': '默认不共享', 'kind': 'SERIAL_SERVER', 'ip': '192.0.2.72'}
    created = client.post('/api/v1/resources', json=body, headers={'Idempotency-Key': 'default-share'})
    assert created.status_code == 201, created.text
    assert created.json()['sharedUserIds'] == []
    invalid = client.post('/api/v1/resources', json=body | {'ip': '192.0.2.73', 'sharedUserIds': ['missing']},
                          headers={'Idempotency-Key': 'invalid-share'})
    assert invalid.status_code == 422


def test_shared_task_cannot_be_rebound_to_an_unshared_resource(client):  # noqa: F811
    """共享仅授予原设备任务操作资格，不能借编辑把任务迁移到未共享设备。"""
    as_user(client, 'alice')
    client.portal.call(client.app.state.repo.db.resources.insert_many, [
        {'id': 'shared-network', 'createdBy': 'alice', 'sharedUserIds': ['bob'], 'deletedAt': None,
         'kind': 'HIKVISION_NETWORK', 'ip': '192.0.2.74', 'model': 'fixture', 'subSerialNumber': 'fixture',
         'authenticatedAt': '2026-10-26T00:00:00Z', 'healthStatus': 'ONLINE'},
        {'id': 'private-serial', 'createdBy': 'alice', 'sharedUserIds': [], 'deletedAt': None,
         'kind': 'SERIAL_SERVER', 'ip': '192.0.2.75'},
    ])
    created_task = client.post('/api/v1/tasks', headers={'Idempotency-Key': 'shared-rebind-task'}, json={
        'name': '共享改绑任务', 'resourceId': 'shared-network', 'protocol': 'TELNET_SERIAL',
        'ip': '192.0.2.75', 'port': 10002,
    })
    assert created_task.status_code == 201, created_task.text
    owned_task = created_task.json()
    as_user(client, 'bob')
    rejected = client.patch(f'/api/v1/tasks/{owned_task["id"]}', json={
        'version': owned_task['version'], 'serialServerResourceId': 'private-serial', 'ip': '192.0.2.75',
    })
    assert rejected.status_code == 403, rejected.text
    stored = client.portal.call(client.app.state.repo.db.tasks.find_one, {'id': owned_task['id']})
    assert stored['resourceId'] == 'shared-network' and stored.get('serialServerResourceId') is None

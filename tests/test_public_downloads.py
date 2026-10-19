"""缺陷附件链接无需登录，下载仍遵守制品发布与保留状态。"""
from datetime import timedelta

import pytest
from camera_logs.common.database import now
from fastapi.responses import Response
from test_api import client  # noqa: F401


@pytest.mark.parametrize('credentials', [{}, {'Authorization': 'Bearer expired-token'}])
@pytest.mark.parametrize('kind', ['downloads', 'coredumps', 'coredump-exports'])
def test_attachment_content_is_public_even_with_stale_credentials(client, monkeypatch, kind, credentials):  # noqa: F811
    """匿名和残留无效凭据均可读取内容，Range参数完整传给节点。"""
    repo = client.app.state.repo
    collections = {'downloads': 'jobs', 'coredumps': 'coredump_files', 'coredump-exports': 'coredump_exports'}
    document = {'id': 'attachment', 'kind': 'DOWNLOAD', 'taskId': 'task', 'resourceId': 'removed-resource',
                'actor': 'other-user', 'nodeId': 'node', 'coordinatorNodeId': 'node',
                'status': 'FROZEN' if kind == 'coredumps' else 'SUCCEEDED', 'snapshot': {'path': 'private'},
                'resultPath': 'private', 'expiresAt': now() + timedelta(hours=1)}
    client.portal.call(repo.db[collections[kind]].insert_one, document)

    async def proxy(_repo, _node, _path, range_header=None, if_range=None):
        assert range_header == 'bytes=0-3'
        assert if_range == '"version"'
        return Response(b'core', status_code=206)

    monkeypatch.setattr('camera_logs.logs.api.proxy_file', proxy)
    monkeypatch.setattr('camera_logs.coredumps.api.proxy_file', proxy)
    client.headers.pop('Authorization', None)
    client.cookies.clear()
    response = client.get(f'/api/v1/{kind}/attachment/content', headers=credentials | {
        'Range': 'bytes=0-3', 'If-Range': '"version"'})
    assert response.status_code == 206, response.text
    assert response.content == b'core'


def test_public_download_bypasses_platform_ip_policy_only_for_content(client, monkeypatch):  # noqa: F811
    """精确内容路由免IP策略，目录和相似路径仍执行既有策略。"""
    from fastapi import HTTPException
    async def blocked(*_args):
        raise HTTPException(403, '测试来源被拒绝')
    monkeypatch.setattr('camera_logs.access_policy.policy.enforce_ip', blocked)
    client.headers.pop('Authorization', None)
    for kind in ('downloads', 'coredumps', 'coredump-exports'):
        assert client.get(f'/api/v1/{kind}/missing/content').status_code == 404
        assert client.get(f'/api/v1/{kind}/missing/content/extra').status_code == 403
        assert client.get(f'/api/v1/{kind}/missing').status_code == 403


def test_public_download_keeps_artifact_state_validation(client):  # noqa: F811
    """过期和未完成文件不因免身份校验变成可下载，原文读取仍需登录。"""
    repo = client.app.state.repo
    for identifier, status, expires in [('pending', 'RUNNING', now() + timedelta(hours=1)),
                                         ('expired', 'SUCCEEDED', now() - timedelta(hours=1))]:
        client.portal.call(repo.db.jobs.insert_one, {'id': identifier, 'kind': 'DOWNLOAD', 'status': status,
            'taskId': 'task', 'resultPath': 'private', 'expiresAt': expires})
    client.headers.pop('Authorization', None)
    assert client.get('/api/v1/downloads/pending/content').status_code == 409
    assert client.get('/api/v1/downloads/expired/content').status_code == 410
    assert client.get('/api/v1/log-files/private/content').status_code == 401


@pytest.mark.parametrize('kind,collection', [('coredumps', 'coredump_files'), ('coredump-exports', 'coredump_exports')])
def test_expired_coredump_attachment_never_reaches_file_proxy(client, monkeypatch, kind, collection):  # noqa: F811
    """维护尚未物理删除过期副本时，公开链接也不得重新取得该文件读者。"""
    repo = client.app.state.repo
    # MongoMock即时执行TTL；删除模拟TTL索引以重现真实维护延迟窗口。
    indexes = client.portal.call(repo.db[collection].index_information)
    for name, options in indexes.items():
        if "expireAfterSeconds" in options:
            client.portal.call(repo.db[collection].drop_index, name)
    client.portal.call(repo.db[collection].insert_one, {
        'id': 'expired-core', 'status': 'FROZEN' if kind == 'coredumps' else 'SUCCEEDED',
        'snapshot': {'path': 'private'}, 'expiresAt': now() - timedelta(seconds=1),
    })
    async def forbidden(*_args):
        pytest.fail('过期附件不应请求节点文件')
    monkeypatch.setattr('camera_logs.coredumps.api.proxy_file', forbidden)
    client.headers.pop('Authorization', None)
    assert client.get(f'/api/v1/{kind}/expired-core/content').status_code == 409

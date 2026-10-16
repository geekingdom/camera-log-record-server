"""旁路索引的失败、重放与阴性证据必须保守，不允许影响采集或漏掉原文。"""
# ruff: noqa: F811


import hashlib
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from camera_logs.common.config import Settings
from camera_logs.indexing.archive import archive_key, visit_archive_members
from camera_logs.indexing.checkpoints import Checkpoints
from camera_logs.indexing.client import bulk, checked, evidence, fingerprint
from camera_logs.indexing.ingest import source_path
from camera_logs.indexing.query import query_evidence
from camera_logs.indexing.runner import add_task_resource_ids, full_archive_group, index_archive_files
from camera_logs.logs.search_plan import plan_candidate_files
from test_api import client  # noqa: F401


def file_doc():
    """固定封存来源，用于检验版本与正文摘要绑定。"""
    return {"id": 'file', "taskId": 'task', "nodeId": 'worker', "runId": 'run', "sessionId": 'session',
            "status": 'READY', "bytes": 4, "sha256": hashlib.sha256(b'abc\n').hexdigest()}


def response(data, status=200):
    """模拟ES响应，并保留raise_for_status所需的请求信息。"""
    return httpx.Response(status, json=data, request=httpx.Request('GET', 'http://es'))


def test_bulk_partial_failure_does_not_look_successful():
    """HTTP200中的单项429仍必须失败；调用者不能提交检查点。"""
    class Client:
        def post(self, *args, **kwargs):
            return response({"errors": True, "items": [{"index": {"status": 201}}, {"index": {"status": 429}}]})
    with pytest.raises(ValueError, match='部分失败'):
        bulk(Client(), 'idx', [('a', {}), ('b', {})])


def test_partial_shard_or_timeout_is_unknown():
    """ES分片失败与超时不能成为无匹配证明。"""
    for result in ({"timed_out": True}, {"_shards": {"failed": 1}}):
        with pytest.raises(ValueError):
            checked(response(result))


def test_missing_chunk_revokes_manifest_negative_evidence():
    """完成证明仍在但正文块已丢失时必须全量回退。"""
    file = file_doc()
    proof = {"kind": 'manifest', "fileId": file['id'], "revision": fingerprint(file), "bytes": 4,
             "chunks": 2, "headHex": b'abc\n'.hex(), "tailHex": b'abc\n'.hex(), "hasNewline": True}
    class Client:
        def post(self, path, **kwargs):
            if path.endswith('_mget'):
                return response({"docs": [{"found": True, "_source": proof}]})
            return response({"aggregations": {"versions": {"buckets": [
                {"key": fingerprint(file), "doc_count": 1, "matches": {"doc_count": 0}}]}}})
    assert evidence(Client(), 'idx', [file], 'ERROR') == {}
    assert plan_candidate_files([file], {}, 'ERROR') == {'file'}


def test_wildcard_literal_is_escaped_and_source_identity_changes_version():
    """*和?是字面关键词而不是通配符，源文件修改必须生成不同版本。"""
    file = file_doc()
    proof = {"kind": 'manifest', "fileId": 'file', "revision": fingerprint(file), "bytes": 4,
             "chunks": 1, "headHex": '', "tailHex": '', "hasNewline": True}
    calls = []
    class Client:
        def post(self, path, **kwargs):
            calls.append(kwargs)
            if path.endswith('_mget'):
                return response({"docs": [{"found": True, "_source": proof}]})
            return response({"aggregations": {"versions": {"buckets": [
                {"key": fingerprint(file), "doc_count": 1, "matches": {"doc_count": 0}}]}}})
    result = evidence(Client(), 'idx', [file], '*?a')
    assert result['file']['mayMatch'] is False
    assert calls[-1]['json']['aggs']['versions']['aggs']['matches']['filter']['wildcard']['text']['value'] == '*\\*\\?a*'
    assert fingerprint(file) != fingerprint(file | {"sha256": 'changed'})


def test_checkpoint_persistence_and_contained_path(tmp_path):
    """重启检查点恢复、路径越界拒绝，不写入任何设备正文。"""
    path = tmp_path / 'state.sqlite'
    checkpoint = Checkpoints(path)
    checkpoint.save('revision', {"offset": 1024, "chunks": 1})
    checkpoint.close()
    checkpoint = Checkpoints(path)
    assert checkpoint.get('revision')['offset'] == 1024
    assert checkpoint.get('new')['offset'] == 0
    checkpoint.close()
    with pytest.raises(ValueError):
        source_path(Settings(log_root=tmp_path), {"path": '/etc/passwd'})


def test_archive_members_are_opened_once_in_stream_order_and_failures_continue(tmp_path, monkeypatch):
    """真实多成员 gzip 包只开一次；前成员失败后仍顺序读取后续成员并限速跳过数据。"""
    archive = tmp_path / 'hour.tar.gz'
    contents = {'skip.log': b'S' * 4096, 'first.log': b'first\n', 'broken.log': b'broken\n', 'last.log': b'last\n'}
    with tarfile.open(archive, 'w:gz') as output:
        for name, data in contents.items():
            member = tarfile.TarInfo(name); member.size = len(data)
            output.addfile(member, BytesIO(data))
    files = [
        {'id': name, 'path': str(archive), 'archiveMember': name, 'bytes': len(contents[name])}
        for name in ('first.log', 'broken.log', 'last.log')
    ]
    settings = SimpleNamespace(log_root=tmp_path)
    limited, opened, seen = [], 0, []
    monkeypatch.setattr('camera_logs.logs.archive_access.read_limiter.consume', limited.append)
    original = tarfile.open
    def count_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original(*args, **kwargs)
    def visitor(file, stream, size):
        data = stream.read()
        seen.append((file['id'], data))
        if file['id'] == 'broken.log':
            raise ValueError('synthetic member failure')
        return hashlib.sha256(data).hexdigest()
    with patch('camera_logs.indexing.archive.tarfile.open', count_open):
        results = visit_archive_members(settings, files, visitor)
    assert archive_key(settings, files[0]) == str(archive)
    assert opened == 1
    assert seen == [('first.log', contents['first.log']), ('broken.log', contents['broken.log']), ('last.log', contents['last.log'])]
    assert results['first.log'] == hashlib.sha256(contents['first.log']).hexdigest()
    assert isinstance(results['broken.log'], ValueError)
    assert results['last.log'] == hashlib.sha256(contents['last.log']).hexdigest()
    assert sum(limited) > archive.stat().st_size
    assert sum(limited) >= sum(len(data) for data in contents.values())
    with pytest.raises(httpx.ReadTimeout):
        visit_archive_members(settings, files, lambda *_args: (_ for _ in ()).throw(httpx.ReadTimeout('offline')))


async def test_runner_fills_resource_id_without_changing_source_fingerprint(client):
    """老目录只在送入ES前补任务资源，不改变来源版本和Mongo中的原始目录。"""
    repo = client.app.state.repo
    await repo.db.tasks.insert_one({'id': 'indexed-task', 'resourceId': 'resource-from-task'})
    file = file_doc() | {'taskId': 'indexed-task'}
    original = fingerprint(file)
    enriched = await add_task_resource_ids(repo.db, [file])
    assert enriched[0]['resourceId'] == 'resource-from-task'
    assert fingerprint(enriched[0]) == original
    assert 'resourceId' not in file


async def test_runner_expands_one_archive_across_regular_pages(client):
    """随机 ID 分页中首次遇包须取全体 49 个成员，避免每页重开同一 gzip 包。"""
    repo = client.app.state.repo
    path = '/logs/shared.tar.gz'
    await repo.db.tasks.insert_one({'id': 'archive-task', 'resourceId': 'archive-resource'})
    await repo.db.files.insert_many([
        file_doc() | {'id': f'{number:03d}', 'taskId': 'archive-task', 'nodeId': 'worker',
                      'path': path, 'archiveMember': f'{number:03d}.log'}
        for number in range(49)
    ])
    settings = SimpleNamespace(node_id='worker')
    group = await full_archive_group(repo.db, settings, {'path': path})
    assert len(group) == 49
    assert [file['id'] for file in group] == [f'{number:03d}' for number in range(49)]
    assert {file['resourceId'] for file in group} == {'archive-resource'}


def test_runner_does_not_open_fully_covered_archive(tmp_path, monkeypatch):
    """完成证明齐全的整个小时包跳过打开，避免轮询反复解压已覆盖成员。"""
    archive = tmp_path / 'covered.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo('covered.log'); member.size = 1
        output.addfile(member, BytesIO(b'x'))
    file = file_doc() | {'path': str(archive), 'archiveMember': 'covered.log'}
    settings = SimpleNamespace(log_root=tmp_path, elasticsearch_index='index')
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
    monkeypatch.setattr('camera_logs.indexing.runner.connection', lambda *_args: Connection())
    monkeypatch.setattr('camera_logs.indexing.runner.evidence', lambda *_args: {file['id']: {}})
    monkeypatch.setattr('camera_logs.indexing.runner.visit_archive_members', lambda *_args: pytest.fail('covered archive was opened'))
    assert index_archive_files(settings, [file]) == {file['id']: False}


async def test_es_outage_and_disabled_do_not_prevent_raw_search(monkeypatch):
    """检索服务故障只取消加速证据，原始文件仍是完整候选。"""
    from camera_logs.indexing import query
    def unavailable(*args):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(query, 'fetch_evidence', unavailable)
    assert await query_evidence(Settings(elasticsearch_url='http://es'), [file_doc()], 'ERROR') == ({}, 'UNAVAILABLE')
    assert await query_evidence(Settings(elasticsearch_url=''), [file_doc()], 'ERROR') == ({}, 'DISABLED')


async def test_coverage_never_claims_latest_hour_is_contiguous(client, monkeypatch):
    """中间小时未覆盖，最近索引时间不能冒充连续覆盖水位。"""
    from camera_logs.indexing import api
    repo = client.app.state.repo
    repo.settings.elasticsearch_url = 'http://es'
    for hour in range(3):
        await repo.db.files.insert_one(file_doc() | {"id": str(hour), "hour": f'2026-10-16T0{hour}:00:00+00:00'})
    async def proof(*args):
        return {'0': {}, '2': {}}, 'AVAILABLE'
    monkeypatch.setattr(api, 'query_evidence', proof)
    result = await api.coverage(repo, 'task', datetime(2026, 10, 16, tzinfo=UTC), datetime(2026, 10, 17, tzinfo=UTC))
    assert result['coveredFiles'] == 2 and not result['coverageComplete']
    assert result['contiguousCoveredThrough'].hour == 1
    assert result['latestIndexedHour'].hour == 2

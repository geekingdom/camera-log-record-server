"""已验证但未登记的小时包：重启补偿、版本校验、失败隔离与迟到水位保护。"""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.logs import maintenance
from camera_logs.logs.archive_receipts import acknowledge, load_verified, receipt_path, signature
from camera_logs.logs.compression import publish_hour_archive
from mongomock_motor import AsyncMongoMockClient


def published(root, name='one', *, corrupt=False):
    """使用生产发布器生成真实可校验gzip，模拟发布后立即退出而未触发目录回调。"""
    folder = root / name
    folder.mkdir()
    log, index = folder / 'part-000001.log', folder / 'part-000001.index.jsonl'
    data = b'complete log line\n' * 100
    rows = b'{"sequence":1,"offset":0,"length":1800,"receivedAt":"2026-10-13T10:00:00+00:00"}\n'
    log.write_bytes(data)
    index.write_bytes(rows)
    member = {'taskId': name, 'runId': 'run', 'sessionId': 'session', 'hourStart': '2026-10-13T10:00:00+00:00',
              'rawSize': len(data), 'sha256': 'bad' if corrupt else hashlib.sha256(data).hexdigest(),
              'firstSequence': 1, 'lastSequence': 1, 'logPath': str(log), 'logName': log.name,
              'indexName': index.name, 'indexBytes': len(rows), 'indexSha256': hashlib.sha256(rows).hexdigest()}
    target = folder / 'hour.tar.gz'
    publish_hour_archive([member], target, create_index=False)
    return target, member


def repository(root):
    """目录对账仅需要节点归属与真实异步Mongo接口替身。"""
    return SimpleNamespace(db=AsyncMongoMockClient().db, settings=SimpleNamespace(log_root=root, node_id='node'))


async def test_completed_archive_catalog_recovers_without_rehash_after_restart(tmp_path, monkeypatch):
    target, member = published(tmp_path)
    repo = repository(tmp_path)
    identifier = maintenance._archive_id(tmp_path, target.parent / member['logName'])
    await repo.db.files.insert_one({'id': identifier, 'nodeId': 'node', 'taskId': 'one', 'runId': 'run',
                                   'sessionId': 'session', 'status': 'OPEN'})
    monkeypatch.setattr(maintenance, '_members', lambda _: pytest.fail('已验证待办不应重复解压校验'))
    assert receipt_path(target).exists()
    await maintenance.pump_pending_archives(repo)
    result = await repo.db.files.find_one({'id': identifier})
    assert result['status'] == 'READY' and result['sha256'] == member['sha256']
    assert result['lastSequence'] == 1 and result['archiveBytes'] == target.stat().st_size
    assert not receipt_path(target).exists()


async def test_bad_receipt_does_not_block_later_hour_and_remains_visible(tmp_path):
    bad, _ = published(tmp_path, 'a-bad')
    good, _ = published(tmp_path, 'b-good')
    with bad.open('ab') as file:
        file.write(b'changed-after-verification')
    repo = repository(tmp_path)
    await maintenance.pump_pending_archives(repo)
    assert receipt_path(bad).exists() and not receipt_path(good).exists()
    assert await repo.db.files.count_documents({'taskId': 'b-good', 'status': 'READY'}) == 1
    assert await repo.db.files.count_documents({'taskId': 'a-bad'}) == 0
    from camera_logs.node.performance_runtime import archive_snapshot
    assert archive_snapshot()['diskPendingJobs'] >= 1


def test_failed_raw_checksum_never_publishes_receipt_or_deletes_raw(tmp_path):
    with pytest.raises(OSError, match='校验失败'):
        published(tmp_path, corrupt=True)
    assert (tmp_path / 'one' / 'part-000001.log').exists()
    assert not receipt_path(tmp_path / 'one' / 'hour.tar.gz').exists()


def test_index_change_invalidates_fast_verification(tmp_path):
    target, member = published(tmp_path)
    (target.parent / member['indexName']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='index version'):
        load_verified(target)


def test_acknowledge_cannot_delete_new_publication(tmp_path):
    target, _ = published(tmp_path)
    version = signature(receipt_path(target))
    document = json.loads(receipt_path(target).read_text())
    replacement = receipt_path(target).with_suffix('.new')
    replacement.write_text(json.dumps(document))
    replacement.replace(receipt_path(target))
    acknowledge(target, version)
    assert receipt_path(target).exists()


async def test_late_open_catalog_flush_cannot_overwrite_recovered_ready(tmp_path):
    import asyncio
    repo = repository(tmp_path)
    await repo.db.files.create_index('id', unique=True)
    identity = {'id': 'file', 'taskId': 'task', 'runId': 'run', 'sessionId': 'session', 'nodeId': 'node'}
    await repo.db.files.insert_one({**identity, 'status': 'READY', 'sha256': 'verified', 'bytes': 10})
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.catalog_lock = repo, asyncio.Lock()
    runtime.catalog_ready, runtime.paths = set(), {'file': 5}
    runtime.catalog_dirty = {'file': {**identity, 'status': 'OPEN', 'bytes': 5, 'firstSequence': 1}}
    await runtime._publish_catalog()
    result = await repo.db.files.find_one({'id': 'file'})
    assert result['status'] == 'READY' and result['bytes'] == 10 and result['sha256'] == 'verified'
    assert not runtime.catalog_dirty


async def test_continuous_writer_seals_all_previous_hours_and_catalog_recovers(tmp_path, monkeypatch):
    """同一会话跨18/19/20时，旧小时分片全部有持久目录补偿，当前小时仍写入。"""
    from camera_logs.logs import compression, storage
    from camera_logs.logs.storage import HourlyWriter
    monkeypatch.setattr(storage, 'MAX_LOG_BYTES', 32)
    repo = repository(tmp_path)
    writer = HourlyWriter('task', 'run', 'session', tmp_path, storage_identity='device')
    try:
        for hour in [10, 11, 12]:
            await writer.write(b'line-data\n' * 10, received_at=datetime(2026, 10, 13, hour, tzinfo=UTC))
        await compression._drain_dispatcher()
        await maintenance.pump_pending_archives(repo)
        assert await repo.db.files.count_documents({'status': 'READY'}) == 8
        assert {row['hour'] async for row in repo.db.files.find({})} == {
            '2026-10-13T10:00:00+00:00', '2026-10-13T11:00:00+00:00'}
    finally:
        await writer.close()


async def test_retention_cannot_delete_archive_with_unregistered_members(tmp_path):
    """旧READY成员不能让保留清理越过新发布目录待办并删除未登记成员。"""
    from datetime import timedelta

    from camera_logs.common.database import now
    target, _ = published(tmp_path)
    repo = repository(tmp_path)
    repo.settings.retention_days = 7
    repo.settings.log_retention_days = 7
    await repo.db.files.insert_one({'id': 'old-member', 'nodeId': 'node', 'status': 'READY',
                                   'hour': (now() - timedelta(days=8)).isoformat(), 'path': str(target)})
    result = await maintenance.apply_retention(repo)
    assert result['removed'] == 0
    assert target.exists() and receipt_path(target).exists()


async def test_slow_partial_catalog_batch_resumes_without_rewriting_completed_members(tmp_path, monkeypatch):
    """每轮预算只够更新一个成员时，持久READY进度仍能让整小时最终完成。"""
    import asyncio

    from camera_logs.logs import archive_catalog_recovery
    target, member = published(tmp_path)
    for number in [2, 3]:
        data = b'late segment\n'
        log = target.parent / f'part-{number:06d}.log'
        log.write_bytes(data)
        publish_hour_archive([{**member, 'logName': log.name, 'logPath': str(log),
                               'rawSize': len(data), 'sha256': hashlib.sha256(data).hexdigest()}],
                             target, create_index=False)
    repo = repository(tmp_path)
    repo.db = SimpleNamespace(files=repo.db.files)
    original = repo.db.files.update_one
    writes = []

    async def slow_update(query, update, **kwargs):
        await asyncio.sleep(.03)
        result = await original(query, update, **kwargs)
        writes.append(query['id'])
        return result

    monkeypatch.setattr(repo.db.files, 'update_one', slow_update)
    monkeypatch.setattr(archive_catalog_recovery, 'CATALOG_TIMEOUT_SECONDS', .05)
    for _ in range(4):
        await maintenance.pump_pending_archives(repo)
    assert await repo.db.files.count_documents({'status': 'READY'}) == 3
    assert len(writes) == len(set(writes)) == 3
    assert not receipt_path(target).exists()

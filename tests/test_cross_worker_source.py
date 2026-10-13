"""回环SSH源的业务暂停不应阻塞新连接登录横幅。"""

import asyncio
import importlib
from pathlib import Path

import asyncssh


async def test_paused_source_still_emits_accounted_login_banner(monkeypatch):
    """连接恢复前业务闸门关闭，横幅仍可确认ASH且纳入逐行完整性期望。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    source = importlib.import_module('cross_worker_ssh_resume_source').SshSource('local-test-only')
    source.emitting.clear()
    await source.start()
    try:
        async with asyncssh.connect('127.0.0.1', source.port, username='collector',
                                    password=source.password, known_hosts=None) as connection:
            process = await connection.create_process(encoding=None)
            line = await asyncio.wait_for(process.stdout.readline(), 2)
            assert b'BusyBox built-in shell (ash)' in line
            assert source.sent == [line]
            await asyncio.sleep(.25)
            assert source.sent == [line]
            source.resume_emitting()
            following = await asyncio.wait_for(process.stdout.readline(), 2)
            assert b'sourceSeq=000001 payload=live' in following
    finally:
        source.resume_emitting()
        await source.close()


async def test_archive_wait_does_not_accept_previous_run(tmp_path, monkeypatch):
    """同节点旧运行已READY不能提前满足新运行归档完成的等待条件。"""
    from mongomock_motor import AsyncMongoMockClient
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    verifier = importlib.import_module('verify_node_auto_failover')
    database = AsyncMongoMockClient().db
    archive = tmp_path / 'hour.tar.gz'
    archive.write_bytes(b'fixture')
    common = {'taskId': 'task', 'nodeId': 'node', 'path': str(archive)}
    await database.files.insert_many([
        dict(common, runId='old', status='READY'), dict(common, runId='new', status='OPEN')])
    waiter = asyncio.create_task(verifier.wait_files(database, 'task', 'node', run_id='new'))
    try:
        await asyncio.sleep(.05)
        assert not waiter.done()
        await database.files.update_one({'runId': 'new'}, {'$set': {'status': 'READY'}})
        result = await asyncio.wait_for(waiter, 2)
        assert [item['runId'] for item in result] == ['new']
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_silent_device_does_not_emit_banner_after_reconnect(monkeypatch):
    """完全静默场景下，新连接不能以登录横幅伪造持续输出。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    source = importlib.import_module('cross_worker_ssh_resume_source').SshSource('local-test-only')
    source.silence_device()
    await source.start()
    try:
        async with asyncssh.connect('127.0.0.1', source.port, username='collector',
                                    password=source.password, known_hosts=None) as connection:
            await connection.create_process(encoding=None)
            await asyncio.sleep(.25)
            assert not source.sent
    finally:
        source.resume_emitting()
        await source.close()

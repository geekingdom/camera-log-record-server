"""日志作业续租与取消必须覆盖整个文件线程生命周期。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.logs import job_execution


@pytest.mark.asyncio
async def test_lease_loss_cancels_execution_and_waits_for_cleanup(monkeypatch):
    """归属丢失不能遗留脱离监督的文件任务，收尾完成前不得释放执行槽。"""
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def renew(*_args):
        nonlocal calls
        calls += 1
        if calls > 1:
            await started.wait()
            return False
        return True

    async def execute(_repo, job):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert job['_execution_lost'] is True
            cleaning.set()
            await release.wait()
            return {'status': 'FAILED', 'error': 'WORKER_EXECUTION_LOST'}

    monkeypatch.setattr(job_execution, 'renew_job', renew)
    monkeypatch.setattr(job_execution, 'run_job', execute)
    monkeypatch.setattr(job_execution, 'JOB_RENEW_SECONDS', 0)
    job = {'id': 'job', 'executionToken': 'owner'}
    task = asyncio.create_task(job_execution.run_leased_job(SimpleNamespace(), job))
    await asyncio.wait_for(cleaning.wait(), 1)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert result['status'] == 'FAILED'
    assert '_execution_lost' not in job


@pytest.mark.asyncio
async def test_lost_initial_lease_never_starts_file_work(monkeypatch):
    """领取确认返回过晚或已被取消时，不再开始快照、压缩和扫描。"""
    async def renew(*_args):
        return False

    async def execute(*_args):
        pytest.fail('失去租约后不应执行文件操作')

    monkeypatch.setattr(job_execution, 'renew_job', renew)
    monkeypatch.setattr(job_execution, 'run_job', execute)
    result = await job_execution.run_leased_job(SimpleNamespace(), {'id': 'job'})
    assert result['status'] == 'FAILED'


@pytest.mark.asyncio
async def test_normal_completion_stops_lease_renewal(monkeypatch):
    """正常作业完成后，续租协程必须同步取消，不能延长已结束作业的生命周期。"""
    renewals = []

    async def renew(*_args):
        renewals.append(True)
        return True

    async def execute(*_args):
        return {'status': 'SUCCEEDED'}

    monkeypatch.setattr(job_execution, 'renew_job', renew)
    monkeypatch.setattr(job_execution, 'run_job', execute)
    assert await job_execution.run_leased_job(SimpleNamespace(), {'id': 'job'}) == {'status': 'SUCCEEDED'}
    assert renewals == [True]


@pytest.mark.asyncio
async def test_shutdown_repeated_cancel_still_waits_for_file_cleanup(monkeypatch):
    """关闭Worker的重复取消不能让文件清理线程失去所属协程。"""
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def renew(*_args):
        return True

    async def execute(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            return {'status': 'CANCELLED'}

    monkeypatch.setattr(job_execution, 'renew_job', renew)
    monkeypatch.setattr(job_execution, 'run_job', execute)
    task = asyncio.create_task(job_execution.run_leased_job(SimpleNamespace(), {'id': 'job'}))
    await started.wait()
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_worker_claims_job_with_lease_and_completes_it(tmp_path, monkeypatch):
    """通过真正Worker调度入口领取并完成，避免租约模块仅在独立单测中生效。"""
    from camera_logs.common.config import Settings
    from camera_logs.common.database import Repository, now
    from camera_logs.node.worker import Worker
    from cryptography.fernet import Fernet
    from mongomock_motor import AsyncMongoMockClient

    repo = Repository(AsyncMongoMockClient().db, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
        node_id='job-node', nfs_server_ip=''))
    await repo.initialize()
    await repo.db.jobs.insert_one({'id': 'queued', 'nodeId': 'job-node', 'status': 'QUEUED',
                                  'kind': 'SEARCH', 'files': [], 'createdAt': now()})
    worker = Worker(repo)

    async def search(_repo, job):
        current = await repo.db.jobs.find_one({'id': job['id']})
        assert current['workerInstanceId'] == worker.instance_id
        assert current['executionToken'] == job['executionToken']
        assert current['leaseUntil'] is not None
        return {'results': [], 'truncated': False}

    monkeypatch.setattr('camera_logs.logs.jobs._search', search)
    try:
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*worker.jobs), 2)
        result = await repo.db.jobs.find_one({'id': 'queued'})
        assert result['status'] == 'SUCCEEDED'
        assert await repo.db.audit.count_documents({'action': 'job_succeeded', 'targetId': 'queued'}) == 1
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_hung_renewal_cancels_file_execution_within_timeout(monkeypatch):
    """已连接Mongo的I/O悬挂也有独立超时，不能只依赖服务器选择超时。"""
    started, closed = asyncio.Event(), asyncio.Event()
    calls = 0

    async def renew(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        await started.wait()
        await asyncio.Event().wait()

    async def execute(_repo, job):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert job['_execution_lost'] is True
            closed.set()
            return {'status': 'FAILED'}

    monkeypatch.setattr(job_execution, 'renew_job', renew)
    monkeypatch.setattr(job_execution, 'run_job', execute)
    monkeypatch.setattr(job_execution, 'JOB_RENEW_SECONDS', 0)
    monkeypatch.setattr(job_execution, 'RENEW_TIMEOUT_SECONDS', .01)
    result = await asyncio.wait_for(job_execution.run_leased_job(SimpleNamespace(), {'id': 'hung'}), 1)
    assert closed.is_set() and result['status'] == 'FAILED'

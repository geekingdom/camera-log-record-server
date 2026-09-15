"""下载导出目录的写入关闭证据、读者租约与维护回收回归。"""

import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from camera_logs.common.database import now
from camera_logs.logs.export_locks import ExportLock
from camera_logs.logs.export_readers import ExportReader
from camera_logs.logs.maintenance import cleanup_exports
from test_maintenance import repository


async def _closed_download(repo, identifier, status="FAILED"):
    """登记一个已由本节点关闭写入但尚待维护回收的下载产物。"""
    await repo.db.jobs.insert_one({
        "id": identifier, "nodeId": "node", "kind": "DOWNLOAD", "status": status,
        "expiresAt": now() - timedelta(seconds=1), "completedAt": now() - timedelta(seconds=2),
        "outputExecutionState": "CLOSED", "outputWriterNodeId": "node",
        "outputWriterClosedAt": now() - timedelta(seconds=2), "outputLockProtocol": 1, "outputReaders": [],
    })


@pytest.mark.asyncio
async def test_active_reader_blocks_expired_failed_output_until_stream_closes(tmp_path):
    """已开始的慢下载必须持有租约，维护不能凭到期时间删除正在读取的文件。"""
    repo = repository(tmp_path)
    path = tmp_path / "exports" / "failed" / "result.tar.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"published")
    await _closed_download(repo, "failed", "SUCCEEDED")

    reader = await ExportReader(repo, "failed").acquire()
    assert await cleanup_exports(repo) == 0
    assert path.exists()

    await reader.close()
    # 首轮已越过受读者保护的候选；尾页空读只重置书签，下一轮才重新检查它。
    assert await cleanup_exports(repo) == 0
    assert await cleanup_exports(repo) == 1
    assert not path.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
async def test_closed_failed_or_cancelled_output_and_scratch_are_reclaimed(tmp_path, status):
    """失败和取消在所有文件线程退出后留下的发布/临时目录可由维护统一回收。"""
    repo = repository(tmp_path)
    output = tmp_path / "exports" / status.lower()
    scratch = tmp_path / "exports" / ".tmp" / status.lower()
    output.mkdir(parents=True)
    scratch.mkdir(parents=True)
    await _closed_download(repo, status.lower(), status)

    assert await cleanup_exports(repo) == 1
    assert not output.exists() and not scratch.exists()


@pytest.mark.asyncio
async def test_cleanup_keeps_terminal_output_without_writer_closed_proof(tmp_path):
    """旧 Worker 崩溃后缺少关闭证据时，即使作业已失败也必须保留目录。"""
    repo = repository(tmp_path)
    output = tmp_path / "exports" / "unknown-writer"
    output.mkdir(parents=True)
    await repo.db.jobs.insert_one({
        "id": "unknown-writer", "nodeId": "node", "kind": "DOWNLOAD", "status": "FAILED",
        "expiresAt": now() - timedelta(seconds=1), "completedAt": now() - timedelta(seconds=2),
        "outputExecutionState": "WRITING", "outputWriterNodeId": "node",
    })

    assert await cleanup_exports(repo) == 0
    assert output.exists()


@pytest.mark.asyncio
async def test_closed_terminal_scratch_without_published_directory_is_reclaimed(tmp_path):
    """压缩尚未发布即失败时，仅存的临时目录也要按相同关闭证据回收。"""
    repo = repository(tmp_path)
    scratch = tmp_path / "exports" / ".tmp" / "scratch-only"
    scratch.mkdir(parents=True)
    await _closed_download(repo, "scratch-only")

    assert await cleanup_exports(repo) == 1
    assert not scratch.exists()


@pytest.mark.asyncio
async def test_cleanup_claim_rechecks_node_identity_after_candidate_page_read(tmp_path, monkeypatch):
    """候选页读取后作业改派时，CAS 不能按过期快照删除旧节点目录。"""
    repo = repository(tmp_path)
    output = tmp_path / "exports" / "moved"
    output.mkdir(parents=True)
    await _closed_download(repo, "moved")
    repo.db = SimpleNamespace(jobs=repo.db.jobs, nodes=repo.db.nodes)
    original = repo.db.jobs.update_one
    reassigned = False

    async def update_after_page(query, update, *args, **kwargs):
        nonlocal reassigned
        if not reassigned and "$pull" in update:
            reassigned = True
            await original({"id": "moved"}, {"$set": {"nodeId": "other-node"}})
        return await original(query, update, *args, **kwargs)

    monkeypatch.setattr(repo.db.jobs, "update_one", update_after_page)

    assert await cleanup_exports(repo) == 0
    assert reassigned and output.exists()


@pytest.mark.asyncio
async def test_kernel_lock_recovery_waits_for_live_writer_then_reclaims_killed_process(tmp_path):
    """真实子进程持 flock 时维护不得删除；终止进程后内核释放锁才允许恢复关闭。"""
    repo = repository(tmp_path)
    output = tmp_path / "exports" / "killed-writer"
    output.mkdir(parents=True)
    await repo.db.jobs.insert_one({
        "id": "killed-writer", "nodeId": "node", "kind": "DOWNLOAD", "status": "FAILED",
        "expiresAt": now() - timedelta(seconds=1), "completedAt": now() - timedelta(seconds=2),
        "outputExecutionState": "WRITING", "outputWriterNodeId": "node", "outputLockProtocol": 1,
    })
    lock_path = tmp_path / "exports" / ".locks" / "killed-writer.lock"
    lock_path.parent.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)", str(lock_path)], stdout=subprocess.PIPE, text=True)  # noqa: ASYNC220 - 验证必须使用独立 OS 写入进程。
    try:
        assert child.stdout and child.stdout.readline().strip() == "locked"
        assert await cleanup_exports(repo) == 0
        assert output.exists()
        child.terminate()
        child.wait(timeout=5)
        # 被内核锁拒绝的候选同样在完整扫描结束后从头重试。
        assert await cleanup_exports(repo) == 0
        assert await cleanup_exports(repo) == 1
        assert not output.exists()
        recovered = await repo.db.jobs.find_one({"id": "killed-writer"})
        assert recovered["outputWriterClosureReason"] == "KERNEL_LOCK_RECOVERY"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_cleanup_recovers_expired_cleaning_claim_on_a_later_bounded_page(tmp_path):
    """过期 CLEANING 由有界页恢复；第 101 个候选必须留给后续维护轮。"""
    repo = repository(tmp_path)
    for index in range(101):
        identifier = f"page-{index:03d}"
        await _closed_download(repo, identifier)
        if index == 100:
            path = tmp_path / "exports" / identifier
            path.mkdir(parents=True)
            await repo.db.jobs.update_one({"id": identifier}, {"$set": {
                "outputCleanupState": "CLEANING", "outputCleanupToken": "abandoned",
                "outputCleanupLeaseUntil": now() - timedelta(seconds=1),
            }})
    assert await cleanup_exports(repo) == 0
    assert (tmp_path / "exports" / "page-100").exists()

    assert await cleanup_exports(repo) == 1
    assert not (tmp_path / "exports" / "page-100").exists()
    # 第二轮处理尾页后仍保留书签，空页才在下一轮复位，避免每轮重扫尾页。
    assert await cleanup_exports(repo) == 0
    node = await repo.db.nodes.find_one({"id": "node"})
    assert node.get("exportCleanupCursor") is None


@pytest.mark.asyncio
async def test_export_lock_rejects_symlinked_locks_parent(tmp_path):
    """锁目录不能经软链接落到根外，防止 flock 协议文件被替换。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / ".locks").symlink_to(outside, target_is_directory=True)
    repo = SimpleNamespace(settings=SimpleNamespace(log_root=tmp_path))
    with pytest.raises(ValueError, match="symlink"):
        await ExportLock(repo, "job").acquire(blocking=False)

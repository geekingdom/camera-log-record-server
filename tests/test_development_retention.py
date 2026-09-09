"""验证受限开发归档清理不会扩大到真实日志或并发导出。"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs.maintenance import apply_retention
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(root):
    """建立仅用于开发清理回归的内存目录和目录服务。"""
    return Repository(AsyncMongoMockClient().db, Settings(
        log_root=root, node_id="development-node", retention_days=7,
        encryption_key=Fernet.generate_key().decode(),
    ))


async def add_stopped_development_task(repo, identifier):
    """登记已随测试资源删除而永久停止的任务，满足开发清理的终态条件。"""
    await repo.db.tasks.insert_one({
        "id": identifier,
        "resourceDeleted": True,
        "status": "STOPPED",
        "desiredState": "STOPPED",
        "nodeId": None,
    })


async def add_archive_file(repo, root, identifier, task_id, *, archive="archive.tar.gz", **fields):
    """创建已发布的临时归档和对应 catalog，不触及真实日志根。"""
    path = root / archive
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(identifier.encode())
    document = {
        "id": identifier,
        "taskId": task_id,
        "nodeId": "development-node",
        "status": "READY",
        "hour": now().isoformat(),
        "path": str(path),
    }
    document.update(fields)
    await repo.db.files.insert_one(document)
    return path


async def test_development_allowlist_skips_natural_age_but_leaves_unselected_archive(tmp_path):
    """已证明的近期开发文件可清理，另一个未选归档不受本次调用影响。"""
    repo = repository(tmp_path)
    await add_stopped_development_task(repo, "selected-task")
    selected = await add_archive_file(repo, tmp_path, "selected", "selected-task", archive="selected.tar.gz")
    real = await add_archive_file(repo, tmp_path, "real", "real-task", archive="real.tar.gz")

    result = await apply_retention(repo, development_file_ids=frozenset({"selected"}))

    assert result == {"removed": 1, "skipped": 0, "failures": 0}
    assert not selected.exists()
    assert await repo.db.files.find_one({"id": "selected"}) is None
    assert real.exists()
    assert await repo.db.files.find_one({"id": "real"}) is not None


async def test_empty_development_allowlist_deletes_nothing(tmp_path):
    """空白名单不能退化为常规 retention 扫描，即使文件已经自然过期。"""
    repo = repository(tmp_path)
    path = await add_archive_file(
        repo, tmp_path, "old-unselected", "old-task", archive="old.tar.gz",
        hour=(now() - timedelta(days=30)).isoformat(),
    )

    result = await apply_retention(repo, development_file_ids=frozenset())

    assert result == {"removed": 0, "skipped": 0, "failures": 0}
    assert path.exists()
    assert await repo.db.files.find_one({"id": "old-unselected"}) is not None


async def test_development_cleanup_preserves_future_hour(tmp_path):
    """开发入口绕过天数但不扩大到未来小时，时钟或目录异常保留待核查。"""
    repo = repository(tmp_path)
    await add_stopped_development_task(repo, "future-task")
    path = await add_archive_file(repo, tmp_path, "future", "future-task",
                                  hour=(now() + timedelta(minutes=30)).isoformat())
    assert (await apply_retention(repo, development_file_ids=frozenset({"future"})))["removed"] == 0
    assert path.exists()


@pytest.mark.parametrize(("field", "value"), [
    ("resourceDeleted", False),
    ("status", "ERROR"),
    ("desiredState", "RUNNING"),
    ("nodeId", "development-node"),
])
async def test_development_cleanup_requires_deleted_unowned_stopped_task(tmp_path, field, value):
    """资源删除标记、停止态、停止意图和无节点归属缺一不可。"""
    repo = repository(tmp_path)
    task = {
        "id": "candidate-task",
        "resourceDeleted": True,
        "status": "STOPPED",
        "desiredState": "STOPPED",
        "nodeId": None,
    }
    task[field] = value
    await repo.db.tasks.insert_one(task)
    path = await add_archive_file(repo, tmp_path, "candidate", "candidate-task")

    result = await apply_retention(repo, development_file_ids=frozenset({"candidate"}))

    assert result == {"removed": 0, "skipped": 1, "failures": 0}
    assert path.exists()
    assert await repo.db.files.find_one({"id": "candidate", "status": "READY"}) is not None


async def test_development_cleanup_keeps_future_protection_and_active_job(tmp_path):
    """未来保护或活动导出任一存在时，开发白名单也不得删除归档。"""
    repo = repository(tmp_path)
    await add_stopped_development_task(repo, "protected-task")
    await add_stopped_development_task(repo, "job-task")
    protected = await add_archive_file(
        repo, tmp_path, "protected", "protected-task", archive="protected.tar.gz",
        retainUntil=now() + timedelta(hours=1),
    )
    active = await add_archive_file(repo, tmp_path, "active", "job-task", archive="active.tar.gz")
    await repo.db.jobs.insert_one({"status": "RUNNING", "files": [{"id": "active"}]})

    result = await apply_retention(repo, development_file_ids=frozenset({"protected", "active"}))

    assert result == {"removed": 0, "skipped": 2, "failures": 0}
    assert protected.exists() and active.exists()
    assert await repo.db.files.count_documents({"status": "READY"}) == 2


async def test_development_cleanup_cas_rejects_protection_added_after_preflight(tmp_path):
    """作业在检查后写入 retainUntil 时，领取 CAS 必须放弃物理删除。"""
    repo = repository(tmp_path)
    await add_stopped_development_task(repo, "candidate-task")
    path = await add_archive_file(repo, tmp_path, "candidate", "candidate-task")
    repo.db = SimpleNamespace(files=repo.db.files, jobs=repo.db.jobs, tasks=repo.db.tasks)

    async def protect_before_claim(*_args, **_kwargs):
        await repo.db.files.update_one({"id": "candidate"}, {"$set": {"retainUntil": now() + timedelta(hours=1)}})

    repo.db.jobs.find_one = AsyncMock(side_effect=protect_before_claim)
    result = await apply_retention(repo, development_file_ids=frozenset({"candidate"}))

    assert result == {"removed": 0, "skipped": 1, "failures": 0}
    assert path.exists()
    assert await repo.db.files.find_one({"id": "candidate", "status": "READY", "retainUntil": {"$gt": now()}})


async def test_development_cleanup_skips_shared_group_with_unselected_member(tmp_path):
    """共享 tar 中只要有一个成员不在白名单，整组归档及选中成员都必须保留。"""
    repo = repository(tmp_path)
    await add_stopped_development_task(repo, "selected-task")
    await add_stopped_development_task(repo, "unselected-task")
    shared = await add_archive_file(repo, tmp_path, "selected", "selected-task", archive="shared.tar.gz")
    await add_archive_file(repo, tmp_path, "unselected", "unselected-task", archive="shared.tar.gz")

    result = await apply_retention(repo, development_file_ids=frozenset({"selected"}))

    assert result == {"removed": 0, "skipped": 1, "failures": 0}
    assert shared.exists()
    assert await repo.db.files.count_documents({"path": str(shared), "status": "READY"}) == 2

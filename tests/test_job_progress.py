"""验证日志搜索和下载作业按冻结片段推进可见进度。"""

import asyncio
from datetime import UTC, datetime

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import jobs
from camera_logs.logs.jobs import run_job
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def _repository(tmp_path):
    """构造只使用临时目录和内存数据库的节点仓库。"""
    repo = Repository(
        AsyncMongoMockClient().db,
        Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node"),
    )
    await repo.initialize()

    async def audit(*_):
        return None

    repo.audit = audit
    return repo


@pytest.mark.parametrize("kind", ["SEARCH", "DOWNLOAD"])
async def test_run_job_persists_intermediate_fragment_progress_then_marks_success(tmp_path, monkeypatch, kind):
    """首片段完成后即可读到进度，只有成功终态才写入 100。"""
    repo = await _repository(tmp_path)
    files = []
    for number in (1, 2):
        path = tmp_path / f"part-{number}.log"
        path.write_bytes(b"needle\n")
        document = {
            "id": f"f{number}",
            "path": str(path),
            "status": "OPEN",
            "bytes": path.stat().st_size,
            "nodeId": "node",
        }
        await repo.db.files.insert_one(document)
        files.append({"id": document["id"], "status": "OPEN", "bytes": document["bytes"]})
    job = {
        "id": f"progress-{kind.lower()}",
        "kind": kind,
        "status": "RUNNING",
        "files": files,
        "keyword": "needle",
        "start": datetime(2026, 9, 7, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
    }
    await repo.db.jobs.insert_one(job)
    original_archive = jobs._archive
    second_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def pause_before_second_archive(*args, **kwargs):
        nonlocal calls
        result = await original_archive(*args, **kwargs)
        calls += 1
        if calls == 2:
            second_started.set()
            await release.wait()
        return result

    monkeypatch.setattr(jobs, "_archive", pause_before_second_archive)
    running = asyncio.create_task(run_job(repo, job))
    await second_started.wait()
    intermediate = await repo.db.jobs.find_one({"id": job["id"]})
    assert intermediate["status"] == "RUNNING"
    assert 0 < intermediate["progress"] < 100
    release.set()
    assert (await running)["status"] == "SUCCEEDED"
    finished = await repo.db.jobs.find_one({"id": job["id"]})
    assert finished["progress"] == 100


async def test_failed_job_preserves_partial_progress_below_one_hundred(tmp_path, monkeypatch):
    """第二个片段出错后，失败作业保留已完成片段的进度而不伪造完成。"""
    repo = await _repository(tmp_path)
    for number in (1, 2):
        path = tmp_path / f"failed-{number}.log"
        path.write_bytes(b"needle\n")
        await repo.db.files.insert_one({
            "id": f"failed-{number}", "path": str(path), "status": "OPEN",
            "bytes": path.stat().st_size, "nodeId": "node",
        })
    job = {
        "id": "failed-progress", "kind": "SEARCH", "status": "RUNNING",
        "files": [{"id": "failed-1", "status": "OPEN", "bytes": 7}, {"id": "failed-2", "status": "OPEN", "bytes": 7}],
        "keyword": "needle", "start": datetime(2026, 9, 7, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
    }
    await repo.db.jobs.insert_one(job)
    original_archive = jobs._archive
    calls = 0

    async def fail_second_archive(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second fragment failure")
        return await original_archive(*args, **kwargs)

    monkeypatch.setattr(jobs, "_archive", fail_second_archive)
    assert (await run_job(repo, job))["status"] == "FAILED"
    stored = await repo.db.jobs.find_one({"id": job["id"]})
    assert 0 < stored["progress"] < 100


async def test_cancelled_job_preserves_partial_progress_below_one_hundred(tmp_path, monkeypatch):
    """取消发生在第二片段前时，条件更新不再把作业或进度推进到完成。"""
    repo = await _repository(tmp_path)
    for number in (1, 2):
        path = tmp_path / f"cancelled-{number}.log"
        path.write_bytes(b"needle\n")
        await repo.db.files.insert_one({
            "id": f"cancelled-{number}", "path": str(path), "status": "OPEN",
            "bytes": path.stat().st_size, "nodeId": "node",
        })
    job = {
        "id": "cancelled-progress", "kind": "SEARCH", "status": "RUNNING",
        "files": [{"id": "cancelled-1", "status": "OPEN", "bytes": 7}, {"id": "cancelled-2", "status": "OPEN", "bytes": 7}],
        "keyword": "needle", "start": datetime(2026, 9, 7, tzinfo=UTC).isoformat(),
        "end": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
    }
    await repo.db.jobs.insert_one(job)
    original_archive = jobs._archive
    second_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def pause_before_second_archive(*args, **kwargs):
        nonlocal calls
        result = await original_archive(*args, **kwargs)
        calls += 1
        if calls == 2:
            second_started.set()
            await release.wait()
        return result

    monkeypatch.setattr(jobs, "_archive", pause_before_second_archive)
    running = asyncio.create_task(run_job(repo, job))
    await second_started.wait()
    await repo.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": "CANCELLED"}})
    release.set()
    assert (await running)["status"] == "CANCELLED"
    stored = await repo.db.jobs.find_one({"id": job["id"]})
    assert 0 < stored["progress"] < 100

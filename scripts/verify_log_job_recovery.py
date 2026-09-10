"""真实副本集验证日志作业 Worker 崩溃后的租约终态恢复，不访问设备或正式作业。"""

import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs.job_completion import complete_job
from camera_logs.logs.job_lease import WORKER_EXECUTION_LOST, claim_job, recover_expired_jobs
from pymongo import AsyncMongoClient

JOB_ID = "crashed-download"
NORMAL_JOB_ID = "normal-download"
CHILD_BOOTSTRAP = '''
import asyncio
import sys
from pathlib import Path

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import jobs
from camera_logs.logs.job_execution import run_leased_job
from camera_logs.logs.job_lease import claim_job
from pymongo import AsyncMongoClient


async def main():
    database_name, temporary_root = sys.argv[1:3]
    configured = Settings()
    settings = configured.model_copy(update={"database_name": database_name, "log_root": Path(temporary_root),
                                             "start_background": False})
    mongo = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
    try:
        repo = Repository(mongo[database_name], settings)
        job = await claim_job(repo, "crashed-worker")
        if job is None or job["id"] != "crashed-download":
            raise RuntimeError("子进程未领取预期的隔离作业")

        async def blocked_download(current_repo, current_job):
            partial = Path(current_repo.settings.log_root) / "exports" / current_job["id"] / "partial.bin"
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"job-recovery-partial")
            await asyncio.Event().wait()

        jobs._download = blocked_download
        await run_leased_job(repo, job)
        raise RuntimeError("被终止的隔离作业不应正常返回")
    finally:
        await mongo.close()


asyncio.run(main())
'''


def _settings(database_name: str, log_root: Path) -> Settings:
    """继承当前 Mongo 连接，只覆盖随机库和临时目录，不读取生产数据。"""
    configured = Settings()
    return configured.model_copy(update={"database_name": database_name, "log_root": log_root,
                                         "start_background": False})


async def _wait_for_running(repo, partial: Path, process) -> dict:
    """轮询隔离库直到真实领取、令牌和本地测试片段均已可见，子进程提前退出即失败。"""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = await repo.db.jobs.find_one({"id": JOB_ID})
        if (current and current.get("status") == "RUNNING" and current.get("executionToken")
                and current.get("workerInstanceId") == "crashed-worker" and partial.is_file()):
            return current
        if process.returncode is not None:
            raise RuntimeError(f"子进程在领取前退出 code={process.returncode}")
        await asyncio.sleep(.05)
    raise TimeoutError("未在限定时间观察到隔离作业的领取和测试片段")


async def _stop_child(process) -> None:
    """终止模拟崩溃子进程；父进程只在确认退出后才开始租约恢复。"""
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 10)
    if process.returncode == 0:
        raise RuntimeError("模拟崩溃子进程异常地正常退出")


async def main() -> None:
    """在随机库完成崩溃恢复与后续正常作业闭环，finally 删除临时库和目录。"""
    configured = Settings()
    database_name = "camera_job_recovery_" + uuid4().hex
    if database_name == configured.database_name:
        raise RuntimeError("随机验证数据库不能等于当前配置数据库")
    temporary = TemporaryDirectory(prefix="camera-job-recovery-")
    temporary_root = Path(temporary.name)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True)
    process = None
    removed_database = removed_directory = False
    try:
        settings = _settings(database_name, temporary_root)
        repo = Repository(mongo[database_name], settings)
        await repo.initialize()
        await repo.db.jobs.insert_one({
            "id": JOB_ID, "kind": "DOWNLOAD", "status": "QUEUED", "nodeId": settings.node_id,
            "actor": "recovery-verifier", "createdAt": now(),
        })
        partial = temporary_root / "exports" / JOB_ID / "partial.bin"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", CHILD_BOOTSTRAP, database_name, str(temporary_root),
        )
        claimed = await _wait_for_running(repo, partial, process)
        await _stop_child(process)

        # 仅隔离库中的已死亡 token 被加速到期，避免脚本等待生产配置的 90 秒租约。
        expired = await repo.db.jobs.update_one(
            {"id": JOB_ID, "status": "RUNNING", "executionToken": claimed["executionToken"]},
            {"$set": {"leaseUntil": now() - timedelta(seconds=1)}},
        )
        if expired.matched_count != 1:
            raise RuntimeError("无法加速隔离作业租约到期")
        if await recover_expired_jobs(repo, all_nodes=True) != 1:
            raise RuntimeError("过期隔离作业未被恢复器终结")
        failed = await repo.db.jobs.find_one({"id": JOB_ID})
        if (failed.get("status") != "FAILED" or failed.get("error") != WORKER_EXECUTION_LOST
                or "executionToken" in failed or "leaseUntil" in failed):
            raise RuntimeError("崩溃恢复后的隔离作业终态不正确")
        if not partial.is_file() or await repo.db.audit.count_documents({"action": "job_failed", "targetId": JOB_ID}) != 1:
            raise RuntimeError("恢复审计或测试片段保护不符合预期")
        if await recover_expired_jobs(repo, all_nodes=True) != 0:
            raise RuntimeError("终态隔离作业被错误地再次恢复")

        await repo.db.jobs.insert_one({
            "id": NORMAL_JOB_ID, "kind": "DOWNLOAD", "status": "QUEUED", "nodeId": settings.node_id,
            "actor": "recovery-verifier", "createdAt": now(),
        })
        normal = await claim_job(repo, "healthy-worker")
        if normal is None or normal["id"] != NORMAL_JOB_ID:
            raise RuntimeError("后续隔离作业无法正常领取")
        completed = await complete_job(repo, normal, {"status": "SUCCEEDED", "completedAt": now()})
        if completed.get("status") != "SUCCEEDED" or await repo.db.audit.count_documents(
                {"action": "job_succeeded", "targetId": NORMAL_JOB_ID}) != 1:
            raise RuntimeError("后续隔离作业无法正常完成")
    finally:
        child_stop_error = None
        if process is not None and process.returncode is None:
            try:
                await _stop_child(process)
            except asyncio.CancelledError as error:  # 父进程自身取消也不能跳过隔离数据的清理。
                child_stop_error = error
            except Exception as error:  # noqa: BLE001 - 子进程收尾异常不能跳过隔离库和目录清理。
                child_stop_error = error
        try:
            await mongo.drop_database(database_name)
            removed_database = database_name not in await mongo.list_database_names()
        finally:
            try:
                await mongo.close()
            finally:
                temporary.cleanup()
                removed_directory = not temporary_root.exists()
        if child_stop_error is not None:
            raise child_stop_error
    if not removed_database or not removed_directory:
        raise RuntimeError("隔离验证数据清理不完整")
    print(json.dumps({
        "passed": True,
        "realChildClaim": True,
        "acceleratedLeaseExpiry": True,
        "workerExecutionLost": True,
        "partialArtifactRetained": True,
        "noReplay": True,
        "subsequentClaimAndCompletion": True,
        "temporaryDatabaseRemoved": removed_database,
        "temporaryDirectoryRemoved": removed_directory,
    }))


if __name__ == "__main__":
    asyncio.run(main())

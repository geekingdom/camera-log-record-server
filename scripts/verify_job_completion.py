"""独立真实副本集验证作业终态审计、取消竞争和提交确认丢失。"""

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.job_completion import complete_job
from camera_logs.logs.job_submission import cancel_job
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


async def main():
    """只生成临时作业元数据，验证完成后删除测试库，不启动设备连接。"""
    settings = Settings()
    mongo = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
    name = "job_completion_" + uuid4().hex
    try:
        with TemporaryDirectory(prefix="job-completion-") as directory:
            repo = Repository(mongo[name], settings.model_copy(update={"log_root": directory}))
            original = repo.audit
            for status in ("SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"):
                job = {"id": status, "kind": "DOWNLOAD", "status": "RUNNING", "actor": "fixture"}
                await repo.db.jobs.insert_one(job.copy())
                update = {"status": status, "completedAt": datetime.now(UTC)}
                attempted = asyncio.Event()

                async def fail(*args, signal=attempted, **kwargs):
                    signal.set()
                    raise PyMongoError("injected audit failure")

                repo.audit = fail
                pending = asyncio.create_task(complete_job(repo, job, update))
                try:
                    await asyncio.wait_for(attempted.wait(), 5)
                    # 阻止重试，只验证第一次事务回滚；取消等待不会改变作业状态。
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
                    assert (await repo.db.jobs.find_one({"id": status}))["status"] == "RUNNING"
                    assert await repo.db.audit.count_documents({"targetId": status}) == 0
                finally:
                    repo.audit = original
                    if not pending.done():
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending
                await complete_job(repo, job, update)
                await complete_job(repo, job, update)
                assert await repo.db.audit.count_documents({"targetId": status}) == 1

            job = {"id": "lost-ack", "kind": "DOWNLOAD", "status": "RUNNING"}
            await repo.db.jobs.insert_one(job.copy())
            transaction = audited_mutations.mutation_transaction
            count = 0

            async def lost_ack(repo, callback):
                nonlocal count
                count += 1
                result = await transaction(repo, callback)
                if count == 1:
                    raise PyMongoError("lost acknowledgement")
                return result

            audited_mutations.mutation_transaction = lost_ack
            try:
                assert (await complete_job(repo, job, {"status": "SUCCEEDED"}))["status"] == "SUCCEEDED"
                assert count == 2
                assert await repo.db.audit.count_documents({"targetId": job["id"]}) == 1
            finally:
                audited_mutations.mutation_transaction = transaction

            for index in range(10):
                job = {"id": f"race-{index}", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "fixture"}
                await repo.db.jobs.insert_one(job.copy())
                result, _ = await asyncio.gather(complete_job(repo, job, {"status": "SUCCEEDED"}),
                                                cancel_job(repo, "fixture", job))
                current = await repo.db.jobs.find_one({"id": job["id"]})
                assert current["status"] == result["status"]
                assert await repo.db.audit.count_documents({"targetId": job["id"]}) == 1
    finally:
        await mongo.drop_database(name)
        assert name not in await mongo.list_database_names()
        await mongo.close()
    print(json.dumps({"terminalAuditRollback": True, "acknowledgementRecovery": True,
                      "cancelCompetition": True, "temporaryDatabaseRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())

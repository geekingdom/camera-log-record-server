"""为日志下载和检索作业提供节点执行租约与崩溃终态恢复。"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id

JOB_LEASE_SECONDS = 90
JOB_RENEW_SECONDS = 15
WORKER_EXECUTION_LOST = "WORKER_EXECUTION_LOST"
WORKER_EXECUTION_LOST_MESSAGE = "执行 Worker 的租约已过期，无法确认作业结果"

logger = logging.getLogger(__name__)


async def claim_job(repo: Any, instance_id: str) -> dict | None:
    """原子领取当前节点最早的队列作业，并登记不可复用的本次执行令牌。"""
    timestamp = now()
    token = new_id()
    return await repo.db.jobs.find_one_and_update(
        {"nodeId": repo.settings.node_id, "status": "QUEUED"},
        {"$set": {"status": "RUNNING", "executionToken": token, "workerInstanceId": instance_id,
                  "leaseUntil": timestamp + timedelta(seconds=JOB_LEASE_SECONDS), "startedAt": timestamp}},
        sort=[("createdAt", 1), ("id", 1)], return_document=ReturnDocument.AFTER,
    )


async def renew_job(repo: Any, job: dict) -> bool:
    """仅由持有未到期令牌的本节点执行实例续租，迟到续租不会复活遗留作业。"""
    token = job.get("executionToken")
    instance_id = job.get("workerInstanceId")
    if not token or not instance_id:
        return False
    timestamp = now()
    renewed = await repo.db.jobs.update_one(
        {"id": job.get("id"), "nodeId": repo.settings.node_id, "status": "RUNNING",
         "executionToken": token, "workerInstanceId": instance_id, "leaseUntil": {"$gt": timestamp}},
        {"$set": {"leaseUntil": timestamp + timedelta(seconds=JOB_LEASE_SECONDS)}},
    )
    return renewed.matched_count == 1


async def _recover_expired_job(repo: Any, job: dict, timestamp) -> bool:
    """在事务内用原令牌与原租约终结单个遗留作业，避免覆盖并发续租。"""
    token, lease_until = job.get("executionToken"), job.get("leaseUntil")
    if not token or lease_until is None:
        return False
    condition = {"id": job["id"], "nodeId": job.get("nodeId"), "status": "RUNNING",
                 "executionToken": token, "leaseUntil": lease_until}

    async def commit(session):
        current = await repo.db.jobs.find_one(condition, session=session)
        if current is None:
            return False
        changed = await repo.db.jobs.update_one(
            condition,
            {"$set": {"status": "FAILED", "error": WORKER_EXECUTION_LOST,
                      "errorMessage": WORKER_EXECUTION_LOST_MESSAGE, "completedAt": timestamp},
             "$unset": {"executionToken": "", "leaseUntil": ""}},
            session=session,
        )
        if changed.matched_count != 1:
            return False
        await repo.audit(current.get("actor", "system"), "job_failed", current["id"], session=session)
        return True

    return await audited_mutations.mutation_transaction(repo, commit)


async def recover_expired_jobs(repo: Any, *, all_nodes: bool = False) -> int:
    """终结已知过期租约；默认本节点，API 维护可显式扫描全节点。"""
    timestamp = now()
    query = {
        "status": "RUNNING",
        "executionToken": {"$exists": True, "$ne": ""},
        "leaseUntil": {"$exists": True, "$lte": timestamp},
    }
    if not all_nodes:
        query["nodeId"] = repo.settings.node_id
    cursor = repo.db.jobs.find(query)
    recovered = 0
    async for job in cursor:
        try:
            recovered += int(await _recover_expired_job(repo, job, timestamp))
        except Exception:
            logger.exception("日志作业崩溃恢复失败 id=%s", job.get("id"))
    return recovered


async def recovery_loop(repo: Any) -> None:
    """API 后台定期扫描所有节点遗留租约；单轮故障不得终止后续崩溃恢复。"""
    while True:
        try:
            await recover_expired_jobs(repo, all_nodes=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("日志作业全节点崩溃恢复轮次失败")
        await asyncio.sleep(30)

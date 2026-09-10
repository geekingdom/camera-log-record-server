"""作业终态与审计共同提交，数据库故障期间保留已生成产物。"""

import asyncio
import logging

from camera_logs.common import audited_mutations
from camera_logs.common.database import now

logger = logging.getLogger(__name__)


def _lost_execution_ownership():
    """返回本执行器已失去作业归属的稳定终态，不写数据库或审计。"""
    return {"status": "FAILED", "error": "EXECUTION_OWNERSHIP_LOST"}


def _running_execution_filter(job, current, timestamp):
    """构造终态 CAS 条件，旧执行器不能覆盖新 token 或到期租约。

    未引入 token 的历史调用仍可完成未引入 token 的作业。只要数据库当前
    作业已有 token，即使调用方未携带 token 也必须拒绝，避免旧 Worker 迟到
    写入。token 所在运行还必须保持同一节点和有效租约。
    """
    current_token = current.get("executionToken")
    submitted_token = job.get("executionToken")
    if current_token is None and submitted_token is None:
        return {"id": job["id"], "status": "RUNNING", "executionToken": None}
    if (not current_token or current_token != submitted_token
            or current.get("nodeId") != job.get("nodeId")
            or current.get("leaseUntil") is None):
        return None
    return {
        "id": job["id"],
        "status": "RUNNING",
        "executionToken": submitted_token,
        "nodeId": job.get("nodeId"),
        "leaseUntil": {"$gt": timestamp},
    }


async def complete_job(repo, job, update):
    """仅重试数据库收尾，不重做文件操作；取消竞争只返回已确认的实际终态。

    状态和审计共享事务，重复尝试先读取当前状态，已提交终态不会再次写审计。
    确认丢失或数据库暂不可用时保留执行槽及产物，指数退避到三十秒。进程关闭
    的取消向上传播，不能在提交结果未知时删除产物或强行覆盖成失败。
    """
    async def commit(session):
        current = await repo.db.jobs.find_one({"id": job["id"]}, session=session)
        if current is None:
            return {"status": "CANCELLED"}
        if current["status"] != "RUNNING":
            if current["status"] == "SUCCEEDED":
                return {key: current[key] for key in update if key in current}
            return {"status": current["status"]}
        execution_filter = _running_execution_filter(job, current, now())
        if execution_filter is None:
            return _lost_execution_ownership()
        changed = await repo.db.jobs.update_one(execution_filter, {"$set": update}, session=session)
        if changed.matched_count != 1:
            return _lost_execution_ownership()
        await repo.audit(job.get("actor", "system"), "job_" + update["status"].lower(),
                         job["id"], session=session)
        return update

    delay = 1
    while True:
        try:
            return await audited_mutations.mutation_transaction(repo, commit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("作业终态提交未确认，保留产物并重试 id=%s status=%s", job["id"], update["status"])
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

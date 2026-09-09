"""作业终态与审计共同提交，数据库故障期间保留已生成产物。"""

import asyncio
import logging

from camera_logs.common import audited_mutations

logger = logging.getLogger(__name__)


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
        await repo.db.jobs.update_one({"id": job["id"], "status": "RUNNING"},
                                      {"$set": update}, session=session)
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

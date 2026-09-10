"""监督日志文件作业及其执行租约，确保失去归属后停止且等待文件线程收尾。"""

import asyncio
import logging

from camera_logs.logs.job_lease import JOB_RENEW_SECONDS, renew_job
from camera_logs.logs.jobs import run_job

logger = logging.getLogger(__name__)
RENEW_TIMEOUT_SECONDS = 5


async def _renew(repo, job):
    """仅活跃执行者续租；数据库异常视为无法确认归属，不继续无人监督的文件工作。"""
    while True:
        await asyncio.sleep(JOB_RENEW_SECONDS)
        if not await asyncio.wait_for(renew_job(repo, job), RENEW_TIMEOUT_SECONDS):
            return


async def _drain(task):
    """重复取消也必须等到底层文件线程结束，不能提早释放Worker执行槽。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            logger.exception("日志作业监督协程收尾失败")
            break
    return await asyncio.gather(task, return_exceptions=True)


async def run_leased_job(repo, job):
    """将文件操作和续租绑定；租约到期由独立恢复器写失败，不自动重做未知操作。"""
    try:
        active = await asyncio.wait_for(renew_job(repo, job), RENEW_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("日志作业执行前无法确认租约 id=%s", job['id'])
        active = False
    if not active:
        return {'status': 'FAILED', 'error': 'EXECUTION_OWNERSHIP_LOST'}
    execution = asyncio.create_task(run_job(repo, job))
    renewal = asyncio.create_task(_renew(repo, job))
    try:
        done, _ = await asyncio.wait({execution, renewal}, return_when=asyncio.FIRST_COMPLETED)
        if execution in done:
            return await execution
        job['_execution_lost'] = True
        try:
            await renewal
        except Exception:
            logger.exception("日志作业续租失败，停止文件执行 id=%s", job['id'])
        else:
            logger.warning("日志作业已失去执行归属 id=%s", job['id'])
        execution.cancel()
        await _drain(execution)
        return {'status': 'FAILED', 'error': 'EXECUTION_OWNERSHIP_LOST'}
    finally:
        renewal.cancel()
        if not execution.done():
            execution.cancel()
        await _drain(execution)
        await _drain(renewal)
        job.pop('_execution_lost', None)

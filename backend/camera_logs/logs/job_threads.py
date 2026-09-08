"""查询作业的线程生命周期：协程取消不能让后台文件操作脱离资源回收。"""

import asyncio
import contextvars
import logging
from functools import partial

_log = logging.getLogger(__name__)


async def job_thread(function, *args, stop=None, executor=None, **kwargs):
    """取消时通知协作式停止，并等待线程退出后再传播原始取消。

    Python 不能强制终止线程。快照、复制等不可中断操作必须先收尾；重复取消
    也不能提前释放目录或作业并发槽。正常执行时保留返回值和异常合同。
    """
    if executor is None:
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    else:
        context = contextvars.copy_context()
        worker = asyncio.get_running_loop().run_in_executor(
            executor, context.run, partial(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if stop is not None:
            stop.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - 取出后台异常后仍保留调用方的取消结果。
                break
        if not worker.cancelled():
            try:
                worker.result()
            except InterruptedError:
                pass
            except Exception:  # noqa: BLE001 - 后台收尾错误单独记录，不掩盖取消。
                _log.exception("作业取消期间后台文件操作失败")
        raise

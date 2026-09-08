"""节点内容读取与快照的有界执行池，避免解压和限速占满采集写盘线程。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from camera_logs.logs.job_threads import job_thread


class ReadThreads:
    """先异步取得执行槽再提交线程；请求取消仍等待文件句柄释放后归还执行槽。"""

    def __init__(self, workers: int = 4):
        self._slots = asyncio.Semaphore(workers)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="log-read")
        self._closed = False

    async def run(self, function, *args, **kwargs):
        """有限数量文件操作进入专用线程，排队协程不占用默认写入执行器。"""
        async with self._slots:
            if self._closed:
                raise RuntimeError("日志读取执行池已关闭")
            return await job_thread(function, *args, executor=self._executor, **kwargs)

    async def close(self):
        """停止接收新操作，等待已提交读取完成后回收专用线程。"""
        self._closed = True
        await job_thread(self._executor.shutdown, wait=True, cancel_futures=True)

"""节点文件读取生命周期：专用线程、归档句柄缓存及定期空闲回收统一关闭。"""

import asyncio
import logging
from time import perf_counter

from camera_logs.logs.archive_readers import ArchiveReaders
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.read_threads import ReadThreads

logger = logging.getLogger(__name__)


class FileReads:
    """归档状态只在线程中访问；缓存空闲句柄不会长期占用采集执行器。"""

    def __init__(self, *, idle_seconds=30):
        self._threads = ReadThreads()
        self._sources = ArchiveReaders(idle_seconds=idle_seconds)
        self._sweeper = None
        self._interval = min(5, idle_seconds)
        self._closed = False

    async def run(self, function, *args, **kwargs):
        """快照继续共用有界专用读取线程，不进入连续分页缓存。"""
        return await self._threads.run(function, *args, **kwargs)

    async def read(self, *args, timings=None):
        """首次请求启动清理协程；安装路由时尚无事件循环也能构造读取服务。"""
        if self._closed:
            raise RuntimeError("节点文件读取服务已关闭")
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep())
        queued = perf_counter()

        def read_source():
            """线程开始前的等待与实际文件操作分别计量，异常仍由原调用链传播。"""
            started = perf_counter()
            try:
                return self._sources.read(*args)
            finally:
                if timings is not None:
                    timings.update(queue=(started - queued) * 1000, io=(perf_counter() - started) * 1000)

        return await self.run(read_source)

    async def _sweep(self):
        while True:
            await asyncio.sleep(self._interval)
            # 回收只关闭空闲句柄，不做正文读写，不能排在长时间解压请求之后。
            try:
                await job_thread(self._sources.prune)
            except Exception:
                logger.exception("归档空闲句柄回收失败")

    async def close(self):
        """先结束周期清理和所有文件线程，再回收空闲流，取消不遗留后台句柄。"""
        self._closed = True
        try:
            if self._sweeper is not None:
                self._sweeper.cancel()
                await asyncio.gather(self._sweeper, return_exceptions=True)
        finally:
            try:
                await self._threads.close()
            finally:
                await job_thread(self._sources.close)

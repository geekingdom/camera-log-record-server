"""日志物理分片大小的平台配置读取与字节换算。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

MIB = 1024 * 1024
DEFAULT_LOG_SEGMENT_MIB = 10
MIN_LOG_SEGMENT_MIB = 10
MAX_LOG_SEGMENT_MIB = 100
LOG_SEGMENT_STEP_MIB = 5
REFRESH_INTERVAL_SECONDS = 1
logger = logging.getLogger(__name__)


def normalize_log_segment_mib(value: object) -> int:
    """将持久化分片配置归一为安全 MiB 值，历史损坏记录回退默认值。"""
    if (not isinstance(value, int) or isinstance(value, bool)
            or not MIN_LOG_SEGMENT_MIB <= value <= MAX_LOG_SEGMENT_MIB
            or (value - MIN_LOG_SEGMENT_MIB) % LOG_SEGMENT_STEP_MIB):
        return DEFAULT_LOG_SEGMENT_MIB
    return value


def log_segment_bytes(value: object) -> int:
    """将统一归一后的持久化配置转换为物理分片字节上限。"""
    return normalize_log_segment_mib(value) * MIB


async def configured_log_segment_bytes(repo: Any) -> int:
    """读取当前平台分片上限；下载作业开始时固定一次，兼容旧配置。"""
    document = await repo.db.platform_settings.find_one({"id": "platform"}) or {}
    return log_segment_bytes(document.get("logSegmentMiB"))


class LogSegmentSizeCache:
    """节点共享的分片上限缓存；写入锁只读取内存值，后台每秒同步平台配置。"""

    def __init__(self) -> None:
        self._bytes = log_segment_bytes(None)

    def current(self) -> int:
        """返回最近一次有效配置；数据库暂时不可达时继续使用最后有效值。"""
        return self._bytes

    async def refresh(self, repo: Any) -> int:
        """读取并验证共享设置，只有有效读完成后才替换节点当前上限。"""
        self._bytes = await configured_log_segment_bytes(repo)
        return self._bytes

    async def loop(self, repo: Any, *, interval_seconds: float = REFRESH_INTERVAL_SECONDS) -> None:
        """后台刷新跨进程配置；失败保留旧值并在下一秒继续尝试。"""
        while True:
            try:
                await self.refresh(repo)
            except Exception:
                logger.exception("日志分片大小刷新失败")
            await asyncio.sleep(interval_seconds)


def segment_size_cache(repo: Any) -> LogSegmentSizeCache:
    """将缓存绑定到节点 Repository，避免每个运行任务各自查询平台设置。"""
    cache = getattr(repo, "_log_segment_size_cache", None)
    if cache is None:
        cache = LogSegmentSizeCache()
        repo._log_segment_size_cache = cache
    return cache

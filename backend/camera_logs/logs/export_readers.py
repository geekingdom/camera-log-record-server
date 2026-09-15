"""日志导出产物的目录内读者租约。

导出文件由节点本地 Worker 生成，但下载可能经 API 长时间代理。读者 token
直接保存在作业记录中，清理器以同一记录的状态转换取得独占权，因而不会在
读者已领取到文件描述符后删除其目录。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, Self

from camera_logs.common.database import now

logger = logging.getLogger(__name__)
READER_LEASE_SECONDS = 90
READER_RENEW_SECONDS = 20
READER_RENEW_TIMEOUT_SECONDS = 5


class ExportReader:
    """持有一个成功下载导出的读者租约，关闭可安全重复调用。"""

    def __init__(self, repo: Any, job: dict[str, Any] | str) -> None:
        self.repo = repo
        self.job_id = job if isinstance(job, str) else job["id"]
        self.identifier = uuid.uuid4().hex
        self._closed = False
        self.invalidated = False
        self._deadline = 0.0
        self._heartbeat: asyncio.Task[None] | None = None

    async def acquire(self) -> ExportReader:
        """仅已关闭写入、尚未被清理器领取的成功导出可以开始读取。"""
        if self._heartbeat and not self._closed:
            return self
        stamp = now()
        await self.repo.db.jobs.update_one(
            {"id": self.job_id}, {"$pull": {"outputReaders": {"expiresAt": {"$lte": stamp}}}},
        )
        claimed = await self.repo.db.jobs.update_one(
            {
                "id": self.job_id,
                "nodeId": self.repo.settings.node_id,
                "kind": "DOWNLOAD",
                "status": "SUCCEEDED",
                "outputExecutionState": "CLOSED",
                "outputWriterNodeId": self.repo.settings.node_id,
                "outputCleanupState": {"$in": ["IDLE", None]},
            },
            {"$push": {"outputReaders": {"id": self.identifier,
                                         "expiresAt": stamp + timedelta(seconds=READER_LEASE_SECONDS)}}},
        )
        if claimed.matched_count != 1:
            raise RuntimeError("导出产物正在写入或回收")
        self._deadline = asyncio.get_running_loop().time() + READER_LEASE_SECONDS
        self._heartbeat = asyncio.create_task(self._renew_loop())
        return self

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(READER_RENEW_SECONDS)
            if not await self._renew_once():
                self.invalidated = True
                return

    async def _renew_once(self) -> bool:
        """在读取持续期间续期；租约无法确认时中断后续读取以避免误读新状态。"""
        try:
            renewed = await asyncio.wait_for(self.repo.db.jobs.update_one(
                {
                    "id": self.job_id,
                    "nodeId": self.repo.settings.node_id,
                    "outputExecutionState": "CLOSED",
                    "outputCleanupState": {"$in": ["IDLE", None]},
                    "outputReaders.id": self.identifier,
                },
                {"$set": {"outputReaders.$.expiresAt": now() + timedelta(seconds=READER_LEASE_SECONDS)}},
            ), READER_RENEW_TIMEOUT_SECONDS)
            if renewed.matched_count == 1:
                self._deadline = asyncio.get_running_loop().time() + READER_LEASE_SECONDS
            return renewed.matched_count == 1
        except Exception:
            logger.exception("导出读者租约续租失败 job=%s", self.job_id)
            return False

    def assert_active(self) -> None:
        """每个读取块前检查续租结果，失去清理互斥时立即终止响应。"""
        if self.invalidated or asyncio.get_running_loop().time() >= self._deadline:
            raise RuntimeError("导出读者租约已失效")

    async def close(self) -> None:
        """先停止心跳再移除自身 token，不影响其它并发读者。"""
        if self._closed:
            return
        self._closed = True
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self.repo.db.jobs.update_one(
            {"id": self.job_id, "nodeId": self.repo.settings.node_id},
            {"$pull": {"outputReaders": {"id": self.identifier}}},
        )

    async def __aenter__(self) -> Self:
        return await self.acquire()

    async def __aexit__(self, *_args) -> None:
        await self.close()

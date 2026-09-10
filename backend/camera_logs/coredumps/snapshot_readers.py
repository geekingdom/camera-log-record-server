"""冻结 coredump 的目录内读者租约，TTL 回收和下载使用同一 catalog 行 CAS。"""

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, Self

from camera_logs.common.database import now

logger = logging.getLogger(__name__)


class SnapshotReader:
    """持有一个 snapshot token 的短期读者租约；关闭操作可安全重复调用。"""

    def __init__(self, repo: Any, document: dict[str, Any], parent_id: str | None = None) -> None:
        self.repo, self.document = repo, document
        self.token = (document.get("snapshot") or {}).get("reservationToken")
        self.identifier = uuid.uuid4().hex
        self.parent_id = parent_id
        self._closed = False
        self.invalidated = False
        self._heartbeat: asyncio.Task[None] | None = None

    async def acquire(self) -> "SnapshotReader":
        """仅 FROZEN 的同一固定版本可取得读者 lease，退休后拒绝新读取。"""
        if not self.token:
            raise RuntimeError("coredump 快照不存在")
        if self._heartbeat and not self._closed:
            return self
        stamp = now()
        await self.repo.db.coredump_files.update_one(
            {"id": self.document["id"], "nodeId": self.document["nodeId"]}, {"$pull": {"snapshotReaders": {"expiresAt": {"$lte": stamp}}}},
        )
        state = {"status": "FROZEN"}
        if self.parent_id:
            state = {"$or": [{"status": "FROZEN"}, {"status": "RETIRING", "snapshotReaders": {"$elemMatch": {"id": self.parent_id, "expiresAt": {"$gt": stamp}}}}]}
        claimed = await self.repo.db.coredump_files.update_one(
            {"id": self.document["id"], "nodeId": self.document["nodeId"], "snapshot.reservationToken": self.token, **state},
            {"$push": {"snapshotReaders": {"id": self.identifier, "expiresAt": stamp + timedelta(seconds=90)}}},
        )
        if claimed.matched_count != 1:
            raise RuntimeError("coredump 快照正在回收")
        self._heartbeat = asyncio.create_task(self._renew_loop())
        return self

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not await self._renew_once():
                self.invalidated = True
                return

    async def _renew_once(self) -> bool:
        """child 已获取自身 lease 后独立续租；父 token 仅用于首次委托授权。"""
        try:
            renewed = await self.repo.db.coredump_files.update_one(
                {"id": self.document["id"], "nodeId": self.document["nodeId"], "snapshot.reservationToken": self.token,
                 "snapshotReaders.id": self.identifier, "status": {"$in": ["FROZEN", "RETIRING"]}},
                {"$set": {"snapshotReaders.$.expiresAt": now() + timedelta(seconds=90)}},
            )
            return renewed.matched_count == 1
        except Exception:
            logger.exception("coredump 读者租约续租失败 file=%s", self.document["id"])
            return False

    def assert_active(self) -> None:
        """消费者在每个长读块前调用；租约丢失后必须中止响应或导出。"""
        if self.invalidated:
            raise RuntimeError("coredump 读者租约已失效")

    async def close(self) -> None:
        """等待心跳退出后仅删除本 reader token，不影响并发下载者。"""
        if self._closed:
            return
        self._closed = True
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self.repo.db.coredump_files.update_one(
            {"id": self.document["id"], "nodeId": self.document["nodeId"], "snapshot.reservationToken": self.token},
            {"$pull": {"snapshotReaders": {"id": self.identifier}}},
        )

    async def __aenter__(self) -> Self:
        return await self.acquire()

    async def __aexit__(self, *_args) -> None:
        await self.close()

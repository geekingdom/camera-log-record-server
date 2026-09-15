"""同节点导出写入与回收共用的内核文件锁。"""
from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from typing import Any


class ExportLock:
    """持有 exports/.locks 下作业锁；进程死亡时内核自动释放 flock。"""

    def __init__(self, repo: Any, identifier: str) -> None:
        if not identifier or Path(identifier).name != identifier:
            raise ValueError("invalid export lock identifier")
        self.root = Path(repo.settings.log_root)
        self.path = self.root / "exports" / ".locks" / f"{identifier}.lock"
        self.descriptor: int | None = None

    def _prepare_parent(self) -> None:
        """逐级创建真实目录；锁目录或任一父级软链接都不能成为文件锁落点。"""
        current = self.root
        for child in (current, current / "exports", current / "exports" / ".locks"):
            if child.is_symlink():
                raise ValueError("export lock parent must not be a symlink")
            child.mkdir(exist_ok=True)
            if child.is_symlink() or not child.is_dir():
                raise ValueError("export lock parent is not a real directory")

    async def acquire(self, *, blocking: bool) -> bool:
        """以可取消的非阻塞轮询取得锁，描述符只在当前协程内创建和持有。"""
        self._prepare_parent()
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not blocking:
                        os.close(descriptor)
                        return False
                    await asyncio.sleep(.05)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return True

    async def close(self) -> None:
        """释放描述符；不删除锁文件，避免旧产物失去协议版本证明。"""
        if self.descriptor is not None:
            descriptor, self.descriptor = self.descriptor, None
            os.close(descriptor)

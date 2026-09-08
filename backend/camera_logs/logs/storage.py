"""按自然小时保存单路原始字节流，并校验后原子发布压缩归档。

写入锁保证同一任务的块序号和文件偏移连续；归档校验完成前绝不删除原始文件。
文件路径中包含 task/run/session，避免不同采集路的数据混写。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from camera_logs.logs.compression import compress
from camera_logs.logs.naming import safe_device_address, safe_filename_component


@dataclass(frozen=True, slots=True)
class HourArchive:
    task_id: str
    run_id: str
    session_id: str
    hour_start: datetime
    path: Path
    raw_size: int
    sha256: str
    first_sequence: int | None
    last_sequence: int | None

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["hour_start"] = self.hour_start.isoformat()
        value["path"] = str(self.path)
        return value


@dataclass(frozen=True, slots=True)
class ChunkPosition:
    sequence: int
    offset: int
    length: int
    path: Path


class HourlyWriter:
    """单任务写入器；异步锁使追加、跨小时轮转和水位更新保持原子性。"""

    def __init__(
        self,
        task_id: str,
        run_id: str,
        session_id: str,
        root: Path,
        *,
        task_name: str | None = None,
        device_ip: str | None = None,
        timezone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.task_id, self.run_id, self.session_id = task_id, run_id, session_id
        self.task_name = safe_filename_component(task_name or task_id)
        self.device_ip = safe_device_address(device_ip or "unknown")
        self.root = Path(root)
        self.zone = ZoneInfo(timezone)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._hour: datetime | None = None
        self._log: Path | None = None
        self._index: Path | None = None
        self._handle = None
        self._size = 0
        self._digest = hashlib.sha256()
        self._sequence = 0
        self._first_sequence: int | None = None
        self._last_sequence: int | None = None
        self._sealed: list[HourArchive] = []
        self._archive_tasks: set[asyncio.Task[HourArchive]] = set()
        self._archive_errors: list[Exception] = []
        self._last_sync = time.monotonic()
        self._durable_size = 0

    @property
    def active_path(self) -> Path | None:
        return self._log

    def snapshot(self) -> dict[str, object] | None:
        """返回当前已完成写入、可供实时读取的文件范围，不暴露未落盘队列。"""
        if self._log is None or self._hour is None:
            return None
        return {
            "path": str(self._log),
            "hourStart": self._hour.astimezone(UTC).isoformat(),
            "bytesWritten": self._size,
            "bytesDurable": self._durable_size,
            "lastSequence": self._last_sequence,
        }

    def drain_archives(self) -> list[HourArchive]:
        sealed, self._sealed = self._sealed, []
        return sealed

    def drain_archive_errors(self) -> list[Exception]:
        errors, self._archive_errors = self._archive_errors, []
        return errors

    @staticmethod
    def _hour_of(value: datetime, zone: ZoneInfo) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(zone).replace(minute=0, second=0, microsecond=0)

    def _base(self, hour: datetime) -> Path:
        return (
            self.root / self.task_id / self.run_id / self.session_id /
            f"{hour:%Y}" / f"{hour:%m}" / f"{hour:%d}" / f"{hour:%H}"
        )

    def _open_sync(self, hour: datetime) -> None:
        base = self._base(hour)
        base.mkdir(parents=True, exist_ok=True)
        # 同时识别新格式和历史 part-001 格式，避免升级后覆写既有分段。
        parts = []
        for path in base.iterdir():
            if not path.is_file():
                continue
            match = re.search(r"(?:^|-)part-(\d+)(?:\.|$)", path.name)
            if match:
                parts.append(int(match.group(1)))
        part = max(parts, default=0) + 1
        end = hour + timedelta(hours=1)
        stem = (
            f"{self.task_name}-{self.device_ip}-"
            f"{hour:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}-"
            f"{self.run_id[:8]}-{self.session_id[:8]}-part-{part:03d}"
        )
        self._log = base / f"{stem}.log"
        self._index = base / f"{stem}.index.jsonl"
        self._handle = self._log.open("ab", buffering=0)
        self._index.touch()
        self._hour = hour
        self._size = 0
        self._durable_size = 0
        self._last_sync = time.monotonic()
        self._digest = hashlib.sha256()
        self._first_sequence = self._last_sequence = None

    async def write(self, data: bytes, *, received_at: datetime | None = None) -> int:
        positions = await self.write_many([(data, received_at)])
        return positions[0].sequence if positions else self._sequence

    async def write_many(
        self, chunks: list[tuple[bytes, datetime | None]]
    ) -> list[ChunkPosition]:
        if not chunks:
            return []
        if any(not isinstance(data, bytes) for data, _ in chunks):
            raise TypeError("raw log chunks must be bytes")
        chunks = [(data, received_at) for data, received_at in chunks if data]
        if not chunks:
            return []
        async with self._lock:
            positions: list[ChunkPosition] = []
            grouped: list[tuple[bytes, int, int, datetime]] = []
            for data, received_at in chunks:
                instant = received_at or self._now()
                hour = self._hour_of(instant, self.zone)
                if self._hour is None:
                    await asyncio.to_thread(self._open_sync, hour)
                elif hour != self._hour:
                    # Bytes belonging to the old hour must reach disk before it is sealed.
                    if grouped:
                        await asyncio.to_thread(self._append_many_sync, grouped)
                        grouped = []
                    await self._close_locked(background=True)
                    await asyncio.to_thread(self._open_sync, hour)
                self._sequence += 1
                sequence, offset = self._sequence, self._size
                assert self._log is not None
                grouped.append((data, sequence, offset, instant))
                positions.append(ChunkPosition(sequence, offset, len(data), self._log))
                self._size += len(data)
                self._digest.update(data)
                self._first_sequence = self._first_sequence or sequence
                self._last_sequence = sequence
            await asyncio.to_thread(self._append_many_sync, grouped)
            return positions

    def _append_sync(self, data: bytes, sequence: int, offset: int, instant: datetime) -> None:
        assert self._handle is not None and self._index is not None
        view = memoryview(data)
        while view:
            count = self._handle.write(view)
            if count is None or count <= 0:
                raise OSError("log file did not accept a complete write")
            view = view[count:]
        entry = {
            "sequence": sequence,
            "offset": offset,
            "length": len(data),
            "receivedAt": instant.astimezone(UTC).isoformat(),
        }
        with self._index.open("a", encoding="utf-8") as index:
            index.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def _append_many_sync(self, entries: list[tuple[bytes, int, int, datetime]]) -> None:
        for data, sequence, offset, instant in entries:
            self._append_sync(data, sequence, offset, instant)
        if time.monotonic() - self._last_sync >= 1:
            self._sync_files()

    def _sync_files(self) -> None:
        """同步正文和索引后才推进持久化水位，故障时不宣称未落盘字节已持久化。"""
        if self._handle is None or self._index is None:
            return
        os.fsync(self._handle.fileno())
        with self._index.open("rb") as handle:
            os.fsync(handle.fileno())
        self._durable_size = self._size
        self._last_sync = time.monotonic()

    async def sync_due(self) -> None:
        """无新输入时也每秒推进持久化水位，由接收看门狗调用。"""
        async with self._lock:
            if time.monotonic() - self._last_sync >= 1:
                await asyncio.to_thread(self._sync_files)

    async def rotate(self, now: datetime | None = None) -> HourArchive | None:
        async with self._lock:
            archive = await self._close_locked(background=False)
            if now is not None:
                hour = self._hour_of(now, self.zone)
                await asyncio.to_thread(self._open_sync, hour)
            return archive

    async def close(self) -> HourArchive | None:
        async with self._lock:
            archive = await self._close_locked(background=False)
            await self._await_archives_locked()
            return archive

    async def _close_locked(self, *, background: bool = False) -> HourArchive | None:
        if self._handle is None:
            return None
        log, index, hour = self._log, self._index, self._hour
        size, digest = self._size, self._digest.hexdigest()
        first, last = self._first_sequence, self._last_sequence
        assert log is not None and index is not None and hour is not None
        await asyncio.to_thread(self._handle.flush)
        await asyncio.to_thread(self._sync_files)
        await asyncio.to_thread(self._handle.close)
        self._handle = None
        if size == 0:
            await asyncio.to_thread(log.unlink, missing_ok=True)
            await asyncio.to_thread(index.unlink, missing_ok=True)
            self._log = self._index = self._hour = None
            return None
        self._log = self._index = self._hour = None
        task = asyncio.create_task(
            self._archive(log, index, hour, size, digest, first, last)
        )
        if background:
            self._archive_tasks.add(task)
            task.add_done_callback(self._archive_done)
            return None
        return await task

    def _archive_done(self, task: asyncio.Task[HourArchive]) -> None:
        self._archive_tasks.discard(task)
        try:
            self._sealed.append(task.result())
        except Exception as error:  # noqa: BLE001 - 文件系统和压缩错误交由所有者告警。
            self._archive_errors.append(error)

    async def _await_archives_locked(self) -> None:
        if not self._archive_tasks:
            return
        tasks = tuple(self._archive_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._archive_tasks.difference_update(tasks)

    async def _archive(
        self,
        log: Path,
        index: Path,
        hour: datetime,
        size: int,
        digest: str,
        first: int | None,
        last: int | None,
    ) -> HourArchive:
        manifest = {
            "taskId": self.task_id,
            "runId": self.run_id,
            "sessionId": self.session_id,
            "hourStart": hour.astimezone(UTC).isoformat(),
            "rawSize": size,
            "sha256": digest,
            "firstSequence": first,
            "lastSequence": last,
        }
        final = await compress(log, index, manifest)
        return HourArchive(
            self.task_id,
            self.run_id,
            self.session_id,
            hour.astimezone(UTC),
            final,
            size,
            digest,
            first,
            last,
        )

"""按设备、任务和自然小时写入原始日志，并严格以 10 MiB 分卷。"""

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
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from camera_logs.logs.compression import compress_hour
from camera_logs.logs.naming import safe_device_address, safe_filename_component

MAX_PENDING_ARCHIVES = 2
MAX_LOG_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HourArchive:
    """一次分卷封存的回调；``path`` 指向共享小时包而非独立分卷包。"""
    task_id: str
    run_id: str
    session_id: str
    hour_start: datetime
    path: Path
    raw_size: int
    sha256: str
    first_sequence: int | None
    last_sequence: int | None
    log_path: Path | None = None
    member_name: str | None = None
    index_path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["hour_start"] = self.hour_start.isoformat()
        for key in ("path", "log_path", "index_path"):
            if value[key] is not None:
                value[key] = str(value[key])
        return value


@dataclass(frozen=True, slots=True)
class ChunkPosition:
    sequence: int
    offset: int
    length: int
    path: Path
    rollback_from: datetime | None = None
    source_index: int = 0
    source_offset: int = 0


@dataclass(frozen=True, slots=True)
class _Segment:
    log: Path
    index: Path
    hour: datetime
    size: int
    digest: str
    first: int | None
    last: int | None


class HourlyWriter:
    """单任务写入器；写入锁同时保护文件容量、序号、索引和小时切换。"""

    def __init__(self, task_id: str, run_id: str, session_id: str, root: Path, *, storage_identity: str,
                 task_name: str | None = None, device_ip: str | None = None,
                 timezone: str = "Asia/Shanghai", now: Callable[[], datetime] | None = None) -> None:
        self.task_id, self.run_id, self.session_id = task_id, run_id, session_id
        self.task_name = safe_filename_component(task_name or task_id)
        # 完整任务 ID 是同名任务的物理隔离键；目录组件保留其全部安全文本，
        # 任务名只占剩余字节，确保改名不会影响已经创建的写入器路径。
        self.task_id_component = safe_filename_component(task_id, max_utf8_bytes=64)
        name_bytes = 80 - len(self.task_id_component.encode("utf-8")) - 1
        self.task_directory = f"{safe_filename_component(task_name or task_id, max_utf8_bytes=name_bytes)}-{self.task_id_component}"
        self.device_ip = safe_device_address(device_ip or "unknown")
        self.storage_identity = safe_filename_component(storage_identity)
        self.root, self.zone, self._now = Path(root), ZoneInfo(timezone), now or (lambda: datetime.now(UTC))
        self._lock, self._hour, self._log, self._index, self._handle = asyncio.Lock(), None, None, None, None
        self._size, self._sequence, self._durable_size = 0, 0, 0
        self._digest = hashlib.sha256()
        self._first_sequence = self._last_sequence = None
        self._last_received_at: datetime | None = None
        self._pending: list[_Segment] = []
        self._sealed: list[HourArchive] = []
        self._archive_tasks: set[asyncio.Task[list[HourArchive]]] = set()
        self._archive_slots = asyncio.Semaphore(MAX_PENDING_ARCHIVES)
        self._archive_errors: list[Exception] = []
        self._last_sync, self._close_error = time.monotonic(), None

    @property
    def active_path(self) -> Path | None:
        return self._log

    def snapshot(self) -> dict[str, object] | None:
        if self._log is None or self._hour is None:
            return None
        return {"path": str(self._log), "hourStart": self._hour.astimezone(UTC).isoformat(),
                "bytesWritten": self._size, "bytesDurable": self._durable_size,
                "lastSequence": self._last_sequence}

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
        """返回新写入的浅层目录；历史 ``resources`` 目录由读取路径继续兼容。"""
        return self.root / self.storage_identity / self.task_directory / f"{hour:%Y-%m-%d}" / f"{hour:%H}"

    def _target(self, hour: datetime, base: Path) -> Path:
        existing = next(iter(sorted(base.glob("*.tar.gz"))), None)
        if existing is not None:
            return existing
        end = hour + timedelta(hours=1)
        return base / f"{self.task_name}-{self.device_ip}-{hour:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}.tar.gz"

    def _open_sync(self, hour: datetime) -> None:
        base = self._base(hour)
        base.mkdir(parents=True, exist_ok=True)
        parts = [int(match.group(1)) for path in base.glob("*.index.jsonl")
                 if (match := re.search(r"-part-(\d+)\.index\.jsonl$", path.name))]
        part = max(parts, default=0) + 1
        end = hour + timedelta(hours=1)
        while True:
            stem = f"{self.task_name}-{self.device_ip}-{hour:%Y%m%d%H%M%S}-{end:%Y%m%d%H%M%S}-part-{part:06d}"
            log, index = base / f"{stem}.log", base / f"{stem}.index.jsonl"
            try:
                descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                part += 1
                continue
            handle = os.fdopen(descriptor, "ab", buffering=0)
            try:
                index.touch(exist_ok=False)
            except FileExistsError:
                # 两个会话可在扫描后竞争同一编号；索引预留失败时撤销已独占的
                # 正文，不能泄漏句柄或把无索引正文误作下一次可恢复分卷。
                handle.close()
                log.unlink(missing_ok=True)
                part += 1
                continue
            except BaseException:
                handle.close()
                log.unlink(missing_ok=True)
                raise
            self._handle, self._log, self._index, self._hour = handle, log, index, hour
            break
        self._size = self._durable_size = 0
        self._digest = hashlib.sha256()
        self._first_sequence = self._last_sequence = None
        self._last_sync = time.monotonic()

    async def write(self, data: bytes, *, received_at: datetime | None = None) -> int:
        positions = await self.write_many([(data, received_at)])
        return positions[-1].sequence if positions else self._sequence

    async def write_many(self, chunks: list[tuple[bytes, datetime | None]]) -> list[ChunkPosition]:
        """整批独占写入；取消调用者也必须等真实I/O结束后才能释放文件锁。"""
        if any(not isinstance(data, bytes) for data, _ in chunks):
            raise TypeError("raw log chunks must be bytes")
        pending = asyncio.create_task(self._write_many_owned(chunks))
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        result = pending.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _write_many_owned(self, chunks: list[tuple[bytes, datetime | None]]) -> list[ChunkPosition]:
        """同小时未满分卷的常见批次合并I/O；轮转和回拨保留逐片边界。"""
        normalized = []
        for source_index, (data, received_at) in enumerate(chunks):
            if data:
                stamp = received_at or self._now()
                stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
                normalized.append((source_index, data, stamp))
        async with self._lock:
            if self._close_error:
                raise self._close_error
            try:
                if normalized:
                    hour = self._hour_of(normalized[0][2], self.zone)
                    if self._hour is None:
                        await asyncio.to_thread(self._open_sync, hour)
                    stamps = [entry[2] for entry in normalized]
                    if (self._hour == hour and all(self._hour_of(stamp, self.zone) == hour for stamp in stamps)
                            and all(a <= b for a, b in pairwise(stamps))
                            and (self._last_received_at is None or stamps[0] >= self._last_received_at)
                            and self._size + sum(len(entry[1]) for entry in normalized) <= MAX_LOG_BYTES):
                        return await asyncio.to_thread(self._append_batch_sync, normalized)
                return await self._write_rotating_locked(normalized)
            except Exception as error:
                # 部分写入不重试、不发布完整归档，保留原错误及原文件供受控恢复。
                self._close_error = error
                if self._handle is not None:
                    try:
                        await asyncio.to_thread(self._handle.close)
                    except Exception as close_error:
                        # 保持原始写入故障为主错误，同时保留句柄关闭失败供诊断。
                        raise error from close_error
                raise

    async def _write_rotating_locked(self, chunks):
        """调用方已持锁；只在跨小时、回拨或10MiB边界时逐片轮转。"""
        positions: list[ChunkPosition] = []
        for source_index, data, instant in chunks:
            rollback = self._last_received_at if self._last_received_at and instant < self._last_received_at else None
            hour = self._hour_of(instant, self.zone)
            if self._hour is None:
                await asyncio.to_thread(self._open_sync, hour)
            elif hour != self._hour or rollback:
                await self._seal_locked()
                await self._publish_pending_locked(background=True)
                await asyncio.to_thread(self._open_sync, hour)
            consumed = 0
            while consumed < len(data):
                if self._size == MAX_LOG_BYTES:
                    await self._seal_locked()
                    await asyncio.to_thread(self._open_sync, hour)
                count = min(MAX_LOG_BYTES - self._size, len(data) - consumed)
                piece = data[consumed:consumed + count]
                self._sequence += 1
                position = ChunkPosition(self._sequence, self._size, count, self._log, rollback if consumed == 0 else None,
                                         source_index, consumed)
                assert self._handle is not None and self._index is not None and self._log is not None
                await asyncio.to_thread(self._append_sync, piece, position, instant)
                positions.append(position)
                self._size += count
                self._digest.update(piece)
                self._first_sequence = self._first_sequence or position.sequence
                self._last_sequence = position.sequence
                if time.monotonic() - self._last_sync >= 1:
                    await asyncio.to_thread(self._sync_files)
                consumed += count
            self._last_received_at = instant
        return positions

    def _append_batch_sync(self, chunks) -> list[ChunkPosition]:
        """一次正文和一次索引追加保存多个源块，确认完成后再推进水位和摘要。"""
        assert self._handle is not None and self._index is not None
        positions, rows, offset = [], [], self._size
        for source_index, data, stamp in chunks:
            position = ChunkPosition(self._sequence + len(positions) + 1, offset, len(data), self._log,
                                     source_index=source_index)
            positions.append(position)
            rows.append(json.dumps({"sequence": position.sequence, "offset": offset, "length": len(data),
                                   "receivedAt": stamp.isoformat()}, separators=(",", ":")) + "\n")
            offset += len(data)
        payload = b"".join(data for _, data, _ in chunks)
        view = memoryview(payload)
        while view:
            written = self._handle.write(view)
            if not written:
                raise OSError("log file did not accept a complete write")
            view = view[written:]
        with self._index.open("a", encoding="utf-8") as index:
            index.write("".join(rows))
        self._size = offset
        self._sequence = self._last_sequence = positions[-1].sequence
        self._first_sequence = self._first_sequence or positions[0].sequence
        self._digest.update(payload)
        self._last_received_at = chunks[-1][2]
        if time.monotonic() - self._last_sync >= 1:
            self._sync_files()
        return positions

    def _append_sync(self, data: bytes, position: ChunkPosition, instant: datetime) -> None:
        assert self._handle is not None and self._index is not None
        view = memoryview(data)
        while view:
            written = self._handle.write(view)
            if not written:
                raise OSError("log file did not accept a complete write")
            view = view[written:]
        with self._index.open("a", encoding="utf-8") as index:
            index.write(json.dumps({"sequence": position.sequence, "offset": position.offset, "length": position.length,
                "receivedAt": instant.astimezone(UTC).isoformat()}, separators=(",", ":")) + "\n")

    def _sync_files(self) -> None:
        if self._handle is None or self._index is None:
            return
        os.fsync(self._handle.fileno())
        with self._index.open("rb") as index:
            os.fsync(index.fileno())
        self._durable_size, self._last_sync = self._size, time.monotonic()

    async def sync_due(self) -> None:
        async with self._lock:
            if self._close_error:
                raise self._close_error
            if time.monotonic() - self._last_sync >= 1:
                await asyncio.to_thread(self._sync_files)

    def _close_files_sync(self) -> None:
        assert self._handle is not None
        try:
            self._handle.flush()
            self._sync_files()
        finally:
            self._handle.close()

    async def _seal_locked(self) -> None:
        if self._close_error:
            raise self._close_error
        if self._handle is None:
            return
        log, index, hour = self._log, self._index, self._hour
        assert log is not None and index is not None and hour is not None
        try:
            await asyncio.to_thread(self._close_files_sync)
        except Exception as error:
            self._close_error = error
            raise
        self._handle = self._log = self._index = self._hour = None
        if self._size:
            self._pending.append(_Segment(log, index, hour, self._size, self._digest.hexdigest(),
                self._first_sequence, self._last_sequence))
        else:
            await asyncio.to_thread(log.unlink, missing_ok=True)
            await asyncio.to_thread(index.unlink, missing_ok=True)

    def _segment_manifest(self, segment: _Segment) -> dict:
        with segment.index.open("rb") as source:
            index_hash = hashlib.file_digest(source, "sha256").hexdigest()
        return {"taskId": self.task_id, "runId": self.run_id, "sessionId": self.session_id,
            "hourStart": segment.hour.astimezone(UTC).isoformat(), "rawSize": segment.size, "sha256": segment.digest,
            "firstSequence": segment.first, "lastSequence": segment.last, "logPath": str(segment.log),
            "logName": segment.log.name, "indexName": segment.index.name, "indexSha256": index_hash,
            "indexBytes": segment.index.stat().st_size}

    def _prepare_archive(self, segments: list[_Segment]) -> tuple[Path, list[dict]]:
        """在线程中扫描已有小时包并读取索引摘要，保持调用方传入的分卷顺序。"""
        target = self._target(segments[0].hour, self._base(segments[0].hour))
        return target, [self._segment_manifest(segment) for segment in segments]

    async def _publish_pending_locked(self, *, background: bool) -> list[HourArchive] | None:
        if not self._pending:
            return []
        await self._archive_slots.acquire()
        segments, self._pending = self._pending, []
        task = asyncio.create_task(self._archive(segments))
        if background:
            self._archive_tasks.add(task)
            task.add_done_callback(self._archive_done)
            return None
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # 线程准备不能随调用方取消；转为受跟踪后台任务，由后续收尾等待并发布结果。
            self._archive_tasks.add(task)
            task.add_done_callback(self._archive_done)
            raise
        except Exception:
            self._archive_slots.release()
            raise
        self._archive_slots.release()
        return result

    async def _archive(self, segments: list[_Segment]) -> list[HourArchive]:
        target, manifests = await asyncio.to_thread(self._prepare_archive, segments)
        await compress_hour(manifests, target)
        return [HourArchive(self.task_id, self.run_id, self.session_id, segment.hour.astimezone(UTC), target,
            segment.size, segment.digest, segment.first, segment.last, segment.log, segment.log.name, segment.index)
            for segment in segments]

    def _archive_done(self, task: asyncio.Task[list[HourArchive]]) -> None:
        self._archive_tasks.discard(task)
        self._archive_slots.release()
        try:
            self._sealed.extend(task.result())
        except Exception as error:  # noqa: BLE001 - 后台压缩错误在下次采集循环统一发布。
            self._archive_errors.append(error)

    async def _await_archives_locked(self) -> None:
        if self._archive_tasks:
            await asyncio.shield(asyncio.gather(*tuple(self._archive_tasks), return_exceptions=True))

    async def rotate(self, now: datetime | None = None) -> HourArchive | None:
        async with self._lock:
            await self._seal_locked()
            archives = await self._publish_pending_locked(background=False)
            if now is not None:
                await asyncio.to_thread(self._open_sync, self._hour_of(now, self.zone))
            if archives and len(archives) > 1:
                self._sealed.extend(archives[1:])
            return archives[0] if archives else None

    async def close(self) -> HourArchive | None:
        async with self._lock:
            await self._seal_locked()
            archives = await self._publish_pending_locked(background=False)
            await self._await_archives_locked()
            if archives and len(archives) > 1:
                self._sealed.extend(archives[1:])
            return archives[0] if archives else None

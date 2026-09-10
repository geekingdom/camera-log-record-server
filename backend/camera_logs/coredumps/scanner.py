"""Worker 对已登记海康设备 NFS 目录的有界轮转扫描。"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from camera_logs.common.database import now
from camera_logs.coredumps.safety import device_directory, regular_unlinked_file


def _scan_directory(
    settings: Any, resource: dict[str, Any], after: str, limit: int
) -> list[tuple[str, os.stat_result]]:
    """在线程内遍历设备目录，最多两层且拒绝链接和特殊文件。"""
    try:
        root = device_directory(settings, resource["ip"])
    except (FileNotFoundError, ValueError):
        return []
    # 用固定大小堆从任意目录遍历顺序中选择游标后的最小 N 项，避免 sorted() 将
    # 整个设备目录读入内存。完整目录扫描仍在线程中进行，API/采集心跳不等待它。
    values: list[tuple[str, str, os.stat_result]] = []
    # 栈只持有每层一个目录迭代器，深度固定为二，宽目录也不会积累 sibling 路径。
    try:
        pending = [(root, 0, os.scandir(root))]
    except (FileNotFoundError, PermissionError):
        return []
    while pending:
        _directory, depth, entries = pending[-1]
        try:
            entry = next(entries)
        except StopIteration:
            entries.close()
            pending.pop()
            continue
        except (FileNotFoundError, PermissionError):
            entries.close()
            pending.pop()
            continue
        if entry.name == ".snapshots" or entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False) and depth < 2:
            try:
                pending.append((Path(entry.path), depth + 1, os.scandir(entry.path)))
            except (FileNotFoundError, PermissionError):
                continue
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        # 设备会在任意子目录写同名标志，catalog 不应把它当作可下载 coredump。
        if path.name.lower() == "coredump_flag.cdf":
            continue
        if relative <= after:
            continue
        try:
            info = regular_unlinked_file(path)
        except (FileNotFoundError, ValueError):
            continue
        inverted = "".join(chr(0x10FFFF - ord(char)) for char in relative)
        candidate = (inverted, relative, info)
        if len(values) < limit:
            heapq.heappush(values, candidate)
        elif candidate[0] > values[0][0]:
            heapq.heapreplace(values, candidate)
    return [(relative, info) for _inverted, relative, info in sorted(values, key=lambda item: item[1])]


def _token(resource_id: str, name: str) -> str:
    return f"{resource_id}\x00{name}"


class CoredumpScanner:
    """轮转扫描目录并维护未冻结当前版本；扫描不复制、不宣称文件完成。"""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self._running = False

    async def scan_once(self) -> int:
        """每次最多处理配置数量，游标环形推进避免大目录长期饿死后段文件。"""
        if self._running:
            return 0
        self._running = True
        try:
            resources = [
                item
                async for item in self.repo.db.resources.find(
                    {
                        "kind": "HIKVISION_NETWORK",
                        "deletedAt": None,
                    },
                    {"id": 1, "ip": 1},
                )
            ]
            state = await self.repo.db.coredump_scan_state.find_one({"id": self.repo.settings.node_id}) or {}
            cursor = str(state.get("cursor", ""))
            discovered: list[tuple[dict[str, Any], str, os.stat_result]] = []
            remaining = int(self.repo.settings.coredump_scan_max_files)
            ordered = sorted(resources, key=lambda item: item["id"])
            cursor_resource, _, cursor_name = cursor.partition("\x00")
            pivot = next((index for index, item in enumerate(ordered) if item["id"] >= cursor_resource), 0)
            cursor_resource_has_entries = False
            for resource in ordered[pivot:] + ordered[:pivot]:
                if remaining <= 0:
                    break
                after = cursor_name if cursor_resource == resource["id"] else ""
                entries = await asyncio.to_thread(
                    _scan_directory, self.repo.settings, resource, after, remaining
                )
                discovered.extend((resource, name, info) for name, info in entries)
                remaining -= len(entries)
                if resource["id"] == cursor_resource and entries:
                    cursor_resource_has_entries = True
            # 当前资源已没有游标后的项才回绕，先保证其它资源有一次扫描机会。
            # 否则一个持续增长的大目录会在同一轮反复占满配额，延迟后续资源。
            if (
                remaining
                and cursor_resource
                and cursor_resource in {item["id"] for item in ordered}
                and not cursor_resource_has_entries
            ):
                resource = next(item for item in ordered if item["id"] == cursor_resource)
                entries = await asyncio.to_thread(
                    _scan_directory, self.repo.settings, resource, "", remaining
                )
                entries = [(name, info) for name, info in entries if name <= cursor_name]
                discovered.extend((resource, name, info) for name, info in entries)
            selected = discovered[: int(self.repo.settings.coredump_scan_max_files)]
            for resource, name, info in selected:
                await self._record(resource, name, info)
            if selected:
                await self.repo.db.coredump_scan_state.update_one(
                    {"id": self.repo.settings.node_id},
                    {"$set": {"cursor": _token(selected[-1][0]["id"], selected[-1][1]), "updatedAt": now()}},
                    upsert=True,
                )
            return len(selected)
        finally:
            self._running = False

    async def _record(self, resource: dict[str, Any], name: str, info: os.stat_result) -> None:
        """同一未冻结源版本只更新观测；已有快照遇变化时创建下一版本。"""
        source_key = hashlib.sha256(
            f"{self.repo.settings.node_id}\x00{resource['id']}\x00{name}\x00{info.st_dev}\x00{info.st_ino}".encode()
        ).hexdigest()
        fingerprint = {
            "inode": info.st_ino,
            "device": info.st_dev,
            "size": info.st_size,
            "mtimeNs": info.st_mtime_ns,
            "ctimeNs": info.st_ctime_ns,
        }
        current = await self.repo.db.coredump_files.find_one(
            {"sourceKey": source_key, "nodeId": self.repo.settings.node_id}, sort=[("version", -1)]
        )
        if current and current.get("snapshot") and current.get("source") != fingerprint:
            current = None
        if current is None:
            previous = (
                await self.repo.db.coredump_files.find_one(
                    {"nodeId": self.repo.settings.node_id, "resourceId": resource["id"], "name": name},
                    sort=[("version", -1)],
                    projection={"version": 1},
                )
                or {}
            )
            version = int(previous.get("version", 0)) + 1
            identifier = hashlib.sha256(f"{source_key}\x00{version}".encode()).hexdigest()[:32]
            timestamp = now()
            document = {
                "id": identifier,
                "nodeId": self.repo.settings.node_id,
                "resourceId": resource["id"],
                "deviceIp": resource["ip"],
                "name": name,
                "sourceKey": source_key,
                "source": fingerprint,
                "version": version,
                "status": "RECEIVING",
                "size": info.st_size,
                "receivedAt": timestamp,
                "firstSeenAt": timestamp,
                "sourceModifiedAt": datetime.fromtimestamp(info.st_mtime_ns / 1_000_000_000, UTC),
                "sourceState": "OBSERVING",
                "sourceObservedAt": timestamp,
                "sourceUnchangedSince": timestamp,
                "sourceStableAt": None,
                "updatedAt": timestamp,
            }
            await self.repo.db.coredump_files.insert_one(document)
            return
        timestamp = now()
        unchanged_since = current.get("sourceUnchangedSince")
        if current.get("source") != fingerprint:
            source_state, unchanged_since, stable_at = "CHANGING", timestamp, None
        else:
            unchanged_since = unchanged_since if isinstance(unchanged_since, datetime) else timestamp
            unchanged_since = unchanged_since.replace(tzinfo=UTC) if unchanged_since.tzinfo is None else unchanged_since
            if timestamp - unchanged_since >= timedelta(seconds=10):
                source_state = "STABLE"
                stable_at = current.get("sourceStableAt") or timestamp
            else:
                source_state = current.get("sourceState") if current.get("sourceState") in {"OBSERVING", "CHANGING"} else "OBSERVING"
                stable_at = None
        await self.repo.db.coredump_files.update_one(
            {"id": current["id"], "status": "RECEIVING", "snapshot": {"$exists": False}},
            {
                "$set": {
                    "source": fingerprint,
                    "size": info.st_size,
                    "sourceModifiedAt": datetime.fromtimestamp(info.st_mtime_ns / 1_000_000_000, UTC),
                    "sourceState": source_state,
                    "sourceObservedAt": timestamp,
                    "sourceUnchangedSince": unchanged_since,
                    "sourceStableAt": stable_at,
                    "updatedAt": timestamp,
                    "status": "RECEIVING",
                },
            },
        )

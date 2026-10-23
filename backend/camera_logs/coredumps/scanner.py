"""Worker 对已登记海康设备 NFS 目录的有界轮转扫描。"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.coredumps.events import record_discovery_event
from camera_logs.coredumps.resource_identity import (
    coredump_device_identity_document,
    legacy_source_key,
    stable_source_key,
)
from camera_logs.coredumps.safety import device_directory, regular_unlinked_file, source_path

logger = logging.getLogger(__name__)


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
                    {"id": 1, "ip": 1, "name": 1, "kind": 1, "model": 1, "subSerialNumber": 1, "version": 1},
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
            await self._probe_known_sources(state)
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
        legacy_key = legacy_source_key(
            self.repo.settings.node_id, resource["id"], name, info.st_dev, info.st_ino
        )
        source_key = stable_source_key(
            self.repo.settings.node_id, resource, name, info.st_dev, info.st_ino
        ) or legacy_key
        fingerprint = {
            "inode": info.st_ino,
            "device": info.st_dev,
            "size": info.st_size,
            "mtimeNs": info.st_mtime_ns,
            "ctimeNs": info.st_ctime_ns,
        }
        device_identity = coredump_device_identity_document(resource)
        current = await self.repo.db.coredump_files.find_one(
            {"sourceKey": {"$in": [source_key, legacy_key]}, "nodeId": self.repo.settings.node_id},
            sort=[("version", -1)],
        )
        if current and not self._catalog_identity_compatible(
            current, resource["id"], device_identity, source_key, legacy_key
        ):
            current = None
        if current and current.get("snapshot") and current.get("source") != fingerprint:
            current = None
        if current is None:
            timestamp = now()
            document = {
                "nodeId": self.repo.settings.node_id,
                "resourceId": resource["id"],
                "deviceIp": resource["ip"],
                "name": name,
                "sourceKey": source_key,
                "source": fingerprint,
                "status": "RECEIVING",
                "size": info.st_size,
                "receivedAt": timestamp,
                "firstSeenAt": timestamp,
                "sourceModifiedAt": datetime.fromtimestamp(info.st_mtime_ns / 1_000_000_000, UTC),
                "sourceState": "OBSERVING",
                "sourcePresent": True,
                "sourceObservedAt": timestamp,
                "sourceUnchangedSince": timestamp,
                "sourceStableAt": None,
                "updatedAt": timestamp,
            }
            if device_identity is not None:
                document["deviceIdentity"] = device_identity
            current, inserted = await self._insert_catalog_with_resource_fence(
                resource, document, source_key, legacy_key
            )
            if current is None:
                return
            if inserted:
                await self._record_discovery_event(resource, current)
                return
        timestamp = now()
        unchanged_since = current.get("sourceUnchangedSince")
        if current.get("source") != fingerprint:
            source_state, unchanged_since, stable_at = "CHANGING", timestamp, None
        else:
            unchanged_since = unchanged_since if isinstance(unchanged_since, datetime) else timestamp
            unchanged_since = (
                unchanged_since.replace(tzinfo=UTC) if unchanged_since.tzinfo is None else unchanged_since
            )
            if timestamp - unchanged_since >= timedelta(seconds=10):
                source_state = "STABLE"
                stable_at = current.get("sourceStableAt") or timestamp
            else:
                source_state = (
                    current.get("sourceState")
                    if current.get("sourceState") in {"OBSERVING", "CHANGING"}
                    else "OBSERVING"
                )
                stable_at = None
        await self.repo.db.coredump_files.update_one(
            {"id": current["id"], "status": "RECEIVING", "snapshot": {"$exists": False}},
            {
                "$set": {
                    "source": fingerprint,
                    "sourceKey": self._observed_source_key(current, device_identity, source_key, legacy_key),
                    "size": info.st_size,
                    "sourceModifiedAt": datetime.fromtimestamp(info.st_mtime_ns / 1_000_000_000, UTC),
                    "sourceState": source_state,
                    "sourcePresent": True,
                    "sourceObservedAt": timestamp,
                    "sourceUnchangedSince": unchanged_since,
                    "sourceStableAt": stable_at,
                    "updatedAt": timestamp,
                    "status": "RECEIVING",
                },
            },
        )
        await self._record_discovery_event(resource, current)

    async def _insert_catalog_with_resource_fence(
        self, resource: dict[str, Any], document: dict[str, Any], source_key: str, legacy_key: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """原子栅栏确认资源仍有效，令软删除与新目录登记不能交错留下旧归属。"""
        async def commit(session):
            device_identity = coredump_device_identity_document(resource)
            resource_filter = {"id": resource["id"], "kind": "HIKVISION_NETWORK", "deletedAt": None}
            if device_identity is not None:
                resource_filter.update(device_identity)
            else:
                resource_filter["version"] = resource.get("version")
            fenced = await self.repo.db.resources.update_one(
                resource_filter,
                {"$set": {"coredumpCatalogFenceAt": now()}}, session=session,
            )
            if not fenced.matched_count:
                return None, False
            existing = await self.repo.db.coredump_files.find_one(
                {"sourceKey": {"$in": [source_key, legacy_key]}, "nodeId": self.repo.settings.node_id},
                sort=[("version", -1)],
                session=session,
            )
            if existing is not None and not self._catalog_identity_compatible(
                existing, resource["id"], device_identity, source_key, legacy_key
            ):
                # 旧资源键可能属于认证前的其它物理设备。仅当它明确是 legacy
                # 键且新扫描拥有完整身份时，才为当前设备登记新的独立版本。
                if device_identity is None or existing.get("sourceKey") != legacy_key or source_key == legacy_key:
                    return None, False
                existing = None
            if existing is not None and not (
                existing.get("snapshot") and existing.get("source") != document["source"]
            ):
                return existing, False
            previous = await self.repo.db.coredump_files.find_one(
                {"nodeId": self.repo.settings.node_id, "resourceId": resource["id"], "name": document["name"]},
                sort=[("version", -1)], projection={"version": 1}, session=session,
            ) or {}
            version = int(previous.get("version", 0)) + 1
            catalog = document | {
                "id": hashlib.sha256(f"{source_key}\x00{version}".encode()).hexdigest()[:32],
                "version": version,
            }
            await self.repo.db.coredump_files.insert_one(catalog, session=session)
            return catalog, True

        return await audited_mutations.mutation_transaction(self.repo, commit)

    @staticmethod
    def _catalog_identity_compatible(catalog: dict[str, Any], resource_id: str,
                                     device_identity: dict[str, str] | None,
                                     source_key: str, legacy_key: str) -> bool:
        """拒绝用当前设备身份观测或升级未知/不同设备留下的 legacy 目录。"""
        recorded = catalog.get("deviceIdentity")
        if recorded is not None:
            return recorded == device_identity
        # 升级前同一资源已有的 legacy catalog 没有证据可推断出它属于另一设备，
        # 必须原地继续维护以避免全量重新发现；但不把它升级为当前认证身份的键。
        return catalog.get("resourceId") == resource_id and catalog.get("sourceKey") == legacy_key

    @staticmethod
    def _observed_source_key(catalog: dict[str, Any], device_identity: dict[str, str] | None,
                             source_key: str, legacy_key: str) -> str:
        """旧同资源 legacy 行保持原键，只有已证明身份一致的目录才升级稳定键。"""
        if catalog.get("deviceIdentity") is None and device_identity is not None \
                and catalog.get("sourceKey") == legacy_key:
            return legacy_key
        return source_key

    async def _record_discovery_event(self, resource: dict[str, Any], catalog: dict[str, Any]) -> None:
        """事件失败只保留未确认 catalog，后续观察可补写且不阻断其它目录登记。"""
        try:
            await record_discovery_event(self.repo, resource, catalog)
        except Exception:
            logger.exception("Coredump 发现事件写入失败 file=%s", catalog["id"])

    async def _probe_known_sources(self, state: dict[str, Any]) -> None:
        """有界复核已登记源路径；仅明确缺失才撤销最后已知存在状态。"""
        limit = int(self.repo.settings.coredump_scan_max_files)
        cursor = str(state.get("probeCursor", ""))
        query = {"nodeId": self.repo.settings.node_id, "sourcePresent": {"$ne": False}}
        candidates = [
            item
            async for item in self.repo.db.coredump_files.find({**query, "id": {"$gt": cursor}})
            .sort("id", 1)
            .limit(limit)
        ]
        if not candidates and cursor:
            candidates = [
                item async for item in self.repo.db.coredump_files.find(query).sort("id", 1).limit(limit)
            ]
        for catalog in candidates:
            present = await asyncio.to_thread(self._source_is_present, catalog)
            if present is not None:
                await self.repo.db.coredump_files.update_one(
                    {"id": catalog["id"]},
                    {"$set": {"sourcePresent": present, "updatedAt": now()}},
                )
        if candidates:
            await self.repo.db.coredump_scan_state.update_one(
                {"id": self.repo.settings.node_id},
                {"$set": {"probeCursor": candidates[-1]["id"], "updatedAt": now()}},
                upsert=True,
            )

    def _source_is_present(self, catalog: dict[str, Any]) -> bool | None:
        """验证一个历史路径仍是安全普通文件；不可达和权限异常不推断为文件已删除。"""
        try:
            # 先单独确认受控根和设备目录。它们不存在时无法区分 NFS 未挂载和
            # 设备文件删除，必须保留最后已知状态；只有其后的叶文件缺失才是删除。
            device_directory(self.repo.settings, catalog["deviceIp"])
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            return None
        try:
            regular_unlinked_file(source_path(self.repo.settings, catalog["deviceIp"], catalog["name"]))
        except FileNotFoundError:
            return False
        except (PermissionError, ValueError, OSError):
            return None
        return True

"""以已认证海康设备身份维持 Coredump 历史目录的连续性。"""

from __future__ import annotations

import hashlib
from typing import Any


def coredump_device_identity(resource: dict[str, Any]) -> tuple[str, str, str] | None:
    """仅接受完整且已认证的 IP、型号和子序列号，避免按 IP 误归并设备历史。"""
    if resource.get("kind") != "HIKVISION_NETWORK":
        return None
    values = (resource.get("ip"), resource.get("model"), resource.get("subSerialNumber"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return tuple(value.strip() for value in values)


def legacy_source_key(node_id: str, resource_id: str, name: str, device: int, inode: int) -> str:
    """计算早期按资源 ID 区分的扫描键，以便滚动升级时原地接续已有目录项。"""
    return hashlib.sha256(f"{node_id}\x00{resource_id}\x00{name}\x00{device}\x00{inode}".encode()).hexdigest()


def stable_source_key(node_id: str, resource: dict[str, Any], name: str, device: int, inode: int) -> str | None:
    """以认证设备身份计算扫描键；身份不完整时返回空以阻止不安全的跨资源复用。"""
    identity = coredump_device_identity(resource)
    if identity is None:
        return None
    ip, model, serial = identity
    return hashlib.sha256(f"{node_id}\x00{ip}\x00{model}\x00{serial}\x00{name}\x00{device}\x00{inode}".encode()).hexdigest()


def coredump_device_identity_document(resource: dict[str, Any]) -> dict[str, str] | None:
    """构造写入 catalog 的认证身份快照，防止资源日后换机时混认旧 Core。"""
    identity = coredump_device_identity(resource)
    if identity is None:
        return None
    ip, model, serial = identity
    return {"ip": ip, "model": model, "subSerialNumber": serial}


async def reassociate_coredump_history(repo, resource: dict[str, Any], session=None) -> int:
    """把同一已软删设备的 Core 目录接到重添资源，保留文件 ID 与首次发现事件。

    认证身份缺失时绝不尝试按地址关联。已迁移目录的 sourceKey 同步升级为稳定
    设备键，因此下一轮扫描会继续命中同一目录行而不会重新登记发现事件。
    """
    identity = coredump_device_identity(resource)
    if identity is None:
        return 0
    ip, model, serial = identity
    previous_resources = [
        item async for item in repo.db.resources.find(
            {
                "id": {"$ne": resource["id"]},
                "kind": "HIKVISION_NETWORK",
                "deletedAt": {"$ne": None},
                "ip": ip,
                "model": model,
                "subSerialNumber": serial,
            },
            {"id": 1}, session=session,
        )
    ]
    previous_ids = [item["id"] for item in previous_resources]
    if not previous_ids:
        return 0
    migrated = 0
    history_identity = {"ip": ip, "model": model, "subSerialNumber": serial}
    async for catalog in repo.db.coredump_files.find({
        "resourceId": {"$in": previous_ids},
        "deviceIp": ip,
        "$or": [
            {"deviceIdentity": history_identity},
            {"deviceIdentity": {"$exists": False}},
        ],
    }, session=session):
        source = catalog.get("source") or {}
        device, inode = source.get("device"), source.get("inode")
        replacement = (
            stable_source_key(catalog.get("nodeId", ""), resource, catalog.get("name", ""), device, inode)
            if isinstance(device, int) and isinstance(inode, int) else None
        )
        changes = {"resourceId": resource["id"], "deviceIdentity": history_identity}
        if replacement:
            changes["sourceKey"] = replacement
        result = await repo.db.coredump_files.update_one(
            {"id": catalog["id"], "resourceId": {"$in": previous_ids}},
            {"$set": changes}, session=session,
        )
        migrated += result.modified_count
    return migrated

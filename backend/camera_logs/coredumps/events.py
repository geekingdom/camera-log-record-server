"""Coredump 目录发现事件的幂等写入。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _event_time(catalog: dict[str, Any]) -> datetime:
    """沿用目录首次观测时刻，避免补写历史事件时伪造本轮扫描发生时间。"""
    value = catalog["receivedAt"]
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def record_discovery_event(repo: Any, resource: dict[str, Any], catalog: dict[str, Any]) -> None:
    """登记单个 catalog 版本的首次发现，并在事件成功后确认其不可重放标记。

    ``events`` 的保留维护可以删除旧事件，故不能把“事件不存在”当作每次扫描都应
    重写的依据。catalog 的确认字段同时让事件库短暂故障能在下一次观察时恢复。
    """
    if catalog.get("discoveryEventRecordedAt") is not None:
        return
    event_id = f"coredump-discovered:{catalog['id']}"
    document = {
        "_id": event_id,
        "type": "COREDUMP_DISCOVERED",
        "resourceId": catalog["resourceId"],
        "fileId": catalog["id"],
        "name": catalog["name"],
        "nodeId": catalog["nodeId"],
        "deviceIp": catalog["deviceIp"],
        "size": catalog["size"],
        "version": catalog["version"],
        "level": "INFO",
        "outcome": "SUCCEEDED",
        "message": "发现 Coredump 文件",
        "createdAt": _event_time(catalog),
    }
    if resource.get("name"):
        document["resourceName"] = resource["name"]
    await repo.db.events.update_one({"_id": event_id}, {"$setOnInsert": document}, upsert=True)
    await repo.db.coredump_files.update_one(
        {"id": catalog["id"], "discoveryEventRecordedAt": {"$exists": False}},
        {"$set": {"discoveryEventRecordedAt": _event_time(catalog)}},
    )

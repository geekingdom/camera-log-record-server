"""以资源小时桶持久化监控样本，并提供有界历史游标读取。"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any


def _utc(value: datetime) -> datetime:
    """Mongo 替身可能返回 naive 时间，统一比较前补上 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bucket_start(value: datetime) -> datetime:
    """以 UTC 自然小时分桶，固定桶起点决定 TTL，持续写入不会延长保存期。"""
    value = _utc(value)
    return value.replace(minute=0, second=0, microsecond=0)


def _minute_key(value: datetime) -> str:
    """同一分钟内的重试覆盖上次结果，避免命令超时重连造成重复点。"""
    return _utc(value).strftime("%Y%m%d%H%M")


def _encode_cursor(value: datetime) -> str:
    """游标仅包含采样 UTC 时间，不接受客户端任意排序字段。"""
    return base64.urlsafe_b64encode(_utc(value).isoformat().encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> datetime | None:
    """严格解析分页游标，防止无效值退化为无界历史请求。"""
    if not value:
        return None
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        return _utc(datetime.fromisoformat(text))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("监控历史游标无效") from error


class ResourceMetricStore:
    """资源小时桶写入器；租约保证每资源同刻只有一个 Worker 追加样本。"""

    def __init__(self, database: Any) -> None:
        self.db = database

    async def save(self, resource_id: str, sample: dict[str, Any], *, retention_days: int) -> None:
        """同一分钟覆盖样本，首次写入固定 expiresAt，绝不持久化原始命令响应。"""
        stamp = _utc(sample["sampledAt"])
        start = _bucket_start(stamp)
        item = {
            key: value
            for key, value in sample.items()
            if key
            in {
                "sampledAt",
                "status",
                "values",
                "errorCode",
                "errors",
                "configVersion",
                "identity",
            }
        }
        item["sampledAt"] = stamp
        key = _minute_key(stamp)
        identifier = f"{resource_id}:{start.strftime('%Y%m%d%H')}"
        initial = {
            "id": identifier,
            "resourceId": resource_id,
            "bucketStart": start,
            "expiresAt": start + timedelta(days=retention_days),
            "samples": [],
            "createdAt": stamp,
        }
        # Mongo 不支持直接按数组内 minute 原子替换并在不存在时追加，租约将同资源写入串行化；
        # 仍采用 CAS revision，避免异常双Worker或重试覆盖较晚的同分钟样本。
        for _ in range(5):
            row = await self.db.resource_metric_hours.find_one({"id": identifier})
            if row is None:
                try:
                    await self.db.resource_metric_hours.insert_one(
                        initial | {"samples": [{"minute": key} | item], "revision": 1}
                    )
                    return
                except Exception as error:
                    if type(error).__name__ != "DuplicateKeyError":
                        raise
                    continue
            samples = list(row.get("samples", []))
            replacement = {"minute": key} | item
            matched = False
            for index, existing in enumerate(samples):
                if existing.get("minute") == key:
                    if _utc(existing["sampledAt"]) > stamp:
                        return
                    samples[index] = replacement
                    matched = True
                    break
            if not matched:
                if len(samples) >= 60:
                    raise ValueError("资源监控小时桶已达到60个样本上限")
                samples.append(replacement)
            result = await self.db.resource_metric_hours.update_one(
                {"id": identifier, "revision": row.get("revision", 0)},
                {"$set": {"samples": samples, "updatedAt": stamp}, "$inc": {"revision": 1}},
            )
            if result.modified_count:
                return
        raise RuntimeError("资源监控样本并发写入冲突")

    async def history(
        self, resource_id: str, start: datetime, end: datetime, *, limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        """按采样时间倒序展开有限小时桶，返回无需精确总数的稳定续页。"""
        after = _decode_cursor(cursor)
        query = {
            "resourceId": resource_id,
            "bucketStart": {
                "$gte": _bucket_start(start),
                "$lte": _bucket_start(end),
            },
            "expiresAt": {"$gt": datetime.now(UTC)},
        }
        if after is not None:
            query["bucketStart"]["$lte"] = _bucket_start(after)
        rows = self.db.resource_metric_hours.find(query).sort("bucketStart", -1)
        items: list[dict[str, Any]] = []
        async for row in rows:
            for sample in sorted(row.get("samples", []), key=lambda value: value["sampledAt"], reverse=True):
                stamp = _utc(sample["sampledAt"])
                if not start <= stamp < end or (after is not None and stamp >= after):
                    continue
                item = {key: value for key, value in sample.items() if key != "minute"}
                item["sampledAt"] = stamp
                items.append(item)
                if len(items) > limit:
                    break
            if len(items) > limit:
                break
        page, has_more = items[:limit], len(items) > limit
        return {
            "items": page,
            "nextCursor": _encode_cursor(page[-1]["sampledAt"]) if page and has_more else None,
        }

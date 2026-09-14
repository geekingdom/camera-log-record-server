"""管理事件游标的编码、校验与 Mongo 稳定续页条件。"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException


@dataclass(frozen=True)
class EventCursorAnchor:
    """一个倒序事件页的末条排序键，保留 Mongo 主键的 BSON 类型。"""

    event_time: datetime
    identifier: ObjectId | str


def _invalid_cursor(error: Exception | None = None) -> None:
    """统一拒绝损坏、跨集合或跨条件复用的游标，避免泄露内部条件。"""
    raise HTTPException(422, "管理事件游标无效") from error


def _utc_database_time(value: datetime) -> datetime:
    """归一为 Mongo 比较使用的 UTC 时刻，兼容 tz-aware 驱动和测试替身。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical(value: Any) -> Any:
    """将查询条件转换为稳定、无 BSON 私有类型的哈希输入。"""
    if isinstance(value, datetime):
        return {"$date": _utc_database_time(value).isoformat()}
    if isinstance(value, ObjectId):
        return {"$objectId": str(value)}
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"游标查询条件包含不支持的类型: {type(value).__name__}")


def _query_hash(query: dict) -> str:
    """只嵌入查询摘要而非完整条件，仍使游标严格绑定当前筛选与时间范围。"""
    encoded = json.dumps(_canonical(query), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_identifier(identifier: object) -> tuple[str, str]:
    """编码可保持 Mongo 排序语义的两类主键，拒绝其他类型以避免字符串漂移。"""
    if isinstance(identifier, ObjectId):
        return "objectId", str(identifier)
    if isinstance(identifier, str) and identifier:
        return "string", identifier
    raise HTTPException(422, "管理事件游标不支持该主键类型")


def encode_event_cursor(*, collection: str, query: dict, legacy_time: bool, event_time: datetime,
                        identifier: object) -> str:
    """将末条原始 Mongo 文档排序键封装为 URL 安全的下一页游标。"""
    if not isinstance(event_time, datetime):
        raise HTTPException(422, "管理事件缺少游标时间键")
    identifier_type, identifier_value = _encode_identifier(identifier)
    payload = {
        "v": 1,
        "collection": collection,
        "queryHash": _query_hash(query),
        "legacyTime": legacy_time,
        "time": _utc_database_time(event_time).isoformat(),
        "idType": identifier_type,
        "id": identifier_value,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def decode_event_cursor(value: str, *, collection: str, query: dict, legacy_time: bool) -> EventCursorAnchor:
    """恢复并校验续页锚点，拒绝不同集合、筛选或时间兼容模式的游标。"""
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        event_time = datetime.fromisoformat(payload["time"])
        identifier_type, identifier_value = payload["idType"], payload["id"]
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _invalid_cursor(error)
    expected_fields = {"v", "collection", "queryHash", "legacyTime", "time", "idType", "id"}
    if (not isinstance(payload, dict) or set(payload) != expected_fields or payload.get("v") != 1
            or payload.get("collection") != collection
            or payload.get("queryHash") != _query_hash(query) or payload.get("legacyTime") is not legacy_time
            or not isinstance(identifier_value, str) or not identifier_value or event_time.tzinfo is None):
        _invalid_cursor()
    try:
        if identifier_type == "objectId":
            identifier: ObjectId | str = ObjectId(identifier_value)
        elif identifier_type == "string":
            identifier = identifier_value
        else:
            _invalid_cursor()
    except (TypeError, ValueError, InvalidId) as error:
        _invalid_cursor(error)
    return EventCursorAnchor(_utc_database_time(event_time), identifier)


def after_event_cursor_clause(*, time_field: str, event_time: datetime,
                              identifier: ObjectId | str) -> dict:
    """构造倒序查询的后续范围，时间相同时显式跨 BSON ``_id`` 类型续页。

    Mongo 比较查询会限制在锚点的 BSON 类型组内。当前历史存在字符串和
    ``ObjectId`` 两种主键：倒序时 ObjectId 组在字符串组之前。因此 ObjectId
    锚点除本组较小值外还必须包含全部字符串；字符串锚点只读取本组较小值。
    """
    database_time = _utc_database_time(event_time).replace(tzinfo=None)
    if isinstance(identifier, ObjectId):
        same_time = {"$or": [
            {"_id": {"$type": "objectId", "$lt": identifier}},
            {"_id": {"$type": "string"}},
        ]}
    else:
        same_time = {"_id": {"$type": "string", "$lt": identifier}}
    return {"$or": [
        {time_field: {"$lt": database_time}},
        {time_field: database_time, **same_time},
    ]}

"""海康资源认证结果的最小可查询历史及其稳定游标，不保存凭据或设备响应正文。"""

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from camera_logs.common.database import now
from camera_logs.common.models import new_id


def decode_cursor(value: str, resource_id: str) -> tuple[datetime, str]:
    """解析认证历史倒序游标，并限制它只能用于原资源避免跨资源跳页。"""
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        created_at = datetime.fromisoformat(payload["createdAt"])
        identifier = payload["id"]
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(422, "认证记录游标无效") from error
    if (not isinstance(payload, dict) or payload.get("resourceId") != resource_id
            or not isinstance(identifier, str) or not identifier or len(identifier) > 256
            or created_at.tzinfo is None):
        raise HTTPException(422, "认证记录游标无效")
    return created_at.astimezone(UTC), identifier


def encode_cursor(document: dict) -> str:
    """把末条的排序键编码为无敏感数据的 URL 安全游标。"""
    created_at = document.get("createdAt")
    if not isinstance(created_at, datetime) or not document.get("id") or not document.get("resourceId"):
        raise ValueError("认证记录缺少游标排序键")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = {"resourceId": document["resourceId"], "createdAt": created_at.astimezone(UTC).isoformat(),
               "id": document["id"]}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def after_cursor_clause(created_at: datetime, identifier: str) -> dict:
    """构造倒序下一页条件；相同秒内以稳定 ID 断开，避免重复或遗漏。"""
    # BSON datetime 在服务端只保存 UTC 毫秒，不带时区；以 naive UTC 送入驱动可同时
    # 匹配 tz_aware 生产客户端和 mongomock，语义仍是明确的 UTC 时间点。
    database_time = created_at.astimezone(UTC).replace(tzinfo=None)
    return {"$or": [{"createdAt": {"$lt": database_time}},
                    {"createdAt": database_time, "id": {"$lt": identifier}}]}


async def record_authentication(repo, resource, *, source: str, result: str, before=None, after=None, message=None,
                                completed_at=None, session=None):
    """写入一次已落库认证结果；调用方只传安全枚举和型号、序列号快照。"""
    before, after = before or {}, after or {}
    model_before, serial_before = (str(before.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    model_after, serial_after = (str(after.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    initial = result == "SUCCESS" and before.get("authenticatedAt") is None
    timestamp = completed_at or now()
    identity_changed = result == "SUCCESS" and not initial and (model_before, serial_before) != (model_after, serial_after)
    retention_days = int(getattr(repo.settings, "authentication_record_retention_days", 90) or 90)
    await repo.db.authentication_records.insert_one({
        "id": new_id(), "resourceId": resource["id"], "createdAt": timestamp,
        "expiresAt": timestamp + timedelta(days=retention_days), "source": source, "result": result,
        "modelBefore": model_before, "modelAfter": model_after,
        "serialBefore": serial_before, "serialAfter": serial_after,
        "identityChanged": identity_changed, "initialAuthentication": initial,
        "message": message, "_id": new_id(),
    }, session=session)

"""海康资源认证结果的最小可查询历史及其稳定游标，不保存凭据或设备响应正文。"""

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pymongo import ReturnDocument

from camera_logs.common import audited_mutations
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
    """记录认证结果，并将同日连续周期成功合并为一条可查询历史。

    认证失败、身份变化、首次认证及人工/创建/编辑认证都是顺序边界。每次写入均先
    原子递增资源专属状态行，令并发事务竞争同一文档；重试后的事务才读取最新历史，
    因而不能跨越失败记录合并成功次数。调用方已持有事务时复用该会话，否则建立独立
    事务，避免手动认证路径绕过这项串行保证。
    """
    if session is None:
        async def commit(transaction_session):
            return await _record_authentication_in_session(
                repo, resource, source=source, result=result, before=before, after=after,
                message=message, completed_at=completed_at, session=transaction_session,
            )

        return await audited_mutations.mutation_transaction(repo, commit)

    return await _record_authentication_in_session(
        repo, resource, source=source, result=result, before=before, after=after,
        message=message, completed_at=completed_at, session=session,
    )


async def _record_authentication_in_session(repo, resource, *, source: str, result: str, before=None, after=None,
                                            message=None, completed_at=None, session=None):
    """在调用方已建立的事务中完成认证历史串行写入。"""

    before, after = before or {}, after or {}
    model_before, serial_before = (str(before.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    model_after, serial_after = (str(after.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    initial = result == "SUCCESS" and before.get("authenticatedAt") is None
    timestamp = completed_at or now()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    identity_changed = result == "SUCCESS" and not initial and (model_before, serial_before) != (model_after, serial_after)
    retention_days = int(getattr(repo.settings, "authentication_record_retention_days", 90))
    # head 行是每个资源认证历史的逻辑互斥锁。不能只更新最后一条成功记录：并发的
    # 失败请求可能插入另一文档而未与该更新冲突，从而让稍后的成功错误跨越失败合并。
    head = await repo.db.authentication_record_heads.find_one_and_update(
        {"_id": resource["id"]},
        {"$inc": {"revision": 1}, "$set": {"updatedAt": timestamp}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    day = timestamp.astimezone(UTC).date().isoformat() if timestamp.tzinfo else timestamp.replace(tzinfo=UTC).date().isoformat()
    mergeable = source == "PERIODIC" and result == "SUCCESS" and not initial and not identity_changed
    if mergeable:
        latest_id = head.get("latestRecordId") if head else None
        latest = await repo.db.authentication_records.find_one(
            {"id": latest_id, "resourceId": resource["id"]}, session=session,
        ) if latest_id else None
        latest_at = latest.get("latestAt", latest.get("createdAt")) if latest else None
        if isinstance(latest_at, datetime) and latest_at.tzinfo is None:
            latest_at = latest_at.replace(tzinfo=UTC)
        if (latest and latest.get("source") == "PERIODIC" and latest.get("result") == "SUCCESS"
                and not latest.get("initialAuthentication") and not latest.get("identityChanged")
                and latest.get("utcDay") == day
                and (latest.get("modelAfter", ""), latest.get("serialAfter", "")) == (model_after, serial_after)
                and isinstance(latest_at, datetime) and timestamp >= latest_at):
            # 迟到观测另起一段，避免其时间早于固定 createdAt 却被累计到区间内，
            # 导致时间范围检索漏掉该观测。首次时间、游标及TTL均保持不可变。
            await repo.db.authentication_records.update_one(
                {"_id": latest["_id"]},
                {"$inc": {"occurrenceCount": 1},
                 "$max": {"latestAt": timestamp},
                 "$set": {"historyLatestRevision": head["revision"]}},
                session=session,
            )
            return latest["id"]

    identifier = new_id()
    await repo.db.authentication_records.insert_one({
        "id": identifier, "resourceId": resource["id"], "createdAt": timestamp, "latestAt": timestamp,
        "occurrenceCount": 1, "utcDay": day,
        "historyFirstRevision": head["revision"], "historyLatestRevision": head["revision"],
        **({"expiresAt": timestamp + timedelta(days=retention_days)} if retention_days > 0 else {}),
        "source": source, "result": result,
        "modelBefore": model_before, "modelAfter": model_after,
        "serialBefore": serial_before, "serialAfter": serial_after,
        "identityChanged": identity_changed, "initialAuthentication": initial,
        "message": message, "_id": new_id(),
    }, session=session)
    advanced = await repo.db.authentication_record_heads.update_one(
        {"_id": resource["id"], "revision": head["revision"]},
        {"$set": {"latestRecordId": identifier, "updatedAt": timestamp}},
        session=session,
    )
    if not advanced.modified_count:
        # 事务提交时会把这类竞争转成重试；禁止留下没有 head 指针的新认证事件。
        raise RuntimeError("认证历史并发顺序已变化，请重试事务")
    return identifier

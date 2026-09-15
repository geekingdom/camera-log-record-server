"""审计过期前归档最小可追溯字段，按UTC日每100条分块压缩，不保留原始正文。"""

import gzip
import hashlib
from datetime import timedelta

from bson import Binary, json_util

from camera_logs.common.database import now

ARCHIVE_DAYS = 365
ARCHIVE_FIELDS = ("id", "actor", "action", "targetId", "requestId", "outcome", "createdAt")


def audit_summary(document):
    """只保留固定身份字段与派生结果，不复制请求正文或任意附加字段。"""
    from camera_logs.administration.event_presenter import event_outcome
    summary = {key: document[key] for key in ARCHIVE_FIELDS if key in document}
    summary.update(sourceId=str(document["_id"]), outcome=event_outcome(document))
    return summary


async def archive_audit(db, document, day, session):
    """与源审计删除同事务写入可校验摘要块；每块最多100项，归档固定保留一年。"""
    counter = await db.record_purge_days.find_one({"_id": f"audit:{day}"}, session=session) or {}
    part = counter.get("deleted", 0) // 100
    identifier = f"audit:{day}:{part}"
    existing = await db.audit_archive_chunks.find_one({"_id": identifier}, session=session)
    records = json_util.loads(gzip.decompress(existing["payload"])) if existing else []
    summary = audit_summary(document)
    # 只有固定字段进入档案；过长或结构异常的旧值保留源记录供人工核查。
    encoded = json_util.dumps(summary, ensure_ascii=False).encode()
    if len(encoded) > 16384 or len(records) >= 100:
        raise ValueError("审计摘要超出有界归档大小")
    records.append(summary)
    body = json_util.dumps(records, ensure_ascii=False).encode()
    await db.audit_archive_chunks.update_one({"_id": identifier}, {
        "$setOnInsert": {"day": day, "part": part, "createdAt": now(), "expiresAt": now() + timedelta(days=ARCHIVE_DAYS)},
        "$set": {"count": len(records), "payload": Binary(gzip.compress(body, compresslevel=1)),
                 "sha256": hashlib.sha256(body).hexdigest(), "updatedAt": now()}}, upsert=True, session=session)

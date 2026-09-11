"""分批补齐旧认证历史的过期时间，不删除日志或改写已有到期策略。"""

from datetime import timedelta


async def backfill_authentication_expiry(db, days: int, *, apply: bool = False,
                                         batch_size: int = 500, max_records: int = 5000) -> dict:
    """通过服务端分批游标补齐历史 TTL；默认仅统计，重复执行不会覆盖已配置记录。"""
    if days <= 0 or not 1 <= batch_size <= 1000 or not 1 <= max_records <= 100_000:
        raise ValueError("保留天数须为正数，每批1至1000条，单次上限1至100000条")
    scanned = updated = 0
    # Mongo 的 null 等值同时匹配缺失字段；两者都不能触发 TTL，需按首次时间补齐。
    query = {"expiresAt": None, "createdAt": {"$type": "date"}}
    # 使用游标而非 _id > 上次值，兼容旧记录同时存在字符串和 ObjectId 主键。
    cursor = db.authentication_records.find(query, {"createdAt": 1}).sort("_id", 1).limit(
        max_records).batch_size(batch_size)
    try:
        async for row in cursor:
            scanned += 1
            if apply:
                result = await db.authentication_records.update_one(
                    {"_id": row["_id"], "expiresAt": None, "createdAt": row["createdAt"]},
                    {"$set": {"expiresAt": row["createdAt"] + timedelta(days=days)}})
                updated += result.modified_count
    finally:
        await cursor.close()
    return {"apply": apply, "eligible": scanned, "updated": updated, "retentionDays": days,
            "limitReached": scanned == max_records, "maxRecords": max_records}

"""以明确的历史检测时间补齐运行事件创建时间，供查询索引迁移前低峰执行。"""

from typing import Any


async def backfill_runtime_event_created_at(
    db: Any, *, apply: bool = False, batch_size: int = 500, max_records: int = 5000,
) -> dict[str, int | bool]:
    """分批预览或补齐 ``events.createdAt``，不为缺少有效检测时间的记录造时间。

    ``createdAt: None`` 同时匹配字段缺失和显式 null。更新条件重复校验旧
    ``detectedAt``，并要求创建时间仍为空，因此读取后被其它进程补齐或改写的记录
    不会被本次迁移覆盖。报告中的 remaining 是本次操作完成后仍可安全迁移的总数。
    """
    if not 1 <= batch_size <= 1000 or not 1 <= max_records <= 100_000:
        raise ValueError("每批1至1000条，单次上限1至100000条")
    # Mongo 的 $type 对数组会检查元素；额外排除数组，确保不会把日期列表写入标量时间字段。
    migratable = {
        "createdAt": None,
        "detectedAt": {"$type": "date"},
        "$nor": [{"createdAt": {"$type": "array"}}, {"detectedAt": {"$type": "array"}}],
    }
    total_before = await db.events.count_documents(migratable)
    cursor = db.events.find(migratable, {"detectedAt": 1}).sort("_id", 1).limit(
        max_records).batch_size(batch_size)
    matched = updated = 0
    try:
        async for row in cursor:
            matched += 1
            if apply:
                result = await db.events.update_one(
                    {
                        "_id": row["_id"],
                        "createdAt": None,
                        "detectedAt": row["detectedAt"],
                        "$nor": [{"createdAt": {"$type": "array"}}, {"detectedAt": {"$type": "array"}}],
                    },
                    {"$set": {"createdAt": row["detectedAt"]}},
                )
                updated += int(result.modified_count)
    finally:
        await cursor.close()
    remaining = await db.events.count_documents(migratable)
    missing_created_at = await db.events.count_documents({
        "createdAt": None,
        "$nor": [{"createdAt": {"$type": "array"}}],
    })
    return {
        "apply": apply,
        "matched": matched,
        "updated": updated,
        "remaining": remaining,
        # 仅计入创建时间缺失但不存在 BSON 日期检测时间的记录；不猜测业务时间。
        "unmigratable": max(0, missing_created_at - remaining),
        "limitReached": total_before > max_records,
        "maxRecords": max_records,
    }

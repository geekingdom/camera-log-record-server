"""把已签名缺口分页快照换为短随机游标，避免经过代理时超过请求行长度限制。"""

from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from camera_logs.common.database import now


async def resolve_gap_cursor(repo, task_id, cursor):
    """按任务和到期时间读取不可猜测快照；过期须重新定位，不使用漂移的新目录。"""
    if not cursor:
        return None
    if len(cursor) != 32 or any(char not in "0123456789abcdef" for char in cursor):
        raise HTTPException(422, "缺口目录游标无效")
    row = await repo.db.log_gap_snapshots.find_one({"_id": cursor, "taskId": task_id, "expiresAt": {"$gt": now()}})
    if not row:
        raise HTTPException(422, "缺口目录游标已失效，请重新定位")
    return row["cursor"]


async def publish_gap_page(repo, task_id, page):
    """每页最多一条临时目录记录，不存正文，一小时TTL删除。"""
    if not page.get("nextCursor"):
        return page
    identifier = uuid4().hex
    await repo.db.log_gap_snapshots.insert_one({"_id": identifier, "taskId": task_id,
        "cursor": page["nextCursor"], "expiresAt": now() + timedelta(hours=1)})
    return page | {"nextCursor": identifier}

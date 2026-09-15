"""对结束运行分段归档，每事务最多处理100条子记录，不因运行过大永久跳过。"""

import re
from collections import Counter

from pymongo import ReturnDocument

from camera_logs.common.database import now

CHUNK_SIZE = 100
COMMAND_TERMINALS = {"SENT", "FAILED", "CANCELLED"}
OPERATION_TERMINALS = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def archive_chunk(db, run, timestamp, session, *, dry_run=False):
    """任务写栅栏与命令预留/恢复共同竞争；摘要累计与本批删除原子提交。"""
    task_id, run_id = run.get("taskId"), run.get("id")
    if not task_id or not run_id or not run.get("endedAt"):
        return False, "运行未完整结束"
    task = await db.tasks.find_one_and_update({"id": task_id}, {"$inc": {"recordArchiveRevision": 1}},
        return_document=ReturnDocument.AFTER, session=session)
    if task and task.get("runId") == run_id:
        return False, "任务仍引用运行"
    base = {"taskId": task_id, "runId": run_id}
    if await db.endpoint_locks.find_one(base, session=session):
        return False, "运行锁仍存在"
    if await db.commands.find_one(base | {"status": {"$nin": list(COMMAND_TERMINALS)}}, session=session):
        return False, "存在未结算命令"
    if await db.operations.find_one(base | {"status": {"$nin": list(OPERATION_TERMINALS)}}, session=session):
        return False, "存在未结算操作"
    archive = await db.run_archives.find_one({"runId": run_id}, session=session) or {}
    query = dict(base)
    if archive.get("commandCursor"):
        query["id"] = {"$gt": archive["commandCursor"]}
    rows = await db.commands.find(query, {"id": 1, "status": 1}, session=session).sort("id", 1).limit(CHUNK_SIZE).to_list(CHUNK_SIZE)
    selected = []
    for row in rows:
        # 每个ID仅查询存在性；重复幂等映射不能让同批无关命令永久跳过。
        pinned = await db.idempotency.find_one({"resourceId": row["id"], "$or": [
            {"expiresAt": {"$gt": now()}}, {"expiresAt": None}]}, {"_id": 1}, session=session)
        if not pinned:
            selected.append(row)
    if dry_run:
        return bool(selected) or not rows, "预览"
    await db.run_archives.update_one({"runId": run_id}, {"$setOnInsert": {
        "id": f"run:{run_id}", "runId": run_id, "taskId": task_id, "schemaVersion": 2,
        "startedAt": run.get("startedAt"), "endedAt": run["endedAt"], "commandCount": 0,
        "budgetCount": 0, "commandStatuses": {}, "state": "ARCHIVING", "startedArchiveAt": timestamp}},
        upsert=True, session=session)
    if selected:
        await db.commands.delete_many(base | {"id": {"$in": [row["id"] for row in selected]}}, session=session)
    changes = {"commandCount": len(selected)}
    changes.update({f"commandStatuses.{status}": count for status, count in Counter(row["status"] for row in selected).items()})
    remaining = await db.commands.find_one(base, {"id": 1}, session=session)
    budget_query = {"_id": {"$regex": f"^{re.escape(run_id)}:"}}
    if not remaining:
        # 仍有命令引用时保留预算；全部命令结束后预算也按批删除。
        budgets = await db.budgets.find(budget_query, {"_id": 1}, session=session).limit(CHUNK_SIZE).to_list(CHUNK_SIZE)
        if budgets:
            await db.budgets.delete_many({"_id": {"$in": [row["_id"] for row in budgets]}}, session=session)
        changes["budgetCount"] = len(budgets)
    budget_left = await db.budgets.find_one(budget_query, {"_id": 1}, session=session)
    finished = not remaining and not budget_left
    await db.run_archives.update_one({"runId": run_id}, {"$inc": changes, "$set": {
        "commandCursor": rows[-1]["id"] if rows else None,
        "state": "ARCHIVED" if finished else "ARCHIVING", "updatedAt": timestamp,
        **({"archivedAt": timestamp} if finished else {})}}, session=session)
    if finished:
        await db.runs.delete_one({"id": run_id, "endedAt": run["endedAt"]}, session=session)
    return finished, "已归档" if finished else ("分段归档" if selected else "有效幂等引用")

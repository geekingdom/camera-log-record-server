"""过期审计、事件和控制操作按书签处理；引用校验、日统计与删除原子提交。"""

from datetime import UTC

from pymongo import ReturnDocument

from camera_logs.common.database import now

TERMINAL_OPERATIONS = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def _transaction(repo, callback):
    """复用多数确认事务；测试注入仅用于语义回归，另验真实副本集。"""
    from camera_logs.common.record_archive import _transaction as execute
    return await execute(repo, callback)


async def _protected(db, collection, document, session):
    """只删除明确终态；任务写栅栏与并发控制/命令预留共同产生写冲突。"""
    if collection == "operations" and document.get("status") not in TERMINAL_OPERATIONS:
        return True
    identifier = document.get("targetId") if collection == "audit" else document.get("id")
    if identifier and await db.idempotency.find_one({"resourceId": identifier, "$or": [
        {"expiresAt": {"$gt": now()}}, {"expiresAt": None}]}, session=session):
        return True
    if collection == "audit":
        from bson import json_util

        from camera_logs.common.audit_archive import audit_summary
        if len(json_util.dumps(audit_summary(document), ensure_ascii=False).encode()) > 16384:
            return True
        target = document.get("targetId")
        if target and await db.commands.find_one({"id": target, "status": {"$nin": ["SENT", "FAILED", "CANCELLED"]}}, session=session):
            return True
        if target and await db.operations.find_one({"id": target, "status": {"$nin": list(TERMINAL_OPERATIONS)}}, session=session):
            return True
    task_id = document.get("taskId")
    task = None
    if task_id:
        task = await db.tasks.find_one_and_update({"id": task_id},
            {"$inc": {"recordArchiveRevision": 1}}, return_document=ReturnDocument.AFTER, session=session)
    if task:
        if collection == "operations" and task.get("controlOperationId") == identifier:
            return True
        if task.get("nodeId") or task.get("resourceHealthRecovery") or task.get("desiredState") in {"RUNNING", "PAUSED"}:
            return True
    run_id = document.get("runId")
    if run_id:
        run = await db.runs.find_one({"id": run_id}, session=session)
        if run and not run.get("endedAt"):
            return True
        if await db.commands.find_one({"runId": run_id, "status": {"$nin": ["SENT", "FAILED", "CANCELLED"]}}, session=session):
            return True
        if await db.endpoint_locks.find_one({"runId": run_id}, session=session):
            return True
    return False


async def purge_expired_records(repo, collection, cutoff, owner, *, batch_size=100, lease_seconds=120, dry_run=False):
    """每轮最多100候选，保留项仍推进书签；扫到底重置以重新检查已解除的保护。"""
    from camera_logs.common.record_archive import _fence
    if collection not in {"audit", "events", "operations"}:
        raise ValueError("不支持的增长集合")
    batch_size = max(1, min(int(batch_size), 100))
    held = await repo.db.record_archive_maintenance.find_one({"_id": "growth-records", "owner": owner})
    bookmark = (held or {}).get("cursors", {}).get(collection)
    query = {"createdAt": {"$lt": cutoff}}
    if bookmark:
        query["$or"] = [{"createdAt": {"$gt": bookmark["createdAt"]}},
            {"createdAt": bookmark["createdAt"], "_id": {"$gt": bookmark["id"]}}]
    rows = await repo.db[collection].find(query, {"_id": 1, "createdAt": 1}).sort([("createdAt", 1), ("_id", 1)]).limit(batch_size).to_list(batch_size)
    removed = protected = 0
    for candidate in rows:
        async def commit(session, candidate=candidate):
            if not await _fence(repo.db, owner, lease_seconds, session):
                return False
            # 不能用事务外候选判断终态：记录可能在维护取得快照前已变化。
            document = await repo.db[collection].find_one({"_id": candidate["_id"], "createdAt": {"$lt": cutoff}}, session=session)
            if not document or await _protected(repo.db, collection, document, session):
                return False
            if dry_run:
                return True
            deleted = await repo.db[collection].delete_one({"_id": document["_id"]}, session=session)
            if deleted.deleted_count:
                created = document["createdAt"]
                created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
                if collection == "audit":
                    from camera_logs.common.audit_archive import archive_audit
                    await archive_audit(repo.db, document, created.strftime("%Y-%m-%d"), session)
                await repo.db.record_purge_days.update_one({"_id": f"{collection}:{created:%Y-%m-%d}"},
                    {"$setOnInsert": {"collection": collection, "day": created.strftime("%Y-%m-%d")},
                     "$inc": {"deleted": 1}, "$set": {"updatedAt": now()}}, upsert=True, session=session)
            return bool(deleted.deleted_count)
        done = await _transaction(repo, commit)
        removed += int(done)
        protected += int(not done)
    if not dry_run:
        await repo.db.record_archive_maintenance.update_one({"_id": "growth-records", "owner": owner, "expiresAt": {"$gt": now()}},
            {"$set": {f"cursors.{collection}": {"createdAt": rows[-1]["createdAt"], "id": rows[-1]["_id"]} if len(rows) == batch_size else None}})
    return {"removed": removed, "protected": protected, "scanned": len(rows), "dryRun": dry_run}

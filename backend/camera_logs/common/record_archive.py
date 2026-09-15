"""增长记录维护的租约和有界调度，运行归档与普通过期记录分开推进。"""

from datetime import UTC, timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.run_archive_chunks import archive_chunk

MAX_BATCH_SIZE = 100


async def _transaction(repo, callback):
    """真实事务使摘要增量与明细删除共同回滚；驱动重试不会重复累计。"""
    async with repo.db.client.start_session() as session:
        return await session.with_transaction(callback, read_concern=ReadConcern("snapshot"),
                                              write_concern=WriteConcern("majority", j=True))


async def _lease(repo, owner, timestamp, seconds):
    """只允许一个维护者推进书签；旧持有者必须在每个事务内重新取得写栅栏。"""
    document = await repo.db.record_archive_maintenance.find_one_and_update(
        {"_id": "growth-records", "$or": [{"expiresAt": {"$lte": timestamp}}, {"owner": owner},
                                          {"expiresAt": None}]},
        {"$set": {"owner": owner, "expiresAt": timestamp + timedelta(seconds=seconds)}},
        return_document=ReturnDocument.AFTER)
    if document:
        return True
    try:
        await repo.db.record_archive_maintenance.insert_one({
            "_id": "growth-records", "owner": owner, "expiresAt": timestamp + timedelta(seconds=seconds)})
        return True
    except DuplicateKeyError:
        return False


async def _fence(db, owner, seconds, session):
    """事务写同一租约行，与接管、续约和其它维护产生真实写冲突。"""
    timestamp = now()
    result = await db.record_archive_maintenance.update_one(
        {"_id": "growth-records", "owner": owner, "expiresAt": {"$gt": timestamp}},
        {"$inc": {"revision": 1}, "$set": {"expiresAt": timestamp + timedelta(seconds=seconds)}}, session=session)
    return result.matched_count == 1


async def _archive_run(repo, run_id, cutoff, owner, lease_seconds, timestamp, *, dry_run=False):
    """每个运行每轮执行一个固定子批次，先取得租约写栅栏再检查最新运行。"""
    async def commit(session):
        if not await _fence(repo.db, owner, lease_seconds, session):
            return False, "维护租约已失效"
        run = await repo.db.runs.find_one({"id": run_id, "endedAt": {"$lt": cutoff}}, session=session)
        if not run:
            return False, "候选已变化"
        return await archive_chunk(repo.db, run, timestamp, session, dry_run=dry_run)
    return await _transaction(repo, commit)


async def _runs(repo, cutoff, owner, seconds, timestamp, batch_size, dry_run, result):
    """持久keyset越过保护运行，扫到底后从头重试部分归档和已解除引用的运行。"""
    held = await repo.db.record_archive_maintenance.find_one({"_id": "growth-records", "owner": owner})
    bookmark = (held or {}).get("runCursor")
    query = {"endedAt": {"$lt": cutoff}}
    if bookmark:
        query["$or"] = [{"endedAt": {"$gt": bookmark["endedAt"]}},
            {"endedAt": bookmark["endedAt"], "id": {"$gt": bookmark["id"]}}]
    rows = await repo.db.runs.find(query).sort([("endedAt", 1), ("id", 1)]).limit(batch_size).to_list(batch_size)
    for run in rows:
        done, reason = await _archive_run(repo, run["id"], cutoff, owner, seconds, timestamp, dry_run=dry_run)
        result["archivedRuns"] += int(done)
        result["partialRuns"] += int(reason == "分段归档")
        result["protected"] += int(not done and reason != "分段归档")
    if not dry_run:
        await repo.db.record_archive_maintenance.update_one({"_id": "growth-records", "owner": owner, "expiresAt": {"$gt": now()}},
            {"$set": {"runCursor": {"endedAt": rows[-1]["endedAt"], "id": rows[-1]["id"]} if len(rows) == batch_size else None}})


async def maintain_growth_records(repo, record_retention, *, reference=None, batch_size=100, lease_seconds=120, dry_run=False):
    """每轮处理最多100个运行和每类100候选；大运行分段，零保留配置禁用对应维护。"""
    from camera_logs.common.record_purge import purge_expired_records
    timestamp = (reference or now()).astimezone(UTC)
    batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    owner = new_id()
    result = {"leased": False, "archivedRuns": 0, "partialRuns": 0, "audit": 0, "events": 0, "operations": 0, "protected": 0, "dryRun": dry_run}
    if not await _lease(repo, owner, now(), lease_seconds):
        return result
    result["leased"] = True
    config = record_retention or {}
    try:
        if config.get("runDays", 0) > 0:
            await _runs(repo, timestamp - timedelta(days=config["runDays"]), owner, lease_seconds,
                        timestamp, batch_size, dry_run, result)
        for collection, field in (("audit", "auditDays"), ("events", "eventDays"), ("operations", "runDays")):
            if config.get(field, 0) > 0:
                purged = await purge_expired_records(repo, collection, timestamp - timedelta(days=config[field]), owner,
                    batch_size=batch_size, lease_seconds=lease_seconds, dry_run=dry_run)
                result[collection] = purged["removed"]
        return result
    finally:
        await repo.db.record_archive_maintenance.update_one({"_id": "growth-records", "owner": owner},
            {"$set": {"expiresAt": now() - timedelta(seconds=1)}})

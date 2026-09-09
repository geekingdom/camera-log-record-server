"""日志作业、文件保护和操作审计共同提交；数据库异常后只读恢复。"""

from datetime import datetime, timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now, public
from camera_logs.common.models import new_id
from camera_logs.logs.order import ordered_files


async def _confirmed(database, actor_id, key, digest, *, session=None):
    """历史 PENDING 若已存在作业只返回原作业，不补建任务或重做文件快照。"""
    existing = await database.idempotency.find_one({"actor": actor_id, "key": key}, session=session)
    if existing and existing.get("state") == "PENDING" and existing.get("digest") == digest:
        previous = await database.jobs.find_one({"id": existing.get("resourceId")}, session=session)
        if previous is not None:
            return previous
    return await audited_mutations._confirmed_existing(database, digest, "jobs", existing, session=session)


def _file_query(body, kind, timestamp):
    """固定本次请求的默认搜索范围，事务重试不能让默认时间窗口漂移。"""
    query = {"taskId": body.taskId, "status": {"$ne": "DELETED"}}
    if kind == "DOWNLOAD":
        query["hour"] = {"$in": body.hourIds}
        return query, {}
    try:
        end = datetime.fromisoformat(body.end) if body.end else timestamp
        start = datetime.fromisoformat(body.start) if body.start else end - timedelta(hours=1)
        if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end - start <= timedelta(hours=24):
            raise ValueError()
    except ValueError as error:
        raise HTTPException(422, "时间范围须包含时区且不超过24小时") from error
    start, end = start.astimezone(timestamp.tzinfo), end.astimezone(timestamp.tzinfo)
    query["hour"] = {"$gte": start.replace(minute=0, second=0, microsecond=0).isoformat(), "$lte": end.isoformat()}
    return query, {"start": start.isoformat(), "end": end.isoformat()}


def _validate_files(files, body, kind):
    """所有范围和大小校验在保护文件前完成，避免无效请求无谓延长保留期。"""
    if not files:
        raise HTTPException(404, "所选范围没有日志")
    if any(file["status"] not in ("OPEN", "READY") for file in files) and (kind != "DOWNLOAD" or not body.allowPartial):
        raise HTTPException(409, "所选范围包含暂不可用片段，请刷新或明确允许部分导出")
    if kind == "DOWNLOAD" and not body.allowPartial and set(body.hourIds) - {file["hour"] for file in files}:
        raise HTTPException(409, "部分小时没有可用片段")
    archive_sizes = {}
    for file in files:
        group = (file["nodeId"], file.get("archiveGroupId") or file["id"])
        archive_sizes[group] = max(archive_sizes.get(group, 0), file.get("archiveBytes") or file.get("bytes", 0))
    if kind == "DOWNLOAD" and sum(archive_sizes.values()) > 20_000_000_000:
        raise HTTPException(422, "预计下载超过20GB，请拆分小时")


async def submit_job(repo, actor_id, key, body, kind):
    """冻结目录水位、保护文件、发布作业与审计共享同一事务及固定幂等 ID。"""
    if not key or len(key) > 128:
        raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
    digest = audited_mutations.request_digest(kind, body.model_dump())
    database = audited_mutations._majority_primary_database(repo)
    previous = await _confirmed(database, actor_id, key, digest)
    if previous is not None:
        return previous
    task = await repo.get("tasks", body.taskId)
    identifier, timestamp = new_id(), now()
    query, bounds = _file_query(body, kind, timestamp)
    expires = timestamp + timedelta(hours=24)

    async def commit(session):
        previous = await _confirmed(repo.db, actor_id, key, digest, session=session)
        if previous is not None:
            return previous
        files = ordered_files([public(file) async for file in repo.db.files.find(query, session=session)])
        _validate_files(files, body, kind)
        for file in files:
            if file["status"] not in ("OPEN", "READY"):
                continue
            protected = await repo.db.files.update_one(
                {"id": file["id"], "status": {"$in": ["OPEN", "READY"]}},
                {"$max": {"retainUntil": expires}}, session=session,
            )
            if protected.matched_count != 1:
                raise HTTPException(409, "日志片段正在清理，请刷新后重试")
        document = body.model_dump() | bounds | {
            "id": identifier, "kind": kind, "status": "QUEUED", "progress": 0,
            "actor": actor_id, "files": files, "nodeId": task.get("nodeId") or files[0]["nodeId"],
            "taskName": task["name"], "taskIp": task["ip"], "createdAt": timestamp, "expiresAt": expires,
        }
        await repo.db.idempotency.insert_one({
            "actor": actor_id, "key": key, "digest": digest, "resourceId": identifier,
            "state": "SUCCEEDED", "updatedAt": timestamp, "expiresAt": timestamp + timedelta(days=7),
        }, session=session)
        await repo.db.jobs.insert_one(document, session=session)
        await repo.audit(actor_id, kind, identifier, session=session)
        return document

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        try:
            previous = await _confirmed(database, actor_id, key, digest)
        except PyMongoError:
            previous = None
        if previous is not None:
            return previous
        if isinstance(error, DuplicateKeyError):
            raise HTTPException(409, "作业幂等键冲突，请使用原请求重试") from error
        raise HTTPException(503, "作业提交结果未知，请使用相同幂等键重试") from error


async def cancel_job(repo, actor_id, job):
    """取消和成功状态审计原子提交；已终止作业不产生新的取消审计。"""
    async def commit(session):
        changed = await repo.db.jobs.update_one(
            {"id": job["id"], "status": {"$in": ["QUEUED", "RUNNING"]}},
            {"$set": {"status": "CANCELLED"}}, session=session,
        )
        if changed.modified_count:
            await repo.audit(actor_id, f"cancel_{job['kind'].lower()}", job["id"], session=session)

    try:
        await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        try:
            database = audited_mutations._majority_primary_database(repo)
            confirmed = await database.jobs.find_one({"id": job["id"], "status": "CANCELLED"})
        except PyMongoError:
            confirmed = None
        if confirmed is None:
            raise HTTPException(503, "作业取消结果未知，请查询状态后重试") from error

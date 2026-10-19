"""日志作业、文件保护和操作审计共同提交；数据库异常后只读恢复。"""

from datetime import datetime, timedelta

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.api_time import parse_api_datetime
from camera_logs.common.database import now, public
from camera_logs.common.models import new_id
from camera_logs.common.presentation_time import SHANGHAI, utc_iso
from camera_logs.logs.order import ordered_files


async def _confirmed(database, actor_id, key, digest, *, session=None):
    """历史 PENDING 若已存在作业只返回原作业，不补建任务或重做文件快照。"""
    existing = await database.idempotency.find_one({"actor": actor_id, "key": key}, session=session)
    if existing and existing.get("state") == "PENDING" and existing.get("digest") == digest:
        previous = await database.jobs.find_one({"id": existing.get("resourceId")}, session=session)
        if previous is not None:
            return previous
    return await audited_mutations._confirmed_existing(database, digest, "jobs", existing, session=session)


def _canonical_hour_ids(hour_ids):
    """把展示层的上海小时和历史 UTC 小时都规范为目录索引使用的 UTC 键。"""
    values = []
    for value in hour_ids:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            # 保持原有语义：未知目录 ID 由后续完整性校验报告 409，而非改成格式错误。
            values.append(value)
            continue
        values.append(utc_iso(parsed) if parsed.tzinfo is not None else value)
    return values


def _range_hour_ids(start, end):
    """把半开时间范围转换为其覆盖的上海逻辑小时，结束整点不额外取下一小时。"""
    try:
        start_at, end_at = parse_api_datetime(start), parse_api_datetime(end)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "时间范围必须是 ISO 8601 日期时间") from error
    if end_at <= start_at:
        raise HTTPException(422, "结束时间必须晚于开始时间")
    try:
        first = start_at.astimezone(SHANGHAI).replace(minute=0, second=0, microsecond=0)
        end_local = end_at.astimezone(SHANGHAI)
        last = end_local.replace(minute=0, second=0, microsecond=0)
        if end_local != last:
            last += timedelta(hours=1)
    except OverflowError as error:
        raise HTTPException(422, "时间范围超出可下载的小时上限") from error
    hour_count = (last - first) // timedelta(hours=1)
    if hour_count > 168:
        raise HTTPException(422, "单次最多下载168小时，请缩小时间范围")
    hours = []
    while first < last:
        hours.append(utc_iso(first))
        first += timedelta(hours=1)
    return hours


def _normalized_download_body(body):
    """在副本中统一范围和历史小时输入，避免调用方对象被幂等重试路径重复改写。"""
    hour_ids = _range_hour_ids(body.start, body.end) if body.hourIds is None else _canonical_hour_ids(body.hourIds)
    return body.model_copy(update={"hourIds": hour_ids, "start": None, "end": None})


def _file_query(body, kind, timestamp):
    """固定本次请求的默认搜索范围，事务重试不能让默认时间窗口漂移。"""
    query = {"taskId": body.taskId, "status": {"$ne": "DELETED"}}
    if kind == "DOWNLOAD":
        query["hour"] = {"$in": body.hourIds}
        return query, {}
    try:
        end = parse_api_datetime(body.end) if body.end else timestamp
        start = parse_api_datetime(body.start) if body.start else end - timedelta(hours=1)
        if not timedelta(0) < end - start <= timedelta(hours=24):
            raise ValueError()
    except ValueError as error:
        raise HTTPException(422, "时间范围必须是 ISO 8601 日期时间且不超过24小时") from error
    # 目录 hour 始终是 UTC 文本键，不能把上海显示偏移量写入字符串范围比较。
    query["hour"] = {"$gte": utc_iso(start.replace(minute=0, second=0, microsecond=0)), "$lte": utc_iso(end)}
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
    if kind == "DOWNLOAD":
        body = _normalized_download_body(body)
    # 排除新增范围字段的空值，确保旧 hourIds 请求仍命中升级前的幂等映射；
    # 搜索模型的序列化未改变，继续保留其原有摘要结构。
    payload = body.model_dump(exclude_none=True) if kind == "DOWNLOAD" else body.model_dump()
    digest = audited_mutations.request_digest(kind, payload)
    database = audited_mutations._majority_primary_database(repo)
    previous = await _confirmed(database, actor_id, key, digest)
    if previous is not None:
        return previous
    task = await repo.get("tasks", body.taskId)
    identifier, timestamp = new_id(), now()
    query, bounds = _file_query(body, kind, timestamp)
    execution_expires = timestamp + timedelta(hours=24)

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
                {"$max": {"retainUntil": execution_expires}}, session=session,
            )
            if protected.matched_count != 1:
                raise HTTPException(409, "日志片段正在清理，请刷新后重试")
        document = body.model_dump() | bounds | {
            "id": identifier, "kind": kind, "status": "QUEUED", "progress": 0,
            "actor": actor_id, "files": files, "nodeId": task.get("nodeId") or files[0]["nodeId"],
            "taskName": task["name"], "taskIp": task["ip"], "createdAt": timestamp,
            # 排队和执行沿用 24 小时上限；成功后覆盖 expiresAt，使下载保留期从
            # 制品真实创建完成而非提交排队时刻开始计算。
            "executionExpiresAt": execution_expires, "expiresAt": execution_expires,
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

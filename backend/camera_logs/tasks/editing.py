"""以 MongoDB 事务提交任务编辑、受控停止操作和审计。"""

import asyncio

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import TaskCreate, new_id
from camera_logs.common.security import authorize, authorize_owner, authorize_resource
from camera_logs.tasks.resource_binding import bind_resource


class _ResourceSnapshots:
    """向既有绑定器提供固定资源读取，确保存储身份和事务 guard 使用同一快照。"""

    def __init__(self, snapshots):
        self.snapshots = snapshots

    async def get(self, collection, identifier):
        """绑定器仅查询资源；缺少快照按正式资源不存在语义拒绝。"""
        if collection != "resources" or identifier not in self.snapshots:
            raise HTTPException(404, "设备资源不存在")
        return self.snapshots[identifier]


async def _prepared(repo, user, task_id, body):
    """在事务外固定密码、命令 ID 和资源绑定，驱动重试不得重复此准备。"""
    old = await repo.get("tasks", task_id)
    authorize_owner(user, old)
    if old["version"] != body.version:
        raise HTTPException(409, "配置版本已变化，请刷新")
    if body.sourceTemplateId and body.sourceTemplateId != old.get("sourceTemplateId"):
        # 只在来源实际改绑时校验读取权限；编辑器重提同一来源和既有独立快照均不受撤销共享影响。
        from camera_logs.commands.templates import readable_template
        template = await readable_template(repo, user, body.sourceTemplateId)
        if body.sourceTemplateVersion is None:
            body = body.model_copy(update={"sourceTemplateVersion": template["version"]})
    updates = body.model_dump(exclude_unset=True)
    updates.pop("version")
    clear = updates.pop("clearPassword", False)
    if updates.get("password") == "":
        updates.pop("password")
    base = {key: old[key] for key in TaskCreate.model_fields if key in old}
    base["password"] = repo.decrypt(old["passwordEncrypted"])
    base.update(updates)
    if clear:
        base["password"] = ""
    try:
        checked = TaskCreate(**base)
    except ValueError as error:
        raise HTTPException(422, "任务配置无效，请检查协议、账号和命令字段") from error
    for identifier in filter(None, (checked.resourceId, checked.serialServerResourceId)):
        authorize_resource(user, identifier)
    identifiers = sorted({value for value in (
        old["resourceId"], old.get("serialServerResourceId"), checked.resourceId,
        checked.serialServerResourceId,
    ) if value})
    # 快照必须早于绑定计算，避免资源身份在两次读取之间变化而混用旧存储身份和新版本。
    snapshots = {identifier: await repo.get("resources", identifier) for identifier in identifiers}
    document = checked.model_dump(exclude={"autoStart", "password"})
    document.update(await bind_resource(_ResourceSnapshots(snapshots), checked))
    if "scheduledCommands" in updates:
        for command in document["scheduledCommands"]:
            command["id"] = new_id()
    document.update(passwordEncrypted=repo.encrypt(checked.password), hasPassword=bool(checked.password))
    return old, updates, clear, document, snapshots


async def _guard_resources(db, snapshots, session):
    """按预处理版本和身份递增当前及新关联资源声明，阻止删除或换身份覆盖。"""
    for identifier, snapshot in snapshots.items():
        query = {"id": identifier, "deletedAt": None, "version": snapshot.get("version"),
                 "kind": snapshot.get("kind"), "ip": snapshot.get("ip")}
        if snapshot.get("kind") == "HIKVISION_NETWORK":
            query.update(model=snapshot.get("model"), subSerialNumber=snapshot.get("subSerialNumber"),
                         authenticatedAt=snapshot.get("authenticatedAt"))
        resource = await db.resources.find_one_and_update(
            query, {"$inc": {"controlClaimVersion": 1}}, session=session,
        )
        if resource is None:
            if await db.resources.find_one({"id": identifier}, session=session) is None:
                raise HTTPException(404, "设备资源不存在")
            raise HTTPException(409, "设备资源已删除，任务不会更新")


async def _confirm_queued_task_is_idle(db, task, session):
    """确认无归属排队任务没有遗留运行锁或未知旧运行，才能原地修改配置。"""
    if await db.endpoint_locks.find_one({"taskId": task["id"]}, session=session):
        raise HTTPException(409, "排队任务仍有运行锁，请先停止并核查")
    run_id = task.get("runId")
    if not run_id:
        return
    run = await db.runs.find_one({"id": run_id}, session=session)
    if run is None or not run.get("endedAt"):
        raise HTTPException(409, "排队任务存在未确认结束的旧运行，请先停止并核查")


async def edit_task(repo, user, task_id, body):
    """原子编辑任务；有归属的运行先停止重启，无归属的安全排队任务保留启动意图。"""
    authorize(user, "tasks:write", task_id)
    _old, updates, clear, document, resource_snapshots = await _prepared(repo, user, task_id, body)
    behavioral = set(updates) - {"name", "description"}
    restart = bool(behavioral or clear)
    operation_id = new_id()

    async def commit(session):
        """事务内按版本重读状态，写操作、任务与审计要么同时提交要么同时回滚。"""
        current = await repo.db.tasks.find_one_and_update(
            {"id": task_id, "version": body.version, "resourceDeleted": {"$ne": True}},
            {"$inc": {"controlClaimVersion": 1}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if current is None:
            raise HTTPException(409, "配置版本已变化或设备资源已删除，请刷新")
        authorize_owner(user, current)
        if (current["desiredState"] == "PAUSED" or current["status"] in {"PAUSED", "PAUSING"}) and behavioral | ({"clearPassword"} if clear else set()):
            raise HTTPException(409, "暂停期间只能修改名称和说明；修改连接或命令前请停止任务")
        await _guard_resources(repo.db, resource_snapshots, session)
        timestamp = now()
        changes = document | {"updatedAt": timestamp}
        if restart and current["desiredState"] == "RUNNING":
            if current.get("nodeId") is None:
                await _confirm_queued_task_is_idle(repo.db, current, session)
                changes.update(desiredState="RUNNING", restartRequested=False)
            else:
                authorize(user, "tasks:control", task_id)
                await repo.db.operations.update_many(
                    {"taskId": task_id, "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
                    {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
                )
                await repo.db.operations.insert_one(
                    {"id": operation_id, "taskId": task_id, "desiredState": "STOPPED", "action": "edit-stop",
                     "actor": user["id"], "status": "PENDING", "createdAt": timestamp}, session=session,
                )
                changes.update(desiredState="STOPPED", restartRequested=True, controlOperationId=operation_id)
        changed = await repo.db.tasks.find_one_and_update(
            {"id": task_id, "version": body.version, "resourceDeleted": {"$ne": True}},
            {"$set": changes, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if changed is None:
            raise HTTPException(409, "配置版本已变化或设备资源已删除，请刷新")
        await repo.audit(user["id"], "edit_task", task_id, session=session)
        return changed

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except asyncio.CancelledError:
        raise
    except PyMongoError as error:
        raise HTTPException(503, "任务编辑提交结果未知，请刷新任务后确认") from error

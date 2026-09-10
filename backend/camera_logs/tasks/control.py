"""原子提交任务控制意图、操作记录及审计，设备连接仍由原 Worker 管理。"""

import asyncio
import logging

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.common.security import authorize, authorize_owner

logger = logging.getLogger(__name__)


async def _guard_resources(db, task, session):
    """实际写入关联资源以与软删除冲突；只读快照不足以防止写偏差。"""
    if task.get("resourceDeleted"):
        raise HTTPException(409, "设备资源已删除，仅可查询已有日志")
    identifiers = sorted({value for value in (task["resourceId"], task.get("serialServerResourceId")) if value})
    for identifier in identifiers:
        resource = await db.resources.find_one_and_update(
            {"id": identifier, "deletedAt": None}, {"$inc": {"controlClaimVersion": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        if resource is None:
            if await db.resources.find_one({"id": identifier}, session=session) is None:
                raise HTTPException(404, "设备资源不存在")
            raise HTTPException(409, "设备资源已删除，仅可查询已有日志")
        if resource.get("healthStatus") in {"AUTH_FAILED", "OFFLINE", "ERROR"}:
            raise HTTPException(409, "设备资源认证或连通性异常，暂不能启动采集")


def _validate(task, desired, require_paused):
    """根据本次事务读取的状态校验 SSH 暂停/继续，不能使用路由外旧快照。"""
    if require_paused and (task["status"] != "PAUSED"
                           or task["desiredState"] != "PAUSED" or task.get("nodeId") is not None):
        raise HTTPException(409, "任务尚未完成SSH暂停")
    if task["protocol"] == "TELNET_SERIAL" and (require_paused or desired == "PAUSED"):
        raise HTTPException(409, "串口 Telnet 任务不支持暂停或恢复")
    if desired == "PAUSED" and task["desiredState"] not in {"RUNNING", "PAUSED"}:
        raise HTTPException(409, "仅运行中的任务可以暂停")


def _reached(task, desired):
    """操作成功必须基于已完成的物理状态；反向意图不能借用过渡前的状态。"""
    if desired == "RUNNING":
        return (task["desiredState"] == "RUNNING" and task["status"] == "COLLECTING"
                and task.get("nodeId") is not None and bool(task.get("runId"))
                and bool(task.get("generation")) and not task.get("restartRequested"))
    return task["status"] == desired and task.get("nodeId") is None


async def _previous_operation(db, task, desired, session):
    """同一已接受意图复用操作；已取消/失败或未达到目标的终态不能冒充新成功。"""
    if task["desiredState"] != desired:
        return None
    query = {"taskId": task["id"], "desiredState": desired}
    if task.get("controlOperationId"):
        query["id"] = task["controlOperationId"]
    else:
        # 未曾由本模块控制的既有任务，只复用原有未完成操作。
        query["status"] = "PENDING"
    previous = await db.operations.find_one(query, session=session)
    if previous and (previous["status"] == "PENDING"
                     or previous["status"] == "SUCCEEDED" and _reached(task, desired)):
        return previous
    return None


async def _validate_paused_run(db, task, session):
    """已暂停的运行只能在锁和未结束运行均完整时恢复；未领取过的暂停可新建运行。"""
    if task["status"] != "PAUSED" or task.get("nodeId") is not None:
        return
    lock = await db.endpoint_locks.find_one({"taskId": task["id"]}, session=session)
    run_id = task.get("runId")
    if not run_id:
        if lock:
            raise HTTPException(409, "暂停任务仍有旧运行锁，请先停止并核查")
        return
    run = await db.runs.find_one({"id": run_id}, session=session)
    if not lock or lock.get("runId") != run_id or not run or run.get("endedAt"):
        raise HTTPException(409, "暂停运行或运行锁不完整，请先停止任务再重新启动")


async def _local_transition(db, task, desired, session):
    """无节点时只处理能够确认无活动连接的本地状态，保留历史预算和运行记录。"""
    changes, unset = {}, {}
    if task.get("nodeId") is not None:
        return changes, unset
    if desired == "STOPPED" and task["status"] in {"PAUSED", "WAITING_DEVICE"}:
        run_id = task.get("runId")
        # 已暂停意味着原连接关闭，缺少旧锁/运行记录仍可停止恢复；但未知运行的锁
        # 不属于本次收尾权限，不能遗留它并向调用者报告停止成功。
        lock_query = {"taskId": task["id"]}
        if run_id:
            lock_query["runId"] = {"$ne": run_id}
        if await db.endpoint_locks.find_one(lock_query, session=session):
            raise HTTPException(409, "暂停任务存在不匹配的运行锁，请先核查旧运行")
        if run_id:
            await db.endpoint_locks.delete_one({"taskId": task["id"], "runId": run_id}, session=session)
            await db.runs.update_one({"id": run_id, "endedAt": None},
                                     {"$set": {"endedAt": now()}}, session=session)
        changes["status"] = "STOPPED"
    elif desired == "PAUSED" and task["status"] == "WAITING_DEVICE":
        # 等待 HTTP 探测时再次暂停只撤销继续意图，保留原暂停运行、锁和命令预算。
        changes["status"] = "PAUSED"
    elif desired == "PAUSED" and task["status"] in {"STOPPED", "PENDING"}:
        # 尚未领取的新任务不需要 Worker 关闭连接；但不能复用上次已结束的 runId。
        if await db.endpoint_locks.find_one({"taskId": task["id"]}, session=session):
            raise HTTPException(409, "任务仍有运行锁，请先确认旧运行已停止")
        if task.get("runId"):
            run = await db.runs.find_one({"id": task["runId"]}, session=session)
            if not run or not run.get("endedAt"):
                raise HTTPException(409, "任务旧运行尚未确认结束，请先停止并核查")
        changes.update(status="PAUSED", pausedAt=now())
        unset.update(runId="", sessionId="")
    elif desired == "RUNNING" and task["status"] == "ERROR":
        changes.update(status="STOPPED", error=None)
    return changes, unset


async def request_control(repo, task_id, desired, user, *, require_paused=False):
    """串行化同任务控制，事务重试复用固定操作 ID；不在 API 中打开设备连接。

    写任务声明版本使并发控制、领取和停止相互冲突。关联资源也实际递增声明版本，
    删除先提交则控制重试后拒绝，控制先提交则后续删除扫尾停止该任务。已复用的
    操作不新增成功审计；每次 HTTP 请求仍由访问日志独立记录。
    """
    authorize(user, "tasks:control", task_id)
    if desired not in {"RUNNING", "STOPPED", "PAUSED"}:
        raise ValueError("不支持的任务控制状态")
    identifier = new_id()
    action = "resume" if require_paused else {"RUNNING": "start", "STOPPED": "stop", "PAUSED": "pause"}[desired]

    async def commit(session):
        db = repo.db
        claim_update = {"$inc": {"controlClaimVersion": 1}}
        # 用户停止（含已停止任务的幂等重放）必须原子撤销系统故障恢复资格。
        if desired == "STOPPED":
            claim_update["$unset"] = {"resourceHealthRecovery": ""}
        task = await db.tasks.find_one_and_update(
            {"id": task_id}, claim_update,
            return_document=ReturnDocument.AFTER, session=session,
        )
        if task is None:
            raise HTTPException(404, "任务不存在")
        authorize_owner(user, task)
        if desired == "RUNNING" and task.get("status") == "BLOCKED":
            raise HTTPException(409, "任务阻塞，必须使用重新启动或隔离确认")
        if require_paused and task.get("status") == "WAITING_DEVICE":
            previous = await db.operations.find_one({"id": task.get("controlOperationId"), "taskId": task_id,
                                                     "action": "resume-wait-device", "status": "PENDING"}, session=session)
            if previous:
                return previous
        # 第三方设备重启期间，暂停运行保留原预算，显式继续转为等待 HTTP 重新认证。
        resource = await db.resources.find_one({"id": task["resourceId"], "deletedAt": None}, session=session)
        if require_paused and task["protocol"] in {"SSH", "TELNET_DEVICE"} and task["status"] == "PAUSED" and task.get("nodeId") is None \
                and task["desiredState"] == "PAUSED" and resource and resource.get("kind") == "HIKVISION_NETWORK":
            await _validate_paused_run(db, task, session)
            timestamp = now()
            operation = {"id": identifier, "taskId": task_id, "desiredState": "RUNNING", "action": "resume-wait-device",
                         "actor": user["id"], "status": "PENDING", "createdAt": timestamp}
            await db.operations.update_many({"taskId": task_id, "status": "PENDING"},
                                            {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session)
            await db.operations.insert_one(operation, session=session)
            await db.tasks.update_one({"id": task_id, "desiredState": "PAUSED"}, {"$set": {
                "desiredState": "RUNNING", "status": "WAITING_DEVICE", "controlOperationId": identifier,
                "resumeWaiting": {"operationId": identifier, "pausedRunId": task.get("runId"),
                                  "generation": task.get("generation"), "requestedAt": timestamp,
                                  "waitingReason": resource.get("healthStatus") or "CHECKING"}, "updatedAt": timestamp}}, session=session)
            await db.resources.update_one({"id": resource["id"]}, {"$set": {"nextHealthCheckAt": timestamp}} ,session=session)
            await repo.audit(user["id"], "control:RUNNING", task_id, session=session)
            return operation
        if desired != "STOPPED" and not (desired == "PAUSED" and task.get("status") == "WAITING_DEVICE"):
            await _guard_resources(db, task, session)
        previous = await _previous_operation(db, task, desired, session)
        # 继续请求一经接受，desiredState 已是 RUNNING。仅重放确由 resume 建立的
        # 当前操作，避免把任意运行任务上的 resume 误当作第一次有效继续。
        if require_paused and previous and previous.get("action") == "resume" and task["protocol"] in {"SSH", "TELNET_DEVICE"}:
            return previous
        _validate(task, desired, require_paused)
        if desired != "STOPPED":
            await _validate_paused_run(db, task, session)
        if previous:
            return previous

        changes, unset = await _local_transition(db, task, desired, session)
        # 无节点排队任务的暂停可以立即完成；运行中反向请求仍等待 Worker 收尾。
        observed = task | changes
        if changes.get("status") == "PAUSED":
            observed["desiredState"] = desired
        timestamp = now()
        operation = {"id": identifier, "taskId": task_id, "desiredState": desired,
                     "action": action,
                     "actor": user["id"], "status": "SUCCEEDED" if _reached(observed, desired) else "PENDING",
                     "createdAt": timestamp}
        if operation["status"] == "SUCCEEDED":
            operation["completedAt"] = timestamp
        await db.operations.update_many(
            {"taskId": task_id, "desiredState": {"$ne": desired}, "status": "PENDING"},
            {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
        )
        await db.operations.insert_one(operation, session=session)
        changes.update(desiredState=desired, updatedAt=timestamp, controlOperationId=identifier)
        if desired == "STOPPED":
            changes["restartRequested"] = False
        if desired in {"STOPPED", "PAUSED"}:
            unset["resumeWaiting"] = ""
        query = {"id": task_id}
        if desired != "STOPPED":
            query["resourceDeleted"] = {"$ne": True}
        update = {"$set": changes}
        if unset:
            update["$unset"] = unset
        changed = await db.tasks.update_one(query, update, session=session)
        if not changed.matched_count:
            raise HTTPException(409, "设备资源已删除，仅可查询已有日志")
        await repo.audit(user["id"], "control:" + desired, task_id, session=session)
        return operation

    try:
        return await audited_mutations.mutation_transaction(repo, commit)
    except asyncio.CancelledError:
        raise
    except PyMongoError as error:
        # 只确认本次固定 ID 的操作；查不到不代表事务已回滚，禁止自行补做控制写入。
        try:
            db = repo.db.with_options(read_concern=ReadConcern("majority"), read_preference=ReadPreference.PRIMARY)
            confirmed = await db.operations.find_one({"id": identifier, "taskId": task_id,
                                                     "actor": user["id"], "desiredState": desired, "action": action})
            if confirmed:
                return confirmed
        except PyMongoError:
            pass
        logger.warning("任务控制提交结果未知 task=%s operation=%s", task_id, identifier)
        raise HTTPException(503, "任务控制提交结果未知，请查询任务与操作状态后再操作") from error

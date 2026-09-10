"""周期认证已保存海康资源，并以版本条件协调任务停止、恢复和身份目录更新。"""

import asyncio
import logging
from datetime import UTC, timedelta

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.resources.authentication import DeviceOfflineError, authenticate_network_resource
from camera_logs.resources.lifecycle import task_resource_query
from camera_logs.tasks.resource_binding import storage_identity

logger = logging.getLogger(__name__)
HEALTH_INTERVAL_SECONDS = 60
HEALTH_CONCURRENCY = 8


def _not_after(first, second):
    """比较 Mongo 可能返回的无时区旧时间与统一 UTC 时间，避免测试库和旧数据类型冲突。"""
    if getattr(first, "tzinfo", None) is None:
        first = first.replace(tzinfo=UTC)
    if getattr(second, "tzinfo", None) is None:
        second = second.replace(tzinfo=UTC)
    return first <= second


def _result_filter(snapshot):
    """健康请求只有持有租约令牌时才可提交；直接单元调用兼容无租约快照。"""
    query = {"id": snapshot["id"], "deletedAt": None}
    if "healthLeaseToken" in snapshot:
        query.update(healthRevision=snapshot["healthRevision"], healthLeaseToken=snapshot["healthLeaseToken"])
    return query


async def check_resource(repo, snapshot):
    """认证单个资源；网络请求在事务外，迟到结果由资源版本 CAS 丢弃。"""
    try:
        metadata = await authenticate_network_resource(
            ip=snapshot["ip"], username=snapshot["username"],
            password=repo.decrypt(snapshot.get("passwordEncrypted", "")), auth_type=snapshot["authType"],
        )
    except PermissionError:
        await _apply_failure(repo, snapshot, "AUTH_FAILED")
    except DeviceOfflineError:
        await _apply_failure(repo, snapshot, "OFFLINE")
    except Exception:  # 认证协议、响应格式和连接错误分别保留在服务日志，状态不混同凭据失败。
        logger.exception("资源周期认证异常 resource=%s", snapshot["id"])
        await _apply_failure(repo, snapshot, "ERROR")
    else:
        await _apply_success(repo, snapshot, metadata)


async def _apply_failure(repo, snapshot, status):
    """保存失败类别并受控停止关联采集；只有原本运行的任务才有恢复资格。"""
    timestamp = now()

    async def commit(session):
        changed = await repo.db.resources.find_one_and_update(
            _result_filter(snapshot),
            {"$set": {"healthStatus": status, "healthCheckedAt": timestamp,
                      "nextHealthCheckAt": timestamp + timedelta(seconds=HEALTH_INTERVAL_SECONDS)},
             "$inc": {"healthRevision": 1}}, session=session,
        )
        if changed is None:
            return False
        query = task_resource_query(snapshot["id"])
        async for task in repo.db.tasks.find(query, session=session):
            # 用户暂停意图一经落库即由 Worker 自行收尾，周期离线不能覆盖预算、会话或控制操作。
            if task.get("desiredState") == "PAUSED" or task.get("status") in {"PAUSED", "WAITING_DEVICE"}:
                if task.get("status") == "WAITING_DEVICE":
                    await repo.db.tasks.update_one({"id": task["id"], "status": "WAITING_DEVICE"},
                                                   {"$set": {"resumeWaiting.waitingReason": status}}, session=session)
                continue
            previous_state = task.get("desiredState")
            if previous_state not in {"RUNNING", "PAUSED"}:
                # 已停止任务没有连接可回收；重复离线不应覆盖手动控制操作或制造无限系统操作。
                continue
            recovery = previous_state in {"RUNNING", "PAUSED"}
            update = {"desiredState": "STOPPED", "restartRequested": False, "updatedAt": timestamp}
            if recovery:
                update["resourceHealthRecovery"] = {"resourceId": snapshot["id"], "stoppedAt": timestamp,
                                                     "desiredState": previous_state}
            await repo.db.tasks.update_one(
                {"id": task["id"], "controlClaimVersion": task.get("controlClaimVersion")},
                {"$set": update}, session=session,
            )
            await repo.db.operations.update_many(
                {"taskId": task["id"], "desiredState": {"$ne": "STOPPED"}, "status": "PENDING"},
                {"$set": {"status": "CANCELLED", "completedAt": timestamp}}, session=session,
            )
            operation_id = new_id()
            await repo.db.operations.insert_one({
                "id": operation_id, "taskId": task["id"], "desiredState": "STOPPED", "action": "system-resource-health-stop",
                "actor": "system", "status": "PENDING", "createdAt": timestamp,
            }, session=session)
            await repo.db.tasks.update_one({"id": task["id"], "desiredState": "STOPPED"},
                                           {"$set": {"controlOperationId": operation_id}}, session=session)
        if snapshot.get("healthStatus") != status:
            await repo.audit("system", "resource_health_stop:" + status, snapshot["id"], session=session)
        return True

    await audited_mutations.mutation_transaction(repo, commit)


async def _apply_success(repo, snapshot, metadata):
    """后台成功只更新健康和身份，恢复资格只能由用户保存认证的事务消费。"""
    timestamp = now()
    async def commit(session):
        changed = await repo.db.resources.find_one_and_update(
            _result_filter(snapshot),
            {"$set": {"healthStatus": "ONLINE", "healthCheckedAt": timestamp,
                      "nextHealthCheckAt": timestamp + timedelta(seconds=HEALTH_INTERVAL_SECONDS),
                      **({**metadata, "authenticatedAt": timestamp, "updatedAt": timestamp}
                         if any(snapshot.get(key) != value for key, value in metadata.items()) else {})},
             "$inc": {"healthRevision": 1}},
            session=session,
        )
        if changed is None:
            return False
        if storage_identity(snapshot) != storage_identity(snapshot | metadata):
            identity = storage_identity(snapshot | metadata)
            async for task in repo.db.tasks.find(task_resource_query(snapshot["id"]), session=session):
                await _transition_identity_task(repo, task, snapshot["id"], identity, timestamp, session)
        # 显式 resume 的设备探测成功，只把同一暂停运行恢复至 PAUSED，领取层再沿用 runId。
        async for task in repo.db.tasks.find({"resourceId": snapshot["id"], "status": "WAITING_DEVICE",
                                              "desiredState": "RUNNING"}, session=session):
            waiting = task.get("resumeWaiting")
            same_paused_run = waiting and waiting.get("pausedRunId") == task.get("runId")
            replaced_device = waiting and waiting.get("identityChangedAt") and not task.get("runId")
            if waiting and (same_paused_run or replaced_device) and waiting.get("generation") == task.get("generation") \
                    and _not_after(waiting.get("requestedAt"), snapshot.get("healthLeaseStartedAt", timestamp)):
                await repo.db.tasks.update_one({"id": task["id"], "status": "WAITING_DEVICE", "resumeWaiting": waiting},
                                               {"$set": {"status": "PAUSED", "updatedAt": timestamp},
                                                "$unset": {"resumeWaiting": ""}}, session=session)
        return True

    await audited_mutations.mutation_transaction(repo, commit)


async def grant_after_user_authentication(repo, resource, session, *, identity_changed=False):
    """用户保存已认证凭据后才恢复系统停止任务，旧节点、锁或运行未收尾时保留标记。"""
    timestamp = now()
    identity = storage_identity(resource)
    async for task in repo.db.tasks.find(task_resource_query(resource["id"]), session=session):
        marker = task.get("resourceHealthRecovery")
        if task.get("resourceId") == resource["id"]:
            if identity_changed:
                await _transition_identity_task(repo, task, resource["id"], identity, timestamp, session)
            else:
                await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"storageIdentity": identity,
                                                                              "updatedAt": timestamp}}, session=session)
        if not marker or marker.get("resourceId") != resource["id"] or task.get("desiredState") != "STOPPED":
            continue
        marker = marker | {"authorizedAt": timestamp}
        await repo.db.tasks.update_one({"id": task["id"], "resourceHealthRecovery": task["resourceHealthRecovery"]},
                                       {"$set": {"resourceHealthRecovery": marker}}, session=session)


async def _transition_identity_task(repo, task, resource_id, identity, timestamp, session):
    """身份变更将旧暂停运行完整结束；活跃运行交给原 Worker 收尾，避免目录与预算跨设备混用。"""
    update = {"storageIdentity": identity, "updatedAt": timestamp}
    if task.get("nodeId") is not None and task.get("desiredState") == "RUNNING":
        update.update(desiredState="STOPPED", restartRequested=True)
        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": update}, session=session)
        return
    if task.get("status") in {"PAUSED", "WAITING_DEVICE"}:
        run_id = task.get("runId")
        if run_id:
            await repo.db.endpoint_locks.delete_one({"taskId": task["id"], "runId": run_id}, session=session)
            await repo.db.runs.update_one({"id": run_id, "endedAt": None}, {"$set": {"endedAt": timestamp}}, session=session)
        # 手动暂停保留用户暂停意图但清空旧运行；等待继续保留原 operation，等待新探测。
        if task.get("status") == "PAUSED":
            update.update(desiredState="PAUSED", status="PAUSED", restartRequested=False)
        else:
            update.update(desiredState="RUNNING", status="WAITING_DEVICE")
            waiting = task.get("resumeWaiting") or {}
            update["resumeWaiting"] = waiting | {"pausedRunId": None, "identityChangedAt": timestamp}
        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": update,
                                                               "$unset": {"runId": "", "sessionId": ""}}, session=session)
        return
    await repo.db.tasks.update_one({"id": task["id"]}, {"$set": update}, session=session)
async def reconcile_authorized_recoveries(repo):
    """调度周期消费已获用户授权但尚在收尾的恢复标记，用户停止会先原子清除标记。"""
    async for resource in repo.db.resources.find({"kind": "HIKVISION_NETWORK", "deletedAt": None,
                                                   "healthStatus": "ONLINE"}):
        async for task in repo.db.tasks.find({**task_resource_query(resource["id"]), "desiredState": "STOPPED",
                                              "resourceHealthRecovery.authorizedAt": {"$exists": True}}):
            await _consume_authorized_recovery(repo, resource["id"], task["id"])


async def _consume_authorized_recovery(repo, resource_id, task_id):
    """事务内同时声明在线资源和任务标记，避免迟到失败将任务错误恢复为运行。"""
    async def commit(session):
        resource = await repo.db.resources.find_one_and_update(
            {"id": resource_id, "deletedAt": None, "healthStatus": "ONLINE"},
            {"$inc": {"healthConsumeClaim": 1}}, session=session)
        if resource is None:
            return False
        task = await repo.db.tasks.find_one({"id": task_id, "desiredState": "STOPPED",
                                             "resourceHealthRecovery.authorizedAt": {"$exists": True}}, session=session)
        if task is None or task.get("nodeId") is not None or await repo.db.endpoint_locks.find_one({"taskId": task_id}, session=session):
            return False
        run_id = task.get("runId")
        run = await repo.db.runs.find_one({"id": run_id}, session=session) if run_id else None
        if run_id and (run is None or not run.get("endedAt")):
            await repo.db.tasks.update_one({"id": task_id, "resourceHealthRecovery": task["resourceHealthRecovery"]},
                                           {"$set": {"status": "BLOCKED", "error": "系统停止后运行记录未收尾"}}, session=session)
            return False
        target = task["resourceHealthRecovery"].get("desiredState", "RUNNING")
        update, unset = {"desiredState": target, "restartRequested": False, "updatedAt": now()}, {"resourceHealthRecovery": ""}
        if target == "PAUSED":
            update["status"] = "PAUSED"
            unset.update(runId="", sessionId="")
        changed = await repo.db.tasks.update_one({"id": task_id, "desiredState": "STOPPED",
                                                   "resourceHealthRecovery": task["resourceHealthRecovery"]},
                                                  {"$set": update, "$unset": unset}, session=session)
        return bool(changed.matched_count)
    return await audited_mutations.mutation_transaction(repo, commit)


async def health_once(repo, *, concurrency=HEALTH_CONCURRENCY):
    """读取未删除网络资源快照并有限并发认证，单个失败不阻塞下一资源。"""
    timestamp = now()
    resources = [item async for item in repo.db.resources.find(
        {"kind": "HIKVISION_NETWORK", "deletedAt": None,
         "$or": [{"nextHealthCheckAt": {"$exists": False}}, {"nextHealthCheckAt": {"$lte": timestamp}}]})]
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(item):
        async with semaphore:
            claimed_at = now()
            token = new_id()
            claimed = await repo.db.resources.find_one_and_update(
                {"id": item["id"], "deletedAt": None,
                 "$and": [
                     {"$or": [{"healthRevision": item.get("healthRevision", 0)},
                               {"healthRevision": {"$exists": False}}]},
                     {"$or": [{"healthLeaseUntil": {"$exists": False}},
                               {"healthLeaseUntil": {"$lte": claimed_at}}]},
                 ]},
                {"$set": {"healthLeaseUntil": claimed_at + timedelta(seconds=20), "healthLeaseStartedAt": claimed_at,
                          "healthLeaseToken": token},
                 "$inc": {"healthRevision": 1}},
            )
            if claimed:
                claimed["healthRevision"] = item.get("healthRevision", 0) + 1
                claimed["healthLeaseToken"] = token
                claimed["healthLeaseStartedAt"] = claimed_at
                await check_resource(repo, claimed)

    results = await asyncio.gather(*(guarded(item) for item in resources), return_exceptions=True)
    for item, result in zip(resources, results, strict=True):
        if isinstance(result, BaseException):
            logger.exception("资源周期认证任务异常 resource=%s", item["id"], exc_info=result)


async def health_loop(repo):
    """独立后台循环；异常仅记录本轮，调度器不会因设备 HTTP 慢请求被阻塞。"""
    while True:
        try:
            await health_once(repo)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("资源周期认证轮次失败")
        await asyncio.sleep(HEALTH_INTERVAL_SECONDS)

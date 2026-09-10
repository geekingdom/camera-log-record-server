"""周期认证已保存海康资源，并以版本条件协调任务停止、恢复和身份目录更新。"""

import asyncio
import logging
from datetime import UTC, timedelta

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.models import new_id
from camera_logs.resources.authentication import DeviceOfflineError, authenticate_network_resource
from camera_logs.resources.authentication_records import record_authentication
from camera_logs.resources.lifecycle import task_resource_query
from camera_logs.tasks.resource_binding import storage_identity

logger = logging.getLogger(__name__)
HEALTH_INTERVAL_SECONDS = 60
# 上一轮到期认证完成后，最多等待本间隔再次读取到期项；慢认证批次会顺延下一轮扫描。
HEALTH_SCAN_INTERVAL_SECONDS = 1
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


async def _resume_requested_after_probe(repo, snapshot, resource, session):
    """识别探测开始后写入的恢复请求，保留其立即到期语义供下一次认证使用。"""
    started_at = snapshot.get("healthLeaseStartedAt")
    next_check_at = resource.get("nextHealthCheckAt")
    previous_next_check_at = snapshot.get("nextHealthCheckAt")
    if not started_at or not next_check_at or next_check_at == previous_next_check_at \
            or _not_after(next_check_at, started_at):
        return False
    async for task in repo.db.tasks.find(
            {"resourceId": snapshot["id"], "status": "WAITING_DEVICE", "desiredState": "RUNNING"},
            session=session):
        requested_at = (task.get("resumeWaiting") or {}).get("requestedAt")
        if requested_at and _not_after(started_at, requested_at):
            return True
    return False


async def _commit_health_result(repo, snapshot, changes, session):
    """按租约令牌提交结果；恢复后来到的旧结果只释放自身租约并保留到期请求。"""
    current = await repo.db.resources.find_one(_result_filter(snapshot), session=session)
    if current is None:
        return False
    if await _resume_requested_after_probe(repo, snapshot, current, session):
        changes.pop("nextHealthCheckAt", None)
    update = {"$set": changes, "$inc": {"healthRevision": 1}}
    if "healthLeaseToken" in snapshot:
        # 网络请求已经完成，且查询条件仍匹配原令牌时才可交还领取权。
        update["$unset"] = {"healthLeaseUntil": "", "healthLeaseStartedAt": "", "healthLeaseToken": ""}
    changed = await repo.db.resources.find_one_and_update(_result_filter(snapshot), update, session=session)
    return changed is not None


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
        changed = await _commit_health_result(
            repo, snapshot,
            {"healthStatus": status, "healthCheckedAt": timestamp,
             "nextHealthCheckAt": timestamp + timedelta(seconds=HEALTH_INTERVAL_SECONDS)},
            session,
        )
        if not changed:
            return False
        await record_authentication(repo, snapshot, source="PERIODIC", result=status, before=snapshot,
                                    after=snapshot, message=status, completed_at=timestamp, session=session)
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
                marker = task.get("resourceHealthRecovery")
                if marker and marker.get("resourceId") == snapshot["id"] and (
                        marker.get("reason") != status or marker.get("authorizedAt") is not None):
                    # 凭据或认证协议失败必须等待用户更新；后续离线不能降低这项恢复门槛。
                    reason = marker.get("reason") if marker.get("reason") in {"AUTH_FAILED", "ERROR"} else status
                    new_marker = marker | {"reason": reason, "observedAt": timestamp}
                    new_marker.pop("authorizedAt", None)
                    await repo.db.tasks.update_one(
                        {"id": task["id"], "resourceHealthRecovery": marker},
                        {"$set": {"resourceHealthRecovery": new_marker}},
                        session=session,
                    )
                continue
            recovery = previous_state in {"RUNNING", "PAUSED"}
            update = {"desiredState": "STOPPED", "restartRequested": False, "updatedAt": timestamp}
            if recovery:
                update["resourceHealthRecovery"] = {"resourceId": snapshot["id"], "stoppedAt": timestamp,
                                                     "desiredState": previous_state, "reason": status}
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
    """认证成功更新健康；仅 OFFLINE 后的同设备系统停止可自动取得恢复资格。"""
    timestamp = now()
    async def commit(session):
        changed = await _commit_health_result(
            repo, snapshot,
            {"healthStatus": "ONLINE", "healthCheckedAt": timestamp,
             "nextHealthCheckAt": timestamp + timedelta(seconds=HEALTH_INTERVAL_SECONDS),
             **({**metadata, "authenticatedAt": timestamp, "updatedAt": timestamp}
                if any(snapshot.get(key) != value for key, value in metadata.items()) else {})},
            session,
        )
        if not changed:
            return False
        await record_authentication(repo, snapshot, source="PERIODIC", result="SUCCESS", before=snapshot,
                                    after=snapshot | metadata, completed_at=timestamp, session=session)
        if storage_identity(snapshot) != storage_identity(snapshot | metadata):
            identity = storage_identity(snapshot | metadata)
            async for task in repo.db.tasks.find(task_resource_query(snapshot["id"]), session=session):
                await _transition_identity_task(repo, task, snapshot["id"], identity, timestamp, session)
        if snapshot.get("healthStatus") == "OFFLINE":
            await _authorize_offline_recoveries(repo, snapshot["id"], timestamp, session)
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


async def _authorize_offline_recoveries(repo, resource_id, timestamp, session):
    """仅为本资源 OFFLINE 产生的系统停止标记授权，实际恢复仍等待收尾确认。"""
    async for task in repo.db.tasks.find(task_resource_query(resource_id), session=session):
        marker = task.get("resourceHealthRecovery")
        if not marker or marker.get("resourceId") != resource_id or marker.get("reason") != "OFFLINE" \
                or task.get("desiredState") != "STOPPED":
            continue
        await repo.db.tasks.update_one(
            {"id": task["id"], "desiredState": "STOPPED", "resourceHealthRecovery": marker},
            {"$set": {"resourceHealthRecovery": marker | {"authorizedAt": timestamp}}}, session=session,
        )


async def grant_after_user_authentication(repo, resource, session, *, identity_changed=False):
    """用户保存并验证凭据后授权系统停止任务，旧节点、锁或运行未收尾时保留标记。"""
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
                                             "resourceHealthRecovery.resourceId": resource_id,
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
        elif task.get("status") == "BLOCKED":
            # 仅在无 owner/lock 且旧运行已确认结束后，才解除隔离阻塞供调度器领取。
            update.update(status="STOPPED", error=None)
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
    """短周期扫描到期资源；单个资源仍按认证周期和租约控制实际网络请求。"""
    while True:
        try:
            await health_once(repo)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("资源周期认证轮次失败")
        await asyncio.sleep(HEALTH_SCAN_INTERVAL_SECONDS)

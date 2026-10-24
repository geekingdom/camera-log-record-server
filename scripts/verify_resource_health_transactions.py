"""在独立 Mongo 副本集库验证资源健康恢复、R80退避和手动认证事务。"""

import asyncio
import json
from datetime import timedelta
from tempfile import TemporaryDirectory
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.health import (
    _apply_failure,
    _apply_success,
    apply_manual_result,
    reconcile_authorized_recoveries,
)
from camera_logs.resources.health_policy import stop_unresponsive_task
from camera_logs.tasks.control import request_control
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient


def _public_document(document):
    """移除 Mongo 内部标识后比较完整任务快照，避免只验证部分状态字段。"""
    return {key: value for key, value in document.items() if key != "_id"}


async def _assert_serial_health_boundary(repo, *, status, manual):
    """在真实事务中验证 HTTP 认证恶化不改变串口任务，网络未领取任务仍停止。"""
    prefix = ("manual" if manual else "periodic") + "-" + status.lower()
    resource = {
        "id": f"serial-boundary-{prefix}", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.251",
        "deletedAt": None, "healthStatus": "ONLINE", "healthFailureCount": 3,
        "version": 1, "healthRevision": 1, "passwordEncrypted": repo.encrypt("verification-secret"),
    }
    await repo.db.resources.insert_one(resource)
    serial_collecting = {
        "id": f"{prefix}-serial-collecting", "resourceId": resource["id"], "protocol": "TELNET_SERIAL",
        "desiredState": "RUNNING", "status": "COLLECTING", "nodeId": "serial-node", "runId": "serial-run",
        "generation": 7, "sessionId": "serial-session", "controlClaimVersion": 11,
        "ip": "192.0.2.251", "port": 9000, "createdBy": "verification",
    }
    serial_pending = {
        "id": f"{prefix}-serial-pending", "resourceId": resource["id"], "protocol": "TELNET_SERIAL",
        "desiredState": "RUNNING", "status": "PENDING", "nodeId": None, "runId": "serial-pending-run",
        "generation": 8, "controlClaimVersion": 12, "ip": "192.0.2.251", "port": 9001,
        "createdBy": "verification",
    }
    network_tasks = [
        {"id": f"{prefix}-ssh", "resourceId": resource["id"], "protocol": "SSH", "desiredState": "RUNNING",
         "status": "PENDING", "nodeId": None, "ip": "192.0.2.251", "port": 22, "createdBy": "verification"},
        {"id": f"{prefix}-telnet-device", "resourceId": resource["id"], "protocol": "TELNET_DEVICE",
         "desiredState": "RUNNING", "status": "PENDING", "nodeId": None, "ip": "192.0.2.251", "port": 23,
         "createdBy": "verification"},
    ]
    await repo.db.tasks.insert_many([serial_collecting, serial_pending, *network_tasks])
    serial_before = {
        task["id"]: _public_document(await repo.db.tasks.find_one({"id": task["id"]}))
        for task in (serial_collecting, serial_pending)
    }
    if manual:
        snapshot = await repo.db.resources.find_one({"id": resource["id"]})
        await apply_manual_result(repo, snapshot | {"manualHealthResult": True}, status=status,
                                  metadata={}, actor="verification")
    else:
        await _apply_failure(repo, await repo.db.resources.find_one({"id": resource["id"]}), status)
    changed_resource = await repo.db.resources.find_one({"id": resource["id"]})
    assert changed_resource["healthStatus"] == status
    for identifier, expected in serial_before.items():
        assert _public_document(await repo.db.tasks.find_one({"id": identifier})) == expected
    for task in network_tasks:
        stopped = await repo.db.tasks.find_one({"id": task["id"]})
        assert stopped["desiredState"] == "STOPPED"
        assert stopped["resourceHealthRecovery"]["reason"] == status
    resource_before_direct_stop = _public_document(changed_resource)
    operation_count = await repo.db.operations.count_documents({"taskId": {"$in": list(serial_before)}})
    assert not await stop_unresponsive_task(repo, serial_collecting, liveness_check=lambda: True)
    assert _public_document(await repo.db.resources.find_one({"id": resource["id"]})) == resource_before_direct_stop
    assert await repo.db.operations.count_documents({"taskId": {"$in": list(serial_before)}}) == operation_count
    for identifier, expected in serial_before.items():
        assert _public_document(await repo.db.tasks.find_one({"id": identifier})) == expected


async def main():
    """只创建随机验证库和临时日志目录，finally 中无条件删除两者。"""
    configured, name = Settings(), "resource_health_verify_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with TemporaryDirectory(prefix="resource-health-") as directory:
        settings = Settings(mongo_uri=configured.mongo_uri, database_name=name, log_root=directory,
                            encryption_key=Fernet.generate_key().decode(), bootstrap_token="verify", start_background=False)
        repo = Repository(mongo[name], settings)
        try:
            await repo.initialize()
            for failure_status in ("AUTH_FAILED", "OFFLINE", "ERROR"):
                await _assert_serial_health_boundary(repo, status=failure_status, manual=False)
                await _assert_serial_health_boundary(repo, status=failure_status, manual=True)
            await repo.db.resources.insert_one({"id": "resource", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.250",
                                                "healthStatus": "ONLINE", "deletedAt": None})
            await repo.db.tasks.insert_one({"id": "offline-recover", "resourceId": "resource", "desiredState": "RUNNING",
                                            "status": "COLLECTING", "nodeId": "node-a", "runId": "offline-run",
                                            "protocol": "SSH", "ip": "192.0.2.250", "port": 22, "createdBy": "owner"})
            # 真实离线停止先保留旧运行归属；Worker 收尾后才能允许 ONLINE 消费恢复。
            await _apply_failure(repo, await repo.db.resources.find_one({"id": "resource"}), "OFFLINE")
            preserved = await repo.db.tasks.find_one({"id": "offline-recover"})
            assert preserved["desiredState"] == "RUNNING"
            # HTTP 失败不能代替日志连接证据；此处模拟 Worker 已确认持续无输出。
            await stop_unresponsive_task(repo, preserved)
            offline = await repo.db.tasks.find_one({"id": "offline-recover"})
            assert offline["desiredState"] == "STOPPED"
            assert offline["resourceHealthRecovery"]["reason"] == "OFFLINE"
            await repo.db.runs.insert_one({"id": "offline-run", "endedAt": now()})
            await repo.db.tasks.update_one({"id": "offline-recover"}, {"$set": {"status": "STOPPED", "nodeId": None}})
            await _apply_success(repo, await repo.db.resources.find_one({"id": "resource"}), {})
            await reconcile_authorized_recoveries(repo)
            recovered = await repo.db.tasks.find_one({"id": "offline-recover"})
            assert (recovered["desiredState"], recovered["status"]) == ("RUNNING", "STOPPED")
            assert "resourceHealthRecovery" not in recovered

            marker = {"resourceId": "resource", "desiredState": "RUNNING", "reason": "OFFLINE", "authorizedAt": now()}
            await repo.db.tasks.insert_one({"id": "stop-race", "resourceId": "resource", "desiredState": "STOPPED",
                                            "status": "STOPPED", "nodeId": None, "protocol": "SSH", "ip": "192.0.2.250",
                                            "port": 22, "createdBy": "owner", "resourceHealthRecovery": marker})
            user = {"id": "owner", "isAdmin": True, "scopes": ["*"]}
            await asyncio.gather(reconcile_authorized_recoveries(repo), request_control(repo, "stop-race", "STOPPED", user))
            task = await repo.db.tasks.find_one({"id": "stop-race"})
            assert task["desiredState"] == "STOPPED" and "resourceHealthRecovery" not in task
            assert await repo.db.operations.count_documents({"taskId": "stop-race", "desiredState": "RUNNING", "status": "PENDING"}) == 0

            await repo.db.tasks.insert_one({"id": "locked", "resourceId": "resource", "desiredState": "STOPPED",
                                            "status": "BLOCKED", "nodeId": None, "runId": "locked-run", "protocol": "SSH",
                                            "ip": "192.0.2.250", "port": 22, "createdBy": "owner",
                                            "resourceHealthRecovery": marker})
            await repo.db.runs.insert_one({"id": "locked-run", "endedAt": now()})
            await repo.db.endpoint_locks.insert_one({"taskId": "locked", "runId": "locked-run"})
            await reconcile_authorized_recoveries(repo)
            locked = await repo.db.tasks.find_one({"id": "locked"})
            assert (locked["desiredState"], locked["status"], locked["nodeId"]) == ("STOPPED", "BLOCKED", None)
            assert locked["resourceHealthRecovery"]["authorizedAt"]

            backoff_cases = ((8, 9, 60), (9, 10, 300), (20, 21, 300),
                             (21, 22, 3600), (44, 45, 3600), (45, 46, 86400))
            observed_backoff = []
            for index, (before_count, expected_count, seconds) in enumerate(backoff_cases):
                identifier = f"backoff-{index}"
                await repo.db.resources.insert_one({
                    "id": identifier, "kind": "HIKVISION_NETWORK", "ip": f"192.0.2.{200 + index}",
                    "deletedAt": None, "healthStatus": "ONLINE", "healthFailureCount": before_count,
                })
                started = now()
                await _apply_failure(repo, await repo.db.resources.find_one({"id": identifier}), "OFFLINE")
                changed = await repo.db.resources.find_one({"id": identifier})
                delay = round((changed["nextHealthCheckAt"] - started).total_seconds())
                assert (changed["healthFailureCount"], delay) == (expected_count, seconds)
                observed_backoff.append(expected_count)

            await repo.db.resources.insert_one({
                "id": "manual-success", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.240", "version": 1,
                "deletedAt": None, "healthStatus": "AUTH_FAILED", "healthFailureCount": 46, "healthRevision": 3,
                "passwordEncrypted": repo.encrypt("verification-secret"), "model": "old", "subSerialNumber": "old",
            })
            await repo.db.tasks.insert_one({"id": "manual-identity", "resourceId": "manual-success",
                                            "desiredState": "STOPPED", "storageIdentity": "old"})
            manual_snapshot = await repo.db.resources.find_one({"id": "manual-success"})
            await apply_manual_result(repo, manual_snapshot | {"manualHealthResult": True}, status="SUCCESS",
                                      metadata={"model": "new", "subSerialNumber": "new", "softwareVersion": "V"},
                                      actor="verification")
            manual_success = await repo.db.resources.find_one({"id": "manual-success"})
            identity_task = await repo.db.tasks.find_one({"id": "manual-identity"})
            assert manual_success["healthFailureCount"] == 0 and manual_success["healthStatus"] == "ONLINE"
            assert (manual_success["model"], manual_success["subSerialNumber"]) == ("new", "new")
            assert identity_task["storageIdentity"] != "old"

            scheduled = now() + timedelta(hours=1)
            await repo.db.resources.insert_one({
                "id": "manual-failure", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.241", "version": 1,
                "deletedAt": None, "healthStatus": "ONLINE", "healthFailureCount": 10, "healthRevision": 3,
                "passwordEncrypted": repo.encrypt("verification-secret"), "nextHealthCheckAt": scheduled,
            })
            await repo.db.tasks.insert_one({"id": "manual-running", "resourceId": "manual-failure",
                                            "desiredState": "RUNNING", "status": "COLLECTING"})
            manual_snapshot = await repo.db.resources.find_one({"id": "manual-failure"})
            await apply_manual_result(repo, manual_snapshot | {"manualHealthResult": True}, status="AUTH_FAILED",
                                      metadata={}, actor="verification")
            manual_failure = await repo.db.resources.find_one({"id": "manual-failure"})
            stopped = await repo.db.tasks.find_one({"id": "manual-running"})
            assert manual_failure["healthFailureCount"] == 10 and manual_failure["healthStatus"] == "AUTH_FAILED"
            assert abs(manual_failure["nextHealthCheckAt"].timestamp() - scheduled.timestamp()) < 1
            assert stopped["desiredState"] == "STOPPED"

            stale = await repo.db.resources.find_one({"id": "manual-success"})
            await repo.db.resources.update_one({"id": "manual-success"}, {"$inc": {"healthRevision": 1}})
            try:
                await apply_manual_result(repo, stale | {"manualHealthResult": True}, status="SUCCESS",
                                          metadata={"model": "late", "subSerialNumber": "late"}, actor="verification")
            except HTTPException as error:
                assert error.status_code == 409
            else:
                raise AssertionError("旧健康修订不应覆盖新资源状态")
            assert (await repo.db.resources.find_one({"id": "manual-success"}))["model"] == "new"

            await repo.db.resources.insert_one({"id": "rollback", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.242",
                                                "deletedAt": None, "healthStatus": "ONLINE", "healthFailureCount": 9})
            original_audit = repo.audit

            async def reject_audit(*_args, **_kwargs):
                raise RuntimeError("injected transaction failure")

            repo.audit = reject_audit
            try:
                await _apply_failure(repo, await repo.db.resources.find_one({"id": "rollback"}), "OFFLINE")
            except RuntimeError:
                pass
            else:
                raise AssertionError("注入审计失败必须使认证事务回滚")
            finally:
                repo.audit = original_audit
            rollback = await repo.db.resources.find_one({"id": "rollback"})
            assert rollback["healthStatus"] == "ONLINE" and rollback["healthFailureCount"] == 9
            assert await repo.db.authentication_records.count_documents({"resourceId": "rollback"}) == 0

            print(json.dumps({"passed": True, "offlineOnlineRecovery": True, "consumeStopRace": True,
                              "unfinishedRunLockProtected": True, "backoffBoundaries": observed_backoff,
                              "manualSuccessIdentityReset": True, "manualFailureStopsWithoutBackoffAdvance": True,
                              "staleRevisionRejected": True, "transactionRollbackVerified": True,
                              "serialHealthBoundaryPeriodic": ["AUTH_FAILED", "OFFLINE", "ERROR"],
                              "serialHealthBoundaryManual": ["AUTH_FAILED", "OFFLINE", "ERROR"],
                              "noDeviceAccess": True}))
        finally:
            await mongo.drop_database(name)
            assert name not in await mongo.list_database_names()
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    asyncio.run(main())

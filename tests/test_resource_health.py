"""资源周期认证、系统停止标记与目录身份更新的隔离回归。"""

import asyncio
from datetime import UTC, timedelta

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.health import (
    check_resource,
    failure_backoff_seconds,
    grant_after_user_authentication,
    health_loop,
    health_once,
    reconcile_authorized_recoveries,
)
from camera_logs.tasks.control import request_control
from mongomock_motor import AsyncMongoMockClient


def _repo(tmp_path):
    """构造不连接真实设备的内存仓库，凭据只用于验证解密和调用边界。"""
    settings = Settings(encryption_key="4SkbHRtubkER4lQFz_4Zj0z3Yb6t86M5A_d9o8RwSgI=", bootstrap_token="test",
                        log_root=tmp_path / "logs", start_background=False)
    return Repository(AsyncMongoMockClient().db, settings)


@pytest.mark.parametrize(("failures", "seconds"), [
    (1, 60), (9, 60), (10, 300), (21, 300), (22, 3600), (45, 3600), (46, 86400),
])
def test_periodic_authentication_failure_backoff_boundaries(failures, seconds):
    """连续周期失败按累计次数切换退避段，成功后的零计数回到一分钟。"""
    assert failure_backoff_seconds(failures) == seconds
    assert failure_backoff_seconds(0) == 60


def test_periodic_failure_count_persists_and_success_resets_to_one_minute(tmp_path, monkeypatch):
    """周期失败的累计计数跨快照保存，认证成功才清零并恢复正常间隔。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({
            "id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.70", "username": "admin",
            "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"), "deletedAt": None,
            "healthFailureCount": 9,
        })

        async def rejected(**_kwargs):
            raise PermissionError("bad")

        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", rejected)
        before = now()
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        failed = await repo.db.resources.find_one({"id": "camera"})
        assert failed["healthFailureCount"] == 10
        assert 295 <= (failed["nextHealthCheckAt"].replace(tzinfo=before.tzinfo) - before).total_seconds() <= 305

        monkeypatch.setattr(
            "camera_logs.resources.health.authenticate_network_resource",
            lambda **_kwargs: asyncio.sleep(0, result={"model": "", "subSerialNumber": "", "softwareVersion": "V"}),
        )
        before = now()
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        succeeded = await repo.db.resources.find_one({"id": "camera"})
        assert succeeded["healthFailureCount"] == 0
        assert 55 <= (succeeded["nextHealthCheckAt"].replace(tzinfo=before.tzinfo) - before).total_seconds() <= 65

    asyncio.run(scenario())


def test_auth_failure_marks_resource_and_stops_only_running_tasks(tmp_path, monkeypatch):
    """401 标识凭据失败，系统停止只作用于未由用户暂停的运行任务。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.1",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "version": 1, "deletedAt": None})
        await repo.db.tasks.insert_many([
            {"id": "running", "resourceId": "camera", "desiredState": "RUNNING", "storageIdentity": "old"},
            {"id": "paused", "resourceId": "camera", "desiredState": "PAUSED", "storageIdentity": "old"},
        ])
        async def rejected(**_kwargs):
            raise PermissionError("bad")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", rejected)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        resource = await repo.db.resources.find_one({"id": "camera"})
        assert resource["healthStatus"] == "AUTH_FAILED"
        running = await repo.db.tasks.find_one({"id": "running"})
        paused = await repo.db.tasks.find_one({"id": "paused"})
        assert running["desiredState"] == "STOPPED"
        assert running["resourceHealthRecovery"]["resourceId"] == "camera"
        assert paused["desiredState"] == "PAUSED"
        assert "resourceHealthRecovery" not in paused
    asyncio.run(scenario())


def test_auth_failure_preserves_paused_run_and_budget_context(tmp_path, monkeypatch):
    """设备离线不会把已暂停 SSH 任务变成 STOPPED，原运行和预算可等待用户显式继续。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.3",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"), "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "paused", "resourceId": "camera", "desiredState": "PAUSED",
                                        "status": "PAUSED", "runId": "kept-run", "generation": 7})
        async def offline(**_kwargs):
            from camera_logs.resources.authentication import DeviceOfflineError
            raise DeviceOfflineError("timeout")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", offline)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        task = await repo.db.tasks.find_one({"id": "paused"})
        assert (task["desiredState"], task["status"], task["runId"], task["generation"]) == ("PAUSED", "PAUSED", "kept-run", 7)
        assert "resourceHealthRecovery" not in task
    asyncio.run(scenario())


def test_offline_during_manual_pause_keeps_pending_worker_cleanup(tmp_path, monkeypatch):
    """暂停请求已落库但 Worker 未停时，离线不能改写用户暂停意图和运行预算。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.31",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"), "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "pausing", "resourceId": "camera", "desiredState": "PAUSED",
                                        "status": "COLLECTING", "runId": "kept-run", "generation": 8,
                                        "remainingCommandBudget": 23})
        async def offline(**_kwargs):
            from camera_logs.resources.authentication import DeviceOfflineError
            raise DeviceOfflineError("timeout")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", offline)

        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))

        task = await repo.db.tasks.find_one({"id": "pausing"})
        assert (task["desiredState"], task["status"], task["runId"], task["generation"]) == (
            "PAUSED", "COLLECTING", "kept-run", 8)
        assert task["remainingCommandBudget"] == 23
        assert await repo.db.operations.count_documents({"taskId": "pausing"}) == 0
    asyncio.run(scenario())


def test_repeated_same_failure_does_not_create_system_operations_for_stopped_task(tmp_path, monkeypatch):
    """同一失败状态后，已系统停止或手动停止的任务不能每分钟新增停止操作。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.30",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"), "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "running", "resourceId": "camera", "desiredState": "RUNNING"})
        async def rejected(**_kwargs):
            raise PermissionError("bad")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", rejected)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        first = await repo.db.operations.count_documents({"taskId": "running", "action": "system-resource-health-stop"})
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        assert first == 1 == await repo.db.operations.count_documents({"taskId": "running", "action": "system-resource-health-stop"})
    asyncio.run(scenario())


def test_identity_change_ends_paused_run_before_new_resume_can_be_claimed(tmp_path, monkeypatch):
    """设备身份变化不能沿用旧暂停预算，旧锁和运行必须结束后才创建新运行。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.4",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "model": "old", "subSerialNumber": "old", "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "waiting", "resourceId": "camera", "desiredState": "RUNNING",
                                        "status": "WAITING_DEVICE", "runId": "old-run", "generation": 3})
        await repo.db.runs.insert_one({"id": "old-run"})
        await repo.db.endpoint_locks.insert_one({"taskId": "waiting", "runId": "old-run"})
        async def changed(**_kwargs):
            return {"model": "new", "subSerialNumber": "new", "softwareVersion": "V"}
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", changed)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        task = await repo.db.tasks.find_one({"id": "waiting"})
        assert (task["desiredState"], task["status"]) == ("RUNNING", "WAITING_DEVICE")
        assert "runId" not in task and await repo.db.endpoint_locks.count_documents({"taskId": "waiting"}) == 0
        assert (await repo.db.runs.find_one({"id": "old-run"}))["endedAt"]
    asyncio.run(scenario())


def test_user_auth_identity_change_ends_manual_paused_run_consistently(tmp_path):
    """用户保存新设备身份也必须结束旧暂停运行，不能留下 PAUSED 意图配 STOPPED 状态。"""
    async def scenario():
        repo = _repo(tmp_path)
        resource = {"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.5", "model": "new",
                    "subSerialNumber": "new", "healthStatus": "ONLINE"}
        await repo.db.resources.insert_one(resource)
        await repo.db.tasks.insert_one({"id": "paused", "resourceId": "camera", "desiredState": "PAUSED",
                                        "status": "PAUSED", "runId": "old-run"})
        await repo.db.runs.insert_one({"id": "old-run"})
        await repo.db.endpoint_locks.insert_one({"taskId": "paused", "runId": "old-run"})
        await grant_after_user_authentication(repo, resource, None, identity_changed=True)
        task = await repo.db.tasks.find_one({"id": "paused"})
        assert (task["desiredState"], task["status"]) == ("PAUSED", "PAUSED")
        assert "runId" not in task and (await repo.db.runs.find_one({"id": "old-run"}))["endedAt"]
    asyncio.run(scenario())


def test_identity_result_started_before_resume_request_cannot_release_waiting_task(tmp_path, monkeypatch):
    """认证在 resume 前已启动时，即使返回新身份也只能更新资源，不能兑现后发等待操作。"""
    async def scenario():
        repo = _repo(tmp_path)
        requested = now()
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.6",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "model": "old", "subSerialNumber": "old", "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "waiting", "resourceId": "camera", "desiredState": "RUNNING",
            "status": "WAITING_DEVICE", "runId": "old-run", "generation": 4,
            "resumeWaiting": {"operationId": "resume", "pausedRunId": "old-run", "generation": 4,
                              "requestedAt": requested}})
        async def changed(**_kwargs):
            return {"model": "new", "subSerialNumber": "new", "softwareVersion": "V"}
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", changed)
        snapshot = await repo.db.resources.find_one({"id": "camera"})
        snapshot["healthLeaseStartedAt"] = requested - timedelta(seconds=1)
        await check_resource(repo, snapshot)
        task = await repo.db.tasks.find_one({"id": "waiting"})
        assert (task["desiredState"], task["status"]) == ("RUNNING", "WAITING_DEVICE")
        assert task["resumeWaiting"]["operationId"] == "resume" and "runId" not in task
    asyncio.run(scenario())


def test_identity_waiting_task_accepts_later_probe_and_becomes_claimable(tmp_path, monkeypatch):
    """换机清理旧 run 后，早期探测不兑现，后续新鲜探测将等待任务恢复为可领取暂停态。"""
    async def scenario():
        repo = _repo(tmp_path)
        requested = now()
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.7",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "model": "old", "subSerialNumber": "old", "deletedAt": None})
        waiting = {"operationId": "resume", "pausedRunId": "old-run", "generation": 5, "requestedAt": requested}
        await repo.db.tasks.insert_one({"id": "waiting", "resourceId": "camera", "desiredState": "RUNNING",
            "status": "WAITING_DEVICE", "runId": "old-run", "generation": 5, "resumeWaiting": waiting})
        await repo.db.runs.insert_one({"id": "old-run"})
        await repo.db.endpoint_locks.insert_one({"taskId": "waiting", "runId": "old-run"})
        async def changed(**_kwargs):
            return {"model": "new", "subSerialNumber": "new", "softwareVersion": "V"}
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", changed)
        early = await repo.db.resources.find_one({"id": "camera"})
        early["healthLeaseStartedAt"] = requested - timedelta(seconds=1)
        await check_resource(repo, early)
        after_early = await repo.db.tasks.find_one({"id": "waiting"})
        assert after_early["status"] == "WAITING_DEVICE" and after_early["resumeWaiting"]["pausedRunId"] is None
        fresh = await repo.db.resources.find_one({"id": "camera"})
        fresh["healthLeaseStartedAt"] = requested + timedelta(seconds=1)
        await check_resource(repo, fresh)
        ready = await repo.db.tasks.find_one({"id": "waiting"})
        assert (ready["desiredState"], ready["status"]) == ("RUNNING", "PAUSED")
        assert "runId" not in ready and "resumeWaiting" not in ready
    asyncio.run(scenario())


def test_offline_recovery_auto_authorizes_only_safe_system_stops(tmp_path, monkeypatch):
    """设备重新在线只恢复离线系统停止，未知归属和用户意图仍保持原状。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.2",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "model": "old", "subSerialNumber": "old", "healthStatus": "OFFLINE", "version": 1, "deletedAt": None})
        marker = {"resourceId": "camera", "stoppedAt": "before", "desiredState": "RUNNING", "reason": "OFFLINE"}
        await repo.db.tasks.insert_many([
            {"id": "recover", "resourceId": "camera", "desiredState": "STOPPED", "status": "STOPPED",
             "storageIdentity": "old", "resourceHealthRecovery": marker},
            {"id": "manual-stop", "resourceId": "camera", "desiredState": "STOPPED", "status": "STOPPED"},
            {"id": "manual-pause", "resourceId": "camera", "desiredState": "PAUSED", "status": "PAUSED",
             "runId": "paused-run"},
            {"id": "safe-blocked", "resourceId": "camera", "desiredState": "STOPPED", "status": "BLOCKED",
             "runId": "ended-run", "resourceHealthRecovery": marker},
            {"id": "unknown-blocked", "resourceId": "camera", "desiredState": "STOPPED", "status": "BLOCKED",
             "runId": "unknown-run", "resourceHealthRecovery": marker},
            {"id": "owned-blocked", "resourceId": "camera", "desiredState": "STOPPED", "status": "BLOCKED",
             "nodeId": "node-a", "resourceHealthRecovery": marker},
            {"id": "foreign-marker", "resourceId": "camera", "desiredState": "STOPPED", "status": "STOPPED",
             "resourceHealthRecovery": marker | {"resourceId": "other", "authorizedAt": "before"}},
        ])
        await repo.db.runs.insert_many([{"id": "paused-run"}, {"id": "ended-run", "endedAt": now()}])
        async def verified(**_kwargs):
            return {"model": "old", "subSerialNumber": "old", "softwareVersion": "V5.8"}
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", verified)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        await reconcile_authorized_recoveries(repo)
        recovered = await repo.db.tasks.find_one({"id": "recover"})
        manual_stop = await repo.db.tasks.find_one({"id": "manual-stop"})
        manual_pause = await repo.db.tasks.find_one({"id": "manual-pause"})
        safe_blocked = await repo.db.tasks.find_one({"id": "safe-blocked"})
        unknown_blocked = await repo.db.tasks.find_one({"id": "unknown-blocked"})
        owned_blocked = await repo.db.tasks.find_one({"id": "owned-blocked"})
        foreign_marker = await repo.db.tasks.find_one({"id": "foreign-marker"})
        assert (recovered["desiredState"], recovered["status"]) == ("RUNNING", "STOPPED")
        assert "resourceHealthRecovery" not in recovered
        assert (safe_blocked["desiredState"], safe_blocked["status"]) == ("RUNNING", "STOPPED")
        assert "resourceHealthRecovery" not in safe_blocked
        assert manual_stop["desiredState"] == "STOPPED" and "resourceHealthRecovery" not in manual_stop
        assert (manual_pause["desiredState"], manual_pause["status"], manual_pause["runId"]) == ("PAUSED", "PAUSED", "paused-run")
        assert unknown_blocked["status"] == "BLOCKED" and unknown_blocked["resourceHealthRecovery"]["authorizedAt"]
        assert owned_blocked["status"] == "BLOCKED" and owned_blocked["nodeId"] == "node-a"
        assert foreign_marker["desiredState"] == "STOPPED" and foreign_marker["resourceHealthRecovery"]["resourceId"] == "other"
        assert (await repo.db.resources.find_one({"id": "camera"}))["healthStatus"] == "ONLINE"
    asyncio.run(scenario())


def test_auth_failure_recovery_still_requires_user_credential_update(tmp_path, monkeypatch):
    """实际 OFFLINE 后若认证失败，随后 ONLINE 仍须由用户更新凭据才能恢复。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.8",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "healthStatus": "ONLINE", "version": 1, "deletedAt": None})
        await repo.db.tasks.insert_one({"id": "requires-user-auth", "resourceId": "camera", "desiredState": "RUNNING",
                                        "status": "COLLECTING"})

        async def offline(**_kwargs):
            from camera_logs.resources.authentication import DeviceOfflineError
            raise DeviceOfflineError("device disconnected")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", offline)
        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        after_offline = await repo.db.tasks.find_one({"id": "requires-user-auth"})
        assert after_offline["resourceHealthRecovery"]["reason"] == "OFFLINE"

        async def rejected(**_kwargs):
            raise PermissionError("credentials rejected")
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", rejected)

        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        after_failure = await repo.db.tasks.find_one({"id": "requires-user-auth"})
        assert after_failure["resourceHealthRecovery"]["reason"] == "AUTH_FAILED"

        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource",
                            lambda **_kwargs: asyncio.sleep(0, result={"model": "", "subSerialNumber": "", "softwareVersion": "V"}))

        await check_resource(repo, await repo.db.resources.find_one({"id": "camera"}))
        await reconcile_authorized_recoveries(repo)

        task = await repo.db.tasks.find_one({"id": "requires-user-auth"})
        assert task["desiredState"] == "STOPPED" and task["resourceHealthRecovery"]["reason"] == "AUTH_FAILED"
        assert "authorizedAt" not in task["resourceHealthRecovery"]
        assert (await repo.db.resources.find_one({"id": "camera"}))["healthStatus"] == "ONLINE"
    asyncio.run(scenario())


def test_health_once_limits_parallel_authentication(tmp_path, monkeypatch):
    """单轮受信号量限制，慢设备认证不会无限制占用 API 后台任务。"""
    async def scenario():
        repo = _repo(tmp_path)
        for index in range(3):
            await repo.db.resources.insert_one({"id": str(index), "kind": "HIKVISION_NETWORK", "ip": f"192.0.2.{index + 10}",
                "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"), "version": 1, "deletedAt": None})
        active = maximum = 0
        async def slow(**_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return {"model": "", "subSerialNumber": "", "softwareVersion": "V"}
        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", slow)
        await health_once(repo, concurrency=1)
        assert maximum == 1
    asyncio.run(scenario())


def test_health_loop_rechecks_control_requested_resource_within_short_scan_interval(tmp_path, monkeypatch):
    """继续操作将资源置为到期后，后台短扫描应立即发现而不等待下一分钟认证周期。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({
            "id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.60",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "deletedAt": None, "nextHealthCheckAt": now() + timedelta(seconds=60),
        })
        authenticated = []

        async def verified(**_kwargs):
            authenticated.append(True)
            return {"model": "", "subSerialNumber": "", "softwareVersion": "V"}

        sleeps = []

        async def advance(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                await repo.db.resources.update_one({"id": "camera"}, {"$set": {"nextHealthCheckAt": now()}})
                return
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", verified)
        monkeypatch.setattr("camera_logs.resources.health.asyncio.sleep", advance)
        with pytest.raises(asyncio.CancelledError):
            await health_loop(repo)
        assert authenticated == [True]
        assert sleeps == [1, 1, 1]

    asyncio.run(scenario())


@pytest.mark.parametrize("result", ["SUCCESS", "AUTH_FAILED", "OFFLINE"])
def test_resume_preserves_due_health_check_when_claimed_probe_finishes_later(tmp_path, monkeypatch, result):
    """恢复后的旧探测无论成败都只释放原租约，不能推迟新的探测请求。"""
    async def scenario():
        repo = _repo(tmp_path)
        started_at = now() - timedelta(seconds=2)
        lease_until = now() + timedelta(seconds=18)
        await repo.db.resources.insert_one({
            "id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.61",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "deletedAt": None, "healthRevision": 1, "healthLeaseToken": "old-probe",
            "healthLeaseStartedAt": started_at, "healthLeaseUntil": lease_until,
            "nextHealthCheckAt": started_at,
        })
        await repo.db.tasks.insert_one({
            "id": "task", "resourceId": "camera", "protocol": "SSH", "status": "PAUSED",
            "desiredState": "PAUSED", "nodeId": None, "runId": "paused-run", "generation": 3,
        })
        await repo.db.runs.insert_one({"id": "paused-run"})
        await repo.db.endpoint_locks.insert_one({"taskId": "task", "runId": "paused-run"})
        snapshot = await repo.db.resources.find_one({"id": "camera"})

        operation = await request_control(
            repo, "task", "RUNNING", {"id": "bootstrap", "scopes": ["*"], "isAdmin": True},
            require_paused=True,
        )

        waiting = await repo.db.tasks.find_one({"id": "task"})
        requested = await repo.db.resources.find_one({"id": "camera"})
        assert (operation["action"], waiting["status"], waiting["desiredState"]) == (
            "resume-wait-device", "WAITING_DEVICE", "RUNNING",
        )
        assert requested["nextHealthCheckAt"].replace(tzinfo=UTC) <= now()
        assert requested["healthLeaseToken"] == "old-probe"
        assert requested["healthLeaseUntil"].replace(tzinfo=UTC) > now()

        async def verified(**_kwargs):
            if result == "AUTH_FAILED":
                raise PermissionError("credentials rejected")
            if result == "OFFLINE":
                from camera_logs.resources.authentication import DeviceOfflineError
                raise DeviceOfflineError("device disconnected")
            return {"model": "", "subSerialNumber": "", "softwareVersion": "V"}

        monkeypatch.setattr("camera_logs.resources.health.authenticate_network_resource", verified)
        await check_resource(repo, snapshot)

        committed = await repo.db.resources.find_one({"id": "camera"})
        assert committed["nextHealthCheckAt"].replace(tzinfo=UTC) <= now()
        assert "healthLeaseToken" not in committed and "healthLeaseUntil" not in committed

    asyncio.run(scenario())


def test_late_probe_cannot_release_a_newer_probe_lease(tmp_path, monkeypatch):
    """旧令牌迟到时必须保留新令牌的租约，避免误开同设备并发认证。"""
    async def scenario():
        repo = _repo(tmp_path)
        await repo.db.resources.insert_one({
            "id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.62",
            "username": "admin", "authType": "DIGEST", "passwordEncrypted": repo.encrypt("secret"),
            "deletedAt": None, "healthRevision": 2, "healthLeaseToken": "new-probe",
            "healthLeaseStartedAt": now(), "healthLeaseUntil": now() + timedelta(seconds=20),
            "nextHealthCheckAt": now(),
        })
        stale = (await repo.db.resources.find_one({"id": "camera"})) | {
            "healthRevision": 1, "healthLeaseToken": "old-probe",
        }

        monkeypatch.setattr(
            "camera_logs.resources.health.authenticate_network_resource",
            lambda **_kwargs: asyncio.sleep(0, result={"model": "", "subSerialNumber": "", "softwareVersion": "V"}),
        )
        await check_resource(repo, stale)

        current = await repo.db.resources.find_one({"id": "camera"})
        assert (current["healthRevision"], current["healthLeaseToken"]) == (2, "new-probe")
        assert "healthLeaseUntil" in current

    asyncio.run(scenario())


def test_user_auth_restores_only_after_worker_cleanup_and_manual_stop_cannot_return(tmp_path):
    """保存认证先保留授权，旧节点收尾后恢复；清除标记的手动停止永不被恢复。"""
    async def scenario():
        repo = _repo(tmp_path)
        resource = {"id": "camera", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.40", "model": "M",
                    "subSerialNumber": "S", "healthStatus": "ONLINE"}
        await repo.db.resources.insert_one(resource)
        marker = {"resourceId": "camera", "desiredState": "RUNNING"}
        await repo.db.tasks.insert_many([
            {"id": "waiting", "resourceId": "camera", "desiredState": "STOPPED", "nodeId": "node",
             "resourceHealthRecovery": marker},
            {"id": "manual", "resourceId": "camera", "desiredState": "STOPPED"},
        ])
        await grant_after_user_authentication(repo, resource, None)
        waiting = await repo.db.tasks.find_one({"id": "waiting"})
        assert waiting["desiredState"] == "STOPPED" and waiting["resourceHealthRecovery"]["authorizedAt"]
        await repo.db.tasks.update_one({"id": "waiting"}, {"$set": {"nodeId": None}})
        await reconcile_authorized_recoveries(repo)
        assert (await repo.db.tasks.find_one({"id": "waiting"}))["desiredState"] == "RUNNING"
        assert (await repo.db.tasks.find_one({"id": "manual"}))["desiredState"] == "STOPPED"
    asyncio.run(scenario())

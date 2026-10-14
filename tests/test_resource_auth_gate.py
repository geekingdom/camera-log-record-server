"""资源 HTTP 认证失败时采集运行的新建连接门控回归。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection.resource_auth_gate import wait_for_resource_authentication
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from mongomock_motor import AsyncMongoMockClient


def _repo(tmp_path):
    """构造无需真实 Mongo 或设备的运行时仓库。"""
    settings = Settings(encryption_key="4SkbHRtubkER4lQFz_4Zj0z3Yb6t86M5A_d9o8RwSgI=", bootstrap_token="test",
                        log_root=tmp_path / "logs", start_background=False, node_id="node-a")
    return Repository(AsyncMongoMockClient().db, settings)


@pytest.mark.parametrize("protocol", ["SSH", "TELNET_DEVICE"])
def test_auth_gate_waits_for_success_after_auth_failed_then_offline(tmp_path, protocol):
    """SSH和Telnet设备在401后即使变离线，也要等待SUCCESS才允许新建连接。"""
    async def scenario():
        repo = _repo(tmp_path)
        task = {"id": "task", "runId": "run", "nodeId": "node-a", "generation": 1,
                "resourceId": "camera", "protocol": protocol, "desiredState": "RUNNING"}
        await repo.db.tasks.insert_one(task)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None,
                                            "healthStatus": "OFFLINE", "authRejectedAt": "401"})
        slept = asyncio.Event()
        release = asyncio.Event()

        async def sleep(_seconds):
            slept.set()
            await release.wait()

        waiting = asyncio.create_task(wait_for_resource_authentication(repo, task, stopping=lambda: False, sleep=sleep))
        await asyncio.wait_for(slept.wait(), 1)
        assert not waiting.done()
        await repo.db.resources.update_one({"id": "camera"}, {"$set": {"healthStatus": "ONLINE"},
                                                               "$unset": {"authRejectedAt": ""}})
        release.set()
        assert await asyncio.wait_for(waiting, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [{"desiredState": "STOPPED"}, {"desiredState": "PAUSED"},
                                      {"generation": 2}, {"nodeId": "node-b"}, {"runId": "next"}])
def test_auth_gate_exits_when_user_control_or_owner_changes(tmp_path, change):
    """等待401恢复的旧实例不能覆盖用户停止、暂停或后继运行的归属。"""
    async def scenario():
        repo = _repo(tmp_path)
        task = {"id": "task", "runId": "run", "nodeId": "node-a", "generation": 1,
                "resourceId": "camera", "protocol": "SSH", "desiredState": "RUNNING"}
        await repo.db.tasks.insert_one(task | change)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None,
                                            "healthStatus": "AUTH_FAILED", "authRejectedAt": "401"})
        assert not await wait_for_resource_authentication(repo, task, stopping=lambda: False)

    asyncio.run(scenario())


def test_auth_gate_retries_database_timeout_without_opening_connection(tmp_path):
    """任务归属读取超时只能进入保守等待，恢复读取后才作出准入判断。"""
    async def scenario():
        repo = _repo(tmp_path)
        task = {"id": "task", "runId": "run", "nodeId": "node-a", "generation": 1,
                "resourceId": "camera", "protocol": "SSH", "desiredState": "RUNNING"}
        await repo.db.tasks.insert_one(task)
        await repo.db.resources.insert_one({"id": "camera", "kind": "HIKVISION_NETWORK", "deletedAt": None,
                                            "healthStatus": "ONLINE"})
        original_find = repo.db.tasks.find_one
        calls = 0
        waited = asyncio.Event()
        release = asyncio.Event()

        async def timeout_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("synthetic database timeout")
            return await original_find(*args, **kwargs)

        async def sleep(_seconds):
            waited.set()
            await release.wait()

        gated_repo = SimpleNamespace(db=SimpleNamespace(
            tasks=SimpleNamespace(find_one=timeout_once), resources=repo.db.resources,
        ))
        admitted = asyncio.create_task(
            wait_for_resource_authentication(gated_repo, task, stopping=lambda: False, sleep=sleep),
        )
        await asyncio.wait_for(waited.wait(), 1)
        assert not admitted.done()
        release.set()
        assert await asyncio.wait_for(admitted, 1)
        assert calls >= 2

    asyncio.run(scenario())


def test_auth_gate_does_not_apply_to_telnet_serial(tmp_path):
    """串口 Telnet 不读取海康 HTTP 健康状态，也不等待认证恢复。"""
    async def scenario():
        repo = _repo(tmp_path)
        task = {"id": "serial", "resourceId": "serial-resource", "protocol": "TELNET_SERIAL"}
        assert await wait_for_resource_authentication(repo, task, stopping=lambda: False)

    asyncio.run(scenario())

"""PSH 握手失败的运行时收尾测试：领域错误可释放，存储错误仍需上报。"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import CommandChannelBlocked
from camera_logs.collection.psh_dialogue import PshSwitchError
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path):
    """建立带任务、运行和端点锁的最小节点仓储，覆盖 Worker.release 的持久化路径。"""
    settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node-debug")
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    task = {
        "id": "debug-task", "runId": "debug-run", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 23,
        "passwordEncrypted": repo.encrypt(""), "scheduledCommands": [], "nodeId": "node-debug",
        "status": "COLLECTING", "desiredState": "RUNNING", "storageIdentity": "testingdevice",
    }
    await repo.db.tasks.insert_one(task)
    await repo.db.runs.insert_one({"id": "debug-run"})
    await repo.db.endpoint_locks.insert_one({"taskId": "debug-task", "runId": "debug-run", "endpoint": "127.0.0.1:23"})
    await repo.db.operations.insert_one({"id": "stop", "taskId": "debug-task", "desiredState": "STOPPED", "status": "PENDING"})
    return repo, task


class HandshakeFailureCollector:
    """模拟连接已完成收尾后抛出的 PSH 领域错误，不能触发运行时重连。"""

    instances = 0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).instances += 1
        self.session_id = "failed-session"

    async def start(self) -> None:
        raise PshSwitchError("synthetic psh handshake failure")

    async def stop(self) -> None:
        raise PshSwitchError("synthetic psh handshake failure")


async def test_psh_failure_ends_runtime_and_worker_releases_lock_without_reconnect(tmp_path, monkeypatch):
    repo, task = await repository(tmp_path)
    monkeypatch.setattr("camera_logs.collection.runtime.Collector", HandshakeFailureCollector)
    HandshakeFailureCollector.instances = 0
    runtime = SessionRuntime(repo, task, connection_factory=AsyncMock())
    await runtime.background

    assert HandshakeFailureCollector.instances == 1
    assert runtime.error == "synthetic psh handshake failure"
    assert runtime.background_failure() is None

    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    await worker.release(runtime)

    stored = await repo.get("tasks", task["id"])
    assert stored["status"] == "ERROR"
    assert stored["desiredState"] == "STOPPED"
    assert stored["nodeId"] is None
    assert await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0
    assert (await repo.get("operations", "stop"))["status"] == "FAILED"
    assert task["id"] not in worker.active


async def test_storage_stop_error_is_not_absorbed_as_psh_failure(tmp_path):
    repo, task = await repository(tmp_path)
    runtime = SimpleNamespace(task=task, error=None, stop=AsyncMock(side_effect=OSError("synthetic storage failure")))
    worker = Worker(repo)

    with pytest.raises(OSError, match="synthetic storage failure"):
        await worker.release(runtime)

    # 未确认收尾时不得伪造端点已释放，也不得写成正常 STOPPED。
    stored = await repo.get("tasks", task["id"])
    assert stored["status"] == "COLLECTING"
    assert await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 1


@pytest.mark.parametrize("publish_delay", [0, .2])
async def test_debug_without_challenge_keeps_runtime_collecting_without_password_or_reconnect(tmp_path, monkeypatch, publish_delay):
    """无密文时只封锁命令，运行与日志继续，且不再解密或自动重试 debug。"""
    repo, task = await repository(tmp_path)
    task["pshSerialCharacterInterval"] = 0
    repo.settings.psh_serial_character_interval = 0
    task["initialCommands"] = [{"command": "debug", "timeoutSeconds": .05}, {"command": "next"}]
    blocked, published = asyncio.Event(), asyncio.Event()
    original_on_log = SessionRuntime.on_log
    original_on_debug = SessionRuntime.on_debug

    async def on_debug(self, event, details):
        """等待无挑战码的失败状态完成持久化，而不是等待不会出现的第三次发送。"""
        await original_on_debug(self, event, details)
        if event == "BLOCKED":
            blocked.set()

    async def on_log(self, chunk):
        """模拟发布协程被调度延迟，完成后以事件通知断言方。"""
        continuous = b"continuous device log" in chunk.data
        if continuous:
            await asyncio.sleep(publish_delay)
        await original_on_log(self, chunk)
        if continuous:
            published.set()

    monkeypatch.setattr(SessionRuntime, "on_log", on_log)
    monkeypatch.setattr(SessionRuntime, "on_debug", on_debug)

    class Connection:
        def __init__(self):
            self.received = asyncio.Queue()
            self.received.put_nowait(b"Protect Shell (psh)\r\n# ")
            self.sent, self.closed = [], False

        async def read(self):
            return await self.received.get()

        async def write(self, data):
            self.sent.append(data)

        async def close(self):
            self.closed = True
            self.received.put_nowait(b"")

    connection = Connection()
    factory = AsyncMock(return_value=connection)
    runtime = SessionRuntime(repo, task, connection_factory=factory)
    provider = AsyncMock(return_value="must-not-be-sent")
    runtime.debug_passwords = provider
    try:
        await asyncio.wait_for(blocked.wait(), timeout=2)
        await connection.received.put(b"continuous device log\r\n")
        await asyncio.wait_for(published.wait(), timeout=2)
        assert runtime.error is None
        assert not runtime.background.done()
        factory.assert_awaited_once()
        provider.assert_not_awaited()
        assert connection.sent[0] == b"debug\n"
        assert connection.sent[1:] and set(connection.sent[1:]) == {b"\x03"}
        assert any(b"continuous device log" in __import__("base64").b64decode(frame["data"]) for frame in runtime.frames)
    finally:
        await runtime.stop()


async def test_debug_events_persist_task_recovery_state_without_run_latch(tmp_path):
    """FAILED 仅记录本次任务状态；RECOVERED 解除阻断，不向 runs 写失败锁存。"""
    repo, task = await repository(tmp_path)
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.task = repo, task
    runtime.collector = SimpleNamespace(session_id="debug-session")
    failure = "PSH 调试失败，本次命令未自动重试"

    await runtime.on_debug("FAILED", {"mode": "PSH", "commandBlocked": True, "debugError": failure})
    await runtime.on_debug("RECOVERED", {"mode": "PSH", "commandBlocked": False, "debugError": failure})

    stored_run = await repo.get("runs", task["runId"])
    stored_task = await repo.get("tasks", task["id"])
    events = [event async for event in repo.db.events.find({"taskId": task["id"]})]
    assert "debugFailed" not in stored_run
    assert stored_task["debugPhase"] == "RECOVERED"
    assert stored_task["commandBlocked"] is False and stored_task["debugError"] == failure
    assert [(event["phase"], event["commandBlocked"]) for event in events] == [("FAILED", True), ("RECOVERED", False)]


async def test_blocked_manual_command_persists_failed_reason(tmp_path):
    """命令通道未确认恢复时，手动命令写为 FAILED 并保存不含密码的原因。"""
    repo, task = await repository(tmp_path)
    command = {"id": "blocked-manual", "taskId": task["id"], "runId": task["runId"], "kind": "MANUAL",
               "sessionId": "debug-session", "command": "show status", "status": "QUEUED"}
    await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"sessionId": "debug-session"}})
    await repo.db.commands.insert_one(command)
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.task, runtime.stopping = repo, task, False
    runtime.collector = SimpleNamespace(
        session_id="debug-session",
        enqueue_manual=AsyncMock(side_effect=CommandChannelBlocked("PSH 调试恢复未确认，当前会话暂不可发送命令")),
    )

    await runtime.manual(command)

    stored = await repo.get("commands", command["id"])
    assert stored["status"] == "FAILED"
    assert stored["error"] == "PSH 调试恢复未确认，当前会话暂不可发送命令"


@pytest.mark.parametrize(
    ("exception_name", "expected_error"),
    [
        ("PermissionDenied", "SSH账号认证失败"),
        ("HostKeyNotVerifiable", "SSH主机指纹未登记或不匹配"),
    ],
)
async def test_ssh_authentication_and_host_key_failures_do_not_retry_and_release(
    tmp_path, monkeypatch, exception_name, expected_error,
):
    """认证和主机指纹错误只尝试一次；失败采集器收尾后 Worker 才能释放端点锁。"""
    repo, task = await repository(tmp_path)

    class PermissionDenied(Exception):
        pass

    class HostKeyNotVerifiable(Exception):
        pass

    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(
        PermissionDenied=PermissionDenied, HostKeyNotVerifiable=HostKeyNotVerifiable,
    ))

    class SshFailureCollector:
        instances = 0
        stops = 0

        def __init__(self, _config, _root, *, connection_factory, **_kwargs):
            type(self).instances += 1
            self.connection_factory = connection_factory
            self.session_id = "failed-ssh-session"

        async def start(self):
            await self.connection_factory({})

        async def stop(self):
            type(self).stops += 1

    failure = getattr(sys.modules["asyncssh"], exception_name)("synthetic SSH failure")
    factory = AsyncMock(side_effect=failure)
    monkeypatch.setattr("camera_logs.collection.runtime.Collector", SshFailureCollector)
    runtime = SessionRuntime(repo, task, connection_factory=factory)
    await runtime.background

    factory.assert_awaited_once()
    assert SshFailureCollector.instances == 1
    assert SshFailureCollector.stops == 1
    assert runtime.error == expected_error

    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    await worker.release(runtime)

    assert task["id"] not in worker.active
    stored = await repo.get("tasks", task["id"])
    assert stored["status"] == "ERROR" and stored["desiredState"] == "STOPPED"
    assert await repo.db.endpoint_locks.count_documents({"taskId": task["id"]}) == 0

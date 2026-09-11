"""SSH 连接名额生命周期测试。

这些用例刻意不连接真实设备：关闭名额只能在底层传输得到 ``wait_closed``
确认后归还。MongoDB admission 是跨 Worker 的唯一设备容量来源；本地回调只
用于没有 admission 的兼容路径，未知关闭不得释放它。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from camera_logs.collection import connections
from camera_logs.collection.connections import _connect_ssh, _SshConnection
from camera_logs.collection.ssh_admission import SshAdmission, SshCapacityError, SshSlotUncertain
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


class _Admission:
    """记录 admission 调用次数，模拟跨 Worker MongoDB 占位。"""

    def __init__(self, acquire_error: BaseException | None = None) -> None:
        self.acquire_error = acquire_error
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> None:
        self.acquired += 1
        if self.acquire_error is not None:
            raise self.acquire_error

    async def release(self) -> None:
        self.released += 1


class _Process:
    """最小 shell 伪件，避免测试需要远端 PTY。"""

    def close(self) -> None:
        return None


class _Client:
    """可控制关闭确认结果的最小 AsyncSSH connection 伪件。"""

    def __init__(self, *, wait_error: BaseException | None = None) -> None:
        self.wait_error = wait_error
        self.closed = False
        self.aborted = False
        self.wait_calls = 0

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True

    async def wait_closed(self) -> None:
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error


def test_confirmed_close_releases_admission_exactly_once():
    """重复收尾不能重复归还已经确认关闭的 MongoDB 名额。"""
    async def scenario() -> tuple[_Admission, int]:
        admission, releases = _Admission(), 0

        def release_local() -> None:
            nonlocal releases
            releases += 1

        connection = _SshConnection(_Client(), _Process(), release_slot=release_local, admission=admission)
        await connection.close()
        await connection.close()
        return admission, releases

    admission, releases = asyncio.run(scenario())
    assert admission.acquired == 0
    assert admission.released == 1
    # admission 存在时 MongoDB 是唯一设备容量账本，不能同时归还本地配额。
    assert releases == 0


def test_close_timeout_keeps_all_slots_until_a_later_confirmed_retry(monkeypatch):
    """关闭未知时不释放；后续明确 wait_closed 成功后才精确归还一次。"""
    monkeypatch.setattr(connections, "CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> tuple[_Admission, int, _Client]:
        admission, releases = _Admission(), 0
        client = _Client()
        waiting = asyncio.Event()

        async def wait_closed() -> None:
            client.wait_calls += 1
            if client.wait_calls == 1:
                waiting.set()
                await asyncio.Event().wait()

        client.wait_closed = wait_closed

        def release_local() -> None:
            nonlocal releases
            releases += 1

        connection = _SshConnection(client, _Process(), release_slot=release_local, admission=admission)
        with pytest.raises(TimeoutError):
            await connection.close()
        assert waiting.is_set()
        assert admission.released == 0
        assert releases == 0
        assert client.aborted
        await connection.close()
        return admission, releases, client

    admission, releases, client = asyncio.run(scenario())
    assert admission.released == 1
    assert releases == 0
    assert client.wait_calls == 2


def test_local_close_failure_does_not_release_compatibility_callback():
    """没有 admission 的兼容路径也不能因关闭异常释放本地名额。"""
    async def scenario() -> tuple[int, _Client]:
        released = 0

        def release_local() -> None:
            nonlocal released
            released += 1

        client = _Client(wait_error=ConnectionResetError("peer reset"))
        with pytest.raises(ConnectionResetError, match="peer reset"):
            await _SshConnection(client, _Process(), release_slot=release_local).close()
        return released, client

    released, client = asyncio.run(scenario())
    assert released == 0
    assert client.aborted


def test_shell_failure_releases_admission_only_after_confirmed_client_close(monkeypatch):
    """shell 创建失败已取得 client 时，先确认关闭再归还 MongoDB 名额。"""
    class Client(_Client):
        async def create_process(self, **_kwargs):
            raise RuntimeError("shell unavailable")

    client, admission = Client(), _Admission()

    async def fake_connect(*_args, **_kwargs):
        return client

    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))
    with pytest.raises(RuntimeError, match="shell unavailable"):
        asyncio.run(_connect_ssh({"username": "u", "password": "p", "_sshAdmission": admission}, "192.0.2.81", 22))
    assert client.closed
    assert admission.acquired == 1
    assert admission.released == 1


def test_unknown_admission_blocks_before_asyncssh_connect(monkeypatch):
    """MongoDB 占位提交未知时，不得尝试建立可能成为第六路的 socket。"""
    calls = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("不应调用 asyncssh.connect")

    admission = _Admission(SshSlotUncertain("192.0.2.82", "unknown-token"))
    monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))
    with pytest.raises(SshSlotUncertain):
        asyncio.run(_connect_ssh({"username": "u", "password": "p", "_sshAdmission": admission}, "192.0.2.82", 22))
    assert admission.acquired == 1
    assert admission.released == 0
    assert calls == 0


def test_mongo_admission_rejects_sixth_before_asyncssh_connect(monkeypatch):
    """五个跨 Worker 占位已满时，第六次不触达设备也不触发 AsyncSSH。"""
    async def scenario() -> int:
        repo = Repository(
            AsyncMongoMockClient().db,
            Settings(_env_file=None, encryption_key=Fernet.generate_key().decode()),
        )
        await repo.initialize()
        calls = 0

        class Client(_Client):
            async def create_process(self, **_kwargs):
                return _Process()

        async def fake_connect(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Client()

        monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))
        task_base = {"ip": "192.0.2.83", "port": 22, "protocol": "SSH", "username": "u", "password": "p"}
        active = []
        for index in range(5):
            task = task_base | {"id": f"task-{index}", "runId": f"run-{index}", "generation": 1,
                                "nodeId": f"worker-{index}"}
            task["_sshAdmission"] = SshAdmission(repo, task)
            active.append(await _connect_ssh(task, task["ip"], task["port"]))
        rejected = task_base | {"id": "task-6", "runId": "run-6", "generation": 1, "nodeId": "worker-6"}
        rejected["_sshAdmission"] = SshAdmission(repo, rejected)
        with pytest.raises(SshCapacityError):
            await _connect_ssh(rejected, rejected["ip"], rejected["port"])
        for connection in active:
            await connection.close()
        return calls

    assert asyncio.run(scenario()) == 5


def test_connect_interruption_after_connection_made_waits_for_cleanup_before_releasing(monkeypatch):
    """建连中断后 client_factory 已观察到连接时，必须回收该连接再归还名额。"""
    async def scenario() -> tuple[_Admission, _Client]:
        admission = _Admission()
        client = _Client()
        entered = asyncio.Event()

        async def fake_connect(*_args, client_factory, **_kwargs):
            observer = client_factory()
            observer.connection_made(client)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setitem(sys.modules, "asyncssh", SimpleNamespace(connect=fake_connect))
        task = asyncio.create_task(_connect_ssh(
            {"username": "u", "password": "p", "_sshAdmission": admission}, "192.0.2.84", 22,
        ))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return admission, client

    admission, client = asyncio.run(scenario())
    assert client.closed
    assert client.wait_calls == 1
    assert admission.released == 1

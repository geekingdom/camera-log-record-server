"""验证从机 SSH 引导的租约恢复、单读取者交接和关闭不确定边界。"""

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.slave_shell import ShellBootstrap
from camera_logs.collection.slave_ssh import (
    SlaveBootstrapUncertain,
    active_task,
    close_bootstrap,
    connect_slave,
    ensure_service,
)
from camera_logs.collection.ssh_admission import SshSlotUncertain
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.ownership import OwnershipLost
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


class _QueueConnection:
    """可控单接收连接，模拟从机横幅和读取等待，不创建网络 socket。"""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.writes = []
        self.closed = False

    async def read(self, _size):
        return await self.queue.get()

    async def write(self, data):
        self.writes.append(data)
        if data.startswith(b"dbclient"):
            await self.queue.put(b"admin@192.168.253.164 password: ")
        elif data == b"secret\n":
            # 新从机横幅可以是 PSH；交接后 Collector 仍应从同一 reader 收到日志。
            await self.queue.put(b"BusyBox Protect Shell (psh)\r\n# slave-log\r\n")

    async def close(self):
        self.closed = True


class _CloseFailsConnection(_QueueConnection):
    async def close(self):
        self.closed = True
        raise OSError("transport close result unknown")


class _TemporaryBootstrapConnection(_QueueConnection):
    """临时主机引导连接；dropbear 命令写入后保持命令响应未完成。"""

    def __init__(self, *, block_close=False, complete_command=False):
        super().__init__()
        self.command_written = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_finish = asyncio.Event()
        self.block_close = block_close
        self.complete_command = complete_command
        self.queue.put_nowait(b"BusyBox main built-in shell (ash)\r\n# ")

    async def read(self, size):
        try:
            return await super().read(size)
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise

    async def write(self, data):
        await super().write(data)
        if data.startswith(b"/usr/sbin/dropbear"):
            self.command_written.set()
            if self.complete_command:
                marker = re.search(rb"(__SLAVE_BOOT_[0-9a-f]+):%s", data)
                assert marker is not None
                await self.queue.put(b"\n" + marker.group(1) + b":0\n")

    async def close(self):
        self.closed = True
        self.close_started.set()
        if self.block_close:
            await self.close_finish.wait()


class _CommandGateConnection(_QueueConnection):
    """阻塞首条监控命令写入，使第二条future在同一Collector FIFO中等待取消。"""

    def __init__(self):
        super().__init__()
        self.first_written = asyncio.Event()
        self.release_first = asyncio.Event()

    async def read(self, size=65536):
        return await super().read(size)

    async def write(self, data):
        self.writes.append(data)
        if b"first" not in data:
            return
        self.first_written.set()
        await self.release_first.wait()
        marker = re.search(rb"(__CAMERA_LOGS_METRIC_[0-9a-f]+__):%s", data)
        assert marker is not None
        await self.queue.put(b"\n" + marker.group(1) + b":0\n")


def _repo(tmp_path):
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path)
    return Repository(AsyncMongoMockClient().db, settings)


def _config(identifier="slave"):
    return {
        "id": identifier,
        "resourceId": "resource",
        "ip": "192.0.2.35",
        "port": 22,
        "protocol": "SSH",
        "sshTarget": "SLAVE_1",
        "password": "secret",
        "runId": identifier + "-run",
        "nodeId": "node",
        "generation": 1,
    }


async def _insert_running(repo, *configs, resource=None):
    await repo.db.resources.insert_one(resource or {"id": "resource", "deletedAt": None, "healthStatus": "ONLINE"})
    for config in configs:
        await repo.db.tasks.insert_one(dict(config) | {"desiredState": "RUNNING", "status": "CONNECTING"})


async def test_psh_slave_banner_hands_only_slave_bytes_to_collector():
    connection = _QueueConnection()
    await connection.queue.put(b"BusyBox main built-in shell (ash)\r\n# ")
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    try:
        await shell.enter_slave("SLAVE_1", "secret")
        # 模式匹配起点后的新横幅及日志进入采集器，主机横幅和密码提示不会泄漏。
        assert await shell.read() == b"Protect Shell (psh)\r\n# slave-log\r\n"
        assert shell.ready
    finally:
        await shell.close()


async def test_read_timeout_and_cancellation_do_not_discard_deque_data():
    connection = _QueueConnection()
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    shell.ready = True
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(shell.read(), .01)
        pending = asyncio.create_task(shell.read())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await connection.queue.put(b"first")
        await connection.queue.put(b"second")
        await asyncio.sleep(0)
        assert await shell.read() == b"first"
        assert await shell.read() == b"second"
    finally:
        await shell.close()


async def test_expired_bootstrap_lease_is_recovered_by_current_owner(tmp_path, monkeypatch):
    from camera_logs.collection import slave_ssh

    repo, config = _repo(tmp_path), _config()
    await _insert_running(repo, config, resource={
        "id": "resource", "deletedAt": None, "healthStatus": "ONLINE",
        "slaveSshBootstrap": {"token": "expired", "expiresAt": now() - timedelta(seconds=1)},
    })
    available, calls = False, 0
    async def service_available(*_args):
        return available

    async def borrow(*_args):
        nonlocal available, calls
        calls += 1
        available = True
        return True

    monkeypatch.setattr(slave_ssh, "ssh_service_available", service_available)
    monkeypatch.setattr(slave_ssh, "borrow_host", borrow)
    direct = AsyncMock()
    await ensure_service(SimpleNamespace(repo=repo), config, 18080, direct)
    assert calls == 1
    direct.assert_not_awaited()
    assert available
    assert (await repo.db.resources.find_one({"id": "resource"})).get("slaveSshBootstrap") is None


async def test_concurrent_same_resource_bootstrap_only_borrows_once(tmp_path, monkeypatch):
    from camera_logs.collection import slave_ssh

    repo, first, second = _repo(tmp_path), _config("first"), _config("second")
    await _insert_running(repo, first, second)
    started, available = asyncio.Event(), False
    calls = 0

    async def service_available(*_args):
        return available

    async def borrow(*_args):
        nonlocal calls, available
        calls += 1
        started.set()
        await asyncio.sleep(0)
        available = True
        return True

    async def wait_or_yield(_seconds):
        await started.wait()

    monkeypatch.setattr(slave_ssh, "ssh_service_available", service_available)
    monkeypatch.setattr(slave_ssh, "borrow_host", borrow)
    monkeypatch.setattr(slave_ssh.asyncio, "sleep", wait_or_yield)
    await asyncio.gather(
        ensure_service(SimpleNamespace(repo=repo), first, 18080, AsyncMock()),
        ensure_service(SimpleNamespace(repo=repo), second, 18080, AsyncMock()),
    )
    assert calls == 1


async def test_active_task_rejects_error_run(tmp_path):
    repo, error = _repo(tmp_path), _config()
    await _insert_running(repo, error)
    await repo.db.tasks.update_one({"id": error["id"]}, {"$set": {"status": "ERROR"}})
    with pytest.raises(OwnershipLost):
        await active_task(repo, error)


async def test_active_task_rejects_paused_run(tmp_path):
    repo, paused = _repo(tmp_path), _config()
    await _insert_running(repo, paused)
    await repo.db.tasks.update_one({"id": paused["id"]}, {"$set": {"desiredState": "PAUSED", "status": "PAUSED"}})
    with pytest.raises(OwnershipLost):
        await active_task(repo, paused)


async def test_bootstrap_close_failure_is_uncertain_and_never_claims_closed():
    shell = ShellBootstrap(_CloseFailsConnection(), {"protocol": "SSH"})
    with pytest.raises(SshSlotUncertain) as raised:
        await close_bootstrap(shell, _config())
    assert raised.value.token == "bootstrap-close"
    assert shell.connection.closed


async def test_bounded_slave_preread_queue_drains_received_sequence_on_collector_stop(tmp_path):
    """停止先关闭输入后，Collector 仍需排空从机 reader 已接收的 16 个有序块。"""
    source = [f"source-{number:03}\n".encode() for number in range(20)]
    connection = _QueueConnection()
    shell = ShellBootstrap(connection, {"protocol": "SSH"})
    shell.ready = True
    received = []
    try:
        for block in source:
            await connection.queue.put(block)
        for _ in range(100):
            if len(shell.queue) == 16:
                break
            await asyncio.sleep(.001)
        assert len(shell.queue) == 16
        # 第17块仍停留在底层连接，证明预读队列没有越过固定上限。
        assert connection.queue.qsize() == 4

        collector = Collector(
            {"id": "bounded", "runId": "run", "storageIdentity": "slave", "initialCommands": []},
            tmp_path, connection_factory=lambda _task: shell, on_log=received.append,
        )
        await collector.start()
        await collector.stop()
    finally:
        if not shell.connection.closed:
            await shell.close()

    full = b"".join(chunk.data for chunk in received)
    # 时间前缀可随运行时变化，只核对唯一源序号的全文和实际写入位置严格连续。
    assert re.findall(rb"source-\d{3}", full) == [block.strip() for block in source[:16]]
    assert [chunk.sequence for chunk in received] == list(range(1, 17))
    expected_offset = 0
    for chunk in received:
        assert chunk.offset == expected_offset
        expected_offset += len(chunk.data)


async def test_every_slave_factory_handshakes_before_collector_initialization(tmp_path, monkeypatch):
    """每个从机重连都必须获得已握手的 shell，Collector 不能把初始化命令发到主机。"""
    from camera_logs.collection import slave_ssh
    config = _config()
    repo = SimpleNamespace(
        settings=SimpleNamespace(),
        audit=AsyncMock(),
        db=SimpleNamespace(tasks=SimpleNamespace(update_one=AsyncMock())),
    )
    worker = SimpleNamespace(repo=repo)
    connections = []

    async def direct(_task):
        connection = _QueueConnection()
        await connection.queue.put(b"BusyBox main built-in shell (ash)\r\n# ")
        connections.append(connection)
        return connection

    monkeypatch.setattr(slave_ssh, "PshPasswordProvider", lambda _settings: None)
    monkeypatch.setattr(slave_ssh, "shared_port", AsyncMock(return_value=18080))
    monkeypatch.setattr(slave_ssh, "ensure_service", AsyncMock())
    monkeypatch.setattr(slave_ssh, "active_task", AsyncMock(return_value={}))

    async def factory(_task):
        return await connect_slave(worker, config, direct)

    for number in range(2):
        collector = Collector(
            {"id": f"collector-{number}", "runId": "run", "storageIdentity": "slave",
             "initialCommands": [{"command": "collector-initial"}]},
            tmp_path, connection_factory=factory,
        )
        await collector.start()
        await collector.stop()

    assert len(connections) == 2
    for connection in connections:
        assert connection.writes == [
            b"dbclient admin@192.168.253.164 -y; exit\n", b"secret\n", b"collector-initial\n",
        ]


async def test_cancelled_temporary_bootstrap_closes_reader_and_clears_resource_lease(tmp_path, monkeypatch):
    """首次取消进入 finally 后仍完成 close，资源租约不会残留至下一次从机恢复。"""
    from camera_logs.collection import slave_ssh

    repo, config = _repo(tmp_path), _config()
    await _insert_running(repo, config)
    connection = _TemporaryBootstrapConnection()
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    monkeypatch.setattr(slave_ssh, "borrow_host", AsyncMock(return_value=False))
    task = asyncio.create_task(ensure_service(SimpleNamespace(repo=repo), config, 18080, AsyncMock(return_value=connection)))
    await asyncio.wait_for(connection.command_written.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.closed
    assert connection.read_cancelled.is_set()
    resource = await repo.db.resources.find_one({"id": "resource"})
    assert resource.get("slaveSshBootstrap") is None


async def test_second_cancellation_during_temporary_bootstrap_close_is_slot_uncertain(tmp_path, monkeypatch):
    """关闭结果被第二次取消打断时不得返回普通取消并伪造 SSH 已安全关闭。"""
    from camera_logs.collection import slave_ssh

    repo, config = _repo(tmp_path), _config()
    await _insert_running(repo, config)
    connection = _TemporaryBootstrapConnection(block_close=True)
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    monkeypatch.setattr(slave_ssh, "borrow_host", AsyncMock(return_value=False))
    task = asyncio.create_task(ensure_service(SimpleNamespace(repo=repo), config, 18080, AsyncMock(return_value=connection)))
    await asyncio.wait_for(connection.command_written.wait(), 1)
    task.cancel()
    await asyncio.wait_for(connection.close_started.wait(), 1)
    task.cancel()
    with pytest.raises(SshSlotUncertain) as raised:
        await task
    assert raised.value.token == "bootstrap-close"
    resource = await repo.db.resources.find_one({"id": "resource"})
    assert resource.get("slaveSshBootstrap") is None


async def test_unknown_cross_node_bootstrap_keeps_lease_and_never_falls_back_to_direct(tmp_path, monkeypatch):
    """跨节点响应未知时，当前及后继代次都不能立刻再向原端口发送引导命令。"""
    from camera_logs.collection import slave_ssh

    repo, config = _repo(tmp_path), _config()
    await _insert_running(repo, config)
    direct = AsyncMock()
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    monkeypatch.setattr(slave_ssh, "borrow_host", AsyncMock(side_effect=SlaveBootstrapUncertain("response lost")))

    with pytest.raises(Exception, match="结果未知"):
        await ensure_service(SimpleNamespace(repo=repo), config, 18080, direct)
    direct.assert_not_awaited()
    event = await repo.db.events.find_one({"type": "SLAVE_SSH_BOOTSTRAP"})
    assert event is not None
    assert (event["phase"], event["outcome"], event["level"]) == ("REMOTE_RESULT_UNKNOWN", "UNKNOWN", "WARNING")
    assert event["taskId"] == config["id"] and event["port"] == 18080
    resource = await repo.db.resources.find_one({"id": "resource"})
    lease = resource["slaveSshBootstrap"]
    assert lease["expiresAt"].replace(tzinfo=now().tzinfo) > now() and lease.get("uncertainAt") is not None

    successor = _config("successor") | {"generation": 2, "runId": "successor-run"}
    await repo.db.tasks.insert_one(successor | {"desiredState": "RUNNING", "status": "CONNECTING"})
    acquired = await repo.db.resources.update_one(
        {"id": "resource", "$or": [{"slaveSshBootstrap": None}, {"slaveSshBootstrap.expiresAt": {"$lt": now()}}]},
        {"$set": {"slaveSshBootstrap": {"token": "new", "expiresAt": now()}}},
    )
    assert acquired.matched_count == 0


async def test_temporary_bootstrap_service_not_ready_records_fixed_failure_event(tmp_path, monkeypatch):
    """临时命令已提交但扩展端口无SSH横幅时，保留可检索失败事件而不写设备输出。"""
    from camera_logs.collection import slave_ssh

    repo, config = _repo(tmp_path), _config()
    await _insert_running(repo, config)
    connection = _TemporaryBootstrapConnection(complete_command=True)
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    monkeypatch.setattr(slave_ssh, "borrow_host", AsyncMock(return_value=False))

    with pytest.raises(slave_ssh.SlaveLoginError, match="未就绪"):
        await ensure_service(SimpleNamespace(repo=repo), config, 18080, AsyncMock(return_value=connection))

    events = [item async for item in repo.db.events.find({"type": "SLAVE_SSH_BOOTSTRAP"})]
    assert [(item["phase"], item["outcome"]) for item in events] == [
        ("TEMPORARY_BOOTSTRAPPED", "UNKNOWN"), ("SERVICE_NOT_READY", "FAILED"),
    ]
    assert "secret" not in str(events)


async def test_cancelled_remote_request_preserves_lease_and_propagates_cancellation(tmp_path, monkeypatch):
    """HTTP请求发出后外层暂停不能吞掉取消，也不能清除可能仍在远端执行的租约。"""
    from camera_logs.collection import slave_ssh

    class BlockingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            request_started.set()
            await asyncio.Event().wait()

    repo, config = _repo(tmp_path), _config()
    host = _config("host") | {"sshTarget": "HOST", "status": "COLLECTING", "nodeId": "remote", "generation": 3}
    await _insert_running(repo, config, host)
    await repo.db.tasks.update_one({"id": "host"}, {"$set": {"status": "COLLECTING"}})
    await repo.db.nodes.insert_one({"id": "remote", "url": "http://remote"})
    request_started, client = asyncio.Event(), BlockingClient()
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    monkeypatch.setattr(slave_ssh.httpx, "AsyncClient", lambda **_kwargs: client)

    task = asyncio.create_task(ensure_service(SimpleNamespace(repo=repo), config, 18080, AsyncMock()))
    await asyncio.wait_for(request_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resource = await repo.db.resources.find_one({"id": "resource"})
    assert resource["slaveSshBootstrap"].get("uncertainAt") is not None
    event = await repo.db.events.find_one({"type": "SLAVE_SSH_BOOTSTRAP", "phase": "REMOTE_RESULT_UNKNOWN"})
    assert event is not None
    assert event["hostTaskId"] == "host" and event["hostNodeId"] == "remote"


@pytest.mark.parametrize("body, raises", [
    ({"bootstrapped": False}, None),
    ([], SlaveBootstrapUncertain),
])
async def test_remote_explicit_false_allows_fallback_but_malformed_response_is_unknown(tmp_path, monkeypatch, body, raises):
    """只有结构正确的明确false表明远端未执行；非对象响应必须保留未知风险。"""
    from camera_logs.collection import slave_ssh

    class Response:
        status_code = 200

        def json(self):
            return body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    repo, config = _repo(tmp_path), _config()
    host = _config("host") | {"sshTarget": "HOST", "status": "COLLECTING", "nodeId": "remote", "generation": 3}
    await _insert_running(repo, config, host)
    await repo.db.tasks.update_one({"id": "host"}, {"$set": {"status": "COLLECTING"}})
    await repo.db.nodes.insert_one({"id": "remote", "url": "http://remote"})
    attempt = dict(config) | {"_bootstrapToken": "token"}
    monkeypatch.setattr(slave_ssh.httpx, "AsyncClient", lambda **_kwargs: Client())
    if raises:
        with pytest.raises(raises):
            await slave_ssh.borrow_host(SimpleNamespace(repo=repo), attempt, 18080)
        assert attempt["_bootstrapRemoteRequestStarted"]
    else:
        assert not await slave_ssh.borrow_host(SimpleNamespace(repo=repo), attempt, 18080)
        assert not attempt["_bootstrapRemoteRequestStarted"]


async def test_cancelled_queued_collector_capture_never_writes_after_active_command_finishes(tmp_path):
    """远端超时取消尚未发送的监控future后，FIFO不得在首条完成时补发dropbear。"""
    connection = _CommandGateConnection()
    collector = Collector(
        {"id": "queue", "runId": "run", "storageIdentity": "slave", "initialCommands": []},
        tmp_path, connection_factory=lambda _task: connection,
    )
    await collector.start()
    first = asyncio.create_task(collector.capture_monitor_command("first", timeout_seconds=1))
    await asyncio.wait_for(connection.first_written.wait(), 1)
    queued = asyncio.create_task(collector.capture_monitor_command("second", timeout_seconds=1))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    connection.release_first.set()
    await first
    await collector.stop()
    assert len(connection.writes) == 1 and b"first" in connection.writes[0]

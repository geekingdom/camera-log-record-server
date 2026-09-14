"""跨 Worker 从机引导的本机 HTTP 加 MongoMock 验证，不访问真实设备。

真实Collector读取模拟ASH横幅会写入专用测试日志目录，fixture仅在Collector停止后清理
该已知子目录。本测试不替代物理Worker、真实MongoDB或跨主机网络验收。
"""

import asyncio
import re
import shutil
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from camera_logs.collection.collector import Collector
from camera_logs.collection.slave_shell import SlaveLoginError
from camera_logs.collection.slave_ssh import ensure_service
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.slave_routes import install_slave_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from mongomock_motor import AsyncMongoMockClient


class _HostConnection:
    """模拟已采集主机的唯一Collector连接，固定ASH并可阻塞dropbear写入。"""

    def __init__(self, *, block=False):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(b"BusyBox main built-in shell (ash)\r\n# ")
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.actions = 0
        self.block = block
        self.closed = False

    async def read(self, _size=65536):
        return await self.queue.get()

    async def write(self, data):
        if b"dropbear" not in data:
            return
        self.actions += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        marker = re.search(rb"(__CAMERA_LOGS_METRIC_[0-9a-f]+__):%s", data)
        assert marker is not None
        await self.queue.put(b"\n" + marker.group(1) + b":0\n")

    async def close(self):
        self.closed = True


class _TemporaryConnection(_HostConnection):
    """模拟临时原端口引导，收到dropbear后让扩展SSH探测变为可用。"""

    def __init__(self):
        super().__init__()
        self.ready = False

    async def write(self, data):
        if b"dropbear" not in data:
            return
        self.actions += 1
        self.ready = True
        marker = re.search(rb"(__SLAVE_BOOT_[0-9a-f]+):%s", data)
        assert marker is not None
        await self.queue.put(b"\n" + marker.group(1) + b":0\n")


def _config(identifier, *, target="SLAVE_1", node="caller", status="CONNECTING"):
    return {
        "id": identifier, "resourceId": "resource", "ip": "192.0.2.35", "port": 22,
        "protocol": "SSH", "sshTarget": target, "password": "secret", "runId": identifier + "-run",
        "generation": 1, "nodeId": node, "desiredState": "RUNNING", "status": status,
    }


@pytest.fixture
def isolated_log_root(tmp_path):
    """为本文件的模拟Collector创建可证明归属的日志目录，绝不清理pytest临时根目录。"""
    root = tmp_path / "cross-worker-http-logs"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


async def _repo(log_root):
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=log_root,
                        node_id="caller", internal_token="cross-worker-token")
    return Repository(AsyncMongoMockClient().db, settings)


async def _wait_for_ash(collector):
    for _ in range(100):
        if collector._commands._debug.mode == "ASH":  # 仅测试同步连接已进入ASH。
            return
        await asyncio.sleep(.005)
    raise AssertionError("host collector did not receive ASH banner")


async def _wait_for_host_attempt(repo):
    """等待远端路由完成命令及审计，避免仅凭开始写入就把未收尾动作当成功。"""
    for _ in range(200):
        event = await repo.db.audit.find_one({"action": "slave-ssh-bootstrap-host-attempt", "targetId": "slave"})
        if event:
            return
        await asyncio.sleep(.005)
    raise AssertionError("remote host bootstrap did not finish its audit")


@asynccontextmanager
async def _server(worker, token):
    """启动并停止临时loopback HTTP服务，测试结束前等待端口和server任务收尾。"""
    app = FastAPI()
    app.state.worker = worker
    install_slave_routes(app, SimpleNamespace(internal_token=token))
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(.005)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 3)
        finally:
            listener.close()


async def test_http_timeout_keeps_lease_and_remote_collector_executes_dropbear_once(isolated_log_root, monkeypatch):
    """调用端超时后远端FIFO仍可结束，但120秒租约阻止后继任务重复引导。"""
    from camera_logs.collection import slave_ssh

    repo = await _repo(isolated_log_root)
    requester, host = _config("slave"), _config("host", target="HOST", node="remote", status="COLLECTING")
    await repo.db.resources.insert_one({"id": "resource", "deletedAt": None, "healthStatus": "ONLINE", "slaveSshPort": 18080})
    await repo.db.tasks.insert_many([requester, host])
    connection = _HostConnection(block=True)
    collector = Collector({"id": "host", "runId": host["runId"], "storageIdentity": "host", "initialCommands": []},
                          isolated_log_root, connection_factory=lambda _task: connection)
    await collector.start()
    await _wait_for_ash(collector)
    remote = SimpleNamespace(repo=repo, active={"host": SimpleNamespace(task=host, collector=collector, stopping=False, retired=False)})
    monkeypatch.setattr(slave_ssh, "REMOTE_BOOTSTRAP_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    try:
        async with _server(remote, repo.settings.internal_token) as url:
            caller = None
            try:
                await repo.db.nodes.insert_one({"id": "remote", "url": url})
                caller = asyncio.create_task(ensure_service(SimpleNamespace(repo=repo), requester, 18080, AsyncMock()))
                # 宽裕的1秒HTTP超时从服务已进入真实Collector FIFO后才开始等待响应。
                await asyncio.wait_for(connection.started.wait(), 1)
                with pytest.raises(SlaveLoginError, match="结果未知"):
                    await caller
                lease = (await repo.db.resources.find_one({"id": "resource"}))["slaveSshBootstrap"]
                assert lease.get("uncertainAt") is not None
                rejected = await repo.db.resources.update_one(
                    {"id": "resource", "$or": [{"slaveSshBootstrap": None}, {"slaveSshBootstrap.expiresAt": {"$lt": now()}}]},
                    {"$set": {"slaveSshBootstrap": {"token": "new", "expiresAt": now()}}},
                )
                assert rejected.matched_count == 0
                connection.release.set()
                await _wait_for_host_attempt(repo)
                assert connection.actions == 1
            finally:
                connection.release.set()
                if caller is not None and not caller.done():
                    caller.cancel()
                    await asyncio.gather(caller, return_exceptions=True)
    finally:
        await collector.stop()


async def test_http_client_cancellation_preserves_lease_while_remote_fifo_is_active(isolated_log_root, monkeypatch):
    """调用端暂停取消HTTP await后仍保留租约，远端已进入FIFO的动作不能被本地重发覆盖。"""
    from camera_logs.collection import slave_ssh

    repo = await _repo(isolated_log_root)
    requester, host = _config("slave"), _config("host", target="HOST", node="remote", status="COLLECTING")
    await repo.db.resources.insert_one({"id": "resource", "deletedAt": None, "healthStatus": "ONLINE", "slaveSshPort": 18080})
    await repo.db.tasks.insert_many([requester, host])
    connection = _HostConnection(block=True)
    collector = Collector({"id": "host", "runId": host["runId"], "storageIdentity": "host", "initialCommands": []},
                          isolated_log_root, connection_factory=lambda _task: connection)
    await collector.start()
    await _wait_for_ash(collector)
    remote = SimpleNamespace(repo=repo, active={"host": SimpleNamespace(task=host, collector=collector, stopping=False, retired=False)})
    monkeypatch.setattr(slave_ssh, "ssh_service_available", AsyncMock(return_value=False))
    try:
        async with _server(remote, repo.settings.internal_token) as url:
            caller = None
            try:
                await repo.db.nodes.insert_one({"id": "remote", "url": url})
                caller = asyncio.create_task(ensure_service(SimpleNamespace(repo=repo), requester, 18080, AsyncMock()))
                await asyncio.wait_for(connection.started.wait(), 1)
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                lease = (await repo.db.resources.find_one({"id": "resource"}))["slaveSshBootstrap"]
                assert lease.get("uncertainAt") is not None
                connection.release.set()
                await _wait_for_host_attempt(repo)
                assert connection.actions == 1
            finally:
                connection.release.set()
                if caller is not None and not caller.done():
                    caller.cancel()
                    await asyncio.gather(caller, return_exceptions=True)
    finally:
        await collector.stop()


async def test_http_explicit_false_falls_back_to_temporary_bootstrap(isolated_log_root, monkeypatch):
    """远端HTTP明确200 false时未写主机命令，调用端可安全使用临时原端口恢复服务。"""
    from camera_logs.collection import slave_ssh

    repo = await _repo(isolated_log_root)
    requester, absent_host = _config("slave"), _config("host", target="HOST", node="remote", status="COLLECTING")
    await repo.db.resources.insert_one({"id": "resource", "deletedAt": None, "healthStatus": "ONLINE", "slaveSshPort": 18080})
    await repo.db.tasks.insert_many([requester, absent_host])
    remote = SimpleNamespace(repo=repo, active={})
    temporary = _TemporaryConnection()

    async def available(*_args):
        return temporary.ready

    monkeypatch.setattr(slave_ssh, "ssh_service_available", available)
    try:
        async with _server(remote, repo.settings.internal_token) as url:
            await repo.db.nodes.insert_one({"id": "remote", "url": url})
            await ensure_service(SimpleNamespace(repo=repo), requester, 18080, AsyncMock(return_value=temporary))
        assert temporary.actions == 1
        assert (await repo.db.resources.find_one({"id": "resource"})).get("slaveSshBootstrap") is None
    finally:
        if not temporary.closed:
            await temporary.close()

"""节点 telemetry 的采样、超时、压力准入和能力声明回归。"""

import asyncio
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.telemetry import TelemetrySampler
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


class Cpu(tuple):
    """提供 cpu_times 所需的可迭代字段。"""

    user = property(lambda self: self[0])
    system = property(lambda self: self[1])
    idle = property(lambda self: self[2])
    iowait = property(lambda self: self[3])


class CpuWithGuest(Cpu):
    """模拟 psutil Linux CPU 字段，guest 已包含于 user。"""

    _fields = ("user", "system", "idle", "iowait", "guest", "guest_nice")


def test_network_proc_excludes_loopback_and_internal_interfaces(tmp_path):
    """宿主网络统计不能把 lo、veth 与 docker bridge 流量重复计入。"""
    proc = tmp_path / "net.dev"
    proc.write_text("h\nh\n lo: 9 0 0 0 0 0 0 0 9\neth0: 10 0 0 0 0 0 0 0 20\nveth1: 30 0 0 0 0 0 0 0 40\n")
    assert TelemetrySampler()._network(proc) == {"eth0": (10, 20)}


def test_first_sample_and_counter_reset_are_unknown(monkeypatch):
    """首轮 CPU/网络及后续计数回退不能伪装成零负载。"""
    sampler = TelemetrySampler()
    values = iter([Cpu((1, 1, 8, 0)), Cpu((2, 1, 9, 0))])
    monkeypatch.setattr("camera_logs.node.telemetry.psutil.cpu_times", lambda: next(values))
    monkeypatch.setattr("camera_logs.node.telemetry.psutil.virtual_memory", lambda: type("Memory", (), {"used": 1, "total": 2, "percent": 50})())
    monkeypatch.setattr(sampler, "_network", lambda _: {"eth0": (10, 20)})
    first = sampler.sample()
    monkeypatch.setattr(sampler, "_network", lambda _: {"eth0": (1, 1)})
    second = sampler.sample()
    assert first["cpuPercent"] is first["networkUploadBytesPerSecond"] is None
    assert second["networkUploadBytesPerSecond"] is second["networkDownloadBytesPerSecond"] is None
    assert first["sampledAt"].tzinfo is UTC
    assert second["cpuPercent"] == pytest.approx(50)


def test_guest_and_interface_change_do_not_create_false_rates(monkeypatch):
    """guest 重复计数和网卡增减都不能伪造 CPU 或网络的连续样本。"""
    sampler = TelemetrySampler()
    values = iter([CpuWithGuest((10, 10, 80, 0, 5, 0)), CpuWithGuest((20, 10, 90, 0, 10, 0))])
    monkeypatch.setattr("camera_logs.node.telemetry.psutil.cpu_times", lambda: next(values))
    monkeypatch.setattr(
        "camera_logs.node.telemetry.psutil.virtual_memory",
        lambda: type("Memory", (), {"used": 1, "total": 2, "percent": 50})(),
    )
    monkeypatch.setattr(sampler, "_network", lambda _: {"eth0": (100, 100)})
    sampler.sample()
    monkeypatch.setattr(sampler, "_network", lambda _: {"eth0": (110, 110), "wlan0": (9_000, 9_000)})
    result = sampler.sample()
    assert result["cpuPercent"] == pytest.approx(50)
    assert result["networkUploadBytesPerSecond"] is None
    assert result["networkDownloadBytesPerSecond"] is None


async def make_worker(tmp_path, *, nfs_server_ip=""):
    """创建不接触开发机配置、磁盘和数据库服务的独立 Worker。"""
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path,
        node_id="telemetry-node",
        nfs_server_ip=nfs_server_ip,
    )
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    worker = Worker(repo)
    worker.last_maintenance = time.monotonic()
    return worker, repo


async def test_timeout_publishes_unknown_without_late_sample_overwrite(tmp_path, monkeypatch):
    """超时后即使线程稍后返回，也只能保留无时间戳的未知采样。"""
    worker, _ = await make_worker(tmp_path)
    monkeypatch.setattr(worker.telemetry, "sample", lambda: {"sampledAt": datetime.now(UTC), "status": "OK"})

    async def timed_out(awaitable, *, timeout):
        raise TimeoutError

    monkeypatch.setattr("camera_logs.node.worker.asyncio.wait_for", timed_out)
    await worker._sample_telemetry()
    assert worker.telemetry_value == {
        "sampledAt": None,
        "scope": "UNKNOWN",
        "status": "UNKNOWN",
        "error": "TimeoutError",
    }


async def test_tick_keeps_one_telemetry_thread_and_close_waits_for_it(tmp_path, monkeypatch):
    """慢采样不能阻塞心跳、重叠启动，退出也必须等其线程收尾。"""
    worker, _ = await make_worker(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = 0

    def slow_sample():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        return {"sampledAt": datetime.now(UTC), "scope": "HOST", "status": "OK"}

    monkeypatch.setattr(worker.telemetry, "sample", slow_sample)
    disk = SimpleNamespace(used=10, total=100, free=90)
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=disk):
        await worker.tick()
        await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
        await worker.tick()
        assert calls == 1
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
    assert worker.telemetry_task.cancelled()
    assert worker.telemetry_work.done()


async def test_pressure_blocks_only_new_admission_and_reports_nfs_capability(tmp_path):
    """遥测压力关闭新建连接，但不停止已有采集，且节点公布 NFS 能力。"""
    worker, repo = await make_worker(tmp_path, nfs_server_ip="192.0.2.10")
    active = {"id": "active", "runId": "run-active", "nodeId": "telemetry-node", "status": "COLLECTING", "desiredState": "RUNNING"}
    pending = {"id": "pending", "runId": "run-pending", "nodeId": "telemetry-node", "status": "PENDING", "desiredState": "RUNNING"}
    await repo.db.tasks.insert_many([active, pending])
    runtime = SimpleNamespace(
        task=active,
        input_bytes=0,
        stopping=False,
        error=None,
        background=asyncio.get_running_loop().create_future(),
        background_failure=lambda: None,
        stop=AsyncMock(),
    )
    worker.active["active"] = runtime
    worker.telemetry_value = {"sampledAt": datetime.now(UTC), "scope": "HOST", "status": "OK", "cpuPercent": 96, "memoryPercent": 10}
    disk = SimpleNamespace(used=10, total=100, free=90)
    with patch("camera_logs.node.worker.shutil.disk_usage", return_value=disk), patch("camera_logs.node.worker.SessionRuntime") as factory:
        await worker.tick()
        factory.assert_not_called()
    heartbeat = await repo.get("nodes", "telemetry-node")
    assert heartbeat["accepting"] is False
    assert heartbeat["capabilities"] == {"coredumpNfs": True}
    runtime.stop.assert_not_awaited()

"""服务压测故障注入：创建或停止失败时仍须请求资源软删除并等待完成。"""

import asyncio
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


async def test_observer_failure_cancels_emitter_immediately(monkeypatch):
    """实时校验失败后不能继续运行完整长测；发送取消完成后才返回异常。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = runpy.run_path(str(scripts / "benchmark_service.py"))
    started, stopped = asyncio.Event(), asyncio.Event()

    async def emit(_seconds):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def failed_observer():
        await started.wait()
        raise ValueError("injected realtime mismatch")

    observer = asyncio.create_task(failed_observer())
    with pytest.raises(ValueError, match="realtime mismatch"):
        await asyncio.wait_for(module["emit_observed"](SimpleNamespace(emit=emit), 86400, 86410, [observer]), 1)
    assert stopped.is_set()


@pytest.mark.parametrize("task_created", [False, True])
async def test_benchmark_failure_still_waits_for_resource_deletion(tmp_path, monkeypatch, task_created):
    """停止异常不能跳过资源回收，202 也不能被当成已完成清理。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = runpy.run_path(str(scripts / "benchmark_service.py"))
    globals_ = module["execute"].__globals__
    calls, deletion_reads = [], 0

    class Source:
        """只注入控制流程失败；日志正确性由独立真实 Telnet 和归档测试证明。"""
        def __init__(self, *_args):
            self.connected, self.release = asyncio.Event(), asyncio.Event()
            self.connected.set()
            self.port = 10001

        async def start(self):
            pass

        async def close(self):
            calls.append("source-closed")

        async def emit(self, _seconds):
            raise RuntimeError("模拟发送失败")

    async def request(_client, method, path, **_kwargs):
        nonlocal deletion_reads
        calls.append((method, path))
        if path == "/api/v1/nodes":
            return {"items": [{"capacity": 100, "activeTasks": 0, "accepting": True}]}
        if path == "/api/v1/resources":
            return {"id": "resource"}
        if path == "/api/v1/tasks":
            if not task_created:
                raise RuntimeError("模拟创建失败")
            return {"id": "task"}
        if path.endswith("/stop"):
            raise RuntimeError("模拟停止失败")
        if method == "DELETE":
            return {"deletionState": "PENDING"}
        if path == "/api/v1/resources/resource":
            deletion_reads += 1
            return {"version": 1, "activeTaskCount": 0,
                    "deletionState": "DONE" if deletion_reads >= 3 else "PENDING"}
        raise AssertionError(f"未预期的请求 {method} {path}")

    monkeypatch.setitem(globals_, "LoadSource", Source)
    monkeypatch.setitem(globals_, "request", request)
    monkeypatch.setitem(globals_, "Settings", lambda **_kwargs: SimpleNamespace(bootstrap_token="synthetic"))
    options = SimpleNamespace(output=tmp_path / "result", env_file=tmp_path / "unused",
        download_concurrency=1, routes=1, seconds=1, url="http://synthetic.invalid",
        bind_host="127.0.0.1", device_host="127.0.0.1", lines_per_second=1200, line_bytes=256, timeout=1,
        realtime_clients_per_route=0, search_interval=0)
    with pytest.raises(RuntimeError, match="模拟"):
        await module["execute"](options)
    assert ("DELETE", "/api/v1/resources/resource?version=1") in calls
    assert deletion_reads == 3
    assert "source-closed" in calls
    cleanup = json.loads((options.output / "cleanup.json").read_text())
    assert bool(cleanup["errors"]) is task_created


async def test_benchmark_zero_realtime_clients_keeps_integrity_report_compatible(tmp_path, monkeypatch):
    """未启用实时观察器时，既有下载校验仍通过并报告零实时统计。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = runpy.run_path(str(scripts / "benchmark_service.py"))
    globals_ = module["execute"].__globals__

    class Source:
        def __init__(self, *_args):
            self.connected = asyncio.Event()
            self.connected.set()
            self.release = asyncio.Event()
            self.peer_closed = asyncio.Event()
            self.peer_closed.set()
            self.port = 10001
            self.failure = None
            self.source_lines = 2
            self.source_bytes = 6
            self.source_sha256 = "source-sha"
            self.elapsed_seconds = .96
            self.max_tick_lag_seconds = 0
            self.connection_count = 1

        async def start(self):
            pass

        async def emit(self, seconds):
            await asyncio.sleep(seconds * .96)

        async def close(self):
            pass

    async def request(_client, method, path, **_kwargs):
        if path == "/api/v1/nodes":
            return {"items": [{"capacity": 1, "activeTasks": 0, "accepting": True}]}
        if path == "/api/v1/resources" and method == "POST":
            return {"id": "resource"}
        if path == "/api/v1/tasks" and method == "POST":
            return {"id": "task"}
        if path == "/api/v1/tasks/task/log-hours":
            return {"items": [{"hourId": "hour", "bytes": 50, "status": "READY"}]}
        if path == "/api/v1/tasks/task/stop":
            return {"id": "stop-operation"}
        if path == "/api/v1/operations/stop-operation":
            return {"status": "SUCCEEDED"}
        if path == "/api/v1/resources/resource" and method == "GET":
            return {"version": 1, "deletionState": "DONE", "activeTaskCount": 0}
        if path == "/api/v1/resources/resource?version=1" and method == "DELETE":
            return {"deletionState": "PENDING"}
        raise AssertionError(f"未预期的请求 {method} {path}")

    async def download(*_args):
        return 50

    monkeypatch.setitem(globals_, "LoadSource", Source)
    monkeypatch.setitem(globals_, "request", request)
    monkeypatch.setitem(globals_, "download", download)
    monkeypatch.setitem(globals_, "verify_download", lambda *_args: {"verified": True})
    monkeypatch.setitem(globals_, "Settings", lambda **_kwargs: SimpleNamespace(bootstrap_token="synthetic"))
    options = SimpleNamespace(output=tmp_path / "result", env_file=tmp_path / "unused",
        download_concurrency=1, routes=1, seconds=1, url="http://synthetic.invalid",
        bind_host="127.0.0.1", device_host="127.0.0.1", lines_per_second=2, line_bytes=64, timeout=1,
        realtime_clients_per_route=0, search_interval=0)

    report = await module["execute"](options)

    assert report["integrityVerified"] and report["cleanupVerified"] and report["passed"]
    assert report["realtimeClientsPerRoute"] == 0
    assert report["realtimeVerified"] is True
    assert report["realtimeFrames"] == 0
    assert report["realtimeLogBytes"] == 0
    assert report["searchIntervalSeconds"] == 0
    assert report["searchCount"] == 0


def test_benchmark_realtime_client_option_defaults_to_zero_and_limits_to_four(tmp_path, monkeypatch):
    """实时观察器默认关闭，并限制每条路由的观察器数量以约束压测连接。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = runpy.run_path(str(scripts / "benchmark_service.py"))
    monkeypatch.setattr(sys, "argv", ["benchmark_service.py", "--output", str(tmp_path / "default")])
    assert module["parse_args"]().realtime_clients_per_route == 0

    monkeypatch.setattr(sys, "argv", ["benchmark_service.py", "--output", str(tmp_path / "maximum"),
        "--realtime-clients-per-route", "4"])
    assert module["parse_args"]().realtime_clients_per_route == 4

    monkeypatch.setattr(sys, "argv", ["benchmark_service.py", "--output", str(tmp_path / "invalid"),
        "--realtime-clients-per-route", "5"])
    with pytest.raises(SystemExit, match="2"):
        module["parse_args"]()


@pytest.mark.parametrize("search_interval", [0, 1])
async def test_benchmark_cancels_and_awaits_realtime_observer_after_route_failure(tmp_path, monkeypatch, search_interval):
    """路由发送失败后，最终清理必须取消并等待已建立的实时观察器。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    module = runpy.run_path(str(scripts / "benchmark_service.py"))
    globals_ = module["execute"].__globals__
    cancelled = asyncio.Event()
    search_cancelled, search_deleted = asyncio.Event(), asyncio.Event()

    class Source:
        def __init__(self, *_args):
            self.connected = asyncio.Event()
            self.connected.set()
            self.release = asyncio.Event()
            self.port = 10001

        async def start(self):
            pass

        async def emit(self, _seconds):
            raise RuntimeError("模拟发送失败")

        async def close(self):
            pass

    async def observe_realtime(*args):
        ready = args[6]
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def observe_searches(*args):
        args[-1].add("active-search")
        try:
            await asyncio.Future()
        finally:
            search_cancelled.set()

    async def delete_search(_client, path):
        assert path == "/api/v1/log-searches/active-search"
        assert search_cancelled.is_set()
        search_deleted.set()
        return SimpleNamespace(raise_for_status=lambda: None)

    async def request(_client, method, path, **_kwargs):
        if path == "/api/v1/log-searches/active-search":
            assert search_deleted.is_set()
            return {"status": "CANCELLED"}
        if path == "/api/v1/tasks/task" and method == "GET":
            return {"id": "task", "runId": "synthetic-run"}
        if path == "/api/v1/nodes":
            return {"items": [{"capacity": 1, "activeTasks": 0, "accepting": True}]}
        if path == "/api/v1/resources" and method == "POST":
            return {"id": "resource"}
        if path == "/api/v1/tasks" and method == "POST":
            return {"id": "task"}
        if path == "/api/v1/tasks/task/stop":
            return {"id": "stop-operation"}
        if path == "/api/v1/operations/stop-operation":
            return {"status": "SUCCEEDED"}
        if path == "/api/v1/resources/resource" and method == "GET":
            return {"version": 1, "deletionState": "DONE", "activeTaskCount": 0}
        if path == "/api/v1/resources/resource?version=1" and method == "DELETE":
            return {"deletionState": "PENDING"}
        raise AssertionError(f"未预期的请求 {method} {path}")

    monkeypatch.setitem(globals_, "LoadSource", Source)
    monkeypatch.setitem(globals_, "observe_realtime", observe_realtime)
    monkeypatch.setitem(globals_, "observe_searches", observe_searches)
    monkeypatch.setattr(globals_["httpx"].AsyncClient, "delete", delete_search)
    monkeypatch.setitem(globals_, "request", request)
    monkeypatch.setitem(globals_, "Settings", lambda **_kwargs: SimpleNamespace(bootstrap_token="synthetic"))
    options = SimpleNamespace(output=tmp_path / "result", env_file=tmp_path / "unused",
        download_concurrency=1, routes=1, seconds=1, url="http://synthetic.invalid",
        bind_host="127.0.0.1", device_host="127.0.0.1", lines_per_second=2, line_bytes=64, timeout=1,
        realtime_clients_per_route=1, search_interval=search_interval)

    with pytest.raises(RuntimeError, match="模拟发送失败"):
        await module["execute"](options)
    assert cancelled.is_set()
    assert search_deleted.is_set() is bool(search_interval)

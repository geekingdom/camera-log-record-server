"""服务压测故障注入：创建或停止失败时仍须请求资源软删除并等待完成。"""

import asyncio
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


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
        bind_host="127.0.0.1", device_host="127.0.0.1", lines_per_second=1200, line_bytes=256, timeout=1)
    with pytest.raises(RuntimeError, match="模拟"):
        await module["execute"](options)
    assert ("DELETE", "/api/v1/resources/resource?version=1") in calls
    assert deletion_reads == 3
    assert "source-closed" in calls
    cleanup = json.loads((options.output / "cleanup.json").read_text())
    assert bool(cleanup["errors"]) is task_created

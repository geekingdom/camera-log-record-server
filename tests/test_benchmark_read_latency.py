"""读延迟基准回归：正式内容摘要、跨文件续读与故障清理不能弱化。"""

import asyncio
import base64
import hashlib
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(monkeypatch):
    """以脚本目录为导入根加载基准模块，便于替换正式 API 依赖。"""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return runpy.run_path(str(scripts / "benchmark_read_latency.py"))


def source(sequence):
    """构造固定宽度的源正文，测试内容 SHA 时不依赖真实服务。"""
    return f"route=0000 seq={sequence:09d} payload\n".encode()


def stored(sequence):
    """构造服务端时间前缀后的内容 API 正文。"""
    return b"[2026-09-08 13:59:59] " + source(sequence)


def test_content_verifier_rejects_same_width_pollution(monkeypatch):
    """正文长度和时间前缀即使正确，任意同宽污染也必须由内容 SHA 发现。"""
    module = load(monkeypatch)
    clean, polluted = source(0), source(0).replace(b"payload", b"payloac")
    pending, bodies = module["complete_lines"](b"", b"[2026-09-08 13:59:59] " + polluted, len(stored(0)))
    digest = hashlib.sha256(); digest.update(bodies[0])
    with pytest.raises(AssertionError):
        module["verify_content"]({"sha256": digest.hexdigest(), "lines": 1}, hashlib.sha256(clean).hexdigest(), 1)
    assert pending == b""


def test_observe_continues_across_successor_file_and_preserves_half_line(monkeypatch):
    """首文件空读后仅续读一次后继文件，跨文件时间前缀和半行仍恢复为完整源序列。"""
    module = load(monkeypatch)
    first, second = stored(0), stored(1)

    class Response:
        def __init__(self, data, offset): self.data, self.offset = data, offset
        def raise_for_status(self): pass
        def json(self): return {"data": base64.b64encode(self.data).decode(), "nextOffset": self.offset}

    class Client:
        def __init__(self): self.calls = {"first": 0, "second": 0}
        async def get(self, path, params):
            file_id = path.split("/")[-2]
            self.calls[file_id] += 1
            chunks = {"first": [first[:11], b""], "second": [first[11:] + second, b""]}[file_id]
            data = chunks[min(self.calls[file_id] - 1, len(chunks) - 1)]
            return Response(data, params["offset"] + len(data))

    async def request(_client, _method, _path):
        return {"items": [
            {"hour": "2026-09-08T00:00:00+00:00", "files": [{"id": "first"}]},
            {"hour": "2026-09-08T01:00:00+00:00", "files": [{"id": "second"}]},
        ]}

    async def scenario():
        batches = [{"writeStartedMonotonic": 0, "before": 0, "after": 2}]
        emitter = asyncio.create_task(asyncio.sleep(.03))
        module["observe"].__globals__["request"] = request
        result = await module["observe"](Client(), "task", "first", batches, emitter, len(first), .5)
        assert result["sha256"] == hashlib.sha256(source(0) + source(1)).hexdigest()
        assert result["lines"] == 2 and result["fileIds"] == ["first", "second"]
    asyncio.run(scenario())


def test_discover_files_orders_natural_hours_and_preserves_hour_file_order(monkeypatch):
    """目录返回乱序自然小时和同小时多文件时，续读队列必须稳定排序且不重复旧文件。"""
    module = load(monkeypatch)

    async def request(_client, _method, _path):
        return {"items": [
            {"hour": "2026-09-09T10:00:00+00:00", "files": [{"id": "late-a"}, {"id": "late-b"}]},
            {"hour": "2026-09-09T09:00:00+00:00", "files": [{"id": "early"}]},
        ]}

    module["discover_files"].__globals__["request"] = request
    found = asyncio.run(module["discover_files"](None, "task", {"late-a"}))
    assert found == ["early", "late-b"]


def test_observe_rejects_bad_cursor_and_times_out_on_permanent_empty_read(monkeypatch):
    """内容 cursor 不连续和持续空读都必须失败，不能把漏读误记为低延迟。"""
    module = load(monkeypatch)

    class Response:
        def __init__(self, data, offset): self.data, self.offset = data, offset
        def raise_for_status(self): pass
        def json(self): return {"data": base64.b64encode(self.data).decode(), "nextOffset": self.offset}

    class BadCursor:
        async def get(self, _path, params): return Response(stored(0), 99)

    class Empty:
        async def get(self, _path, params): return Response(b"", params["offset"])

    async def request(_client, _method, _path):
        return {"items": [{"hour": "2026-09-09T00:00:00+00:00", "files": [{"id": "first"}]}]}

    async def scenario():
        module["observe"].__globals__["request"] = request
        emitter = asyncio.create_task(asyncio.sleep(0))
        with pytest.raises(AssertionError, match="nextOffset"):
            await module["observe"](BadCursor(), "task", "first", [], emitter, len(stored(0)), .1)
        with pytest.raises(TimeoutError):
            await module["observe"](Empty(), "task", "first", [{"writeStartedMonotonic": 0, "after": 1}],
                                      asyncio.create_task(asyncio.sleep(0)), len(stored(0)), .04)
    asyncio.run(scenario())


async def test_read_latency_failure_still_closes_source_and_deletes_resource(tmp_path, monkeypatch):
    """任务创建失败后依然回收本地源，并等待资源软删除完成。"""
    module = load(monkeypatch)
    globals_, calls = module["execute"].__globals__, []

    class Source:
        def __init__(self, *_args, **_kwargs): self.port = 10001
        async def start(self): pass
        async def close(self): calls.append("source-close")

    async def request(_client, method, path, **_kwargs):
        calls.append((method, path))
        if path == "/api/v1/nodes": return {"items": [{"capacity": 1, "activeTasks": 0, "accepting": True}]}
        if path == "/api/v1/resources" and method == "POST": return {"id": "resource"}
        if path == "/api/v1/tasks": raise RuntimeError("create failure")
        if path == "/api/v1/resources/resource" and method == "GET": return {"version": 1, "deletionState": "DONE"}
        if method == "DELETE": return {}
        raise AssertionError(path)

    monkeypatch.setitem(globals_, "LoadSource", Source)
    monkeypatch.setitem(globals_, "request", request)
    monkeypatch.setitem(globals_, "Settings", lambda **_kwargs: SimpleNamespace(bootstrap_token="synthetic"))
    options = SimpleNamespace(output=tmp_path / "result", env_file=tmp_path / "unused", url="http://synthetic.invalid",
        bind_host="127.0.0.1", device_host="127.0.0.1", lines_per_second=1, line_bytes=64, seconds=1, timeout=1)
    with pytest.raises(RuntimeError, match="create failure"):
        await module["execute"](options)
    assert "source-close" in calls and ("DELETE", "/api/v1/resources/resource?version=1") in calls
    assert (options.output / "cleanup.json").exists()

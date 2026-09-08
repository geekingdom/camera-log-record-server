"""服务压测搜索观察器的 HTTP 合同与作业清理边界测试。"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class Response:
    """最小 HTTP 响应替身，观察器只应读取已成功响应的 JSON 正文。"""

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class Client:
    """按方法和路径分派响应，并记录搜索作业生命周期请求。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return Response(await self.handler(method, path, **kwargs))


@pytest.fixture
def resource_api(monkeypatch):
    """导入待测脚本模块，保持测试不触发真实 HTTP 请求。"""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("service_benchmark_search")


def source(*, route=7, line_bytes=64, source_lines=5, finished=True):
    """构造含已登记行数水位和完成事件的合成源。"""
    event = asyncio.Event()
    if finished:
        event.set()
    return SimpleNamespace(route=route, line_bytes=line_bytes, source_lines=source_lines, finished=event)


async def test_observe_searches_verifies_registered_watermark_keyword_and_unique_result(resource_api):
    """搜索只能命中已登记水位中的本路唯一关键词，并在成功后释放作业登记。"""
    source_data = source(finished=False)
    keyword = "route=0007 seq=000000002 "

    async def handler(method, path, **kwargs):
        if (method, path) == ("GET", "/api/v1/tasks/task/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if (method, path) == ("POST", "/api/v1/log-searches"):
            assert kwargs["json"] == {"taskId": "task", "keyword": keyword}
            assert kwargs["headers"]["Idempotency-Key"]
            return {"id": "search"}
        if (method, path) == ("GET", "/api/v1/log-searches/search"):
            return {"id": "search", "status": "SUCCEEDED", "files": [{"id": "file-a"}]}
        if (method, path) == ("GET", "/api/v1/log-searches/search/results"):
            assert kwargs == {"params": {"pageSize": 100}}
            source_data.finished.set()
            return {"status": "SUCCEEDED", "truncated": False, "total": 1,
                    "items": [{"fileId": "file-a", "offset": 0, "text": keyword + "payload"}]}
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    result = await resource_api.observe_searches(Client(handler), "task", source_data, .001, 1, created_jobs)

    assert result["count"] == 1 and result["maxElapsedSeconds"] >= 0
    assert created_jobs == set()


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"status": "SUCCEEDED", "truncated": False, "total": 0, "items": []}, "唯一"),
        ({"status": "SUCCEEDED", "truncated": True, "total": 1,
          "items": [{"fileId": "file-a", "offset": 0, "text": "route=0007 seq=000000002 "}]}, "截断"),
        ({"status": "SUCCEEDED", "truncated": False, "total": 1,
          "items": [{"fileId": "other-file", "offset": 0, "text": "route=0007 seq=000000002 "}]}, "冻结日志"),
        ({"status": "SUCCEEDED", "truncated": False, "total": 1,
          "items": [{"fileId": "file-a", "offset": 0, "text": "route=0008 seq=000000002 "}]}, "目标关键字"),
    ],
)
async def test_observe_searches_rejects_non_unique_or_cross_route_results(resource_api, result, message):
    """零命中、截断、越界文件和错路正文都不能计作搜索验证成功。"""
    source_data = source(finished=False)

    async def handler(method, path, **_kwargs):
        if path.endswith("/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if method == "POST":
            return {"id": "search"}
        if path == "/api/v1/log-searches/search":
            return {"status": "SUCCEEDED", "files": [{"id": "file-a"}]}
        if path.endswith("/results"):
            source_data.finished.set()
            return result
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    with pytest.raises(AssertionError, match=message):
        await resource_api.observe_searches(Client(handler), "task", source_data, .001, 1, created_jobs)
    assert created_jobs == set()


async def test_observe_searches_discards_terminal_failed_job(resource_api):
    """服务已明确失败的搜索作业无需交给调用方进行二次取消。"""
    source_data = source(finished=False)

    async def handler(method, path, **_kwargs):
        if path.endswith("/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if method == "POST":
            return {"id": "failed-search"}
        if path == "/api/v1/log-searches/failed-search":
            return {"status": "FAILED", "files": []}
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    with pytest.raises(RuntimeError, match="FAILED"):
        await resource_api.observe_searches(Client(handler), "task", source_data, .001, 1, created_jobs)
    assert created_jobs == set()


async def test_observe_searches_keeps_active_job_after_timeout(resource_api):
    """轮询超时的搜索尚可能在服务端运行，ID 必须留给调用方统一取消。"""
    source_data = source(finished=False)

    async def handler(method, path, **_kwargs):
        if path.endswith("/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if method == "POST":
            return {"id": "active-search"}
        if path == "/api/v1/log-searches/active-search":
            return {"status": "RUNNING", "files": [{"id": "file-a"}]}
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    with pytest.raises(TimeoutError):
        await resource_api.observe_searches(Client(handler), "task", source_data, .001, .01, created_jobs)
    assert created_jobs == {"active-search"}


async def test_observe_searches_keeps_active_job_after_poll_request_failure(resource_api):
    """轮询请求本身失败时无法判断服务端状态，作业 ID 必须保留给调用方清理。"""
    source_data = source(finished=False)

    async def handler(method, path, **_kwargs):
        if path.endswith("/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if method == "POST":
            return {"id": "active-search"}
        if path == "/api/v1/log-searches/active-search":
            raise RuntimeError("poll transport failed")
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    with pytest.raises(RuntimeError, match="poll transport failed"):
        await resource_api.observe_searches(Client(handler), "task", source_data, .001, 1, created_jobs)
    assert created_jobs == {"active-search"}


async def test_observe_searches_keeps_active_job_when_cancelled(resource_api):
    """调用方取消观察器时不应丢失已经提交且尚未终态的作业 ID。"""
    source_data = source(finished=False)
    polling_started = asyncio.Event()

    async def handler(method, path, **_kwargs):
        if path.endswith("/log-hours"):
            return {"items": [{"bytes": (source_data.line_bytes + 22) * 4}]}
        if method == "POST":
            return {"id": "active-search"}
        if path == "/api/v1/log-searches/active-search":
            polling_started.set()
            await asyncio.Event().wait()
        raise AssertionError(f"未预期请求 {method} {path}")

    created_jobs = set()
    task = asyncio.create_task(resource_api.observe_searches(
        Client(handler), "task", source_data, .001, 10, created_jobs))
    await polling_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created_jobs == {"active-search"}


async def test_observe_searches_repeats_at_interval_until_source_finishes(resource_api):
    """源尚未结束且无可读水位时按 interval 重试，完成事件会停止后续轮询。"""
    source_data = source(finished=False)
    polls = 0

    async def handler(method, path, **_kwargs):
        nonlocal polls
        assert (method, path) == ("GET", "/api/v1/tasks/task/log-hours")
        polls += 1
        if polls == 2:
            source_data.finished.set()
        return {"items": [{"bytes": 0}]}

    result = await resource_api.observe_searches(Client(handler), "task", source_data, .001, 1, set())

    assert result == {"count": 0, "maxElapsedSeconds": 0}
    assert polls == 2

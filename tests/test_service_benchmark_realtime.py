"""实时压测 WebSocket 观察器的协议边界测试，不连接服务或设备。"""

import asyncio
import base64
import hashlib
import importlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class FakeSocket:
    """以预置 JSON 帧模拟 WebSocket，超时由测试明确注入。"""

    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        item = next(self.frames)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item


class FakeConnect:
    """记录连接参数并返回单个受控的异步 WebSocket 上下文。"""

    def __init__(self, frames):
        self.frames = frames
        self.calls = []
        self.socket = None

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.socket = FakeSocket(self.frames)

        @asynccontextmanager
        async def connection():
            yield self.socket

        return connection()


@pytest.fixture
def realtime_module(monkeypatch):
    """加载脚本模块，使每个用例只替换该模块的网络连接。"""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("service_benchmark_realtime")


def source_line(route, sequence, line_bytes):
    """构造与压测源一致的定长原始正文。"""
    head = f"route={route:04d} seq={sequence:09d} ".encode("ascii")
    return head + b"x" * (line_bytes - len(head) - 1) + b"\n"


def prefixed_line(route, sequence, line_bytes):
    """构造服务保存后的上海时间前缀日志行。"""
    return b"[2026-09-09 09:30:00] " + source_line(route, sequence, line_bytes)


def data_frame(data, *, file_id="file-a", session_id="session-a", offset=0):
    """按生产实时帧合同编码一个日志分片。"""
    return {
        "type": "data",
        "fileId": file_id,
        "sessionId": session_id,
        "offset": offset,
        "endOffset": offset + len(data),
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


async def observe(module, monkeypatch, frames, *, route=7, line_bytes=64, expected_lines=1):
    """用 fake WebSocket 调用观察器，并返回调用后的就绪事件和连接记录。"""
    connector = FakeConnect(frames)
    monkeypatch.setattr(module.websockets, "connect", connector)
    ready = asyncio.Event()
    result = await module.observe_realtime(
        "https://service.example/base", "secret-token", "task / 1", route,
        line_bytes, expected_lines, ready, 1,
    )
    return result, ready, connector


async def test_observer_reassembles_split_lines_across_packets_and_files(realtime_module, monkeypatch):
    """分片和文件轮转都不能改变行数、摘要或原始字节统计。"""
    route, line_bytes = 7, 64
    source = b"".join(source_line(route, sequence, line_bytes) for sequence in range(2))
    stored = b"".join(prefixed_line(route, sequence, line_bytes) for sequence in range(2))
    split = 37
    result, ready, connector = await observe(
        realtime_module, monkeypatch,
        [data_frame(stored[:split]), data_frame(stored[split:], file_id="file-b")],
        route=route, line_bytes=line_bytes, expected_lines=2,
    )

    assert ready.is_set()
    assert result == {
        "frames": 2,
        "logBytes": len(stored),
        "sourceLines": 2,
        "sourceSha256": hashlib.sha256(source).hexdigest(),
    }
    assert connector.calls == [(
        "wss://service.example/base/api/v1/tasks/task%20%2F%201/logs",
        {"max_size": 2 * 1024 * 1024, "open_timeout": 1},
    )]
    assert [json.loads(item) for item in connector.socket.sent] == [{"token": "secret-token"}]


async def test_observer_requests_explicit_run_start_cursor(realtime_module, monkeypatch):
    """完整性压测必须请求运行首帧，不能依赖默认最近四块的尾部窗口。"""
    connector = FakeConnect([data_frame(prefixed_line(7, 0, 64))])
    monkeypatch.setattr(realtime_module.websockets, "connect", connector)
    await realtime_module.observe_realtime("ws://service.example", "token", "task", 7, 64, 1,
                                          asyncio.Event(), 1, "run-id:0")
    assert json.loads(connector.socket.sent[0]) == {"token": "token", "cursor": "run-id:0"}


@pytest.mark.parametrize(
    ("frames", "message"),
    [
        ([data_frame(b"first"), data_frame(b"second", offset=0)], "偏移不连续"),
        ([data_frame(b"first"), data_frame(b"second", offset=8)], "偏移不连续"),
        ([data_frame(b"first", offset=1)], "首个实时文件偏移"),
        ([data_frame(b"first"), data_frame(b"second", session_id="session-b", offset=5)], "会话"),
        ([{"type": "gap"}], "报告 gap"),
    ],
)
async def test_observer_rejects_order_session_and_gap_protocol_breaks(
    realtime_module, monkeypatch, frames, message,
):
    """重叠、缺失、首偏移、会话切换和服务 gap 都必须立即失败。"""
    connector = FakeConnect(frames)
    monkeypatch.setattr(realtime_module.websockets, "connect", connector)
    ready = asyncio.Event()

    with pytest.raises(AssertionError, match=message):
        await realtime_module.observe_realtime("ws://service.example", "token", "task", 7, 64, 1, ready, 1)
    assert ready.is_set()


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (data_frame(prefixed_line(8, 0, 64)), "路由或序号不连续"),
        (data_frame(prefixed_line(7, 1, 64)), "路由或序号不连续"),
        ({**data_frame(b"unused"), "data": "not base64!"}, "严格 Base64"),
    ],
)
async def test_observer_rejects_invalid_line_identity_and_encoding(realtime_module, monkeypatch, frame, message):
    """行路由、连续序号和严格 Base64 是不可放宽的传输合同。"""
    connector = FakeConnect([frame])
    monkeypatch.setattr(realtime_module.websockets, "connect", connector)

    with pytest.raises(AssertionError, match=message):
        await realtime_module.observe_realtime("ws://service.example", "token", "task", 7, 64, 1, asyncio.Event(), 1)


async def test_observer_rejects_extra_complete_line_in_final_data_frame(realtime_module, monkeypatch):
    """达到预期行数的同帧额外完整行不能被静默吞掉。"""
    payload = prefixed_line(7, 0, 64) + prefixed_line(7, 1, 64)
    connector = FakeConnect([data_frame(payload)])
    monkeypatch.setattr(realtime_module.websockets, "connect", connector)

    with pytest.raises(AssertionError, match="超过预期"):
        await realtime_module.observe_realtime("ws://service.example", "token", "task", 7, 64, 1, asyncio.Event(), 1)


async def test_observer_preserves_timeout_for_truncated_final_line(realtime_module, monkeypatch):
    """半行未结束时，观察器继续等数据并把接收超时传播给调用方。"""
    payload = prefixed_line(7, 0, 64)[:-1]
    connector = FakeConnect([data_frame(payload), TimeoutError()])
    monkeypatch.setattr(realtime_module.websockets, "connect", connector)

    with pytest.raises(asyncio.TimeoutError):
        await realtime_module.observe_realtime("ws://service.example", "token", "task", 7, 64, 1, asyncio.Event(), 1)

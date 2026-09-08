"""服务压测的实时 WebSocket 观测器，流式校验日志正文而不缓存全量数据。"""

import asyncio
import base64
import binascii
import hashlib
import json
import time
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import websockets
from service_benchmark_io import BODY, PREFIX


def _socket_url(url, task_id):
    """将 HTTP 基地址转换为 WebSocket 地址，并保留部署在子路径的 basepath。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("实时观测地址必须是完整的 HTTP(S) 或 WS(S) URL")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    basepath = parsed.path.rstrip("/")
    path = f"{basepath}/api/v1/tasks/{quote(task_id, safe='')}/logs"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _complete_line(raw, *, route, line_bytes, sequence, digest):
    """验证一条含上海时间前缀的完整日志，并累积原始采集正文摘要。"""
    prefix = PREFIX.match(raw)
    if prefix is None:
        raise AssertionError("实时日志缺少服务器时间前缀")
    try:
        datetime.strptime(prefix.group(1).decode("ascii"), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    except ValueError as error:
        raise AssertionError("实时日志时间前缀格式错误") from error
    body = raw[prefix.end():] + b"\n"
    match = BODY.match(body)
    if match is None:
        raise AssertionError("实时日志正文缺少路由或序号")
    if int(match.group(1)) != route or int(match.group(2)) != sequence:
        raise AssertionError("实时日志路由或序号不连续")
    if len(body) != line_bytes:
        raise AssertionError("实时日志正文长度与压测源不一致")
    digest.update(body)


async def observe_realtime(url, token, task_id, route, line_bytes, expected_lines, ready_event, timeout):
    """订阅正式实时接口并校验单会话、顺序文件和逐行正文摘要。

    连接并发出首帧认证请求后设置 ``ready_event``；接口没有认证成功确认帧，调用方应同时监督本协程，连接或
    鉴权失败时本协程会立即抛出，不能只等待事件而永久挂起。达到预期行数即返回，
    同一数据帧中的额外完整行仍会作为错误报告。
    """
    if expected_lines < 0 or line_bytes <= 0 or timeout <= 0:
        raise ValueError("实时观测的行数、行长度和超时必须为正值或零行")
    deadline = time.monotonic() + timeout
    socket_url = _socket_url(url, task_id)
    digest = hashlib.sha256()
    pending = b""
    session_id = None
    current_file = None
    completed_files = set()
    next_offset = 0
    frames = 0
    log_bytes = 0
    lines = 0

    async with websockets.connect(socket_url, max_size=2 * 1024 * 1024, open_timeout=min(timeout, 15)) as socket:
        await socket.send(json.dumps({"token": token}))
        ready_event.set()
        while lines < expected_lines:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("实时日志未在规定时间内收齐")
            payload = await asyncio.wait_for(socket.recv(), timeout=remaining)
            try:
                event = json.loads(payload)
            except (TypeError, ValueError) as error:
                raise AssertionError("实时 WebSocket 帧不是 JSON") from error
            if not isinstance(event, dict):
                raise TypeError("实时 WebSocket 帧必须是对象")
            event_type = event.get("type")
            if event_type == "status":
                continue
            if event_type in {"gap", "error"}:
                raise AssertionError(f"实时 WebSocket 报告 {event_type}")
            if event_type != "data":
                raise AssertionError("实时 WebSocket 返回未知帧类型")

            file_id = event.get("fileId")
            frame_session = event.get("sessionId")
            offset = event.get("offset")
            end_offset = event.get("endOffset")
            size = event.get("size")
            encoded = event.get("data")
            if (
                not isinstance(file_id, str)
                or not isinstance(frame_session, str)
                or type(offset) is not int
                or type(end_offset) is not int
                or type(size) is not int
                or not isinstance(encoded, str)
            ):
                raise AssertionError("实时数据帧字段不完整")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise AssertionError("实时数据帧不是严格 Base64") from error
            if size != len(data) or end_offset != offset + len(data):
                raise AssertionError("实时数据帧 size 或 endOffset 不一致")
            if session_id is None:
                session_id = frame_session
            elif frame_session != session_id:
                raise AssertionError("实时日志会话在订阅期间发生变化")
            if current_file is None:
                if offset != 0:
                    raise AssertionError("首个实时文件偏移必须从零开始")
                current_file = file_id
                next_offset = 0
            elif file_id != current_file:
                if file_id in completed_files or offset != 0:
                    raise AssertionError("实时文件切换后不能回读旧文件且必须从零开始")
                completed_files.add(current_file)
                current_file = file_id
                next_offset = 0
            if offset != next_offset:
                raise AssertionError("实时日志文件偏移不连续")
            next_offset = end_offset
            frames += 1
            log_bytes += len(data)

            complete = (pending + data).split(b"\n")
            pending = complete.pop()
            if len(pending) > line_bytes + 22:
                raise AssertionError("实时日志跨帧半行超过单行上限")
            for raw in complete:
                if lines >= expected_lines:
                    raise AssertionError("实时日志包含超过预期的额外行")
                _complete_line(raw, route=route, line_bytes=line_bytes, sequence=lines, digest=digest)
                lines += 1

    if pending:
        raise AssertionError("实时日志在预期行数后留下截断半行")
    if lines != expected_lines:
        raise AssertionError("实时日志行数不足")
    return {"frames": frames, "logBytes": log_bytes, "sourceLines": lines, "sourceSha256": digest.hexdigest()}

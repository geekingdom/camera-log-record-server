#!/usr/bin/env python3
"""通过正式 API 验证容器或本机服务的 Telnet 串口采集完整链路。

脚本启动一个本地合成串口服务，不触碰真实设备。它依次验证任务创建和初始化
命令、经 Web 前端代理的实时 WebSocket、手动命令、停止释放、归档下载及 Range。
令牌只从 .env 读取，绝不打印；即使中途失败也会停止本脚本创建的任务。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import secrets
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import telnetlib3
import websockets
from camera_logs.common.config import Settings

logger = logging.getLogger(__name__)
INITIAL = ["verify-init-first", "verify-init-second"]
MANUAL = "verify-manual-status"
LINES = [f"verify-serial seq={number:03d}\n".encode() for number in range(16)]


class ModuleAssetParser(HTMLParser):
    """从首页提取 Vite 入口模块，避免把 Nginx 返回的空白回退页当作构建成功。"""

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("type") == "module" and values.get("src"):
            self.sources.append(str(values["src"]))


@dataclass
class SerialSimulator:
    """提供最小 Telnet 串口服务，按命令与日志分别记录验收证据。"""

    bind_host: str
    server: Any = None
    port: int = 0
    commands: list[str] = field(default_factory=list)
    sent: list[bytes] = field(default_factory=list)
    initial_received: asyncio.Event = field(default_factory=asyncio.Event)
    release_logs: asyncio.Event = field(default_factory=asyncio.Event)
    logs_sent: asyncio.Event = field(default_factory=asyncio.Event)
    peer_closed: asyncio.Event = field(default_factory=asyncio.Event)
    handlers: set[asyncio.Task] = field(default_factory=set)
    failure: BaseException | None = None

    async def start(self) -> None:
        """绑定随机端口，容器可通过 --device-host 指向宿主机网关访问它。"""
        self.server = await telnetlib3.create_server(
            host=self.bind_host, port=0, shell=self._connected, encoding=False, connect_maxwait=.05, timeout=False
        )
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        """取消连接处理协程并释放本脚本监听端口。"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)

    async def _connected(self, reader: Any, writer: Any) -> None:
        task = asyncio.current_task()
        if task:
            self.handlers.add(task)
        emitter = asyncio.create_task(self._emit(writer))
        buffer = b""
        try:
            while data := await reader.read(4096):
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    # 串口任务不需要 Telnet 协商；这里只记录应用层换行命令。
                    command = raw.rstrip(b"\r").decode("utf-8", "replace")
                    if command:
                        self.commands.append(command)
                        if self.commands[:len(INITIAL)] == INITIAL:
                            self.initial_received.set()
        except BaseException as error:
            if not isinstance(error, asyncio.CancelledError):
                self.failure = error
            raise
        finally:
            emitter.cancel()
            result = (await asyncio.gather(emitter, return_exceptions=True))[0]
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                self.failure = result
            writer.close()
            await writer.wait_closed()
            self.peer_closed.set()
            if task:
                self.handlers.discard(task)

    async def _emit(self, writer: Any) -> None:
        """等待 WebSocket 订阅后再稳定输出有限序列，便于端到端顺序校验。"""
        await self.release_logs.wait()
        for line in LINES:
            writer.write(line)
            await writer.drain()
            self.sent.append(line)
            # 让 API 的 100ms 实时轮询能逐步推进 cursor，不依赖初始尾随窗口。
            await asyncio.sleep(0.12)
        self.logs_sent.set()


async def open_log_socket(base_url: str, task_id: str, token: str) -> Any:
    """通过服务 URL 建立日志订阅，验证前端反向代理的 Upgrade 端到端路径。"""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--url 必须是 http:// 或 https:// 地址")
    prefix = parsed.path.rstrip("/")
    path = f"{prefix}/api/v1/tasks/{task_id}/logs" if prefix else f"/api/v1/tasks/{task_id}/logs"
    scheme = "wss" if parsed.scheme == "https" else "ws"
    authority = parsed.netloc
    socket = await websockets.connect(f"{scheme}://{authority}{path}", max_size=None, open_timeout=15)
    await socket.send(json.dumps({"token": token, "cursor": None}))
    return socket


def token_from(path: Path) -> str:
    """通过统一配置读取 bootstrap token，失败消息不包含令牌本身。"""
    token = Settings(_env_file=path).bootstrap_token
    if not token:
        raise RuntimeError(f"{path} 未配置 BOOTSTRAP_TOKEN")
    return token


async def wait_for(client: httpx.AsyncClient, path: str, predicate, label: str, timeout: float = 90) -> dict[str, Any]:
    """轮询正式资源直到满足状态谓词，超时时保留最后的脱敏状态用于排障。"""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        response = await client.get(path)
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(0.5)
    raise TimeoutError(f"等待{label}超时，最后状态: {last}")


def strip_prefixed_lines(raw: bytes) -> list[bytes]:
    """严格校验每条服务器时间前缀，再返回未改变的设备正文行。"""
    recovered = []
    for line in raw.splitlines(keepends=True):
        if len(line) < 22 or line[:1] != b"[" or line[20:22] != b"] ":
            raise AssertionError("日志缺少服务器时间前缀")
        try:
            datetime.strptime(line[1:20].decode("ascii"), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
        except ValueError as error:
            raise AssertionError("日志时间前缀格式错误") from error
        recovered.append(line[22:])
    return recovered


def archive_members(content: bytes) -> list[bytes]:
    """下载可能是单一 tar.gz 或 ZIP STORE，统一取出按条目顺序排列的归档。"""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return [content]
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        return [bundle.read(name) for name in sorted(name for name in bundle.namelist() if name.endswith(".tar.gz"))]


def verify_archive(content: bytes, expected: list[bytes]) -> str:
    """逐归档核对 manifest 摘要和大小，再拼接验证设备序列没有乱序或缺失。"""
    parts: list[tuple[tuple[str, int], bytes]] = []
    for packed in archive_members(content):
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
            log = next(item for item in archive if item.name.endswith(".log"))
            manifest = archive.extractfile("manifest.json")
            stream = archive.extractfile(log)
            if manifest is None or stream is None:
                raise AssertionError("归档缺少正文或完整性清单")
            metadata, raw = json.load(manifest), stream.read()
        digest = hashlib.sha256(raw).hexdigest()
        if metadata.get("rawSize") != len(raw) or metadata.get("sha256") != digest:
            raise AssertionError("归档 manifest 的正文大小或摘要不匹配")
        parts.append(((str(metadata.get("hourStart", "")), int(metadata.get("firstSequence") or -1)), raw))
    all_raw = bytearray()
    for _order, raw in sorted(parts, key=lambda item: item[0]):
        all_raw.extend(raw)
    recovered = strip_prefixed_lines(bytes(all_raw))
    if recovered != expected:
        raise AssertionError(f"归档正文顺序或完整性错误，实际 {len(recovered)} 行，期望 {len(expected)} 行")
    return hashlib.sha256(all_raw).hexdigest()


def realtime_lines(chunks: dict[tuple[str, str], dict[int, bytes]]) -> list[bytes]:
    """按首次文件顺序和各自偏移重组实时帧，允许正常轮转产生多个文件。"""
    combined = bytearray()
    for offsets in chunks.values():
        position = 0
        for offset, data in sorted(offsets.items()):
            if offset != position:
                raise AssertionError(f"实时日志偏移不连续，期望 {position}，实际 {offset}")
            combined.extend(data)
            position += len(data)
    # 实时传输可能停在半行；只有换行结束后才能与完整源端记录比较。
    complete_end = combined.rfind(b"\n") + 1
    return strip_prefixed_lines(bytes(combined[:complete_end]))


async def stop_task(client: httpx.AsyncClient, task_id: str, timeout: float = 90) -> None:
    """经正式控制 API 停止任务并确认异步释放成功，避免清理留下活动连接。"""
    response = await client.post(f"/api/v1/tasks/{task_id}/stop")
    response.raise_for_status()
    await wait_for(client, f"/api/v1/operations/{response.json()['id']}", lambda item: item["status"] == "SUCCEEDED", "停止操作", timeout)


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    """执行完整验收并在 finally 回收模拟端和通过 API 创建的任务。"""
    token, task_id, socket = token_from(args.env_file), None, None
    simulator = SerialSimulator(args.bind_host)
    await simulator.start()
    headers = {"Authorization": "Bearer " + token}
    suffix = secrets.token_hex(6)
    try:
        async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers=headers, timeout=30) as client:
            if args.check_frontend:
                response = await client.get("/")
                response.raise_for_status()
                if "<html" not in response.text.lower():
                    raise AssertionError("前端首页未返回 HTML")
                parser = ModuleAssetParser()
                parser.feed(response.text)
                if not parser.sources:
                    raise AssertionError("前端首页没有 Vite 模块入口")
                asset = await client.get(parser.sources[0])
                asset.raise_for_status()
                if not asset.content:
                    raise AssertionError("前端模块入口为空")
            await wait_for(client, "/api/v1/nodes", lambda item: bool(item.get("items")), "采集节点注册")
            body = {"name": f"容器验收串口-{suffix}", "description": "脚本自动清理的合成任务",
                    "protocol": "TELNET_SERIAL", "ip": args.device_host, "port": simulator.port,
                    "initialCommands": [{"command": command} for command in INITIAL], "scheduledCommands": [], "autoStart": True}
            response = await client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": f"container-verify-{suffix}"})
            response.raise_for_status()
            task_id = response.json()["id"]
            await wait_for(client, f"/api/v1/tasks/{task_id}", lambda task: task["status"] == "COLLECTING", "采集开始")
            await asyncio.wait_for(simulator.initial_received.wait(), timeout=30)
            if simulator.commands[:len(INITIAL)] != INITIAL:
                raise AssertionError("初始化命令没有按配置顺序到达串口服务")
            socket = await open_log_socket(args.url, task_id, token)
            simulator.release_logs.set()
            await asyncio.wait_for(simulator.logs_sent.wait(), timeout=30)
            if simulator.failure:
                raise simulator.failure
            realtime: dict[tuple[str, str], dict[int, bytes]] = {}
            deadline = time.monotonic() + 30
            recovered: list[bytes] = []
            while time.monotonic() < deadline:
                if realtime:
                    recovered = realtime_lines(realtime)
                    if recovered == LINES:
                        break
                    if recovered != LINES[:len(recovered)]:
                        raise AssertionError("实时 WebSocket 正文顺序错误")
                payload = await asyncio.wait_for(socket.recv(), timeout=max(.1, deadline - time.monotonic()))
                event = json.loads(payload)
                if event and event.get("type") == "data":
                    key = event.get("fileId"), event.get("sessionId")
                    offset, data = event.get("offset"), base64.b64decode(event["data"])
                    if not all(isinstance(value, str) for value in key) or type(offset) is not int:
                        raise AssertionError("实时 WebSocket 数据帧缺少文件、会话或偏移")
                    existing = realtime.setdefault(key, {}).get(offset)
                    if existing is not None and existing != data:
                        raise AssertionError("实时 WebSocket 出现同偏移不同正文")
                    realtime[key][offset] = data
            if recovered != LINES:
                raise AssertionError("实时 WebSocket 未收到完整有序合成日志")
            response = await client.post(f"/api/v1/tasks/{task_id}/commands", json={"command": MANUAL},
                                         headers={"Idempotency-Key": f"container-manual-{suffix}"})
            response.raise_for_status()
            command_id = response.json()["id"]
            await wait_for(client, f"/api/v1/commands/{command_id}", lambda item: item["status"] == "SENT", "手动命令发送")
            deadline = time.monotonic() + 15
            while MANUAL not in simulator.commands and time.monotonic() < deadline:
                await asyncio.sleep(.1)
            if MANUAL not in simulator.commands:
                raise AssertionError("手动命令没有到达串口服务")
            await stop_task(client, task_id)
            await asyncio.wait_for(simulator.peer_closed.wait(), timeout=30)
            if simulator.failure:
                raise simulator.failure
            hours = await wait_for(client, f"/api/v1/tasks/{task_id}/log-hours",
                lambda item: bool(item["items"]) and all(hour["status"] == "READY" for hour in item["items"]), "小时归档")
            hour_ids = [item["hourId"] for item in hours["items"] if item["status"] == "READY"]
            if not hour_ids:
                raise AssertionError("没有可下载的完整小时归档")
            response = await client.post("/api/v1/downloads", json={"taskId": task_id, "hourIds": hour_ids, "allowPartial": False},
                                         headers={"Idempotency-Key": f"container-download-{suffix}"})
            response.raise_for_status()
            download_id = response.json()["id"]
            await wait_for(client, f"/api/v1/downloads/{download_id}", lambda item: item["status"] == "SUCCEEDED", "下载作业")
            download = await client.get(f"/api/v1/downloads/{download_id}/content")
            download.raise_for_status()
            partial = await client.get(f"/api/v1/downloads/{download_id}/content", headers={"Range": "bytes=0-99"})
            if partial.status_code != 206 or partial.content != download.content[:100]:
                raise AssertionError("下载 Range 响应不符合 206 或内容不连续")
            digest = verify_archive(download.content, simulator.sent)
            return {"passed": True, "taskId": task_id, "taskStopped": True, "initialCommands": len(INITIAL),
                    "manualCommand": "SENT", "realtimeLines": len(LINES), "archiveSha256": digest,
                    "downloadBytes": len(download.content), "range206": True}
    finally:
        if socket:
            await socket.close()
        if task_id:
            try:
                async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers=headers, timeout=15) as client:
                    await stop_task(client, task_id, timeout=30)
            except Exception:
                # 清理失败应保留原始异常；任务 ID 会在结果或异常上下文中供人工处理。
                logger.exception("验收任务清理失败 task=%s", task_id)
        await simulator.close()


def parse_args() -> argparse.Namespace:
    """解析服务、模拟端绑定和容器访问宿主机的地址。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:15173")
    parser.add_argument("--device-host", required=True, help="服务可访问的模拟串口地址；容器场景传 Docker 网关 IPv4")
    parser.add_argument("--bind-host", default="0.0.0.0", help="模拟串口监听地址，本机试跑可保留默认值")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--check-frontend", action="store_true", help="额外验证 --url 首页由前端返回 HTML")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        print(json.dumps(asyncio.run(execute(arguments)), ensure_ascii=False, indent=2))
    except Exception as error:  # noqa: BLE001 - CLI 仅序列化失败类型和非敏感信息。
        print(json.dumps({"passed": False, "error": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(2)

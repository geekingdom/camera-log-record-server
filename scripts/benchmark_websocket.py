#!/usr/bin/env python3
"""订阅真实任务日志 WebSocket，并校验实时帧的文件与字节偏移连续性。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from camera_logs.common.config import Settings

try:
    import websockets
except ImportError as error:
    raise SystemExit("Install the benchmark dependency: python -m pip install websockets") from error


logger = logging.getLogger(__name__)


@dataclass
class ClientMetrics:
    """保存一个订阅客户端收到的实时帧、字节量和协议校验结果。"""

    frames_received: int = 0
    transport_bytes: int = 0
    log_bytes: int = 0
    offset_errors: int = 0
    protocol_errors: int = 0
    gaps: int = 0
    client_errors: int = 0
    next_offsets: dict[tuple[str, str], int] = field(default_factory=dict)


def load_bootstrap_token(path: Path) -> str:
    """从本地 .env 读取 BOOTSTRAP_TOKEN，不输出令牌内容或将其写入结果。"""

    if not path.is_file():
        raise SystemExit(f"无法读取令牌文件: {path}")
    token = Settings(_env_file=path).bootstrap_token
    if token:
        return token
    raise SystemExit(f"{path} 未配置 BOOTSTRAP_TOKEN")


def validate_data_frame(event: object, metrics: ClientMetrics) -> None:
    """按 fileId 与 sessionId 分组校验实时数据帧的偏移和 Base64 正文长度。"""

    if not isinstance(event, dict):
        metrics.protocol_errors += 1
        return
    file_id, session_id = event.get("fileId"), event.get("sessionId")
    offset, end_offset, encoded = event.get("offset"), event.get("endOffset"), event.get("data")
    if (
        not isinstance(file_id, str)
        or not isinstance(session_id, str)
        or type(offset) is not int
        or type(end_offset) is not int
        or not isinstance(encoded, str)
    ):
        metrics.protocol_errors += 1
        return
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        metrics.protocol_errors += 1
        return
    if event.get("size") is not None and event["size"] != len(data):
        metrics.protocol_errors += 1
    if end_offset != offset + len(data):
        metrics.protocol_errors += 1
        return
    key = (file_id, session_id)
    expected = metrics.next_offsets.get(key)
    if expected is not None and offset != expected:
        metrics.offset_errors += 1
    metrics.next_offsets[key] = end_offset
    metrics.frames_received += 1
    metrics.log_bytes += len(data)


async def consume(url: str, token: str, expected_frames: int, deadline: float, metrics: ClientMetrics) -> None:
    """订阅一个真实任务日志端点，直到收到目标数据帧数量或到达时限。"""

    try:
        async with websockets.connect(url, max_size=None, open_timeout=10) as socket:
            await socket.send(json.dumps({"token": token}))
            while metrics.frames_received < expected_frames and time.monotonic() < deadline:
                payload = await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic()))
                raw = payload.encode() if isinstance(payload, str) else payload
                metrics.transport_bytes += len(raw)
                try:
                    event = json.loads(payload)
                except (TypeError, ValueError):
                    metrics.protocol_errors += 1
                    continue
                if not isinstance(event, dict):
                    metrics.protocol_errors += 1
                    continue
                if event.get("type") == "data":
                    validate_data_frame(event, metrics)
                elif event.get("type") == "gap":
                    metrics.gaps += 1
                elif event.get("type") != "status":
                    metrics.protocol_errors += 1
    # 单客户端失败仍应让其他并发订阅完成，并在聚合结果中体现失败。
    except Exception:
        logger.exception("实时日志 WebSocket 客户端失败")
        metrics.client_errors += 1


async def main_async(args: argparse.Namespace) -> int:
    """并发运行真实订阅并输出实时传输检查结果，不度量设备端生成吞吐。"""

    token = load_bootstrap_token(args.env_file)
    url = args.url or f"{args.base_url.rstrip('/')}/api/v1/tasks/{quote(args.task_id, safe='')}/logs"
    started = time.monotonic()
    metrics = [ClientMetrics() for _ in range(args.clients)]
    await asyncio.gather(*(consume(url, token, args.frames, started + args.timeout, item) for item in metrics))
    elapsed = time.monotonic() - started
    received = sum(item.frames_received for item in metrics)
    transport_bytes = sum(item.transport_bytes for item in metrics)
    log_bytes = sum(item.log_bytes for item in metrics)
    errors = sum(item.offset_errors + item.protocol_errors + item.gaps + item.client_errors for item in metrics)
    expected = args.clients * args.frames
    result = {
        "url": url,
        "clients": args.clients,
        "frames_per_client": args.frames,
        "expected_data_frames": expected,
        "received_data_frames": received,
        "delivery_ratio": received / expected if expected else 0,
        "websocket_transport_bytes": transport_bytes,
        "log_payload_bytes": log_bytes,
        "elapsed_seconds": round(elapsed, 3),
        "realtime_frames_per_second": round(received / elapsed, 2) if elapsed else 0,
        "offset_errors": sum(item.offset_errors for item in metrics),
        "protocol_errors": sum(item.protocol_errors for item in metrics),
        "gap_frames": sum(item.gaps for item in metrics),
        "client_errors": sum(item.client_errors for item in metrics),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if received == expected and errors == 0 else 2


def parse_args() -> argparse.Namespace:
    """解析任务、并发订阅数、目标帧数、超时和本地令牌文件路径。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--base-url", default="ws://127.0.0.1:8000")
    parser.add_argument("--url", help="完整 WebSocket URL；提供时覆盖 --base-url 与 --task-id 的 URL 拼接")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--frames", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=300)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))

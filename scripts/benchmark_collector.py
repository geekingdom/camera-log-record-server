"""本地验证 Collector 与 HourlyWriter 的有序传输和归档摘要。

默认生成 500 路、每路每秒 1200 行的模拟输入。报告同时列出实际耗时，不能把
配置时长冒充达到目标吞吐的证据。归档逐行检查时间前缀，再移除前缀比对源摘要。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import resource
import sys
import tarfile
import time
from pathlib import Path

from camera_logs.collection.collector import Collector


class Source:
    """用有界异步队列模拟单条设备连接的输入流。"""
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)

    async def read(self, _size: int = 65536) -> bytes:
        return (await self.queue.get()) or b""

    async def write(self, _data: bytes) -> None:
        return None

    async def close(self) -> None:
        await self.queue.put(None)


def line(route: int, sequence: int, width: int) -> bytes:
    """生成含路由和单调序号的定长测试日志行。"""
    prefix = f"route={route:04d} seq={sequence:09d} ".encode()
    return prefix + b"x" * max(0, width - len(prefix) - 1) + b"\n"


def verify_route_archives(archives: list) -> tuple[str, int, int]:
    """按块序号流式校验整路归档，跨文件保留半行及半个时间前缀。

    每个归档独立验证原始正文摘要；只有完整逻辑行才剥离一次时间前缀。
    合成源总是以换行结束，因此最终残留半行必须判为截断，不能忽略。
    """
    digest = hashlib.sha256()
    stored_bytes = compressed_bytes = 0
    pending = b""
    prefix = re.compile(rb"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
    for archive in sorted(archives, key=lambda item: item.first_sequence or 0):
        archived_digest = hashlib.sha256()
        compressed_bytes += archive.path.stat().st_size
        with tarfile.open(archive.path, "r:gz") as bundle:
            members = [item for item in bundle.getmembers() if item.name.endswith(".log")]
            assert len(members) == 1, "归档必须包含唯一正文文件"
            stream = bundle.extractfile(members[0])
            assert stream is not None, "归档正文不是普通文件"
            while chunk := stream.read(256 * 1024):
                archived_digest.update(chunk)
                stored_bytes += len(chunk)
                complete = (pending + chunk).split(b"\n")
                pending = complete.pop()
                for stored_line in complete:
                    match = prefix.match(stored_line)
                    assert match, "归档行缺少服务器时间前缀"
                    digest.update(stored_line[match.end():] + b"\n")
        assert archived_digest.hexdigest() == archive.sha256, "归档清单摘要与正文不一致"
    assert not pending, "归档末尾存在截断的日志行"
    return digest.hexdigest(), stored_bytes, compressed_bytes


async def execute(args: argparse.Namespace) -> dict[str, object]:
    """生成多路输入、读取归档并比对每路 SHA-256 摘要。"""
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("benchmark output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    sources = [Source() for _ in range(args.routes)]
    archives: list[list] = [[] for _ in sources]
    collectors = [Collector(
        {"id": f"route-{route}", "runId": f"run-{route}", "initialCommands": []}, output,
        connection_factory=lambda _task, source=source: source,
        on_archive=lambda archive, route=route: archives[route].append(archive),
    ) for route, source in enumerate(sources)]
    await asyncio.gather(*(collector.start() for collector in collectors))
    digests = [hashlib.sha256() for _ in sources]
    counts = [0] * args.routes
    source_started = time.monotonic()
    next_tick = source_started
    target_lines = args.routes * args.lines_per_second * args.seconds
    for tick in range(args.seconds * 10):
        for route, source in enumerate(sources):
            # 模拟实际网络分块：每 100ms 提交一批，避免每行一个协程调度掩盖存储能力。
            count = ((tick + 1) * args.lines_per_second // 10) - (tick * args.lines_per_second // 10)
            payload = b"".join(line(route, counts[route] + n, args.line_bytes) for n in range(count))
            await source.queue.put(payload)
            digests[route].update(payload)
            counts[route] += count
        next_tick += .1
        await asyncio.sleep(max(0, next_tick - time.monotonic()))
    source_elapsed = time.monotonic() - source_started
    await asyncio.gather(*(source.close() for source in sources))
    await asyncio.gather(*(collector.wait_closed() for collector in collectors))
    await asyncio.gather(*(collector.stop() for collector in collectors))
    total_elapsed = time.monotonic() - source_started
    actual = []
    stored_bytes = compressed_bytes = 0
    for route in range(args.routes):
        digest, raw_size, packed_size = verify_route_archives(archives[route])
        stored_bytes += raw_size
        compressed_bytes += packed_size
        actual.append(digest)
    passed = all(expected.hexdigest() == found for expected, found in zip(digests, actual, strict=True))
    maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        maximum_rss *= 1024
    return {
        "routes": args.routes, "configuredSeconds": args.seconds,
        "linesPerSecond": args.lines_per_second, "lineBytes": args.line_bytes,
        "targetLines": target_lines, "sourceLines": sum(counts), "sourceBytes": sum(counts) * args.line_bytes,
        "sourceElapsedSeconds": round(source_elapsed, 3), "totalElapsedSeconds": round(total_elapsed, 3),
        "observedSourceLinesPerSecond": round(sum(counts)/source_elapsed, 2),
        "storedBytes": stored_bytes, "compressedBytes": compressed_bytes,
        "timestampPrefixVerified": True,
        "verified": passed,
        "rssBytes": maximum_rss,
        "archives": sum(len(value) for value in archives),
    }


def parse_args() -> argparse.Namespace:
    """解析本地摘要比对的路数、时长和每行大小。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--lines-per-second", type=int, default=1200)
    parser.add_argument("--line-bytes", type=int, default=256)
    parser.add_argument("--output", default=".local/collector-benchmark")
    return parser.parse_args()


if __name__ == "__main__":
    options = parse_args()
    try:
        report = asyncio.run(execute(options))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["verified"] else 2)
    finally:
        if not options.output.startswith(".local/"):
            print("benchmark output retained:", options.output)

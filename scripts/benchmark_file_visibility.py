"""测量新接收日志在本机页面缓存中可由独立进程尾读的时间。

探针使用真实 Collector 和 HourlyWriter；观察端是独立 spawn 进程，只增量读取
``.log``。它不调用 API、不请求 fsync、不访问归档或 NFS，因此报告只代表本机
页面缓存中原始文件可见性，不能替代持久化、下载或生产容量验收。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import multiprocessing
import os
import queue
import re
import shutil
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from benchmark_collector import verify_route_archives
from camera_logs.collection.collector import Collector

PREFIX = re.compile(rb"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] route=(\d{4}) seq=(\d{9}) ")
ROUTE_DIRECTORY = re.compile(r"^visibility-route-(\d{4})-")
POLL_SECONDS = .005
MAX_SAMPLES = 200_000
MAX_INPUT_BYTES = 64 * 1024 * 1024
EVENT_BATCH_SIZE = 256


class Source:
    """有界模拟设备输入，严格遵守 Collector 给出的单次读取字节上限。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        self._remaining = b""
        self._closed = False

    async def read(self, size: int = 65536) -> bytes:
        """返回最多 ``size`` 字节，未读网络块留给后续同一接收器。"""
        while not self._remaining:
            if self._closed and self.queue.empty():
                return b""
            value = await self.queue.get()
            if value is None:
                return b""
            self._remaining = value
        value, self._remaining = self._remaining[:size], self._remaining[size:]
        return value

    async def write(self, _data: bytes) -> None:
        """探针不发送设备命令，保留连接合同的写入入口。"""

    async def close(self) -> None:
        """标记 EOF；队列满时不等待，读取端排空现有数据后自行结束。"""
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


def line(route: int, sequence: int, width: int) -> bytes:
    """生成固定路由和单调序号的单行正文，便于逐字节校验观察结果。"""
    head = f"route={route:04d} seq={sequence:09d} ".encode()
    if width < len(head) + 1:
        raise ValueError("line_bytes is too small for route and sequence")
    return head + b"x" * (width - len(head) - 1) + b"\n"


def parse_visible_lines(buffer: bytes, route_hint: int | None = None) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """解析完整前缀行并保留最后半行；前缀、路由与序号错误都立即失败。"""
    complete, remainder = buffer.split(b"\n")[:-1], buffer.rsplit(b"\n", 1)[-1]
    values: list[tuple[int, int, bytes]] = []
    for stored in complete:
        match = PREFIX.match(stored)
        if match is None:
            raise AssertionError("visible log line is missing the Shanghai timestamp prefix")
        route, sequence = int(match.group(1)), int(match.group(2))
        if route_hint is not None and route != route_hint:
            raise AssertionError("visible log route does not match its file")
        # ``match`` 已覆盖 route/seq；正文完整性仍必须包含二者，只去掉22字节服务器前缀。
        values.append((route, sequence, stored[22:] + b"\n"))
    return values, remainder


@dataclass
class VisibilityAggregator:
    """汇总接收起点与独立观察时刻，验证每路严格序号、正文和 SHA-256。"""

    expected: Mapping[tuple[int, int], bytes]
    started: dict[tuple[int, int], float] = field(default_factory=dict)
    seen: dict[tuple[int, int], float] = field(default_factory=dict)
    next_sequence: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def received(self, route: int, sequence: int, at: float) -> None:
        """在 Collector 的 on_receive 回调记录起点，禁止用 on_log 掩盖写入延迟。"""
        key = route, sequence
        if not isinstance(at, (int, float)) or not math.isfinite(at):
            raise AssertionError("Collector receive time is not finite")
        if key not in self.expected or key in self.started:
            raise AssertionError("unexpected or duplicate Collector receive event")
        self.started[key] = at
        if key in self.seen and self.seen[key] < at:
            raise AssertionError("file visibility preceded Collector receive time")

    def visible(self, route: int, sequence: int, raw: bytes, at: float) -> None:
        """接收子进程尾读结果，确保同一路没有重复、倒序或跨小时遗漏。"""
        key = route, sequence
        if not isinstance(at, (int, float)) or not math.isfinite(at):
            raise AssertionError("file visibility time is not finite")
        expected = self.expected.get(key)
        if expected is None or expected != raw or key in self.seen:
            raise AssertionError("visible log content is unknown, altered, or duplicated")
        if sequence != self.next_sequence[route]:
            raise AssertionError("visible log sequence is missing or out of order")
        if key in self.started and at < self.started[key]:
            raise AssertionError("file visibility preceded Collector receive time")
        self.next_sequence[route] += 1
        self.seen[key] = at

    @property
    def complete(self) -> bool:
        """只有每条预期正文均被独立进程确认后才允许关闭源并归档。"""
        return set(self.seen) == set(self.expected) and set(self.started) == set(self.expected)

    def report(self) -> dict[str, object]:
        """生成有界样本的延迟分位和逐路 SHA，缺项必须由调用方先作为失败处理。"""
        if not self.complete or set(self.started) != set(self.expected):
            raise AssertionError("missing Collector receive or file visibility samples")
        latencies = sorted((self.seen[key] - self.started[key]) * 1000 for key in self.expected)
        if any(value < 0 for value in latencies):
            raise AssertionError("visibility monotonic clock moved backwards")
        digests: dict[str, str] = {}
        by_route: dict[int, list[tuple[int, bytes]]] = defaultdict(list)
        for (route, sequence), raw in self.expected.items():
            by_route[route].append((sequence, raw))
        for route, values in by_route.items():
            digest = hashlib.sha256()
            for sequence, raw in sorted(values):
                if sequence >= self.next_sequence[route]:
                    raise AssertionError("route has a missing visible sequence")
                digest.update(raw)
            digests[str(route)] = digest.hexdigest()
        return {
            "samples": len(latencies), "p50Ms": _percentile(latencies, .50),
            "p95Ms": _percentile(latencies, .95), "p99Ms": _percentile(latencies, .99),
            "maxMs": round(latencies[-1], 3), "routeSha256": digests,
        }


def _percentile(values: list[float], fraction: float) -> float:
    """取最近本轮完整样本的上界分位，避免小样本向下低估尾延迟。"""
    return round(values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)], 3)


def _route_for_path(path: Path) -> int | None:
    """从探针专用存储目录恢复路由，分卷间半行才能继续拼接到同一路。"""
    for part in path.parts:
        match = ROUTE_DIRECTORY.match(part)
        if match is not None:
            return int(match.group(1))
    return None


def _tail_logs(root: str, output: multiprocessing.Queue, stop: multiprocessing.Event) -> None:
    """独立 spawn 进程的唯一职责：递归尾读当前 ``.log``，不接触归档或 API。"""
    offsets: dict[Path, int] = {}
    remains: dict[int, bytes] = {}
    identities: dict[Path, tuple[int, int]] = {}
    ready_sent = False
    try:
        directory = Path(root)
        while True:
            for path in sorted(directory.rglob("*.log")):
                route_hint = _route_for_path(path)
                if route_hint is None:
                    raise AssertionError("visibility log path has no probe route marker")
                offset = offsets.get(path, 0)
                try:
                    with path.open("rb") as source:
                        metadata = os.fstat(source.fileno())
                        identity = metadata.st_dev, metadata.st_ino
                        if path in identities and identities[path] != identity:
                            raise AssertionError("live log inode changed during visibility probe")
                        identities[path] = identity
                        if source.seek(0, 2) < offset:
                            raise AssertionError("live log was truncated during visibility probe")
                        source.seek(offset)
                        data = source.read()
                except FileNotFoundError:
                    continue
                offsets[path] = offset + len(data)
                if not data:
                    continue
                observed = time.monotonic()
                values, remains[route_hint] = parse_visible_lines(remains.get(route_hint, b"") + data, route_hint)
                for route, sequence, raw in values:
                    output.put(("visible", route, sequence, raw, observed), timeout=1)
            if not ready_sent:
                # 至少完成一轮目录扫描再通知父进程，采样不计入 spawn/import 启动开销。
                output.put(("ready",), timeout=1)
                ready_sent = True
            if stop.is_set():
                if any(remains.values()):
                    raise AssertionError("visibility reader stopped with an incomplete cross-volume line")
                output.put(("done",), timeout=1)
                return
            time.sleep(POLL_SECONDS)
    except BaseException as error:
        try:
            output.put(("error", type(error).__name__, str(error)), timeout=.1)
        except queue.Full:
            # 队列满也必须保留原始观察错误；父进程会看到子进程非零退出。
            pass
        raise


def _event_batch(events: multiprocessing.Queue, timeout: float) -> list[tuple]:
    """在线程中先短等首条 IPC，再无阻塞取有限后续项，避免每行一次线程切换。"""
    values = [events.get(True, timeout)]
    for _ in range(EVENT_BATCH_SIZE - 1):
        try:
            values.append(events.get_nowait())
        except queue.Empty:
            break
    return values


async def _read_events(events: multiprocessing.Queue, aggregator: VisibilityAggregator, deadline: float,
                       reader: multiprocessing.Process) -> None:
    """在事件循环中有界消费跨进程观察消息，超时或观察端报错即失败。"""
    while not aggregator.complete:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            missing = len(aggregator.expected) - len(aggregator.seen)
            raise TimeoutError(f"file visibility timed out with {missing} missing samples")
        try:
            batch = await asyncio.to_thread(_event_batch, events, min(.1, remaining))
        except queue.Empty:
            if not reader.is_alive():
                raise RuntimeError(f"reader process exited before visibility completed: {reader.exitcode}")
            continue
        for event in batch:
            if event[0] == "error":
                raise RuntimeError(f"reader process failed: {event[1]}: {event[2]}")
            if event[0] == "done":
                raise AssertionError("reader ended before all expected lines became visible")
            if event[0] == "ready":
                raise AssertionError("reader repeated its initial scan acknowledgement")
            _, route, sequence, raw, observed = event
            aggregator.visible(route, sequence, raw, observed)


async def _wait_reader_ready(events: multiprocessing.Queue, deadline: float, reader: multiprocessing.Process) -> None:
    """等待独立观察进程完成首次目录扫描，排除启动成本后再记录采集接收起点。"""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reader did not finish its initial scan")
        try:
            event = await asyncio.to_thread(events.get, True, min(.1, remaining))
        except queue.Empty:
            if not reader.is_alive():
                raise RuntimeError(f"reader process exited before ready: {reader.exitcode}")
            continue
        if event[0] == "ready":
            return
        if event[0] == "error":
            raise RuntimeError(f"reader process failed before ready: {event[1]}: {event[2]}")
        raise AssertionError("reader emitted data before its initial scan acknowledgement")


async def _finish_reader(events: multiprocessing.Queue, aggregator: VisibilityAggregator, deadline: float,
                         reader: multiprocessing.Process) -> None:
    """请求最终扫描并消费到 done；额外行、残余半行和观察端异常都不能静默跳过。"""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reader did not acknowledge final file scan")
        try:
            batch = await asyncio.to_thread(_event_batch, events, min(.1, remaining))
        except queue.Empty:
            if not reader.is_alive():
                raise RuntimeError(f"reader process exited before final scan acknowledgement: {reader.exitcode}")
            continue
        done = False
        for event in batch:
            if event[0] == "done":
                done = True
                continue
            if event[0] == "error":
                raise RuntimeError(f"reader process failed: {event[1]}: {event[2]}")
            if event[0] == "ready":
                raise AssertionError("reader repeated its initial scan acknowledgement")
            if done:
                raise AssertionError("reader emitted visibility data after final scan acknowledgement")
            _, route, sequence, raw, observed = event
            aggregator.visible(route, sequence, raw, observed)
        if done:
            return


async def execute(args: argparse.Namespace) -> dict[str, object]:
    """运行短时可扩路探针；成功或失败均先停止观察进程再删除临时目录。"""
    _validate(args)
    base = Path(args.work_root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="file-visibility-", dir=base))
    context = multiprocessing.get_context("spawn")
    events: multiprocessing.Queue = context.Queue(maxsize=args.ipc_queue)
    stop = context.Event()
    reader = context.Process(target=_tail_logs, args=(str(work), events, stop), name="file-visibility-reader")
    sources = [Source() for _ in range(args.routes)]
    received_route = [0] * args.routes
    expected = {(route, sequence): line(route, sequence, args.line_bytes)
                for route in range(args.routes) for sequence in range(args.lines_per_second * args.seconds)}
    aggregator = VisibilityAggregator(expected)
    collectors = []
    archives: list[list[object]] = [[] for _ in range(args.routes)]
    result: dict[str, object] | None = None
    cleanup_failure: Exception | None = None
    try:
        reader.start()
        await _wait_reader_ready(events, time.monotonic() + args.timeout_seconds, reader)
        marker = f"{time.monotonic_ns():x}"
        for route, source in enumerate(sources):
            def received(route=route):
                sequence = received_route[route]
                started = collectors[route].last_received_monotonic
                if started is None:
                    raise AssertionError("Collector did not record receive monotonic time")
                aggregator.received(route, sequence, started)
                received_route[route] += 1

            collectors.append(Collector(
                {"id": f"visibility-{marker}-{route}", "runId": f"run-{marker}-{route}",
                 "storageIdentity": f"visibility-route-{route:04d}-{marker}",
                 "initialCommands": []}, work, connection_factory=lambda _task, source=source: source,
                on_receive=received, on_archive=lambda archive, route=route: archives[route].append(archive),
            ))
        await asyncio.gather(*(collector.start() for collector in collectors))
        visible = asyncio.create_task(_read_events(
            events, aggregator, time.monotonic() + args.seconds + args.timeout_seconds, reader))
        source_started, next_tick = time.monotonic(), time.monotonic()
        async def produce() -> None:
            """按固定短窗口输入；全程受同一超时约束，避免生产端无界等待。"""
            nonlocal next_tick
            for tick in range(args.seconds * 10):
                for route, source in enumerate(sources):
                    begin = tick * args.lines_per_second // 10
                    end = (tick + 1) * args.lines_per_second // 10
                    for sequence in range(begin, end):
                        await source.queue.put(expected[route, sequence])
                next_tick += .1
                await asyncio.sleep(max(0, next_tick - time.monotonic()))
        await asyncio.wait_for(produce(), args.seconds + args.timeout_seconds)
        source_elapsed = time.monotonic() - source_started
        await visible
        # 生产端已结束但仍保持连接：先最终扫描 .log 并确认观察进程退出，EOF 后的
        # 归档删除不能再掩盖最后一行或跨分卷残余。
        stop.set()
        await _finish_reader(events, aggregator, time.monotonic() + args.timeout_seconds, reader)
        await asyncio.to_thread(reader.join, args.timeout_seconds)
        if reader.is_alive() or reader.exitcode != 0:
            raise RuntimeError(f"reader final scan did not exit cleanly: {reader.exitcode}")
        # 最终扫描已证明所有 live .log 可见，现在才允许 EOF 驱动归档。
        await asyncio.gather(*(source.close() for source in sources))
        await asyncio.gather(*(collector.wait_closed() for collector in collectors))
        await asyncio.gather(*(collector.stop() for collector in collectors))
        archive_digests = {}
        for route, route_archives in enumerate(archives):
            if not route_archives:
                raise AssertionError("collector closed without a route archive")
            digest, _stored, _compressed = verify_route_archives(route_archives)
            expected_digest = hashlib.sha256(b"".join(
                expected[route, sequence] for sequence in range(args.lines_per_second * args.seconds)
            )).hexdigest()
            if digest != expected_digest:
                raise AssertionError("archive content digest does not match all expected source lines")
            archive_digests[str(route)] = digest
        result = aggregator.report() | {
            "routes": args.routes, "configuredSeconds": args.seconds, "linesPerSecond": args.lines_per_second,
            "lineBytes": args.line_bytes, "sourceLines": len(expected),
            "sourceElapsedSeconds": round(source_elapsed, 3),
            "actualSourceLinesPerSecond": round(len(expected) / source_elapsed, 2),
            "archiveRouteSha256": archive_digests, "verified": True,
            "scope": "local-page-cache-live-log-tail", "pollIntervalMs": POLL_SECONDS * 1000,
            "inputBurstIntervalMs": 100,
            "inputContract": "synthetic single complete line per Collector read; not arbitrary network fragmentation",
            "filePageCacheReadable": True, "fsyncMeasured": False, "apiMeasured": False, "nfsMeasured": False,
        }
        return result
    finally:
        if "visible" in locals() and not visible.done():
            visible.cancel()
            await asyncio.gather(visible, return_exceptions=True)
        stop.set()
        if reader.pid is not None:
            await asyncio.to_thread(reader.join, 2)
            if reader.is_alive():
                reader.terminate()
                await asyncio.to_thread(reader.join, 2)
            if reader.is_alive():
                reader.kill()
                await asyncio.to_thread(reader.join, 2)
            if reader.is_alive():
                cleanup_failure = RuntimeError(f"reader cleanup could not stop; preserved probe directory: {work}")
        for source in sources:
            if source.queue.empty():
                source.queue.put_nowait(None)
        try:
            stopped = await asyncio.wait_for(
                asyncio.gather(*(collector.stop() for collector in collectors), return_exceptions=True), 5)
            failures = [value for value in stopped if isinstance(value, BaseException)]
            if failures:
                cleanup_failure = cleanup_failure or RuntimeError(f"collector cleanup failed: {failures[0]}")
        except TimeoutError:
            cleanup_failure = cleanup_failure or RuntimeError(f"collector cleanup timed out; preserved probe directory: {work}")
        events.close()
        events.join_thread()
        if cleanup_failure is None:
            try:
                await asyncio.to_thread(shutil.rmtree, work)
                if work.exists():
                    raise OSError("probe directory still exists after cleanup")
            except Exception as error:  # noqa: BLE001 - 产物未删除时必须把路径暴露给调用者。
                cleanup_failure = RuntimeError(f"probe cleanup failed; preserved directory: {work}: {error}")
        if cleanup_failure is not None:
            raise cleanup_failure
        if result is not None:
            result["cleanupVerified"] = True


def _validate(args: argparse.Namespace) -> None:
    """限制短时本机实验输入、IPC 和等待时间，避免脚本伪装为容量压测。"""
    if not 1 <= args.routes <= 500 or not 1 <= args.seconds <= 60 or not 1 <= args.lines_per_second <= 10_000:
        raise ValueError("routes, seconds, and lines_per_second exceed bounded probe limits")
    if not 32 <= args.line_bytes <= 2048 or args.ipc_queue < 64 or not 1 <= args.timeout_seconds <= 120:
        raise ValueError("line_bytes, ipc_queue, or timeout_seconds is outside probe limits")
    samples = args.routes * args.seconds * args.lines_per_second
    if samples > MAX_SAMPLES or samples * args.line_bytes > MAX_INPUT_BYTES:
        raise ValueError("probe sample or input byte limit exceeded")


def parse_args() -> argparse.Namespace:
    """解析短时单机文件可见性实验参数；临时目录无论结果都由脚本删除。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=int, default=1)
    parser.add_argument("--seconds", type=int, default=2)
    parser.add_argument("--lines-per-second", type=int, default=100)
    parser.add_argument("--line-bytes", type=int, default=128)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument("--ipc-queue", type=int, default=2048)
    parser.add_argument("--work-root", default=".local")
    return parser.parse_args()


if __name__ == "__main__":
    options = parse_args()
    try:
        print(json.dumps(asyncio.run(execute(options)), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({"verified": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        raise SystemExit(2) from error

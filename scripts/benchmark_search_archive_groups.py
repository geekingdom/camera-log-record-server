"""确定性比较共享小时包逐成员快照搜索与单次归档打开搜索。"""

from __future__ import annotations

import argparse
import io
import json
import random
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from camera_logs.logs import archive_access, jobs
from camera_logs.logs.archive_access import snapshot
from camera_logs.logs.search_archives import ReadyArchiveGroup
from camera_logs.logs.search_stream import StreamSearch


def _fixture(root: Path, members: int, payload_bytes: int):
    """生成256字节上海时间前缀行、采集分包索引及跨读取块的中文命中。"""
    archive, files = root / "hour.tar.gz", []
    stamp = datetime(2026, 9, 18, tzinfo=UTC)
    with tarfile.open(archive, "w:gz") as bundle:
        for number in range(members):
            name = f"part-{number:04d}.log"
            prefix = b"2026-09-18 08:00:00.000 "
            random_source = random.Random(number)
            lines = []
            for line_number in range(payload_bytes // 256):
                width = 255 if line_number == 0 else 256
                body = bytearray(random_source.randbytes(width - 1 - len(prefix)))
                # 保持固定行长，正文高熵同时不引入额外换行。
                body[:] = bytes(value if value != 10 else 11 for value in body)
                lines.append(prefix + bytes(body) + b"\n")
            payload = bytearray(b"".join(lines))
            # 关键词跨 256KiB StreamSearch 读取块，但仍落在一个完整256字节日志行。
            offset = 262143
            payload[offset:offset + len("企业检索".encode())] = "企业检索".encode()
            payload = bytes(payload)
            item = tarfile.TarInfo(name)
            item.size = len(payload)
            bundle.addfile(item, io.BytesIO(payload))
            index = root / f"part-{number:04d}.index.jsonl"
            with index.open("w", encoding="utf-8") as index_output:
                for offset in range(0, len(payload), 64 * 256):
                    index_output.write(json.dumps({
                        "offset": offset, "length": min(64 * 256, len(payload) - offset),
                        "receivedAt": stamp.isoformat(), "sequence": number * 1000 + offset // (64 * 256) + 1,
                        "taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node",
                    }) + "\n")
            files.append({"id": f"file-{number}", "taskId": "task", "runId": "run", "sessionId": "session",
                          "nodeId": "node", "path": str(archive), "archiveMember": name,
                          "indexPath": str(index), "bytes": len(payload), "status": "READY"})
    return archive, files


def _baseline(root, archive, files, start, end, first_ready):
    """重现变更前逐成员快照、再双 tar 打开扫描的确定性工作量。"""
    scanner, results = StreamSearch("企业检索".encode(), start, end), []
    for file in files:
        target = root / f"snapshot-{file['id']}.tar.gz"
        snapshot(archive, target, file["bytes"], Path(file["indexPath"]), file["id"], file["archiveMember"], True)
        matches, _size, _bounded = jobs._search_limited(target, scanner, file, 1000, 8 * 1024 * 1024,
                                                         lambda: False, file["archiveMember"])
        results.extend(matches)
        if matches:
            first_ready()
        target.unlink()
    results.extend(scanner.finish())
    return results


def _grouped(archive, files, start, end, first_ready):
    """执行本轮单打开归档实现，保持相同扫描器、顺序和结果预算。"""
    scanner, results = StreamSearch("企业检索".encode(), start, end), []
    group = ReadyArchiveGroup(archive, scanner, lambda: False)
    group.open()
    try:
        for file in files:
            matches, _size, _bounded = group.scan_member(file, Path(file["indexPath"]), 1000 - len(results),
                                                          8 * 1024 * 1024, lambda: False)
            results.extend(matches)
            if matches:
                first_ready()
    finally:
        group.close()
    results.extend(scanner.finish())
    return results


def _measure(operation, *, disable_rate_limit):
    """计时并统计 tar 打开次数；测试期间取消读速率睡眠以测量解压与调度本身。"""
    opened, original_open = 0, tarfile.open

    def counted_open(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original_open(*args, **kwargs)

    original_consume = archive_access.read_limiter.consume
    tarfile.open = counted_open
    if disable_rate_limit:
        archive_access.read_limiter.consume = lambda _count: None
    try:
        started = time.perf_counter()
        first_ready_at = None

        def first_ready():
            nonlocal first_ready_at
            if first_ready_at is None:
                first_ready_at = time.perf_counter() - started

        results = operation(first_ready)
        return results, time.perf_counter() - started, opened, first_ready_at
    finally:
        tarfile.open = original_open
        archive_access.read_limiter.consume = original_consume


def main():
    """运行同一合成数据的前后比较；任何结果差异立即失败而不输出性能结论。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--members", type=int, default=24)
    parser.add_argument("--payload-bytes", type=int, default=1_048_576)
    parser.add_argument("--respect-rate-limit", action="store_true")
    arguments = parser.parse_args()
    if arguments.members < 2 or arguments.payload_bytes < 32:
        raise ValueError("members 至少为 2，payload-bytes 至少为 32")
    start = datetime(2026, 9, 18, tzinfo=UTC) - timedelta(minutes=1)
    end = start + timedelta(minutes=2)
    with tempfile.TemporaryDirectory(prefix="camera-log-search-benchmark-") as temporary:
        archive, files = _fixture(Path(temporary), arguments.members, arguments.payload_bytes)
        baseline, baseline_seconds, baseline_opens, baseline_first = _measure(
            lambda first_ready: _baseline(Path(temporary), archive, files, start, end, first_ready),
            disable_rate_limit=not arguments.respect_rate_limit,
        )
        grouped, grouped_seconds, grouped_opens, grouped_first = _measure(
            lambda first_ready: _grouped(archive, files, start, end, first_ready),
            disable_rate_limit=not arguments.respect_rate_limit,
        )
    if grouped != baseline:
        raise AssertionError("前后搜索结果不等价")
    print(json.dumps({
        "members": arguments.members, "payloadBytes": arguments.payload_bytes,
        "rateLimitEnabled": arguments.respect_rate_limit,
        "results": len(grouped), "baselineSeconds": round(baseline_seconds, 6),
        "groupedSeconds": round(grouped_seconds, 6), "speedup": round(baseline_seconds / grouped_seconds, 2),
        "baselineFirstResultSeconds": round(baseline_first or 0, 6),
        "groupedFirstResultSeconds": round(grouped_first or 0, 6),
        "baselineTarOpens": baseline_opens, "groupedTarOpens": grouped_opens,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

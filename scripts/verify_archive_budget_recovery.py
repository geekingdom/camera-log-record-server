"""用八个小型真实小时包比较提交时冻结预算与执行前刷新预算，结束清理全部模拟产物。"""

import asyncio
import hashlib
import json
import multiprocessing
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from camera_logs.logs import compression
from camera_logs.node import performance_runtime

MIB = 1024 * 1024


def prepare(root, route):
    """每路构造独立正文与旁路索引，使用序号区分重复正文与跨路混写。"""
    directory = root / str(route)
    directory.mkdir(parents=True)
    line = f"route={route} synthetic archive budget verification\n".encode()
    payload = line * (512 * 1024 // len(line))
    log, index = directory / "part-000001.log", directory / "part.index.jsonl"
    log.write_bytes(payload)
    index.write_bytes(b"{}\n")
    segment = {"logPath": str(log), "logName": log.name, "indexName": index.name,
               "rawSize": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
               "indexBytes": 3, "indexSha256": hashlib.sha256(b"{}\n").hexdigest()}
    return [segment], directory / "hour.tar.gz", payload


def verify_archives(items):
    """重新解压逐字节比对，仅允许日志成员，确认原始子日志已被回收。"""
    for segments, target, payload in items:
        with tarfile.open(target, "r:gz") as archive:
            assert archive.getnames() == ["part-000001.log"]
            assert archive.extractfile("part-000001.log").read() == payload
        assert not Path(segments[0]["logPath"]).exists()


async def measure(root, *, frozen):
    """旧路径模拟已排队低预算；新路径在第一项开始后恢复预算，余项读取新预算。"""
    items = [prepare(root, route) for route in range(8)]
    started = time.monotonic()
    if frozen:
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
            await asyncio.gather(*[asyncio.get_running_loop().run_in_executor(
                pool, compression.publish_hour_archive, segments, target, MIB, False)
                for segments, target, _ in items])
    else:
        performance_runtime._effective = {"readMiBPerSecond": 1}
        performance_runtime._pressured = True
        jobs = [asyncio.create_task(compression.compress_hour(segments, target)) for segments, target, _ in items]
        try:
            async with asyncio.timeout(10):
                while performance_runtime.archive_snapshot()["runningJobs"] != 1:
                    await asyncio.sleep(.01)
            snapshot = performance_runtime.archive_snapshot()
            assert snapshot["waitingJobs"] == 7
            performance_runtime._effective = {"readMiBPerSecond": 50}
            await asyncio.gather(*jobs)
        finally:
            await asyncio.gather(*jobs, return_exceptions=True)
            await compression.shutdown_compression()
    elapsed = time.monotonic() - started
    verify_archives(items)
    return round(elapsed, 3)


async def main():
    """仅使用随机临时目录；不连接数据库、不访问设备、不删除其他日志。"""
    effective, pressured = performance_runtime._effective, performance_runtime._pressured
    try:
        with TemporaryDirectory(prefix="archive-budget-verification-") as temporary:
            root = Path(temporary)
            frozen = await measure(root / "frozen", frozen=True)
            refreshed = await measure(root / "refreshed", frozen=False)
        print(json.dumps({"frozenSeconds": frozen, "refreshedSeconds": refreshed,
                          "routes": 8, "rawKiBPerRoute": 512, "allBytesVerified": True,
                          "originalSegmentsRemoved": True, "temporaryDataRemoved": not root.exists(),
                          "scope": "synthetic budget recovery, not production capacity"}))
    finally:
        performance_runtime._effective, performance_runtime._pressured = effective, pressured


if __name__ == "__main__":
    asyncio.run(main())

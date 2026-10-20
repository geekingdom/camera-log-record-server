"""合成量测小时切换前后写入等待，不访问设备或持久数据库。"""

import asyncio
import hashlib
import json
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from camera_logs.logs import compression
from camera_logs.logs.storage import HourlyWriter


def percentile(values, fraction):
    """使用排序样本最近索引计算报告 p50/p99，样本为空时明确返回零。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


async def measure(root: Path, *, legacy: bool, routes: int, delay: float):
    """多任务同时跨小时，对比旧索引重读和增量摘要的锁内等待。"""
    original_manifest = HourlyWriter._segment_manifest

    def legacy_manifest(self, segment):
        """模拟旧实现关闭分卷后重读索引；受控延迟隔离慢盘差异。"""
        with segment.index.open("rb") as source:
            index_hash = hashlib.file_digest(source, "sha256").hexdigest()
        time.sleep(delay)
        manifest = original_manifest(self, segment)
        return manifest | {"indexSha256": index_hash, "indexBytes": segment.index.stat().st_size}

    if legacy:
        HourlyWriter._segment_manifest = legacy_manifest
    writers = [HourlyWriter(f"task-{index}", f"run-{index}", f"session-{index}", root,
                            storage_identity="synthetic") for index in range(routes)]
    first = datetime(2026, 10, 20, 1, 59, 59, tzinfo=UTC)
    try:
        await asyncio.gather(*(writer.write(b"before\n", received_at=first) for writer in writers))
        started = time.monotonic()

        async def switch(writer):
            stamp = time.monotonic()
            await writer.write(b"after\n", received_at=first.replace(hour=2, minute=0, second=0))
            return (time.monotonic() - stamp) * 1000

        waits = await asyncio.gather(*(switch(writer) for writer in writers))
        elapsed = (time.monotonic() - started) * 1000
        for writer in writers:
            assert writer._sequence == 2 and writer.active_path is not None
        # 验证两种 manifest 都可交给既有压缩调度器；量测已在此之前结束。
        await asyncio.gather(*(writer.close(defer=True) for writer in writers))
        return {"waitMs": {"p50": round(percentile(waits, .5), 3), "p99": round(percentile(waits, .99), 3),
                           "max": round(max(waits), 3)}, "elapsedMs": round(elapsed, 3), "writers": writers}
    finally:
        HourlyWriter._segment_manifest = original_manifest


async def main():
    """临时目录完成前后量测、序号/正文和增量索引摘要检查后自动清理。"""
    with TemporaryDirectory(prefix="hour-boundary-") as directory:
        root = Path(directory)
        old = await measure(root / "old", legacy=True, routes=8, delay=.04)
        new = await measure(root / "new", legacy=False, routes=8, delay=.04)
        old.pop("writers")
        new.pop("writers")
        await compression._drain_dispatcher()
        await compression.shutdown_compression()
        # 有界调度器可能把超额任务留给磁盘 spool；合成验收显式发布这些遗留项。
        for spool in sorted(root.rglob("*.pending.*.json")):
            await asyncio.to_thread(compression.publish_spooled_archive, spool, None, False)
        for variant in ("old", "new"):
            payloads = []
            for path in (root / variant).rglob("*.tar.gz"):
                with tarfile.open(path, "r:gz") as archive:
                    payloads.extend(archive.extractfile(member).read() for member in archive.getmembers() if member.isfile())
            assert payloads.count(b"before\n") == 8, (variant, payloads)
            assert payloads.count(b"after\n") == 8, (variant, payloads)
            assert len(payloads) == 16, (variant, payloads)
        print(json.dumps({"oldIndexReread": old, "newIncrementalDigest": new,
                          "checks": ["multi-route", "sequence-bytes", "incremental-index-digest"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

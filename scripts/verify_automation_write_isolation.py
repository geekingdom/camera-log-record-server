"""百任务重建与跨小时合成写入验收：两个阻塞会话不得阻止其他会话完成。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from camera_logs.logs import storage, write_io
from camera_logs.logs.archive_spool import load
from camera_logs.logs.compression import publish_spooled_archive


async def verify() -> dict:
    """仅使用自建临时目录，完成或失败都释放阻塞器、收尾线程并清理文件。"""
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    append_calls = 0
    writes = []
    writers = []
    summaries = []
    await write_io.shutdown()
    write_io.configure(12)

    class ObservedWriter(storage.HourlyWriter):
        """以真实文件操作验证隔离，注入等待只针对首轮两个任务的第一次追加。"""

        def _append_batch_sync(self, chunks):
            nonlocal append_calls
            append_calls += 1
            index = int(self.task_id.split("-")[-1])
            if self.run_id == "run-0" and index < 2 and not entered[index].is_set():
                entered[index].set()
                if not release.wait(20):
                    raise TimeoutError("隔离验收阻塞器未及时释放")
            return super()._append_batch_sync(chunks)

    with TemporaryDirectory(prefix="camera-automation-write-") as directory:
        root = Path(directory)
        # 将归档调度暂停为确定性的磁盘积压；真实发布在正文校验完成后执行。
        with patch.object(storage, "schedule_pending_archive", return_value=None):
            try:
                for cycle in range(3):
                    writers = [ObservedWriter(f"task-{i}", f"run-{cycle}", f"session-{cycle}", root,
                               storage_identity=f"device-{i}") for i in range(100)]
                    stamp = datetime(2026, 10, 30, 7, 59, 59, cycle, tzinfo=UTC)
                    batches = [[((f"task={i} run={cycle} block={block} ".encode()).ljust(4095, b"x") + b"\n",
                                 stamp if block < 64 else stamp + timedelta(seconds=2))
                                for block in range(128)] for i in range(100)]
                    started = time.monotonic()
                    writes = [asyncio.create_task(writer.write_many(batch))
                              for writer, batch in zip(writers, batches, strict=True)]
                    if cycle == 0:
                        async with asyncio.timeout(15):
                            await asyncio.gather(*writes[2:])
                        if not all(event.is_set() for event in entered) or any(task.done() for task in writes[:2]):
                            raise AssertionError("未形成两个独立在途写入，不能证明隔离")
                        summaries.append({"healthyCompletedWhileTwoBlocked": 98,
                                          "healthyDurationMs": round((time.monotonic() - started) * 1000, 3)})
                        release.set()
                    positions = await asyncio.wait_for(asyncio.gather(*writes), 20)
                    for batch, rows in zip(batches, positions, strict=True):
                        if [row.sequence for row in rows] != list(range(1, 129)):
                            raise AssertionError("会话重建后的源序号不完整")
                        groups = {}
                        for row in rows:
                            groups.setdefault(row.path, []).append(row)
                        for path, members in groups.items():
                            content = path.read_bytes()
                            expected = b"".join(batch[row.source_index][0] for row in members)
                            if content != expected:
                                raise AssertionError("跨任务、小时或重建会话的正文不一致")
                            index = [json.loads(line) for line in path.with_suffix(".index.jsonl").read_text().splitlines()]
                            if [entry["sequence"] for entry in index] != [row.sequence for row in members]:
                                raise AssertionError("旁路索引与接收顺序不一致")
                    await asyncio.gather(*(writer.close(defer=True) for writer in writers))
                    summaries.append({"cycle": cycle + 1, "tasks": 100, "bytes": 100 * 128 * 4096,
                                      "durationMs": round((time.monotonic() - started) * 1000, 3)})
                spools = sorted(root.rglob("*.pending.*.json"))
                if len(spools) != 600 or append_calls != 600:
                    raise AssertionError(f"跨小时批写或恢复清单数量错误 appends={append_calls} spools={len(spools)}")
                # 所有清单摘要均与真实原始文件一致；同任务三次重建的两个小时实际发布复验。
                selected = []
                for spool in spools:
                    _target, segments = load(spool)
                    for segment in segments:
                        if hashlib.sha256(Path(segment["logPath"]).read_bytes()).hexdigest() != segment["sha256"]:
                            raise AssertionError("封存增量摘要与正文不符")
                    if segments[0]["taskId"] == "task-0":
                        selected.append(spool)
                archives = set()
                for spool in selected:
                    archives.add(await asyncio.to_thread(publish_spooled_archive, spool, 50 * 1024 * 1024, False))
                for archive in archives:
                    with tarfile.open(archive, "r:gz") as bundle:
                        if len(bundle.getmembers()) != 3 or sum(member.size for member in bundle.getmembers()) != 3 * 64 * 4096:
                            raise AssertionError("重建会话小时合并归档不完整")
                return {"passed": True, "syntheticLocalFilesystem": True, "sessions": 300,
                        "writeConcurrency": 12, "appendCalls": append_calls, "persistedSpools": len(spools),
                        "verifiedMergedHourArchives": len(archives), "cycles": summaries}
            finally:
                release.set()
                if writes:
                    await asyncio.gather(*writes, return_exceptions=True)
                await asyncio.gather(*(writer.close(defer=True) for writer in writers), return_exceptions=True)
                await write_io.shutdown()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify()), ensure_ascii=False))

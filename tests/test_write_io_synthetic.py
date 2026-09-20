"""小规模合成写入验证：共享门槛下仍按任务保持原文和索引顺序。"""

import asyncio
import json

from camera_logs.logs import write_io
from camera_logs.logs.storage import HourlyWriter


async def test_small_synthetic_concurrent_writes_keep_each_file_complete(tmp_path):
    """四个任务并发写入小批次，验证有界写盘不会交叉或遗漏索引记录。"""
    await write_io.shutdown()
    write_io.configure(2)
    writers = [HourlyWriter(f"task-{number}", "run", "session", tmp_path, storage_identity=f"device-{number}")
               for number in range(4)]
    batches = [[(f"task-{number}-line-{line}\n".encode(), None) for line in range(40)] for number in range(4)]
    try:
        await asyncio.gather(*(writer.write_many(batch) for writer, batch in zip(writers, batches, strict=True)))
        for number, writer in enumerate(writers):
            expected = b"".join(data for data, _ in batches[number])
            assert writer.active_path.read_bytes() == expected
            rows = [json.loads(row) for row in writer._index.read_text().splitlines()]
            assert [row["sequence"] for row in rows] == list(range(1, 41))
            offsets, offset = [], 0
            for data, _ in batches[number]:
                offsets.append(offset)
                offset += len(data)
            assert [row["offset"] for row in rows] == offsets
    finally:
        await asyncio.gather(*(writer.close() for writer in writers), return_exceptions=True)
        await write_io.shutdown()

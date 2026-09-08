"""在随机临时 MongoDB 库验证目录 OPEN 尾部与 READY 发布，不访问设备。"""

import asyncio
import json
import tempfile
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from camera_logs.collection.collector import LogChunk
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.storage import HourArchive
from pymongo import AsyncMongoClient


def check(condition, message):
    """失败时给出可直接定位目录发布合同的中文原因。"""
    if not condition:
        raise AssertionError(message)


async def main():
    """创建随机库和临时日志根目录，验证真实 upsert 不冲突且最终 READY 可见。"""
    settings = Settings()
    database_name = f"camera_logs_catalog_verify_{uuid4().hex}"
    check(database_name != settings.database_name, "临时目录验证不能连接主库")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        with tempfile.TemporaryDirectory(prefix="camera-logs-catalog-") as temporary:
            root = Path(temporary)
            runtime = object.__new__(SessionRuntime)
            runtime.repo = Repository(client[database_name], settings.model_copy(update={"log_root": root, "node_id": "verify-node"}))
            runtime.task = {"id": "verify-task", "runId": "verify-run"}
            runtime.collector = None
            runtime.frames, runtime.paths = deque(), {}
            runtime.catalog_dirty, runtime.catalog_ready = {}, set()
            runtime.catalog_lock = asyncio.Lock()
            runtime.frame_bytes = runtime.frame_number = runtime.input_bytes = 0
            runtime.last_catalog = 0.
            runtime.started_at = datetime(2026, 9, 8, tzinfo=UTC)
            runtime.stopping = False
            await runtime.repo.initialize()
            path = root / "resources" / "device" / "verify-task" / "2026" / "09" / "08" / "09" / "part-000001.log"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"tail")
            await runtime.on_log(LogChunk("verify-task", "verify-run", "session", 1, b"tail", 0, str(path)))
            identifier = runtime.file_id(path)
            opened = await runtime.repo.db.files.find_one({"id": identifier})
            check(opened["status"] == "OPEN" and opened["firstSequence"] == 1, "真实 Mongo OPEN upsert 字段冲突")
            runtime.last_catalog = time.monotonic()
            await runtime.on_log(LogChunk("verify-task", "verify-run", "session", 2, b"!", 4, str(path)))
            await runtime._publish_catalog()
            tailed = await runtime.repo.db.files.find_one({"id": identifier})
            check(tailed["bytes"] == 5, "最后尾块未发布到真实 Mongo")
            archive_path = path.parent / "hour.tar.gz"
            archive_path.write_bytes(b"archive")
            await runtime.on_archive(HourArchive("verify-task", "verify-run", "session", datetime(2026, 9, 8, tzinfo=UTC),
                archive_path, 5, "digest", 1, 2, path, path.name, path.with_suffix(".index.jsonl")))
            ready = await runtime.repo.db.files.find_one({"id": identifier})
            check(ready["status"] == "READY" and ready["bytes"] == 5, "READY 目录记录未发布")
        print(json.dumps({"passed": True, "openUpsertVerified": True, "tailWatermarkVerified": True,
            "readyWatermarkVerified": True}))
    finally:
        await client.drop_database(database_name)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

"""验证日志分片配置从 25 MiB 收紧到 10 MiB 的真实归档结果。"""

import asyncio
import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from camera_logs.logs import compression
from camera_logs.logs.segment_size import MIB, LogSegmentSizeCache
from camera_logs.logs.storage import HourlyWriter


class PlatformSettings:
    """提供可在写入期间修改的最小平台配置集合，不连接 MongoDB。"""

    def __init__(self, log_segment_mib: int) -> None:
        self.log_segment_mib = log_segment_mib

    async def find_one(self, _query: dict) -> dict[str, int]:
        """返回当前保存的分片大小，模拟节点刷新时的配置读取。"""
        return {"logSegmentMiB": self.log_segment_mib}


def _metadata(archive_path: Path) -> dict:
    """读取小时包旁路清单，以其成员顺序作为正文重组顺序。"""
    return json.loads(Path(f"{archive_path}.metadata.json").read_text(encoding="utf-8"))


async def verify(root: Path) -> dict:
    """以真实 MiB 正文验证缓存刷新只影响后续物理分片。"""
    settings = PlatformSettings(25)
    repo = SimpleNamespace(db=SimpleNamespace(platform_settings=settings))
    cache = LogSegmentSizeCache()
    assert await cache.refresh(repo) == 25 * MIB
    first_body, second_body = b"A" * (25 * MIB), b"B" * (10 * MIB)
    first_at = datetime(2026, 10, 23, 1, 5, 6, tzinfo=UTC)
    second_at = datetime(2026, 10, 23, 1, 7, 8, tzinfo=UTC)
    writer = HourlyWriter(
        "segment-resize-task", "segment-resize-run", "segment-resize-session", root,
        storage_identity="segment-resize-device", task_name="segment-resize", device_ip="192.0.2.25",
        segment_bytes=cache.current,
    )
    await writer.write(first_body, received_at=first_at)
    settings.log_segment_mib = 10
    assert await cache.refresh(repo) == 10 * MIB
    await writer.write(second_body, received_at=second_at)
    archive = await writer.close()
    assert archive is not None
    metadata = _metadata(archive.path)
    members = metadata["members"]
    assert [member["rawSize"] for member in members] == [25 * MIB, 10 * MIB]
    assert [member["logName"] for member in members] == [
        "segment-resize-192.0.2.25-20261023090506-20261023090506-part-000001.log",
        "segment-resize-192.0.2.25-20261023090708-20261023090708-part-000002.log",
    ]
    with tarfile.open(archive.path, "r:gz") as bundle:
        bodies = [bundle.extractfile(member["logName"]).read() for member in members]
    combined = b"".join(bodies)
    expected = first_body + second_body
    assert combined == expected
    assert len(combined) == 35 * MIB
    return {
        "configurationMiB": [25, 10],
        "memberRawSizes": [member["rawSize"] for member in members],
        "memberNames": [member["logName"] for member in members],
        "totalRawBytes": len(combined),
        "bodySha256": hashlib.sha256(combined).hexdigest(),
    }


async def main() -> None:
    """运行合成写入并确认退出时临时目录和压缩进程均已回收。"""
    directory_path: Path | None = None
    try:
        with TemporaryDirectory(prefix="log-segment-resize-") as directory:
            directory_path = Path(directory)
            result = await verify(directory_path)
        assert directory_path is not None and not directory_path.exists()
        result["temporaryDirectoryRemoved"] = True
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await compression.shutdown_compression(timeout_seconds=5)


if __name__ == "__main__":
    asyncio.run(main())

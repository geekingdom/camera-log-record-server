"""存储故障注入：同步失败必须释放句柄，并保留未确认文件供恢复。"""

import asyncio
import errno

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.logs.storage import HourlyWriter


@pytest.mark.parametrize("failed_sync", [1, 2])
async def test_close_sync_failure_releases_handle_and_remains_failed(tmp_path, monkeypatch, failed_sync):
    """分别注入正文和索引同步失败，不允许第二次关闭把不确定结果变成成功。"""
    writer = HourlyWriter("task", "run", "session", tmp_path)
    await writer.write(b"preserve-this-line\n")
    handle, path = writer._handle, writer.active_path
    calls = 0

    def fail_sync(fd):
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError(errno.ENOSPC, "injected sync failure")

    with monkeypatch.context() as patch:
        patch.setattr("camera_logs.logs.storage.os.fsync", fail_sync)
        try:
            with pytest.raises(OSError, match="injected sync failure"):
                await writer.close()
            assert handle.closed
            assert writer.snapshot()["bytesDurable"] == 0
            assert path.read_bytes() == b"preserve-this-line\n"
            assert path.with_suffix(".index.jsonl").exists()
            assert not list(tmp_path.rglob("*.tar.gz"))
        finally:
            # 旧实现复现失败时也释放测试自己的真实文件，避免影响后续用例。
            handle.close()

    with pytest.raises(OSError, match="injected sync failure"):
        await writer.close()
    with pytest.raises(OSError, match="injected sync failure"):
        await writer.write(b"must-not-be-appended\n")
    assert path.read_bytes() == b"preserve-this-line\n"


async def test_collector_sync_failure_closes_device_and_reports_stop_failure(tmp_path, monkeypatch):
    """用真实写入器走采集收尾，验证磁盘错误不会留下设备连接或伪造成功归档。"""
    class Connection:
        closed = False
        received = False

        async def read(self):
            if not self.received:
                self.received = True
                return b"device-output\n"
            return b""

        async def close(self):
            self.closed = True

    connection = Connection()
    handles, archives = [], []
    collector = Collector(
        {"id": "task", "runId": "run", "initialCommands": []}, tmp_path,
        connection_factory=lambda _: connection,
        on_log=lambda _: handles.append(collector._writer._handle),
        on_archive=archives.append,
    )

    def fail_sync(fd):
        raise OSError(errno.EIO, "injected device sync failure")

    monkeypatch.setattr("camera_logs.logs.storage.os.fsync", fail_sync)
    await collector.start()
    await asyncio.wait_for(collector.wait_closed(), 2)
    for _ in range(2):
        with pytest.raises(OSError, match="injected device sync failure"):
            await collector.stop()
    assert connection.closed
    assert handles and all(handle.closed for handle in handles)
    assert not archives
    assert not list(tmp_path.rglob("*.tar.gz"))
    assert next(tmp_path.rglob("*.log")).read_bytes().endswith(b"device-output\n")

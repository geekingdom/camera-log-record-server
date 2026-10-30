"""最终写入明确失败必须清除 pending；真实未返回的写入继续作为 pending 暴露。"""

from types import SimpleNamespace

import pytest
from camera_logs.collection.collector import Collector


class Tracker:
    def __init__(self):
        self.begun = self.finished = self.aborted = 0

    def begin(self): self.begun += 1
    def finish(self): self.finished += 1
    def abort(self): self.aborted += 1


@pytest.mark.asyncio
async def test_flush_failure_aborts_ended_end_to_end_pending(monkeypatch):
    """失败 writer 已给出结果时必须结束延迟样本，避免故障后永久阻断新准入。"""
    collector = object.__new__(Collector)
    collector.write_latency = Tracker()
    collector.write_io_latency = Tracker()
    collector.liveness_storage_busy = False
    collector._buffer_reservation = SimpleNamespace(release_all=lambda: None)
    monkeypatch.setattr("camera_logs.collection.collector.buffering.budget", lambda: SimpleNamespace(pending={}))

    async def fail(_chunks):
        raise OSError("synthetic write failure")
    collector._writer = SimpleNamespace(write_many=fail)

    with pytest.raises(OSError, match="synthetic write failure"):
        await collector._flush([(b"safe", object())])
    assert collector.write_latency.begun == 1
    assert collector.write_latency.finished == 0
    assert collector.write_latency.aborted == 1

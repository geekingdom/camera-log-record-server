"""模拟服务器时间回拨，验证分片、接收顺序、归档背压及独立审计事件。"""

import asyncio
import json
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.logs import storage
from camera_logs.logs.storage import HourlyWriter


@pytest.mark.parametrize("rollback_seconds", [1, 3601])
async def test_rollback_seals_a_new_fragment_without_reordering(tmp_path, rollback_seconds):
    """小时内和跨小时回拨均分片，已归档内容不被覆盖。"""
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    earlier = first - timedelta(seconds=rollback_seconds)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="testingdevice")
    positions = await writer.write_many([
        (b"first\n", first), (b"second\n", earlier), (b"third\n", earlier),
        (b"fourth\n", first),
    ])
    await writer.close()
    assert [p.sequence for p in positions] == [1, 2, 3, 4]
    assert positions[0].path != positions[1].path
    assert positions[1].path == positions[2].path
    assert positions[1].rollback_from == first
    assert all(p.rollback_from is None for p in (positions[0], positions[2], positions[3]))
    fragments = []
    for path in tmp_path.rglob("*.tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            log = next(name for name in archive.getnames() if name.endswith(".log"))
            fragments.append((manifest["firstSequence"], archive.extractfile(log).read()))
    assert b"".join(data for _, data in sorted(fragments)) == b"first\nsecond\nthird\nfourth\n"


async def test_repeated_rollbacks_bound_pending_archives_and_preserve_sequence(tmp_path, monkeypatch):
    """慢压缩时回拨写入受背压限制，释放后所有分片仍按接收序列归档。"""
    release, started = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    original_compress = storage.compress

    async def slow_compress(log, index, manifest):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            return await original_compress(log, index, manifest)
        finally:
            active -= 1

    monkeypatch.setattr(storage, "compress", slow_compress)
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    chunks = [(f"chunk-{number}\n".encode(), first-timedelta(seconds=number)) for number in range(6)]
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="testingdevice")
    writing = asyncio.create_task(writer.write_many(chunks))

    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0)
    assert not writing.done()
    assert maximum == 2 and len(writer._archive_tasks) == 2

    release.set()
    positions = await asyncio.wait_for(writing, 5)
    await writer.close()

    fragments = []
    for path in tmp_path.rglob("*.tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            log = next(name for name in archive.getnames() if name.endswith(".log"))
            fragments.append((manifest["firstSequence"], archive.extractfile(log).read()))
    assert maximum <= 2
    assert [position.sequence for position in positions] == list(range(1, len(chunks)+1))
    assert b"".join(data for _, data in sorted(fragments)) == b"".join(data for data, _ in chunks)


async def test_collector_publishes_rollback_with_original_session_and_file(tmp_path):
    """从写入器经采集器贯通到事件数据库，告警不得把任务状态改为异常。"""
    runtime = object.__new__(SessionRuntime)
    runtime.task = {"id": "task", "runId": "run", "storageIdentity": "testingdevice"}
    events, tasks = AsyncMock(), AsyncMock()
    runtime.repo = SimpleNamespace(
        settings=SimpleNamespace(log_root=tmp_path, node_id="node"),
        db=SimpleNamespace(events=events, tasks=tasks),
    )
    collector = Collector(runtime.task, tmp_path, connection_factory=AsyncMock(), on_state=runtime.on_state)
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    await collector._flush([(b"one\n", first), (b"two\n", first - timedelta(seconds=1))])
    await collector._writer.close()
    event = events.insert_one.call_args.args[0]
    assert event["type"] == "CLOCK_ROLLBACK"
    assert event["taskId"] == "task" and event["runId"] == "run"
    assert event["sessionId"] == collector.session_id and event["nodeId"] == "node"
    assert event["sequence"] == 2
    assert event["previousReceivedAt"] == first.isoformat()
    assert event["receivedAt"] == (first - timedelta(seconds=1)).isoformat()
    assert event["fileId"] and "path" not in event
    events.insert_one.assert_awaited_once()
    tasks.update_one.assert_not_awaited()

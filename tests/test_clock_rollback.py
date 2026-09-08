"""模拟服务器时间回拨，验证分片顺序、归档背压及独立审计事件。"""

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


def _fragments(root):
    values = []
    for path in root.rglob("*.tar.gz"):
        metadata = json.loads(path.with_name(path.name + ".metadata.json").read_text())
        with tarfile.open(path, "r:gz") as archive:
            for member in metadata["members"]:
                values.append((member["firstSequence"], archive.extractfile(member["logName"]).read()))
    return values


@pytest.mark.parametrize("rollback_seconds", [1, 3601])
async def test_rollback_seals_a_new_fragment_without_reordering(tmp_path, rollback_seconds):
    """同小时和跨小时回拨都新建分卷，并严格保留接收顺序。"""
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    earlier = first - timedelta(seconds=rollback_seconds)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="testingdevice")
    positions = await writer.write_many([(b"first\n", first), (b"second\n", earlier),
        (b"third\n", earlier), (b"fourth\n", first)])
    await writer.close()
    assert [item.sequence for item in positions] == [1, 2, 3, 4]
    assert positions[0].path != positions[1].path
    assert positions[1].path == positions[2].path
    assert positions[1].rollback_from == first
    assert b"".join(data for _, data in sorted(_fragments(tmp_path))) == b"first\nsecond\nthird\nfourth\n"


async def test_repeated_rollbacks_bound_pending_archives_and_preserve_sequence(tmp_path, monkeypatch):
    """反复回拨最多保留两个后台压缩，第三次封存等待已有槽位释放。"""
    releases = [asyncio.Event() for _ in range(4)]
    two_started, third_started = asyncio.Event(), asyncio.Event()
    active = maximum = calls = 0
    original = storage.compress_hour

    async def slow(segments, target):
        nonlocal active, maximum, calls
        call = calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_started.set()
        if call == 2:
            third_started.set()
        try:
            await releases[call].wait()
            return await original(segments, target)
        finally:
            active -= 1

    monkeypatch.setattr(storage, "compress_hour", slow)
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    chunks = [(f"chunk-{number}\n".encode(), first - timedelta(seconds=number)) for number in range(4)]
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="testingdevice")
    writing = asyncio.create_task(writer.write_many(chunks))
    await asyncio.wait_for(two_started.wait(), 1)
    assert not writing.done() and maximum == 2
    releases[0].set()
    await asyncio.wait_for(third_started.wait(), 1)
    positions = await asyncio.wait_for(writing, 2)
    closing = asyncio.create_task(writer.close())
    assert not closing.done() and maximum == 2
    releases[1].set()
    await asyncio.sleep(0)
    releases[2].set()
    releases[3].set()
    await asyncio.wait_for(closing, 5)
    assert maximum <= 2
    assert [item.sequence for item in positions] == [1, 2, 3, 4]
    assert b"".join(data for _, data in sorted(_fragments(tmp_path))) == b"".join(data for data, _ in chunks)


async def test_collector_publishes_rollback_with_original_session_and_file(tmp_path):
    """回拨事件保留会话和文件归属，且不会把任务状态改为异常。"""
    runtime = object.__new__(SessionRuntime)
    runtime.task = {"id": "task", "runId": "run", "storageIdentity": "testingdevice"}
    events, tasks = AsyncMock(), AsyncMock()
    runtime.repo = SimpleNamespace(settings=SimpleNamespace(log_root=tmp_path, node_id="node"),
        db=SimpleNamespace(events=events, tasks=tasks))
    collector = Collector(runtime.task, tmp_path, connection_factory=AsyncMock(), on_state=runtime.on_state)
    first = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)
    await collector._flush([(b"one\n", first), (b"two\n", first - timedelta(seconds=1))])
    await collector._writer.close()
    event = events.insert_one.call_args.args[0]
    assert event["type"] == "CLOCK_ROLLBACK"
    assert event["taskId"] == "task" and event["runId"] == "run"
    assert event["sessionId"] == collector.session_id and event["nodeId"] == "node"
    assert event["sequence"] == 2 and event["fileId"] and "path" not in event
    events.insert_one.assert_awaited_once()
    tasks.update_one.assert_not_awaited()

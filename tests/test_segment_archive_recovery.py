"""实际接收时间成员名在收据补偿、重启恢复和旧索引下的回归。"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import maintenance
from camera_logs.logs.archive_receipts import receipt_path
from camera_logs.logs.compression import publish_hour_archive
from camera_logs.logs.public import public_log_file
from camera_logs.logs.source import open_log_source
from camera_logs.logs.storage import HourlyWriter
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


def repository(root: Path) -> Repository:
    """创建与生产恢复路径相同的独立节点目录。"""
    return Repository(AsyncMongoMockClient().db, Settings(
        log_root=root, node_id="node", encryption_key=Fernet.generate_key().decode(),
    ))


def published_actual_member(root: Path) -> tuple[Path, Path, str, dict]:
    """发布源名和实际接收时间成员名不同的小时包，并留下目录收据。"""
    folder = root / "device" / "task" / "2026-09-08" / "09"
    folder.mkdir(parents=True)
    source_name = "task-192.0.2.9-20260908090000-20260908100000-part-000001.log"
    member_name = "task-192.0.2.9-20260908090506-20260908090708-part-000001.log"
    raw, index = b"server output\n", b'{"sequence":1,"offset":0,"length":14,"receivedAt":"2026-09-08T01:05:06+00:00"}\n'
    source = folder / source_name
    source.write_bytes(raw)
    index_name = source.with_suffix(".index.jsonl").name
    (folder / index_name).write_bytes(index)
    member = {
        "taskId": "task", "runId": "run", "sessionId": "session",
        "hourStart": "2026-09-08T01:00:00+00:00", "rawSize": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "firstSequence": 1, "lastSequence": 1,
        "logPath": str(source), "sourceLogName": source_name, "logName": member_name,
        "indexName": index_name, "indexSha256": hashlib.sha256(index).hexdigest(),
        "indexBytes": len(index), "firstReceivedAt": "2026-09-08T01:05:06+00:00",
        "lastReceivedAt": "2026-09-08T01:07:08+00:00",
    }
    target = folder / "hour.tar.gz"
    publish_hour_archive([member], target, create_index=False)
    return target, source, member_name, member


async def test_receipt_then_restart_keeps_single_source_id_and_reads_deleted_raw(tmp_path):
    """收据快补偿和后续重启都沿用源路径 ID，删除原文仍打开实际成员。"""
    target, source, member_name, member = published_actual_member(tmp_path)
    repo = repository(tmp_path)
    identifier = maintenance._archive_id(tmp_path, source, "node")
    await repo.db.files.insert_one({
        "id": identifier, "taskId": "task", "runId": "run", "sessionId": "session",
        "nodeId": "node", "status": "OPEN", "path": str(source),
    })

    assert not source.exists() and receipt_path(target).exists()
    with open_log_source(source) as opened:
        assert opened.name == member_name
        assert opened.stream.read() == b"server output\n"

    await maintenance.pump_pending_archives(repo)
    assert not receipt_path(target).exists()
    record = await repo.db.files.find_one({"id": identifier})
    assert record["status"] == "READY"
    assert record["archiveMember"] == member_name
    assert record["rawFileName"] == member_name
    assert record["firstReceivedAt"] == member["firstReceivedAt"]
    assert record["lastReceivedAt"] == member["lastReceivedAt"]

    assert await maintenance.recover_orphan_archives(repo) == 0
    assert await repo.db.files.count_documents({"id": identifier}) == 1
    assert await repo.db.files.count_documents({}) == 1


def test_recovery_rejects_duplicate_source_or_published_member_names(tmp_path):
    """一个原始身份或 tar 成员只能出现一次，避免重启恢复覆盖同一目录记录。"""
    target, _source, member_name, member = published_actual_member(tmp_path)
    metadata_path = Path(f"{target}.metadata.json")
    metadata = json.loads(metadata_path.read_text())
    duplicate = member | {"logName": member_name.replace("000001", "000002")}
    metadata["members"].append({key: value for key, value in duplicate.items() if key != "logPath"})
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="member is invalid"):
        maintenance._members(target)

    metadata["members"] = metadata["members"][:1] + [metadata["members"][0].copy()]
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="member is invalid"):
        maintenance._members(target)


def test_raw_recovery_naive_received_at_uses_unknown_time(tmp_path):
    """历史无时区索引不能被宿主时区伪造成服务器收到时间。"""
    folder = tmp_path / "device" / "task" / "2026-09-08" / "09"
    folder.mkdir(parents=True)
    log = folder / "task-192.0.2.9-20260908090000-20260908100000-part-000001.log"
    first, second = b"old ", b"output\n"
    body = first + second
    log.write_bytes(body)
    log.with_suffix(".index.jsonl").write_text(
        json.dumps({"sequence": 1, "offset": 0, "length": len(first),
                    "receivedAt": "2026-09-08T01:05:06+00:00"}) + "\n"
        + json.dumps({"sequence": 2, "offset": len(first), "length": len(second),
                      "receivedAt": "2026-09-08T09:05:06"}) + "\n"
    )

    segment = maintenance._raw_segment(log, {
        "taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node", "generation": 1,
    })
    assert segment["logName"].endswith("-unknown-time-part-000001.log")
    assert segment["firstReceivedAt"] is None and segment["lastReceivedAt"] is None


def test_deleted_raw_rejects_unsafe_published_member_name(tmp_path):
    """打开已删原文时也只接受同目录普通 tar 成员名，不能采纳路径穿越元数据。"""
    target, source, _member_name, _member = published_actual_member(tmp_path)
    metadata_path = Path(f"{target}.metadata.json")
    metadata = json.loads(metadata_path.read_text())
    metadata["members"][0]["logName"] = "../outside.log"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="member name"), open_log_source(source):
        pass


async def test_capacity_rotation_keeps_clock_rollback_boundary(tmp_path, monkeypatch):
    """容量换卷不能清空跨分卷时钟水位，紧随其后的回拨仍必须单独封存。"""
    monkeypatch.setattr("camera_logs.logs.storage.MAX_LOG_BYTES", 4)
    writer = HourlyWriter("task", "run", "session", tmp_path, storage_identity="device")
    later = datetime(2026, 9, 8, 1, 5, tzinfo=UTC)
    try:
        positions = await writer.write_many([(b"aaaa", later), (b"b", later.replace(minute=4))])
        assert positions[1].rollback_from == later
        # 第二条先按上一分卷水位判定回拨，再打开新的物理分卷。
        assert writer._last_received_at == later.replace(minute=4)
    finally:
        await writer.close()


def test_public_log_projection_excludes_source_identity():
    """sourceLogName 仅用于恢复身份，不能进入小时列表和作业快照。"""
    published = public_log_file({"id": "file", "sourceLogName": "raw.log", "archiveMember": "shown.log"})
    assert published == {"id": "file", "archiveMember": "shown.log"}

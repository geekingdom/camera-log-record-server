"""Coredump 扫描游标和连续观测状态回归。"""

from datetime import UTC, datetime, timedelta

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps import scanner as scanner_module
from camera_logs.coredumps.scanner import CoredumpScanner
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


async def repository(tmp_path, maximum=500):
    """构造隔离的 NFS 目录和目录数据库，不访问真实设备。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path / "logs",
                        nfs_root=tmp_path / "nfs", node_id="node", coredump_scan_max_files=maximum)
    settings.nfs_root.mkdir()
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    return repo


async def test_single_resource_cursor_wraps_to_earlier_nested_none_file(tmp_path):
    """游标在字典序末尾后，新出现的 `(none)` 子目录文件仍会在回绕时被登记。"""
    repo = await repository(tmp_path, maximum=1)
    directory = repo.settings.nfs_root / "192.0.2.31"; directory.mkdir()
    (directory / "z-last.core").write_bytes(b"first")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.31", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    scanner = CoredumpScanner(repo)
    assert await scanner.scan_once() == 1
    nested = directory / "(none)"; nested.mkdir()
    (nested / "new.core").write_bytes(b"later")
    assert await scanner.scan_once() == 1
    assert await repo.db.coredump_files.find_one({"name": "(none)/new.core"})


async def test_scanner_tracks_observing_changing_and_stable_without_changing_receiving_status(tmp_path, monkeypatch):
    """连续同指纹十秒后稳定，写入变化重置计时且首次接收时间始终不变。"""
    repo = await repository(tmp_path)
    directory = repo.settings.nfs_root / "192.0.2.32"; directory.mkdir()
    source = directory / "core.bin"; source.write_bytes(b"one")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.32", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    clock = datetime(2026, 9, 10, tzinfo=UTC)
    monkeypatch.setattr(scanner_module, "now", lambda: clock)
    scanner = CoredumpScanner(repo)
    await scanner.scan_once()
    first = await repo.db.coredump_files.find_one({"name": "core.bin"})
    assert first["status"] == "RECEIVING" and first["sourceState"] == "OBSERVING"
    received_at = first["receivedAt"]
    clock += timedelta(seconds=10)
    await scanner.scan_once()
    stable = await repo.db.coredump_files.find_one({"id": first["id"]})
    assert stable["sourceState"] == "STABLE" and stable["sourceStableAt"].replace(tzinfo=UTC) == clock
    source.write_bytes(b"changed")
    clock += timedelta(seconds=1)
    await scanner.scan_once()
    changed = await repo.db.coredump_files.find_one({"id": first["id"]})
    assert changed["status"] == "RECEIVING" and changed["sourceState"] == "CHANGING"
    assert changed["sourceStableAt"] is None and changed["receivedAt"] == received_at


async def test_scanner_ignores_flag_files_in_all_subdirectories_and_keeps_resource_fair_at_limit(tmp_path):
    """标志文件不入库；首个资源占满 500 时下一轮仍先观察后续资源。"""
    repo = await repository(tmp_path)
    first = repo.settings.nfs_root / "192.0.2.33"; first.mkdir()
    second = repo.settings.nfs_root / "192.0.2.34"; second.mkdir()
    for number in range(500):
        (first / f"core-{number:03}.bin").write_bytes(b"x")
    nested = first / "nested"; nested.mkdir()
    (nested / "coredump_flag.cdf").write_bytes(b"flag")
    (second / "new.core").write_bytes(b"new")
    await repo.db.resources.insert_many([
        {"id": "a", "ip": "192.0.2.33", "kind": "HIKVISION_NETWORK", "deletedAt": None},
        {"id": "b", "ip": "192.0.2.34", "kind": "HIKVISION_NETWORK", "deletedAt": None},
    ])
    scanner = CoredumpScanner(repo)
    assert await scanner.scan_once() == 500
    assert await scanner.scan_once() >= 1
    assert await repo.db.coredump_files.find_one({"resourceId": "b", "name": "new.core"})
    assert await repo.db.coredump_files.count_documents({"name": {"$regex": "coredump_flag"}}) == 0


async def test_scanner_records_one_discovery_event_and_recovers_a_failed_event_write(tmp_path, monkeypatch):
    """目录登记成功而事件暂时失败时，下轮补记同一发现，成功后不再重复。"""
    repo = await repository(tmp_path); directory = repo.settings.nfs_root / "192.0.2.35"; directory.mkdir(); (directory / "core.bin").write_bytes(b"core")
    await repo.db.resources.insert_one({"id": "camera", "name": "大厅相机", "ip": "192.0.2.35", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    scanner, collection_type = CoredumpScanner(repo), type(repo.db.events); original_update, attempts = collection_type.update_one, 0
    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1: raise RuntimeError("events temporarily unavailable")
        return await original_update(*args, **kwargs)
    monkeypatch.setattr(collection_type, "update_one", fail_once); await scanner.scan_once()
    catalog = await repo.db.coredump_files.find_one({"resourceId": "camera", "name": "core.bin"})
    assert catalog.get("discoveryEventRecordedAt") is None
    await scanner.scan_once(); event = await repo.db.events.find_one({"type": "COREDUMP_DISCOVERED"})
    assert event["resourceId"] == "camera" and event["fileId"] == catalog["id"] and event["createdAt"] == catalog["receivedAt"]
    await scanner.scan_once(); assert await repo.db.events.count_documents({"type": "COREDUMP_DISCOVERED"}) == 1
    await repo.db.events.delete_one({"_id": event["_id"]}); await scanner.scan_once()
    assert await repo.db.events.count_documents({"type": "COREDUMP_DISCOVERED"}) == 0


async def test_probe_marks_only_missing_leaf_and_keeps_snapshot_visible(tmp_path):
    """源叶文件删除会撤销存在标记，固定快照仍保留可下载目录事实。"""
    repo = await repository(tmp_path)
    directory = repo.settings.nfs_root / "192.0.2.36"
    directory.mkdir()
    source = directory / "removed.core"
    source.write_bytes(b"core")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.36", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    scanner = CoredumpScanner(repo)
    await scanner.scan_once()
    catalog = await repo.db.coredump_files.find_one({"resourceId": "camera"})
    source.unlink()
    await scanner.scan_once()
    assert (await repo.db.coredump_files.find_one({"id": catalog["id"]}))["sourcePresent"] is False
    await repo.db.coredump_files.update_one({"id": catalog["id"]}, {"$set": {"status": "FROZEN", "snapshot": {"path": "fixed"}}})
    assert await repo.db.coredump_files.count_documents({"$and": [
        {"resourceId": "camera"}, {"$or": [{"sourcePresent": {"$ne": False}}, {"status": "FROZEN", "snapshot.path": {"$exists": True}}]},
    ]}) == 1


async def test_probe_keeps_last_known_status_when_root_or_device_directory_is_unavailable(tmp_path, monkeypatch):
    """根目录、设备目录或权限失败不是叶文件删除，不能误清除红点。"""
    repo = await repository(tmp_path)
    scanner = CoredumpScanner(repo)
    catalog = {"deviceIp": "192.0.2.37", "name": "core.bin"}
    monkeypatch.setattr(scanner_module, "device_directory", lambda *_: (_ for _ in ()).throw(FileNotFoundError()))
    assert scanner._source_is_present(catalog) is None
    monkeypatch.setattr(scanner_module, "device_directory", lambda *_: (_ for _ in ()).throw(PermissionError()))
    assert scanner._source_is_present(catalog) is None


async def test_probe_cursor_rotates_between_registered_sources(tmp_path):
    """探测游标以固定上限轮转，不让一个资源的历史目录长期占满扫描预算。"""
    repo = await repository(tmp_path, maximum=1)
    for identifier in ("a", "b"):
        await repo.db.coredump_files.insert_one({"id": identifier, "nodeId": "node", "deviceIp": "192.0.2.1",
                                                  "name": f"{identifier}.core", "sourcePresent": True})
    scanner = CoredumpScanner(repo)
    observed = []
    scanner._source_is_present = lambda catalog: observed.append(catalog["id"]) or True
    await scanner._probe_known_sources({})
    state = await repo.db.coredump_scan_state.find_one({"id": "node"})
    await scanner._probe_known_sources(state)
    assert observed == ["a", "b"]

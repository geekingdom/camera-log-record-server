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

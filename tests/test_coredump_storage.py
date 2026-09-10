"""NFS coredump 扫描和冻结快照只使用临时目录，不接触真实设备或开发库。"""

import asyncio
from pathlib import Path

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.coredumps.jobs import _complete, _release, _reserve, cleanup_expired_exports
from camera_logs.coredumps.scanner import CoredumpScanner
from camera_logs.coredumps.snapshots import freeze
from camera_logs.logs.file_reads import FileReads
from camera_logs.node.files import _limited_response
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient
from starlette.requests import Request


async def repository(tmp_path, *, maximum=20_000_000_000, quota=100_000_000_000):
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path / "logs",
                        nfs_root=tmp_path / "nfs", coredump_snapshot_max_bytes=maximum,
                        coredump_snapshot_quota_bytes=quota, coredump_scan_max_files=500, node_id="node-a")
    settings.nfs_root.mkdir(parents=True)
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    return repo


async def test_scanner_records_server_observation_and_rotates_past_first_500(tmp_path):
    """第二轮能找到第一轮上限之后的文件，不把设备 mtime 当服务端接收时间。"""
    repo = await repository(tmp_path)
    directory = repo.settings.nfs_root / "192.0.2.44"; directory.mkdir()
    for number in range(501):
        (directory / f"core-{number:03}.bin").write_bytes(b"x")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.44", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    scanner = CoredumpScanner(repo)
    assert await scanner.scan_once() == 500
    assert await scanner.scan_once() == 1
    assert await repo.db.coredump_files.count_documents({"resourceId": "camera"}) == 501
    entry = await repo.db.coredump_files.find_one({"name": "core-500.bin"})
    assert entry["receivedAt"] != entry["sourceModifiedAt"]


async def test_scanner_rotates_to_later_resource_before_wrapping_full_first_resource(tmp_path):
    """资源 A 恰好占满本轮上限时，下一轮必须先观察资源 B。"""
    repo = await repository(tmp_path)
    first = repo.settings.nfs_root / "192.0.2.48"; first.mkdir()
    second = repo.settings.nfs_root / "192.0.2.49"; second.mkdir()
    for number in range(500):
        (first / f"core-{number:03}.bin").write_bytes(b"a")
    (second / "new.bin").write_bytes(b"b")
    await repo.db.resources.insert_many([
        {"id": "a", "ip": "192.0.2.48", "kind": "HIKVISION_NETWORK", "deletedAt": None},
        {"id": "b", "ip": "192.0.2.49", "kind": "HIKVISION_NETWORK", "deletedAt": None},
    ])
    scanner = CoredumpScanner(repo)
    assert await scanner.scan_once() == 500
    assert await scanner.scan_once() >= 1
    assert await repo.db.coredump_files.find_one({"resourceId": "b", "name": "new.bin"})


async def test_freeze_copies_only_registered_version_and_rejects_hardlink(tmp_path):
    """冻结产物在 Worker 本地目录，硬链接源不能绕过文件身份检查。"""
    repo = await repository(tmp_path)
    directory = repo.settings.nfs_root / "192.0.2.45"; directory.mkdir()
    source = directory / "core.bin"; source.write_bytes(b"stable core")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.45", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    await CoredumpScanner(repo).scan_once()
    entry = await repo.db.coredump_files.find_one({"resourceId": "camera"})
    frozen = await freeze(repo, entry)
    assert frozen["status"] == "FROZEN"
    assert Path(frozen["snapshot"]["path"]).read_bytes() == b"stable core"
    assert repo.settings.nfs_root not in Path(frozen["snapshot"]["path"]).parents
    hardlink = directory / "linked.bin"; hardlink.hardlink_to(source)
    await CoredumpScanner(repo).scan_once()
    assert await repo.db.coredump_files.count_documents({"name": "linked.bin"}) == 0


async def test_freeze_rejects_source_changed_after_catalog(tmp_path):
    """catalog 与冻结之间文件变化时不发布副本。"""
    repo = await repository(tmp_path)
    directory = repo.settings.nfs_root / "192.0.2.46"; directory.mkdir()
    source = directory / "core.bin"; source.write_bytes(b"before")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.46", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    await CoredumpScanner(repo).scan_once()
    entry = await repo.db.coredump_files.find_one({"resourceId": "camera"})
    source.write_bytes(b"after changed")
    with pytest.raises(RuntimeError):
        await freeze(repo, entry)
    assert not await repo.db.coredump_files.find_one({"id": entry["id"], "status": "FROZEN"})


async def test_sparse_large_file_respects_configured_limit_without_loading_memory(tmp_path):
    """稀疏大文件只按 stat 判定并返回上限错误，不进入全量内存复制。"""
    repo = await repository(tmp_path, maximum=1024)
    directory = repo.settings.nfs_root / "192.0.2.47"; directory.mkdir()
    source = directory / "large.core"
    with source.open("wb") as output:
        output.seek(2 * 1024 * 1024); output.write(b"x")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.47", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    await CoredumpScanner(repo).scan_once()
    entry = await repo.db.coredump_files.find_one({"resourceId": "camera"})
    with pytest.raises(OverflowError):
        await freeze(repo, entry)


async def test_concurrent_freezes_cannot_overcommit_snapshot_quota(tmp_path):
    """两份各八字节源并发冻结时，十字节节点配额只能发布其中一份。"""
    repo = await repository(tmp_path, quota=10)
    directory = repo.settings.nfs_root / "192.0.2.50"; directory.mkdir()
    (directory / "first.core").write_bytes(b"12345678")
    (directory / "second.core").write_bytes(b"abcdefgh")
    await repo.db.resources.insert_one({"id": "camera", "ip": "192.0.2.50", "kind": "HIKVISION_NETWORK", "deletedAt": None})
    await CoredumpScanner(repo).scan_once()
    entries = [item async for item in repo.db.coredump_files.find({"resourceId": "camera"})]
    results = await asyncio.gather(*(freeze(repo, entry) for entry in entries), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    reserved = await repo.db.coredump_snapshot_reservations.find_one({"id": "node-a"})
    assert reserved["used"] == 8


def request_with_headers(headers: dict[str, str]) -> Request:
    """构造最小 ASGI 请求以验证内部 Range 响应，而不启动真实节点服务。"""
    return Request({"type": "http", "method": "GET", "path": "/internal/test",
                    "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})


async def test_limited_response_handles_suffix_range_and_utf8_filename(tmp_path):
    """尾缀 Range 必须返回文件末尾，中文名称以 RFC 5987 编码且不破坏响应头。"""
    source = tmp_path / "frozen.core"; source.write_bytes(b"0123456789")
    reads = FileReads()
    response = await _limited_response(reads, asyncio.Semaphore(1), source,
                                       request_with_headers({"range": "bytes=-3"}), filename="设备.core", etag='"v1"')
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 7-9/10"
    assert "filename*=UTF-8''%E8%AE%BE%E5%A4%87.core" in response.headers["content-disposition"]
    assert b"".join([part async for part in response.body_iterator]) == b"789"
    with pytest.raises(HTTPException) as invalid:
        await _limited_response(reads, asyncio.Semaphore(1), source,
                                request_with_headers({"range": "bytes=20-"}), filename="x")
    assert invalid.value.status_code == 416
    assert invalid.value.headers["Content-Range"] == "bytes */10"
    await reads.close()


async def test_limited_response_releases_fd_when_response_start_fails(tmp_path):
    """ASGI 尚未开始正文即失败时，预打开文件描述符和下载名额必须立即归还。"""
    source = tmp_path / "frozen.core"; source.write_bytes(b"content")
    reads, slots = FileReads(), asyncio.Semaphore(1)
    response = await _limited_response(reads, slots, source, request_with_headers({}), filename="core")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        await response({"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "path": "/"}, receive, send)
    assert not slots.locked()
    await reads.close()


async def test_export_completion_does_not_audit_success_after_cancel_race(tmp_path):
    """取消先提交后，晚到的成功收尾不覆盖状态，也不能追加成功审计。"""
    repo = await repository(tmp_path)
    job = {"id": "export", "actor": "bootstrap"}
    await repo.db.coredump_exports.insert_one({**job, "status": "CANCELLED"})
    actual = await _complete(repo, job, {"status": "SUCCEEDED", "resultPath": "/private"})
    assert actual == {"status": "CANCELLED", "_transitioned": False}
    assert await repo.db.audit.count_documents({"action": "coredump_export_succeeded", "targetId": "export"}) == 0


async def test_export_reservation_is_idempotent_and_releases_once(tmp_path):
    """同一作业重复领取只占一次额度，重复 TTL 清理不能使 used 变负。"""
    repo = await repository(tmp_path, quota=1024)
    repo.settings.coredump_export_quota_bytes = 100_000
    job = {"id": "export", "estimatedBytes": 10, "sources": [{}, {}]}
    first = await _reserve(repo, job)
    assert await _reserve(repo, job) == first
    total = await repo.db.coredump_export_reservations.find_one({"id": "node-a"})
    assert total["used"] == first
    assert await _release(repo, "export") is True
    assert await _release(repo, "export") is False
    total = await repo.db.coredump_export_reservations.find_one({"id": "node-a"})
    assert total["used"] == 0


async def test_expired_export_cleanup_keeps_running_output_and_releases_missing_ttl_claim(tmp_path):
    """运行中目录不被维护删除；TTL 删除目录文档后仍按 claim 只释放一次。"""
    repo = await repository(tmp_path)
    root = repo.settings.log_root.resolve() / "exports" / "coredumps"
    running = root / "running"; running.mkdir(parents=True)
    await repo.db.coredump_exports.insert_one({"id": "running", "status": "RUNNING"})
    assert await cleanup_expired_exports(repo) == 0
    assert running.exists()
    expired = root / "expired"; expired.mkdir()
    scratch = root / ".tmp" / "expired"; scratch.mkdir(parents=True)
    await _reserve(repo, {"id": "expired", "estimatedBytes": 1, "sources": []})
    assert await cleanup_expired_exports(repo) == 1
    claim = await repo.db.coredump_export_reservation_claims.find_one({"id": "expired"})
    assert claim["state"] == "RELEASED"
    assert not expired.exists() and not scratch.exists()
    assert await cleanup_expired_exports(repo) == 0

"""旧归档全文索引补建的游标、限额和失败回退。"""

from __future__ import annotations

import sqlite3
import tarfile
from io import BytesIO
from types import SimpleNamespace

from camera_logs.logs import search_index_maintenance
from camera_logs.logs.search_index import build_index, index_path, query_index
from mongomock_motor import AsyncMongoMockClient


async def _repo(tmp_path, files):
    """创建本节点目录和持久游标可观察的最小仓库。"""
    database = AsyncMongoMockClient().db
    await database.files.insert_many(files)
    await database.nodes.insert_one({"id": "local"})
    return SimpleNamespace(db=database, settings=SimpleNamespace(log_root=tmp_path, node_id="local"))


async def test_backfill_visits_at_most_64_and_builds_one_then_advances_cursor(tmp_path, monkeypatch):
    """单周期只补建一个，下一周期从已提交的游标后继续，避免长期卡在目录头部。"""
    files = []
    for number in range(65):
        path = tmp_path / f"{number:03d}.tar.gz"
        path.write_bytes(b"archive")
        files.append({"id": f"{number:03d}", "nodeId": "local", "status": "READY", "path": str(path)})
    repo = await _repo(tmp_path, files)
    checked, built = [], []

    def cold(path, _keyword):
        checked.append(path.name)

    async def build(path):
        built.append(path.name)
        return True

    monkeypatch.setattr(search_index_maintenance, "query_index", cold)
    monkeypatch.setattr(search_index_maintenance, "build_search_index", build)
    assert await search_index_maintenance.backfill_search_indexes(repo) == 1
    assert checked == ["000.tar.gz"] and built == ["000.tar.gz"]
    assert (await repo.db.nodes.find_one({"id": "local"}))["searchIndexCursor"] == "000"
    assert await search_index_maintenance.backfill_search_indexes(repo) == 1
    assert checked[-1] == "001.tar.gz" and built[-1] == "001.tar.gz"


async def test_backfill_moves_cursor_over_healthy_window_then_wraps_next_cycle(tmp_path, monkeypatch):
    """前64项均健康时本轮不构建，下一轮必须继续处理第65项而非永久重复第一页。"""
    files = []
    for number in range(65):
        path = tmp_path / f"{number:03d}.tar.gz"
        path.write_bytes(b"archive")
        files.append({"id": f"{number:03d}", "nodeId": "local", "status": "READY", "path": str(path)})
    repo = await _repo(tmp_path, files)
    checked, built = [], []

    def health(path, _keyword):
        checked.append(path.name)
        return {} if path.name != "064.tar.gz" else None

    async def build(path):
        built.append(path.name)
        return True

    monkeypatch.setattr(search_index_maintenance, "query_index", health)
    monkeypatch.setattr(search_index_maintenance, "build_search_index", build)
    assert await search_index_maintenance.backfill_search_indexes(repo) == 0
    assert len(checked) == 64 and checked[-1] == "063.tar.gz"
    assert (await repo.db.nodes.find_one({"id": "local"}))["searchIndexCursor"] == "063"
    assert await search_index_maintenance.backfill_search_indexes(repo) == 1
    assert built == ["064.tar.gz"]


async def test_backfill_rebuilds_real_schema_one_sidecar_and_advances_catalog_cursor(tmp_path, monkeypatch):
    """旧 schema 探针返回未知后，维护入口必须重建真实索引并提交本机目录游标。"""
    archive = tmp_path / "legacy.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("part.log")
        payload = b"prefix\x00needle\n"
        member.size = len(payload)
        output.addfile(member, BytesIO(payload))
    assert build_index(archive)
    with sqlite3.connect(index_path(archive)) as database:
        database.execute("UPDATE metadata SET value='1' WHERE key='schemaVersion'")
        database.commit()
    assert query_index(archive, "needle") is None
    repo = await _repo(tmp_path, [{"id": "legacy", "nodeId": "local", "status": "READY", "path": str(archive)}])

    async def build_inline(path):
        return build_index(path)

    monkeypatch.setattr(search_index_maintenance, "build_search_index", build_inline)
    assert await search_index_maintenance.backfill_search_indexes(repo) == 1
    assert query_index(archive, "needle")["part.log"]["mayMatch"]
    with sqlite3.connect(index_path(archive)) as database:
        assert dict(database.execute("SELECT key, value FROM metadata"))["schemaVersion"] == "2"
    assert (await repo.db.nodes.find_one({"id": "local"}))["searchIndexCursor"] == "legacy"

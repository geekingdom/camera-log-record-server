"""旧归档全文索引补建的游标、限额和失败回退。"""

from __future__ import annotations

from types import SimpleNamespace

from camera_logs.logs import search_index_maintenance
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

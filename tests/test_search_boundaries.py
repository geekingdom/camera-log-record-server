"""搜索作业跨冻结文件边界匹配的身份、索引和时间边界回归。"""

from __future__ import annotations

import asyncio
import json
import tarfile
from datetime import UTC, datetime, timedelta

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.jobs import run_job
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def assert_match(result, keyword):
    """严格检查唯一命中身份和偏移；正文合同是命中所在完整逻辑行。"""
    assert result["status"] == "SUCCEEDED"
    assert len(result["results"]) == 1
    item = result["results"][0]
    assert (item["fileId"], item["offset"]) == ("file-0", 7)
    assert keyword in item["text"]
    assert "\n" not in item["text"]


async def search(tmp_path, chunks, keyword, *, start=NOW - timedelta(minutes=1), end=NOW + timedelta(minutes=1), archived=False):
    """建立完整冻结文件和旁路索引，再通过正式 SEARCH 作业返回命中结果。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = Settings(encryption_key=Fernet.generate_key().decode(), log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)

    async def audit(*_args, **_kwargs):
        return None

    repo.audit = audit
    frozen = []
    for number, chunk in enumerate(chunks):
        path = tmp_path / f"part-{number}.log"
        index = tmp_path / f"part-{number}.index.jsonl"
        path.write_bytes(chunk["data"])
        record = {
            "offset": 0,
            "length": len(chunk["data"]),
            "receivedAt": chunk.get("received_at", NOW).isoformat(),
            "sequence": chunk.get("sequence", number + 1),
            "taskId": "task",
            "runId": "run",
            "sessionId": chunk.get("session_id", "session"),
            "nodeId": "node",
        }
        index.write_text(json.dumps(record) + "\n", encoding="utf-8")
        file = {
            "id": f"file-{number}",
            "taskId": "task",
            "runId": "run",
            "sessionId": chunk.get("session_id", "session"),
            "nodeId": "node",
            "status": "OPEN",
            "bytes": len(chunk["data"]),
            "path": str(path),
            "indexPath": str(index),
        }
        frozen.append(file.copy())
    if archived:
        archive = tmp_path / "hour.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for number in range(len(chunks)):
                path = tmp_path / f"part-{number}.log"
                output.add(path, arcname=path.name)
        for number, file in enumerate(frozen):
            path = tmp_path / f"part-{number}.log"
            file.update(path=str(archive), archiveMember=path.name, status="READY")
            path.unlink()
    for file in frozen:
        await repo.db.files.insert_one(file)
    job = {
        "id": "search-boundary",
        "kind": "SEARCH",
        "status": "RUNNING",
        "actor": "tester",
        "keyword": keyword.decode("utf-8"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "files": frozen,
    }
    await repo.db.jobs.insert_one(job)
    return await run_job(repo, job)


def test_search_matches_keyword_split_across_continuous_files_at_start_file_offset(tmp_path):
    """同一会话连续索引的跨文件关键词必须归属起始文件及其精确偏移。"""
    async def scenario():
        return await search(tmp_path, [
            {"data": b"prefix nee", "sequence": 10},
            {"data": b"dle suffix", "sequence": 11},
        ], b"needle")

    result = asyncio.run(scenario())
    assert result["status"] == "SUCCEEDED"
    assert_match(result, "needle")


def test_search_does_not_join_different_sessions_or_non_contiguous_sequences(tmp_path):
    """会话切换或索引序号缺口不能把相邻冻结文件误作同一正文流。"""
    async def scenario():
        different_session = await search(tmp_path / "session", [
            {"data": b"prefix nee", "sequence": 10},
            {"data": b"dle suffix", "sequence": 11, "session_id": "new-session"},
        ], b"needle")
        sequence_gap = await search(tmp_path / "gap", [
            {"data": b"prefix nee", "sequence": 10},
            {"data": b"dle suffix", "sequence": 12},
        ], b"needle")
        return different_session, sequence_gap

    different_session, sequence_gap = asyncio.run(scenario())
    assert different_session["results"] == []
    assert sequence_gap["results"] == []


def test_search_matches_keyword_spanning_three_continuous_files(tmp_path):
    """尾部保留需跨多个小文件累积，不能只支持相邻两文件。"""
    async def scenario():
        return await search(tmp_path, [
            {"data": b"prefix ne", "sequence": 20},
            {"data": b"ed", "sequence": 21},
            {"data": b"le suffix", "sequence": 22},
        ], b"needle")

    result = asyncio.run(scenario())
    assert_match(result, "needle")


def test_search_excludes_cross_file_match_when_start_byte_time_is_outside_range(tmp_path):
    """跨界关键词应按起始字节时间过滤，后续字节进入范围也不能补回该匹配。"""
    async def scenario():
        return await search(tmp_path, [
            {"data": b"prefix nee", "sequence": 30, "received_at": NOW - timedelta(minutes=2)},
            {"data": b"dle suffix", "sequence": 31, "received_at": NOW},
        ], b"needle", start=NOW - timedelta(seconds=1), end=NOW + timedelta(seconds=1))

    result = asyncio.run(scenario())
    assert result["results"] == []


def test_search_excludes_cross_file_match_when_start_byte_is_exactly_end(tmp_path):
    """跨文件关键词按首字节归属，等于 end 时必须被半开区间排除。"""
    async def scenario():
        return await search(tmp_path, [
            {"data": b"prefix nee", "sequence": 32, "received_at": NOW + timedelta(seconds=1)},
            {"data": b"dle suffix", "sequence": 33, "received_at": NOW},
        ], b"needle", start=NOW, end=NOW + timedelta(seconds=1))

    assert asyncio.run(scenario())["results"] == []


def test_search_matches_utf8_keyword_split_across_continuous_files(tmp_path):
    """跨界窗口按字节保留，UTF-8 多字节字符在文件间拆分仍应被字面匹配。"""
    async def scenario():
        needle = "摄像机".encode()
        return await search(tmp_path, [
            {"data": b"prefix " + needle[:4], "sequence": 40},
            {"data": needle[4:] + b" suffix", "sequence": 41},
        ], needle)

    result = asyncio.run(scenario())
    assert_match(result, "摄像机")


def test_search_matches_shared_hour_archive_after_raw_parts_deleted(tmp_path):
    """同一小时包的两个成员连续扫描，原始子日志已删除仍能跨界搜索。"""
    result = asyncio.run(search(tmp_path, [
        {"data": b"prefix nee", "sequence": 1},
        {"data": b"dle suffix", "sequence": 2},
    ], b"needle", archived=True))
    assert_match(result, "needle")
    assert not list(tmp_path.glob("*.log"))

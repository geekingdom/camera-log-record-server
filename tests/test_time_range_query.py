"""无关键词按接收时间读取日志，二进制区间可精确拼接且不能猜测缺失索引。"""
import base64
import io
import json
import tarfile
from datetime import UTC, datetime

import pytest
from camera_logs.common.models import SearchCreate
from camera_logs.logs.jobs import run_job
from camera_logs.logs.time_range import TimeRangeScan
from test_api import client  # noqa: F401


def test_time_range_query_accepts_no_keyword_and_preserves_bytes():
    assert SearchCreate(taskId="task", start="2026-09-09T00:00:00Z", end="2026-09-09T01:00:00Z").keyword == ""
    data = "设备日志\n".encode() * 2000
    source = b"before" + data + b"after"
    entries = [
        {"offset": 0, "length": 6, "receivedAt": "2026-09-08T23:59:59Z"},
        {"offset": 6, "length": len(data), "receivedAt": "2026-09-09T00:00:00Z"},
        {"offset": 6 + len(data), "length": 5, "receivedAt": "2026-09-09T01:00:00Z"},
    ]
    scanner = TimeRangeScan(datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 9, 1, tzinfo=UTC))
    results = list(scanner.scan(io.BytesIO(source), entries, {"id": "file"}, lambda: False))
    assert b"".join(base64.b64decode(item["data"]) for item in results) == data
    assert results[0]["offset"] == 6
    assert all(item["length"] <= 4096 and item["fileId"] == "file" for item in results)
    assert results[-1]["offset"] + results[-1]["length"] == 6 + len(data)


def test_time_query_refuses_missing_timestamp_index():
    scanner = TimeRangeScan(datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 9, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="索引"):
        list(scanner.scan(io.BytesIO(b"unindexed"), [], {"id": "file"}, lambda: False))


def test_time_query_api_reads_archived_member_and_preserves_bytes(client, tmp_path):  # noqa: F811
    """正式提交、归档外部索引快照和分页结果共同验证，临时文件由pytest回收。"""
    repo = client.app.state.repo
    data = "连续设备打印\n".encode() * 700
    archive, index = tmp_path / "hour.tar.gz", tmp_path / "part.index.jsonl"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("part-000001.log")
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
    stamp = "2026-09-09T10:00:00+00:00"
    index.write_text(json.dumps({"offset": 0, "length": len(data), "receivedAt": stamp}) + "\n")
    client.portal.call(repo.db.tasks.insert_one, {"id": "range-task", "name": "模拟任务", "ip": "192.0.2.1"})
    client.portal.call(repo.db.files.insert_one, {
        "id": "range-file", "taskId": "range-task", "nodeId": "test", "hour": stamp,
        "status": "READY", "bytes": len(data), "path": str(archive), "indexPath": str(index),
        "archiveMember": "part-000001.log",
    })
    response = client.post("/api/v1/log-searches", headers={"Idempotency-Key": "range-query"}, json={
        "taskId": "range-task", "start": stamp, "end": "2026-09-09T11:00:00+00:00",
    })
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    client.portal.call(repo.db.jobs.update_one, {"id": identifier}, {"$set": {"status": "RUNNING"}})
    job = client.portal.call(repo.db.jobs.find_one, {"id": identifier})
    result = client.portal.call(run_job, repo, job)
    assert result["status"] == "SUCCEEDED", result
    assert client.get(f"/api/v1/log-searches/{identifier}").json()["status"] == "SUCCEEDED"
    first = client.get(f"/api/v1/log-searches/{identifier}/results?pageSize=1").json()
    assert first["total"] > 1 and not first["truncated"]
    chunks = [client.get(f"/api/v1/log-searches/{identifier}/results?pageSize=1&page={page}").json()["items"][0]
              for page in range(1, first["total"] + 1)]
    assert b"".join(base64.b64decode(item["data"]) for item in chunks) == data
    assert all(item["fileId"] == "range-file" for item in chunks)
    assert not (tmp_path / "exports" / ".tmp" / identifier).exists()

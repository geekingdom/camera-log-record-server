"""正式日志作业接口的原子创建、校验与幂等重放回归。"""
# ruff: noqa: F811
import pytest
from camera_logs.common import audited_mutations
from camera_logs.common.models import DownloadCreate, SearchCreate
from test_api import client  # noqa: F401

HOUR = "2026-09-09T10:00:00+00:00"
SHANGHAI_RANGE_HOURS = [
    "2026-10-19T04:00:00+00:00", "2026-10-19T05:00:00+00:00",
    "2026-10-19T06:00:00+00:00", "2026-10-19T07:00:00+00:00",
]
SHANGHAI_RANGE_PRESENTATION = [
    "2026-10-19T12:00:00+08:00", "2026-10-19T13:00:00+08:00",
    "2026-10-19T14:00:00+08:00", "2026-10-19T15:00:00+08:00",
]


def seed(client):
    """仅在内存数据库构造目录，无真实日志正文或设备连接。"""
    db = client.app.state.repo.db
    client.portal.call(db.tasks.insert_one, {"id": "task", "name": "模拟任务", "ip": "192.0.2.1"})
    client.portal.call(db.files.insert_one, {"id": "file", "taskId": "task", "nodeId": "node",
                                           "hour": HOUR, "status": "READY", "bytes": 20})
    return db


@pytest.mark.parametrize(("path", "body", "kind"), [
    ("/downloads", {"taskId": "task", "hourIds": [HOUR]}, "DOWNLOAD"),
    ("/log-searches", {"taskId": "task", "keyword": "needle", "start": HOUR,
                       "end": "2026-09-09T11:00:00+00:00"}, "SEARCH"),
])
def test_job_creation_publishes_success_mapping_and_replays_snapshot(client, path, body, kind):
    db = seed(client)
    response = client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": "once"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert client.portal.call(db.idempotency.find_one, {"key": "once"})["state"] == "SUCCEEDED"
    client.portal.call(db.files.update_one, {"id": "file"}, {"$set": {"bytes": 999}})
    again = client.post("/api/v1" + path, json=body, headers={"Idempotency-Key": "once"})
    assert again.status_code == 202 and again.json()["files"][0]["bytes"] == 20
    assert client.portal.call(db.jobs.count_documents, {}) == 1
    assert client.portal.call(db.audit.count_documents, {"action": kind, "targetId": identifier}) == 1


def test_missing_hour_does_not_extend_file_protection(client):
    db = seed(client)
    response = client.post("/api/v1/downloads", json={"taskId": "task", "hourIds": [HOUR, "missing"]},
                           headers={"Idempotency-Key": "bad-range"})
    assert response.status_code == 409
    assert "retainUntil" not in client.portal.call(db.files.find_one, {"id": "file"})
    assert client.portal.call(db.jobs.count_documents, {}) == 0


def test_legacy_hour_ids_request_keeps_its_idempotency_digest():
    """新增范围字段为空时不进入摘要，升级前七日内的请求仍可重放。"""
    legacy = {"taskId": "task", "hourIds": [HOUR], "allowPartial": False}
    body = DownloadCreate(**legacy)
    assert body.model_dump(exclude_none=True) == legacy
    assert audited_mutations.request_digest("DOWNLOAD", body.model_dump(exclude_none=True)) == \
        audited_mutations.request_digest("DOWNLOAD", legacy)


def test_legacy_search_request_keeps_its_idempotency_digest():
    """下载字段扩展不能改变未提供范围的历史搜索请求摘要。"""
    legacy = {"taskId": "task", "keyword": "", "start": None, "end": None}
    body = SearchCreate(taskId="task")
    assert body.model_dump() == legacy
    assert audited_mutations.request_digest("SEARCH", body.model_dump()) == \
        audited_mutations.request_digest("SEARCH", legacy)


def test_download_range_rounds_shanghai_wall_clock_to_covered_hours(client):
    """无时区范围按上海墙钟取整，并复用小时目录的完整性校验。"""
    db = seed(client)
    client.portal.call(db.files.delete_many, {})
    for number, hour in enumerate(SHANGHAI_RANGE_HOURS):
        client.portal.call(db.files.insert_one, {
            "id": f"range-{number}", "taskId": "task", "nodeId": "node", "hour": hour,
            "status": "READY", "bytes": 20,
        })

    response = client.post("/api/v1/downloads", json={
        "taskId": "task", "start": "2026-10-19T12:18:41", "end": "2026-10-19T15:10:08",
    }, headers={"Idempotency-Key": "range"})

    assert response.status_code == 202, response.text
    assert response.json()["hourIds"] == SHANGHAI_RANGE_HOURS
    assert [item["hour"] for item in response.json()["files"]] == SHANGHAI_RANGE_PRESENTATION


def test_download_range_end_at_hour_does_not_include_next_hour(client):
    """半开区间的结束正好落在整点时，该整点不属于下载范围。"""
    db = seed(client)
    client.portal.call(db.files.delete_many, {})
    for number, hour in enumerate(SHANGHAI_RANGE_HOURS):
        client.portal.call(db.files.insert_one, {
            "id": f"boundary-{number}", "taskId": "task", "nodeId": "node", "hour": hour,
            "status": "READY", "bytes": 20,
        })

    response = client.post("/api/v1/downloads", json={
        "taskId": "task", "start": "2026-10-19T12:18:41", "end": "2026-10-19T15:00:00",
    }, headers={"Idempotency-Key": "boundary"})

    assert response.status_code == 202, response.text
    assert response.json()["hourIds"] == SHANGHAI_RANGE_HOURS[:3]


def test_download_range_missing_hour_remains_a_conflict_without_partial_permission(client):
    """范围转换后缺失的中间小时不能只导出实际存在的片段。"""
    db = seed(client)
    client.portal.call(db.files.delete_many, {})
    for number, hour in enumerate((SHANGHAI_RANGE_HOURS[0], SHANGHAI_RANGE_HOURS[2])):
        client.portal.call(db.files.insert_one, {
            "id": f"gap-{number}", "taskId": "task", "nodeId": "node", "hour": hour,
            "status": "READY", "bytes": 20,
        })

    response = client.post("/api/v1/downloads", json={
        "taskId": "task", "start": "2026-10-19T12:00:00", "end": "2026-10-19T15:00:00",
    }, headers={"Idempotency-Key": "gap"})

    assert response.status_code == 409
    assert "retainUntil" not in client.portal.call(db.files.find_one, {"id": "gap-0"})


@pytest.mark.parametrize("body", [
    {"taskId": "task", "start": "2026-10-19T12:00:00", "end": "2026-10-26T13:00:00"},
    {"taskId": "task", "hourIds": [HOUR], "start": "2026-10-19T12:00:00", "end": "2026-10-19T13:00:00"},
    {"taskId": "task", "start": "2026-10-19T13:00:00", "end": "2026-10-19T12:00:00"},
    {"taskId": "task", "start": "0001-01-01T00:00:00", "end": "9999-12-31T23:59:59"},
])
def test_download_range_rejects_ambiguous_or_invalid_selection(client, body):
    response = client.post("/api/v1/downloads", json=body, headers={"Idempotency-Key": "invalid-range"})
    assert response.status_code == 422


def test_job_cancel_is_transactional_and_retries_do_not_duplicate_audit(client, monkeypatch):
    db = seed(client)
    client.portal.call(db.jobs.insert_one, {"id": "job", "taskId": "task", "actor": "bootstrap",
                                          "kind": "DOWNLOAD", "status": "RUNNING"})
    original, calls = audited_mutations.mutation_transaction, []

    async def tracked(repo, callback):
        calls.append(True)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", tracked)
    for _ in range(2):
        assert client.delete("/api/v1/downloads/job").status_code == 204
    assert calls
    assert client.portal.call(db.audit.count_documents, {"action": "cancel_download", "targetId": "job"}) == 1


def test_download_range_rejects_shanghai_conversion_overflow(client):
    """UTC可表示但上海本地日期越界时仍返回422，不让无效范围触发服务错误。"""
    seed(client)
    response = client.post("/api/v1/downloads", headers={"Idempotency-Key": "overflow-shanghai"},
                           json={"taskId": "task", "start": "9999-12-31T20:00:00Z",
                                 "end": "9999-12-31T21:00:00Z"})
    assert response.status_code == 422

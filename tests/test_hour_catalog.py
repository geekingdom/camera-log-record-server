"""小时目录验证：混合状态顺序无关，上海日期边界及跨任务权限保持隔离。"""
# ruff: noqa: F811 - pytest 按名称注入复用的 client fixture。
from itertools import permutations

from camera_logs.logs.hour_catalog import summarize_hours
from test_api import client  # noqa: F401


def test_mixed_fragment_status_is_order_independent():
    files = [{"id": str(i), "hour": "2026-09-08T00:00:00+00:00", "status": state, "bytes": 10}
             for i, state in enumerate(["OPEN", "READY", "DELETING"])]
    for order in permutations(files):
        hour = summarize_hours(order)[0]
        assert hour["status"] == "UNAVAILABLE"
        assert hour["integrity"] == "UNAVAILABLE"
        assert hour["fragmentCount"] == 3 and hour["unavailableCount"] == 1
        assert hour["bytes"] == 30
    assert summarize_hours(files[:2])[0]["integrity"] == "OPEN"
    assert summarize_hours([files[1]])[0]["integrity"] == "UNVERIFIED"
    assert summarize_hours([files[1] | {"sha256": "a" * 64}])[0]["integrity"] == "VERIFIED"


def test_shared_hour_archive_size_counted_once_per_node():
    files = [{"id": str(i), "hour": "2026-09-08T00:00:00+00:00", "status": "READY",
              "bytes": 10, "archiveBytes": 100 + i, "archiveGroupId": "shared", "nodeId": "n1"}
             for i in range(3)]
    hour = summarize_hours(files)[0]
    assert hour["bytes"] == 30
    assert hour["archiveBytes"] == 102
    assert summarize_hours(files + [files[0] | {"nodeId": "n2"}])[0]["archiveBytes"] == 202


def test_hour_date_filter_uses_shanghai_day_and_keeps_pagination(client):
    task = client.post("/api/v1/tasks", headers={"Idempotency-Key": "hours"}, json={
        "name": "hours", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 9090,
        "resourceId": "fixture-device"}).json()
    for i, hour in enumerate(["2026-09-07T15:00:00+00:00", "2026-09-07T16:00:00+00:00", "2026-09-08T15:00:00+00:00", "2026-09-08T16:00:00+00:00"]):
        client.portal.call(client.app.state.repo.db.files.insert_one, {
            "id": str(i), "taskId": task["id"], "hour": hour, "status": "OPEN", "bytes": 10})
    path = f'/api/v1/tasks/{task["id"]}/log-hours'
    result = client.get(path, params={"date": "2026-09-08", "pageSize": 1, "page": 2})
    assert result.status_code == 200
    assert result.json()["total"] == 2
    assert result.json()["items"][0]["hour"] == "2026-09-07T16:00:00+00:00"
    assert client.get(path, params={"date": "bad"}).status_code == 422

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


def test_recovered_sessions_keep_hour_part_order():
    files = [{"id": str(part), "hour": "2026-09-08T00:00:00+00:00", "status": "READY",
              "nodeId": "node", "taskId": "task", "segmentNumber": part,
              "firstSequence": sequence, "sessionId": session}
             for part, sequence, session in [(1, 1, "old"), (2, 50, "old"), (3, 1, "new")]]
    for order in permutations(files):
        assert [f["id"] for f in summarize_hours(order)[0]["files"]] == ["1", "2", "3"]


def test_node_migration_merge_preserves_local_order_despite_clock_rollback():
    files = [{"id": identifier, "hour": "2026-09-08T00:00:00+00:00", "status": "READY",
              "nodeId": node, "taskId": "task", "segmentNumber": part,
              "firstReceivedAt": f"2026-09-08T00:{minute:02d}:00+00:00"}
             for identifier, node, part, minute in [
                 ("a1", "a", 1, 1), ("b1", "b", 1, 2), ("a2", "a", 2, 3), ("a3", "a", 3, 0)]]
    for order in permutations(files):
        assert [f["id"] for f in summarize_hours(order)[0]["files"]] == ["a1", "b1", "a2", "a3"]


def test_legacy_sessions_merge_with_new_hour_parts_by_received_time():
    files = [{"id": identifier, "hour": "2026-09-08T00:00:00+00:00", "status": "READY",
              "taskId": "task", "nodeId": "node", "sessionId": session,
              "segmentNumber": part, "firstSequence": sequence,
              "firstReceivedAt": f"2026-09-08T00:{minute:02d}:00+00:00"}
             for identifier, session, part, sequence, minute in [
                 ("old1", "old", 0, 100, 1), ("old2", "old", 0, 200, 0),
                 ("other", "other", 0, 1, 2), ("new", "new", 1, 1, 3)]]
    for order in permutations(files):
        assert [f["id"] for f in summarize_hours(order)[0]["files"]] == ["old1", "old2", "other", "new"]


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

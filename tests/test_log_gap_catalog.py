"""未知实时缺口只能按已保存文件目录定位，不能补造采集源端正文。"""

from camera_logs.common.security import actor
from test_api import client  # noqa: F401


def _file(identifier, node, part, size):
    return {"id": identifier, "taskId": "gap-task", "nodeId": node, "runId": "run",
        "sessionId": "session", "status": "READY", "bytes": size,
            "hour": "2026-09-14T00:00:00+00:00", "segmentNumber": part,
            "firstReceivedAt": f"2026-09-14T00:0{part}:00+00:00"}


def _query(**extra):
    values = {"beforeFileId": "first", "beforeOffset": 3, "beforeSessionId": "session",
              "afterFileId": "last", "afterOffset": 4, "afterSessionId": "session", "limit": 2}
    values.update(extra)
    return values


def test_gap_catalog_authorizes_and_pages_cross_node_saved_fragments(client):  # noqa: F811
    """跨节点分卷只返回锚点之间的目录字节，续页仍绑定原任务和锚点。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "gap-task", "name": "缺口任务", "ip": "192.0.2.1"})
    client.portal.call(repo.db.files.insert_many, [_file("first", "node-a", 1, 10), _file("middle", "node-b", 2, 8), _file("last", "node-a", 3, 9),
                                                     {**_file("other", "node-a", 4, 12), "taskId": "other-task"}])
    response = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query())
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["items"] == [{"fileId": "first", "sessionId": "session", "start": 3, "end": 10},
                               {"fileId": "middle", "sessionId": "session", "start": 0, "end": 8}]
    assert first["nextCursor"]
    second = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(cursor=first["nextCursor"]))
    assert second.json() == {"items": [{"fileId": "last", "sessionId": "session", "start": 0, "end": 4}], "nextCursor": None, "unrecoverable": []}
    client.app.dependency_overrides[actor] = lambda: {"id": "limited", "scopes": ["tasks:read"], "isAdmin": False}
    try:
        assert client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query()).status_code == 403
    finally:
        client.app.dependency_overrides.clear()


def test_gap_catalog_reports_deleted_middle_file_after_pagination(client):  # noqa: F811
    """中间目录已删除时明确报告不可补读，续页不能丢失首次快照的问题。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "gap-task", "name": "缺口任务", "ip": "192.0.2.1"})
    client.portal.call(repo.db.files.insert_many, [
        _file("first", "node-a", 1, 10),
        {**_file("deleted-middle", "node-b", 2, 8), "status": "DELETED"},
        _file("last", "node-c", 3, 9),
    ])

    first = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(limit=1)).json()
    assert first["items"] == [{"fileId": "first", "sessionId": "session", "start": 3, "end": 10}]
    assert first["unrecoverable"] == [{
        "reason": "FILE_UNAVAILABLE",
        "message": "文件 deleted-middle 已删除或缺少可靠字节水位，无法补读。",
    }]
    assert first["nextCursor"]

    second = client.get(
        "/api/v1/tasks/gap-task/log-gap-catalog", params=_query(limit=1, cursor=first["nextCursor"])
    )
    assert second.json() == {
        "items": [{"fileId": "last", "sessionId": "session", "start": 0, "end": 4}],
        "nextCursor": None,
        "unrecoverable": first["unrecoverable"],
    }


def test_gap_catalog_rejects_unreliable_sessions_offsets_and_cursor(client):  # noqa: F811
    """跨会话只补已保存目录，超出确认水位和替换锚点不得产生可读范围。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "gap-task", "name": "缺口任务", "ip": "192.0.2.1"})
    client.portal.call(repo.db.files.insert_many, [_file("first", "node-a", 1, 10), _file("last", "node-b", 2, 9)])
    client.portal.call(repo.db.files.update_one, {"id": "last"}, {"$set": {"sessionId": "new"}})
    changed = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(afterSessionId="new"))
    assert changed.json()["items"] == [
        {"fileId": "first", "sessionId": "session", "start": 3, "end": 10},
        {"fileId": "last", "sessionId": "new", "start": 0, "end": 4},
    ]
    watermark = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(afterOffset=10, afterSessionId="new"))
    assert watermark.json()["unrecoverable"][0]["reason"] == "WATERMARK_NOT_READY"
    mismatch = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query()).json()
    assert mismatch["unrecoverable"][0]["reason"] == "SESSION_MISMATCH"
    page = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(limit=1, afterSessionId="new")).json()
    assert len(page["nextCursor"]) == 32
    invalid = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(beforeOffset=4, cursor=page["nextCursor"]))
    assert invalid.status_code == 422
    forged = page["nextCursor"][:-1] + ("A" if page["nextCursor"][-1] != "A" else "B")
    assert client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(cursor=forged)).status_code == 422
    client.portal.call(repo.db.files.update_one, {"id": "last"}, {"$set": {"hour": "2026-09-16T00:00:00+00:00"}})
    broad = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(afterSessionId="new"))
    assert broad.json()["unrecoverable"][0]["reason"] == "CATALOG_TOO_BROAD"
    cross_task = client.get("/api/v1/tasks/gap-task/log-gap-catalog", params=_query(afterFileId="other-file", afterSessionId="new"))
    assert cross_task.json()["unrecoverable"][0]["reason"] == "CATALOG_UNAVAILABLE"

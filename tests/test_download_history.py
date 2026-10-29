"""任务下载历史、制品到期快照和跨节点重复下载的 API 回归。"""
# ruff: noqa: F811

from datetime import timedelta

from camera_logs.common.database import now
from camera_logs.logs.download_availability import artifact_retention
from fastapi.responses import JSONResponse
from test_api import client  # noqa: F401


def _job(identifier, task_id, created_at, **changes):
    """创建已关闭的下载制品夹具，避免测试暴露或依赖真实节点路径。"""
    return {
        "id": identifier,
        "kind": "DOWNLOAD",
        "taskId": task_id,
        "nodeId": "remote-node",
        "status": "SUCCEEDED",
        "createdAt": created_at,
        "artifactCreatedAt": created_at,
        "artifactRetentionDays": 7,
        "expiresAt": created_at + timedelta(days=7),
        "resultPath": f"/not-public/{identifier}.tar.gz",
        "bytes": 321,
        "outputExecutionState": "CLOSED",
        "outputWriterNodeId": "remote-node",
        "outputCleanupState": "IDLE",
    } | changes


def test_task_download_history_pages_only_download_jobs_and_exposes_safe_availability(client):
    """历史列表按新近创建稳定分页，不能混入检索作业或泄露节点文件路径。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "history-task", "name": "下载历史任务"})
    stamp = now()
    client.portal.call(repo.db.jobs.insert_many, [
        _job("old", "history-task", stamp - timedelta(minutes=2)),
        _job("new", "history-task", stamp - timedelta(minutes=1), files=[{"id": "large-frozen-directory"}]),
        _job("search", "history-task", stamp, kind="SEARCH", resultPath="/not-public/search"),
        _job("other", "other-task", stamp),
    ])

    first = client.get("/api/v1/tasks/history-task/downloads?page=1&pageSize=1")
    second = client.get("/api/v1/tasks/history-task/downloads?page=2&pageSize=1")

    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == 2
    assert [first.json()["items"][0]["id"], second.json()["items"][0]["id"]] == ["new", "old"]
    item = first.json()["items"][0]
    assert item["bytes"] == 321 and item["downloadAvailable"] is True
    assert "resultPath" not in item and "outputReaders" not in item and "files" not in item


def test_download_history_exposes_creator_snapshot_and_discrete_artifact_log_segments(client):
    """历史行展示创建人和实际进入制品的离散分卷，不能用首尾伪造连续日志范围。"""
    repo = client.app.state.repo
    stamp = now()
    client.portal.call(repo.db.tasks.insert_one, {"id": "metadata-task", "name": "制品元数据任务"})
    client.portal.call(repo.db.jobs.insert_many, [
        _job("recorded", "metadata-task", stamp,
             createdBy="operator-id", createdByName="值班操作员",
             artifactLogSegments=[
                 {"fileId": "part-01", "hour": "2026-10-29T00:00:00+00:00",
                  "firstReceivedAt": "2026-10-29T00:02:03+00:00", "lastReceivedAt": "2026-10-29T00:08:09+00:00"},
                 {"fileId": "part-03", "hour": "2026-10-29T02:00:00+00:00",
                  "firstReceivedAt": "2026-10-29T02:30:00+00:00", "lastReceivedAt": "2026-10-29T02:31:00+00:00"},
             ]),
        _job("legacy", "metadata-task", stamp - timedelta(minutes=1)),
    ])

    response = client.get("/api/v1/tasks/metadata-task/downloads")

    assert response.status_code == 200
    recorded, legacy = response.json()["items"]
    assert recorded["createdBy"] == "operator-id"
    assert recorded["createdByName"] == "值班操作员"
    assert recorded["artifactLogSegments"] == [
        {"fileId": "part-01", "hour": "2026-10-29T08:00:00+08:00",
         "firstReceivedAt": "2026-10-29T08:02:03+08:00", "lastReceivedAt": "2026-10-29T08:08:09+08:00"},
        {"fileId": "part-03", "hour": "2026-10-29T10:00:00+08:00",
         "firstReceivedAt": "2026-10-29T10:30:00+08:00", "lastReceivedAt": "2026-10-29T10:31:00+08:00"},
    ]
    assert legacy.get("createdBy") is None
    assert legacy.get("createdByName") is None
    assert legacy.get("artifactLogSegments") is None


def test_expired_artifact_is_not_listed_as_available_or_downloadable(client, monkeypatch):
    """到期制品不能再签浏览器票据或跨 Worker 代理，历史行仍保留可审计。"""
    from camera_logs.logs import api

    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "expired-task", "name": "过期下载任务"})
    client.portal.call(repo.db.jobs.insert_one, _job("expired", "expired-task", now() - timedelta(days=8)))
    proxied = []

    async def proxy(_repo, node_id, path, *_args):
        proxied.append((node_id, path))
        return JSONResponse({"download": "ok"})

    monkeypatch.setattr(api, "proxy_file", proxy)
    history = client.get("/api/v1/tasks/expired-task/downloads")
    content = client.get("/api/v1/downloads/expired/content")
    ticket = client.post("/api/v1/downloads/expired/browser-session")

    assert history.status_code == 200 and history.json()["items"][0]["downloadAvailable"] is False
    assert content.status_code == ticket.status_code == 410
    assert proxied == []


def test_valid_artifact_is_proxied_from_its_owner_node(client, monkeypatch):
    """重复下载仍由产物所属 Worker 提供，API 不把远端制品误读为本地文件。"""
    from camera_logs.logs import api

    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {"id": "remote-task", "name": "远端下载任务"})
    client.portal.call(repo.db.jobs.insert_one, _job("remote", "remote-task", now()))
    calls = []

    async def proxy(_repo, node_id, path, *_args):
        calls.append((node_id, path))
        return JSONResponse({"download": "ok"})

    monkeypatch.setattr(api, "proxy_file", proxy)
    response = client.get("/api/v1/downloads/remote/content")

    assert response.status_code == 200 and response.json() == {"download": "ok"}
    assert calls == [("remote-node", "/internal/downloads/remote")]


def test_artifact_retention_snapshots_current_log_retention_days(client):
    """已生成制品的到期时间固定，管理员之后修改日志保留期不回写旧下载。"""
    repo = client.app.state.repo
    created = now()
    client.portal.call(repo.db.platform_settings.insert_one, {"id": "platform", "retentionDays": 3})
    first = client.portal.call(artifact_retention, repo, created)
    client.portal.call(repo.db.platform_settings.update_one, {"id": "platform"}, {"$set": {"retentionDays": 30}})

    assert first == {
        "artifactCreatedAt": created,
        "artifactRetentionDays": 3,
        "expiresAt": created + timedelta(days=3),
    }


def test_history_bounds_segment_preview_and_keeps_actor_for_legacy_creator(client):
    """数千分卷不进入普通列表；详情仍完整，历史actor可作为可靠创建人标识。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.tasks.insert_one, {'id': 'bounded-task'})
    segments = [{'fileId': f'part-{index}', 'hour': '2026-10-29T00:00:00+00:00',
                 'firstReceivedAt': None, 'lastReceivedAt': None} for index in range(120)]
    client.portal.call(repo.db.jobs.insert_one, _job('bounded', 'bounded-task', now(),
        actor='historical-user-id', artifactLogSegments=segments, artifactLogSegmentCount=len(segments)))
    response = client.get('/api/v1/tasks/bounded-task/downloads')
    assert response.status_code == 200, response.text
    record = response.json()['items'][0]
    assert record['createdBy'] == 'historical-user-id'
    assert record['artifactLogSegmentCount'] == 120
    assert len(record['artifactLogSegments']) == 8
    detail = client.get('/api/v1/downloads/bounded')
    assert detail.status_code == 200, detail.text
    assert len(detail.json()['artifactLogSegments']) == 120

"""资源看板聚合接口的权限、上海日期和目录统计回归。"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture
def dashboard_client(tmp_path):
    """创建不启动后台任务的独立看板 API，避免测试受实时采集影响。"""
    settings = Settings(
        bootstrap_token="dashboard-admin",
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path,
        node_id="dashboard-test",
        start_background=False,
    )
    with TestClient(create_app(settings, AsyncMongoMockClient().camera_logs), client=("203.0.113.7", 45100)) as client:
        yield client


def _seed_dashboard_data(client):
    """写入同日归档成员、跨日目录和可见 core，验证看板不读取正文。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.resources.insert_many, [
        {"id": "camera-a", "name": "大厅", "ip": "192.0.2.10", "deletedAt": None,
         "healthStatus": "ONLINE"},
        {"id": "camera-b", "name": "走廊", "ip": "192.0.2.11", "deletedAt": None,
         "healthStatus": "OFFLINE"},
    ])
    client.portal.call(repo.db.tasks.insert_many, [
        {"id": "task-a", "resourceId": "camera-a", "desiredState": "RUNNING", "status": "COLLECTING"},
        {"id": "task-b", "resourceId": "camera-b", "desiredState": "STOPPED", "status": "STOPPED"},
    ])
    client.portal.call(repo.db.files.insert_many, [
        {"id": "a-first", "taskId": "task-a", "hour": "2026-10-12T00:10:00+00:00", "status": "READY",
         "bytes": 100, "archiveGroupId": "shared-hour", "archiveBytes": 20},
        {"id": "a-second", "taskId": "task-a", "hour": "2026-10-12T01:10:00+00:00", "status": "READY",
         "bytes": 50, "archiveGroupId": "shared-hour", "archiveBytes": 20},
        {"id": "b-open", "taskId": "task-b", "hour": "2026-10-11T15:00:00+00:00", "status": "OPEN", "bytes": 40},
        {"id": "deleted", "taskId": "task-a", "hour": "2026-10-12T01:10:00+00:00", "status": "DELETED", "bytes": 999},
    ])
    client.portal.call(repo.db.coredump_files.insert_many, [
        {"id": "core-a", "resourceId": "camera-a", "name": "core.1", "receivedAt": datetime(2026, 10, 12, 2, tzinfo=UTC), "size": 70},
        {"id": "core-b", "resourceId": "camera-a", "name": "core.2", "receivedAt": datetime(2026, 10, 12, 3, tzinfo=UTC), "size": 30},
        {"id": "flag", "resourceId": "camera-b", "name": "coredump_flag.cdf", "receivedAt": datetime(2026, 10, 12, 3, tzinfo=UTC), "size": 99},
    ])
    client.portal.call(repo.db.nodes.insert_one, {
        "id": "node-a", "name": "节点A", "heartbeat": now(), "accepting": True, "activeTasks": 1,
        "capacity": 4, "telemetry": {"sampledAt": now(), "networkUploadBytesPerSecond": 12,
                                      "networkDownloadBytesPerSecond": 3, "cpuPercent": 10, "memoryPercent": 10},
        "diskPercent": 10, "writeLatencyMs": 1,
    })


def test_dashboard_uses_shanghai_days_and_raw_segment_bytes(dashboard_client):
    """共享 tar 的两个原始分卷仍各计一次，上海日窗口补齐空日。"""
    _seed_dashboard_data(dashboard_client)
    response = dashboard_client.get("/api/v1/dashboard?days=7&date=2026-10-12",
                                    headers={"Authorization": "Bearer dashboard-admin"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["date"] == "2026-10-12"
    assert body["generatedAt"].endswith("+00:00")
    assert body["range"] == {"startDate": "2026-10-06", "endDate": "2026-10-12"}
    assert len(body["daily"]) == 7
    assert body["daily"][-1] == {"date": "2026-10-12", "bytes": 150, "fileCount": 2}
    assert body["topResources"] == [{"resourceId": "camera-a", "name": "大厅", "ip": "192.0.2.10",
                                      "bytes": 150, "fileCount": 2}]
    assert body["coredumps"] == {"resourceCount": 1, "fileCount": 2, "bytes": 100, "items": [{
        "resourceId": "camera-a", "name": "大厅", "ip": "192.0.2.10", "fileCount": 2, "bytes": 100,
        "lastReceivedAt": "2026-10-12T03:00:00+00:00",
    }], "truncated": False}
    assert body["overview"] == {"resources": 2, "onlineResources": 1, "collectingTasks": 1,
                                 "tasks": 2, "nodes": 1}
    assert body["nodes"] == [{"id": "node-a", "name": "节点A", "healthLevel": "HEALTHY", "reasons": [], "accepting": True,
                               "activeTasks": 1, "capacity": 4, "inputBytesPerSecond": None,
                               "lastHeartbeat": body["nodes"][0]["lastHeartbeat"]}]


def test_dashboard_allows_task_read_without_log_read_but_marks_statistics_unavailable(dashboard_client):
    """导航所需的任务概览不能因日志权限缺失失败，也不能把不可见日志显示为零。"""
    _seed_dashboard_data(dashboard_client)
    repo = dashboard_client.app.state.repo
    token = "dashboard-reader-token"
    client = dashboard_client
    client.portal.call(repo.db.users.insert_one, {"id": "reader", "username": "reader", "displayName": "只读", "isAdmin": False,
                                                   "scopes": [], "enabled": True, "authVersion": 1})
    client.portal.call(repo.db.tokens.insert_one, {"id": "reader-token", "userId": "reader", "tokenHash": sha256(token.encode()).hexdigest(),
                                                    "revoked": False, "expiresAt": now() + timedelta(hours=1)})
    client.portal.call(repo.db.ip_policy.insert_one, {"id": "platform-ip-policy", "enabled": True,
                                                       "rules": [{"label": "看板只读", "network": "203.0.113.7/32", "scopes": ["tasks:read"]}]})

    response = client.get("/api/v1/dashboard?date=2026-10-12", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["overview"] == {"resources": 2, "onlineResources": 1, "collectingTasks": 1,
                                 "tasks": 2, "nodes": None}
    assert body["availability"] == {"logs": False, "nodes": False}
    assert body["daily"] is None and body["topResources"] is None and body["coredumps"] is None
    assert body["nodes"] is None


def test_dashboard_excludes_deleted_current_objects_but_keeps_historical_log_resource(dashboard_client):
    """资源/任务软删除后不计当前概览，但原有目录统计不能被无声丢弃。"""
    _seed_dashboard_data(dashboard_client)
    repo = dashboard_client.app.state.repo
    client = dashboard_client
    client.portal.call(repo.db.resources.insert_one, {"id": "deleted-camera", "name": "旧设备", "ip": "192.0.2.12",
                                                       "deletedAt": now(), "healthStatus": "ONLINE"})
    client.portal.call(repo.db.tasks.insert_one, {"id": "deleted-task", "resourceId": "deleted-camera", "deletedAt": now(),
                                                   "desiredState": "STOPPED", "status": "STOPPED"})
    client.portal.call(repo.db.files.insert_one, {"id": "deleted-history", "taskId": "deleted-task",
                                                   "hour": "2026-10-12T02:00:00+00:00", "status": "READY", "bytes": 500})

    body = client.get("/api/v1/dashboard?date=2026-10-12", headers={"Authorization": "Bearer dashboard-admin"}).json()

    assert body["overview"] == {"resources": 2, "onlineResources": 1, "collectingTasks": 1,
                                 "tasks": 2, "nodes": 1}
    assert body["topResources"][0] == {"resourceId": "deleted-camera", "name": "旧设备", "ip": "192.0.2.12",
                                      "bytes": 500, "fileCount": 1}


def test_dashboard_excludes_resource_deleted_task_from_current_overview(dashboard_client):
    """资源软删除以任务标记收尾，不会写 task.deletedAt，当前概览必须排除它。"""
    _seed_dashboard_data(dashboard_client)
    repo = dashboard_client.app.state.repo
    dashboard_client.portal.call(repo.db.tasks.update_one, {"id": "task-a"}, {"$set": {"resourceDeleted": True}})

    body = dashboard_client.get("/api/v1/dashboard?date=2026-10-12",
                                headers={"Authorization": "Bearer dashboard-admin"}).json()

    assert body["overview"] == {"resources": 2, "onlineResources": 1, "collectingTasks": 0,
                                 "tasks": 1, "nodes": 1}
    assert body["topResources"] == [{"resourceId": "camera-a", "name": "大厅", "ip": "192.0.2.10",
                                      "bytes": 150, "fileCount": 2}]


def test_dashboard_coalesces_same_permission_snapshot_requests(dashboard_client, monkeypatch):
    """同窗口重复刷新复用短期成功快照，避免多个页面并发扫描全量任务和目录。"""
    from camera_logs.dashboard import api

    _seed_dashboard_data(dashboard_client)
    original = api._dashboard_payload
    calls = 0

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "_dashboard_payload", counted)
    headers = {"Authorization": "Bearer dashboard-admin"}
    first = dashboard_client.get("/api/v1/dashboard?date=2026-10-12", headers=headers)
    second = dashboard_client.get("/api/v1/dashboard?date=2026-10-12", headers=headers)

    assert first.status_code == second.status_code == 200
    assert calls == 1


async def test_dashboard_coalesces_concurrent_same_permission_requests(monkeypatch):
    """同一权限窗口的并发请求只能创建一个聚合生产者。"""
    from camera_logs.dashboard import api

    app = SimpleNamespace(state=SimpleNamespace(dashboard_cache={}, repo=SimpleNamespace()))
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"producer": calls}

    monkeypatch.setattr(api, "_dashboard_payload", blocked)
    arguments = {"days": 7, "selected": date(2026, 10, 12), "lower": now(),
                 "upper": now(), "start_day": date(2026, 10, 6)}
    user = {"scopes": ["tasks:read", "logs:read"], "isAdmin": False}
    first = asyncio.create_task(api._cached_dashboard_payload(app, user, **arguments))
    await started.wait()
    second = asyncio.create_task(api._cached_dashboard_payload(app, user, **arguments))
    await asyncio.sleep(0)

    assert calls == 1
    release.set()
    assert await first == await second == {"producer": 1}


async def test_dashboard_cache_separates_log_and_administrator_permissions(monkeypatch):
    """管理员可见的节点和日志统计不能复用给仅任务读取用户。"""
    from camera_logs.dashboard import api

    app = SimpleNamespace(state=SimpleNamespace(dashboard_cache={}, repo=SimpleNamespace()))
    calls = []

    async def payload(_repo, user, **_arguments):
        calls.append(user)
        return {"logs": "logs:read" in user["scopes"], "nodes": user["isAdmin"]}

    monkeypatch.setattr(api, "_dashboard_payload", payload)
    arguments = {"days": 7, "selected": date(2026, 10, 12), "lower": now(),
                 "upper": now(), "start_day": date(2026, 10, 6)}
    admin = {"scopes": ["tasks:read", "logs:read"], "isAdmin": True}
    reader = {"scopes": ["tasks:read"], "isAdmin": False}

    assert await api._cached_dashboard_payload(app, admin, **arguments) == {"logs": True, "nodes": True}
    assert await api._cached_dashboard_payload(app, reader, **arguments) == {"logs": False, "nodes": False}
    assert len(calls) == 2
    assert len(app.state.dashboard_cache) == 2


async def test_dashboard_cache_removes_timed_out_task_before_retry(monkeypatch):
    """共享聚合超时后必须移除失败任务，使下一次刷新可以重新计算。"""
    from camera_logs.dashboard import api

    app = SimpleNamespace(state=SimpleNamespace(dashboard_cache={}, repo=SimpleNamespace()))
    attempts = 0

    async def blocked(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "_REQUEST_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(api, "_dashboard_payload", blocked)
    arguments = {"days": 7, "selected": date(2026, 10, 12), "lower": now(),
                 "upper": now(), "start_day": date(2026, 10, 6)}
    user = {"scopes": ["tasks:read"], "isAdmin": False}

    with pytest.raises(TimeoutError):
        await api._cached_dashboard_payload(app, user, **arguments)
    await asyncio.sleep(0)
    assert app.state.dashboard_cache == {}

    async def recovered(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return {"recovered": True}

    monkeypatch.setattr(api, "_dashboard_payload", recovered)
    assert await api._cached_dashboard_payload(app, user, **arguments) == {"recovered": True}
    assert attempts == 2


async def test_dashboard_cache_keeps_capacity_when_all_entries_are_active(monkeypatch):
    """缓存已被进行中的聚合占满时，新窗口直算且不能越过容量上限。"""
    from camera_logs.dashboard import api

    app = SimpleNamespace(state=SimpleNamespace(dashboard_cache={}, repo=SimpleNamespace()))
    first_started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def payload(_repo, _user, *, selected, **_arguments):
        calls.append(selected.isoformat())
        if selected.isoformat() == "2026-10-12":
            first_started.set()
            await release.wait()
        return {"date": selected.isoformat()}

    monkeypatch.setattr(api, "_MAX_CACHE_ENTRIES", 1)
    monkeypatch.setattr(api, "_dashboard_payload", payload)
    user = {"scopes": ["tasks:read"], "isAdmin": False}

    def arguments(selected):
        return {"days": 7, "selected": selected, "lower": now(), "upper": now(),
                "start_day": selected - timedelta(days=6)}

    first = asyncio.create_task(api._cached_dashboard_payload(app, user, **arguments(date(2026, 10, 12))))
    await first_started.wait()
    second = await api._cached_dashboard_payload(app, user, **arguments(date(2026, 10, 13)))

    assert second == {"date": "2026-10-13"}
    assert len(app.state.dashboard_cache) == 1
    assert calls == ["2026-10-12", "2026-10-13"]
    release.set()
    assert await first == {"date": "2026-10-12"}


async def test_dashboard_cache_shutdown_cancels_pending_tasks_and_clears_entries():
    """应用退出时取消未完成看板聚合，不能让缓存任务持有即将关闭的数据库。"""
    from camera_logs.dashboard import api

    cancelled = asyncio.Event()

    async def pending():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(pending())
    await asyncio.sleep(0)
    app = SimpleNamespace(state=SimpleNamespace(dashboard_cache={
        (7, "2026-10-12", False, False): api._CacheEntry(float("inf"), task),
    }))

    await api.shutdown_dashboard_cache(app)

    assert task.cancelled()
    assert cancelled.is_set()
    assert app.state.dashboard_cache == {}


async def test_dashboard_lifespan_runs_cache_cleanup_before_closing_client(tmp_path, monkeypatch):
    """API 生命周期退出服务阶段时必须执行看板缓存收尾。"""
    from camera_logs import main
    from camera_logs.common import observability
    from camera_logs.dashboard import api

    settings = Settings(
        bootstrap_token="dashboard-lifecycle",
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path,
        start_background=False,
    )
    repo = SimpleNamespace(settings=SimpleNamespace(), initialize=AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "Repository", lambda *_args: repo)
    monkeypatch.setattr(observability, "setup_logging", lambda *_args: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(observability, "refresh_service_log_retention", AsyncMock())
    monkeypatch.setattr("camera_logs.users.sessions.initialize_admin", AsyncMock())
    monkeypatch.setattr(api, "shutdown_dashboard_cache", cleanup)
    app = main.create_app(settings, db=SimpleNamespace())

    async with app.router.lifespan_context(app):
        assert app.state.repo is repo

    cleanup.assert_awaited_once_with(app)


def test_dashboard_does_not_mark_stale_node_as_accepting(dashboard_client):
    """worker 的旧准入标记不改变超过心跳窗口后的离线事实。"""
    _seed_dashboard_data(dashboard_client)
    repo = dashboard_client.app.state.repo
    dashboard_client.portal.call(repo.db.nodes.update_one, {"id": "node-a"},
                                 {"$set": {"heartbeat": now() - timedelta(minutes=1)}})

    body = dashboard_client.get("/api/v1/dashboard?date=2026-10-12",
                                headers={"Authorization": "Bearer dashboard-admin"}).json()

    assert body["nodes"][0]["healthLevel"] == "OFFLINE"
    assert body["nodes"][0]["accepting"] is False


def test_dashboard_returns_retryable_error_when_the_bounded_query_times_out(dashboard_client, monkeypatch):
    """目录或数据库失去响应时，总请求预算必须结束并给调用方可重试状态。"""
    from camera_logs.dashboard import api

    async def blocked(*_args, **_kwargs):
        await asyncio.sleep(.05)

    monkeypatch.setattr(api, "_REQUEST_TIMEOUT_SECONDS", .001)
    monkeypatch.setattr(api, "_dashboard_payload", blocked)

    response = dashboard_client.get("/api/v1/dashboard", headers={"Authorization": "Bearer dashboard-admin"})

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "资源看板统计超时，请稍后重试"


@pytest.mark.parametrize("query", ["days=8", "date=2099-10-13", "days=30&date=0001-01-01"])
def test_dashboard_rejects_invalid_window_and_future_date(dashboard_client, query):
    """只允许固定窗口且选定日不可晚于上海今天，避免无界或误导性查询。"""
    response = dashboard_client.get(f"/api/v1/dashboard?{query}", headers={"Authorization": "Bearer dashboard-admin"})
    assert response.status_code == 422

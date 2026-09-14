"""验证管理事件列表只展示记录可证实的来源，不从任务当前归属反推历史操作者。"""

from datetime import UTC, datetime, timedelta

pytest_plugins = ("test_api",)


def test_event_api_presents_verified_user_system_and_service_token_sources(client):
    """审计记录优先显示真实用户，系统和服务账号保留各自可追溯身份。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.users.insert_one, {
        "id": "operator", "username": "operator-login", "displayName": "值班操作员",
    })
    client.portal.call(repo.db.tokens.insert_one, {"id": "service-token", "name": "巡检服务账号"})
    client.portal.call(repo.db.audit.insert_many, [
        {"actor": "operator", "action": "edit_task", "clientIp": "198.51.100.5", "createdAt": stamp},
        {"actor": "system", "action": "job_succeeded", "createdAt": stamp + timedelta(seconds=1)},
        {"serviceTokenId": "service-token", "action": "DOWNLOAD", "createdAt": stamp + timedelta(seconds=2)},
        {"actor": "anonymous", "action": "login_failed", "clientIp": "198.51.100.6", "createdAt": stamp + timedelta(seconds=3)},
        {"actor": "bootstrap", "action": "create_user", "createdAt": stamp + timedelta(seconds=4)},
    ])

    response = client.get("/api/v1/audit-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00",
    })

    assert response.status_code == 200, response.text
    rows = {item["action"]: item for item in response.json()["items"]}
    assert {"sourceKind": "USER", "sourceName": "值班操作员", "sourceDetail": "用户 ID：operator；来源 IP：198.51.100.5"}.items() <= rows["edit_task"].items()
    assert rows["edit_task"]["actorName"] == "值班操作员"
    assert {"sourceKind": "SYSTEM", "sourceName": "平台后台", "sourceDetail": "后台自动处理（无客户端地址）"}.items() <= rows["job_succeeded"].items()
    assert "actorName" not in rows["job_succeeded"]
    assert {"sourceKind": "SERVICE_TOKEN", "sourceName": "巡检服务账号", "sourceDetail": "服务账号 ID：service-token"}.items() <= rows["DOWNLOAD"].items()
    assert "actorName" not in rows["DOWNLOAD"]
    assert {"sourceKind": "ANONYMOUS", "sourceName": "未认证身份", "sourceDetail": "来源 IP：198.51.100.6"}.items() <= rows["login_failed"].items()
    assert {"sourceKind": "BOOTSTRAP", "sourceName": "平台引导服务账号"}.items() <= rows["create_user"].items()


def test_event_api_presents_runtime_node_task_anonymous_http_and_historical_sources(client):
    """无 actor 时仅按事件记录的节点、任务、HTTP 来源或缺失状态说明来源。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.nodes.insert_one, {"id": "collector-a", "name": "采集节点 A"})
    # task 的 createdBy 故意存在，断言运行事件不会把它展示成操作者。
    client.portal.call(repo.db.tasks.insert_one, {
        "id": "runtime-task", "name": "夜间采集", "ip": "192.0.2.8", "createdBy": "unrelated-user",
    })
    client.portal.call(repo.db.events.insert_many, [
        {"type": "CONNECTION_GAP", "nodeId": "collector-a", "taskId": "runtime-task", "createdAt": stamp},
        {"type": "USER_PAUSED", "taskId": "runtime-task", "createdAt": stamp + timedelta(seconds=1)},
        {"type": "DISK_PRESSURE_CHANGED", "createdAt": stamp + timedelta(seconds=2)},
    ])
    client.portal.call(repo.db.request_events.insert_many, [
        {"method": "POST", "route": "/api/v1/tasks", "httpStatus": 401, "clientIp": "198.51.100.4", "createdAt": stamp},
        {"method": "GET", "route": "/api/v1/legacy", "createdAt": stamp + timedelta(seconds=1)},
    ])

    runtime = client.get("/api/v1/runtime-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00",
    })
    request = client.get("/api/v1/request-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00",
    })

    assert runtime.status_code == request.status_code == 200
    runtime_rows = {item["type"]: item for item in runtime.json()["items"]}
    assert {"sourceKind": "NODE", "sourceName": "采集节点：采集节点 A", "sourceDetail": "节点 ID：collector-a"}.items() <= runtime_rows["CONNECTION_GAP"].items()
    assert {"sourceKind": "TASK_RUNTIME", "sourceName": "任务运行事件", "sourceDetail": "任务：夜间采集"}.items() <= runtime_rows["USER_PAUSED"].items()
    assert {"sourceKind": "SYSTEM_RUNTIME", "sourceName": "系统运行事件", "sourceDetail": "后台自动处理（无客户端地址）"}.items() <= runtime_rows["DISK_PRESSURE_CHANGED"].items()
    assert all("actorName" not in row for row in runtime_rows.values())
    assert "unrelated-user" not in str(runtime_rows["USER_PAUSED"])
    request_rows = {item["route"]: item for item in request.json()["items"]}
    assert {"sourceKind": "UNAUTHENTICATED_HTTP", "sourceName": "认证未通过的请求", "sourceDetail": "来源 IP：198.51.100.4"}.items() <= request_rows["/api/v1/tasks"].items()
    assert {"sourceKind": "REQUEST_IDENTITY_MISSING", "sourceName": "请求来源（未记录身份）"}.items() <= request_rows["/api/v1/legacy"].items()


def test_runtime_event_api_presents_debug_and_coredump_phases_with_matching_outcomes(client):
    """调试与核心转储阶段必须给出可行动摘要，列表结果与派生筛选保持一致。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 9, 14, 8, tzinfo=UTC)
    client.portal.call(repo.db.events.insert_many, [
        {"type": "DEBUG_MODE", "phase": "ALREADY_ASH", "createdAt": stamp},
        {"type": "DEBUG_MODE", "phase": "STARTED", "createdAt": stamp + timedelta(seconds=1)},
        {"type": "DEBUG_MODE", "phase": "CHALLENGE_RECEIVED", "createdAt": stamp + timedelta(seconds=2)},
        {"type": "DEBUG_MODE", "phase": "ASH_READY", "createdAt": stamp + timedelta(seconds=3)},
        {"type": "DEBUG_MODE", "phase": "FAILED", "debugError": "switch failed", "createdAt": stamp + timedelta(seconds=4)},
        {"type": "COREDUMP_MOUNT", "status": "MOUNTED", "createdAt": stamp + timedelta(seconds=5)},
        {"type": "COREDUMP_MOUNT", "status": "REMOUNTING", "createdAt": stamp + timedelta(seconds=6)},
        {"type": "COREDUMP_MOUNT", "status": "FAILED", "createdAt": stamp + timedelta(seconds=7)},
    ])

    response = client.get("/api/v1/runtime-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00",
    })
    pending = client.get("/api/v1/runtime-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00", "outcome": "PENDING",
    })
    failed = client.get("/api/v1/runtime-events", params={
        "start": "2026-09-14T00:00:00+00:00", "end": "2026-09-15T00:00:00+00:00", "outcome": "FAILED",
    })

    assert response.status_code == pending.status_code == failed.status_code == 200
    rows = {(item["type"], item.get("phase") or item.get("status")): item for item in response.json()["items"]}
    assert (rows["DEBUG_MODE", "ALREADY_ASH"]["summary"], rows["DEBUG_MODE", "ALREADY_ASH"]["outcome"]) == ("已确认设备处于 ASH 模式", "SUCCEEDED")
    assert (rows["DEBUG_MODE", "STARTED"]["summary"], rows["DEBUG_MODE", "STARTED"]["outcome"]) == ("开始切换设备调试模式", "PENDING")
    assert (rows["DEBUG_MODE", "CHALLENGE_RECEIVED"]["summary"], rows["DEBUG_MODE", "CHALLENGE_RECEIVED"]["outcome"]) == ("已收到 PSH 调试挑战，正在切换", "PENDING")
    assert (rows["DEBUG_MODE", "ASH_READY"]["summary"], rows["DEBUG_MODE", "ASH_READY"]["outcome"]) == ("已切换至 ASH 模式", "SUCCEEDED")
    assert (rows["DEBUG_MODE", "FAILED"]["summary"], rows["DEBUG_MODE", "FAILED"]["outcome"]) == ("设备调试模式切换失败", "FAILED")
    assert (rows["COREDUMP_MOUNT", "FAILED"]["summary"], rows["COREDUMP_MOUNT", "FAILED"]["outcome"]) == ("核心转储 NFS 挂载失败", "FAILED")
    assert (rows["COREDUMP_MOUNT", "MOUNTED"]["summary"], rows["COREDUMP_MOUNT", "MOUNTED"]["outcome"]) == ("核心转储 NFS 已挂载", "SUCCEEDED")
    assert (rows["COREDUMP_MOUNT", "REMOUNTING"]["summary"], rows["COREDUMP_MOUNT", "REMOUNTING"]["outcome"]) == ("检测到核心转储 NFS 挂载丢失，准备重新挂载", "PENDING")
    assert {item.get("phase") or item.get("status") for item in pending.json()["items"]} == {"STARTED", "CHALLENGE_RECEIVED", "REMOUNTING"}
    assert {(item["type"], item.get("phase") or item.get("status")) for item in failed.json()["items"]} == {("DEBUG_MODE", "FAILED"), ("COREDUMP_MOUNT", "FAILED")}

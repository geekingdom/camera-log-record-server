"""以真实路由响应核对 API 文档响应合同，避免合同仅由自身示例证明。"""

from datetime import UTC, datetime, timedelta

import pytest
from test_api import client as _api_client


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """复用正式 API 夹具，避免本模块将导入的 fixture 名称重定义。"""
    yield from _api_client.__wrapped__(tmp_path)


def _resolve(schema, catalog):
    """解析合同中的组件引用；合同可直接使用内联对象。"""
    while "$ref" in schema:
        schema = catalog["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _assert_documented(value, schema, catalog, path="response"):
    """递归确保实际公开响应的每个键均有合同字段和中文说明。"""
    schema = _resolve(schema, catalog)
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        errors = []
        for variant in alternatives:
            try:
                _assert_documented(value, variant, catalog, path)
                return
            except AssertionError as error:
                errors.append(str(error))
        raise AssertionError(f"{path} 不匹配任何响应合同分支：{errors}")
    if value is None:
        assert schema.get("nullable"), f"{path} 返回 null 但合同未声明 nullable"
        return
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert properties, f"{path} 返回对象但合同没有 properties"
        for key in schema.get("required", []):
            assert key in value, f"{path} 合同要求 {key}，但实际成功响应未返回"
        for key, child in value.items():
            assert key in properties, f"{path}.{key} 缺少响应合同"
            assert properties[key].get("description"), f"{path}.{key} 缺少中文说明"
            _assert_documented(child, properties[key], catalog, f"{path}.{key}")
    elif isinstance(value, list):
        items = schema.get("items")
        assert items, f"{path} 返回数组但合同没有 items"
        for index, child in enumerate(value):
            _assert_documented(child, items, catalog, f"{path}[{index}]")


def _operation(client, identifier):
    """读取真实服务生成的合同目录项。"""
    catalog = client.get("/api/v1/api-reference").json()
    return catalog, next(item for item in catalog["operations"] if item["id"] == identifier)


def _assert_response(client, identifier, response):
    """用成功响应直接检查对应操作合同。"""
    assert response.is_success, response.text
    catalog, operation = _operation(client, identifier)
    _assert_documented(response.json(), operation["responseContract"]["schema"], catalog)


def test_created_task_template_and_soft_delete_responses_are_documented(client):
    """创建、读取和软删除任务分别走实际路由而非合成 examples。"""
    template = client.post("/api/v1/command-templates", headers={"Idempotency-Key": "contract-template"}, json={
        "name": "合同模板", "description": "响应字段覆盖", "initialCommands": [{"command": "date"}],
        "scheduledCommands": [{"command": "uptime", "totalExecutions": 2, "intervalSeconds": 5}],
    })
    _assert_response(client, "POST /api/v1/command-templates", template)
    task = client.post("/api/v1/tasks", headers={"Idempotency-Key": "contract-task"}, json={
        "name": "合同任务", "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": 9023,
        "resourceId": "fixture-device", "sourceTemplateId": template.json()["id"],
        "sourceTemplateVersion": template.json()["version"], "initialCommands": template.json()["initialCommands"],
        "scheduledCommands": template.json()["scheduledCommands"],
    })
    _assert_response(client, "POST /api/v1/tasks", task)
    _assert_response(client, "GET /api/v1/tasks/{task_id}", client.get(f"/api/v1/tasks/{task.json()['id']}"))
    deleted = client.delete(f"/api/v1/tasks/{task.json()['id']}?version={task.json()['version']}")
    _assert_response(client, "DELETE /api/v1/tasks/{task_id}", deleted)


def test_resource_core_metric_and_log_metadata_responses_are_documented(client):
    """资源列表、稳定 Core、指标、日志元数据均直接取路由真实投影。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 10, 20, tzinfo=UTC)
    client.portal.call(repo.db.coredump_files.insert_one, {"id": "contract-core", "resourceId": "fixture-device", "nodeId": "test", "name": "core.contract", "status": "RECEIVING", "size": 9, "receivedAt": stamp, "firstSeenAt": stamp, "sourceState": "STABLE", "sourceObservedAt": stamp, "sourceUnchangedSince": stamp, "sourceStableAt": stamp, "version": 1})
    client.portal.call(repo.db.files.insert_one, {"id": "contract-file", "taskId": "contract-log-task", "runId": "run", "sessionId": "session", "nodeId": "test", "status": "READY", "bytes": 9, "hour": "2026-10-20T00:00:00+00:00", "archiveName": "contract.tar.gz", "rawFileName": "contract.log", "segmentNumber": 1, "firstSequence": 1, "lastSequence": 2})
    client.portal.call(repo.db.tasks.insert_one, {"id": "contract-log-task", "name": "日志任务", "createdBy": "bootstrap", "status": "STOPPED", "desiredState": "STOPPED"})
    _assert_response(client, "GET /api/v1/resources", client.get("/api/v1/resources"))
    _assert_response(client, "GET /api/v1/resources/{resource_id}/coredumps", client.get("/api/v1/resources/fixture-device/coredumps"))
    _assert_response(client, "GET /api/v1/log-files/{identifier}", client.get("/api/v1/log-files/contract-file"))


def test_log_hour_download_search_and_gap_responses_are_documented(client):
    """日志目录、作业和实时缺口均通过真实路由输出覆盖合同。"""
    repo = client.app.state.repo
    stamp = datetime(2026, 10, 20, tzinfo=UTC)
    task = {"id": "contract-log", "name": "合同日志任务", "ip": "127.0.0.1", "createdBy": "bootstrap", "status": "STOPPED", "desiredState": "STOPPED"}
    client.portal.call(repo.db.tasks.insert_one, task)
    files = [
        {"id": "contract-log-a", "taskId": task["id"], "nodeId": "test", "runId": "run", "sessionId": "session", "status": "READY", "bytes": 10, "hour": stamp.isoformat(), "segmentNumber": 1, "firstReceivedAt": stamp.isoformat(), "archiveBytes": 6, "sha256": "a", "archiveMember": "part-000001.log", "runStartedAt": stamp, "sessionStartedAt": stamp, "createdAt": stamp, "updatedAt": stamp, "retainUntil": stamp + timedelta(hours=1), "deploymentClosedProof": {"token": "recovery-token", "nodeId": "test", "taskId": task["id"], "runId": "run", "generation": 1, "sessionId": "session", "confirmedAt": stamp, "fileId": "contract-log-a", "path": "/logs/contract.log", "createdAt": stamp, "watermark": 10, "firstSequence": 1, "archiveRecoveredAt": stamp}},
        {"id": "contract-log-b", "taskId": task["id"], "nodeId": "test", "runId": "run", "sessionId": "session", "status": "READY", "bytes": 8, "hour": stamp.isoformat(), "segmentNumber": 2, "firstReceivedAt": (stamp + timedelta(seconds=1)).isoformat(), "archiveBytes": 6, "sha256": "b"},
    ]
    client.portal.call(repo.db.files.insert_many, files)
    _assert_response(client, "GET /api/v1/tasks/{task_id}/log-hours", client.get(f"/api/v1/tasks/{task['id']}/log-hours"))
    download = client.post("/api/v1/downloads", headers={"Idempotency-Key": "contract-download"}, json={"taskId": task["id"], "hourIds": [stamp.isoformat()], "allowPartial": False})
    _assert_response(client, "POST /api/v1/downloads", download)
    _assert_response(client, "GET /api/v1/downloads/{identifier}", client.get(f"/api/v1/downloads/{download.json()['id']}"))
    client.portal.call(repo.db.jobs.update_one, {"id": download.json()["id"]}, {"$set": {
        "status": "SUCCEEDED", "progress": 100, "startedAt": stamp, "completedAt": stamp,
        "filename": "contract.tar.gz", "bytes": 12, "missing": ["contract-log-b"], "outputLockProtocol": 1,
    }})
    _assert_response(client, "GET /api/v1/downloads/{identifier}", client.get(f"/api/v1/downloads/{download.json()['id']}"))
    _assert_response(client, "GET /api/v1/tasks/{task_id}/downloads", client.get(f"/api/v1/tasks/{task['id']}/downloads"))
    client.portal.call(repo.db.jobs.insert_one, {"id": "contract-search", "kind": "SEARCH", "taskId": task["id"], "actor": "bootstrap", "nodeId": "test", "taskName": task["name"], "taskIp": task["ip"], "status": "SUCCEEDED", "progress": 100, "createdAt": stamp, "expiresAt": stamp + timedelta(hours=1), "results": [{"fileId": files[0]["id"], "offset": 1, "lineStartFileId": files[0]["id"], "lineStartOffset": 0, "text": "matched"}], "truncated": False})
    _assert_response(client, "GET /api/v1/log-searches/{identifier}/results", client.get("/api/v1/log-searches/contract-search/results"))
    gap = client.get(f"/api/v1/tasks/{task['id']}/log-gap-catalog", params={"beforeFileId": files[0]["id"], "beforeOffset": 1, "beforeSessionId": "session", "afterFileId": files[1]["id"], "afterOffset": 2, "afterSessionId": "session"})
    _assert_response(client, "GET /api/v1/tasks/{task_id}/log-gap-catalog", gap)

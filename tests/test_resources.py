"""设备资源 API 与海康认证探测的隔离回归测试。"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from camera_logs.resources.authentication import MAX_DEVICE_INFO_BYTES, authenticate_network_resource
from camera_logs.resources.lifecycle import reconcile_resource_deletion
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

DEVICE_INFO = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<DeviceInfo xmlns=\"http://www.hikvision.com/ver20/XMLSchema\">
  <model>DS-2CD</model>
  <subSerialNumber>ABC123</subSerialNumber>
  <firmwareVersion>V5.7.0</firmwareVersion>
  <firmwareReleasedDate>20240101</firmwareReleasedDate>
</DeviceInfo>"""


@pytest.fixture
def resource_client(tmp_path):
    """创建独立资源 API 数据库，避免任务测试的预置资源影响结果。"""
    settings = Settings(bootstrap_token="resource-admin", encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id="resources", start_background=False)
    with TestClient(create_app(settings, AsyncMongoMockClient().camera_logs)) as result:
        result.headers["Authorization"] = "Bearer resource-admin"
        yield result


@pytest.mark.asyncio
async def test_network_authentication_parses_namespaced_hikvision_xml():
    """Digest 成功响应必须解析并返回由设备提供的只读元数据。"""
    requests = []

    async def respond(request):
        requests.append(request)
        assert str(request.url) == "http://192.0.2.8/ISAPI/System/deviceInfo"
        if len(requests) == 1:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="camera", nonce="abc", qop="auth"'})
        assert request.headers["Authorization"].startswith("Digest ")
        return httpx.Response(200, text=DEVICE_INFO)

    result = await authenticate_network_resource(
        ip="192.0.2.8", username="admin", password="secret", auth_type="DIGEST",
        transport=httpx.MockTransport(respond),
    )

    assert result == {"model": "DS-2CD", "subSerialNumber": "ABC123", "softwareVersion": "V5.7.0 20240101"}


@pytest.mark.asyncio
async def test_network_authentication_accepts_comments_but_rejects_utf16_entities():
    """注释是正常 XML，而以 UTF-16 编码的实体声明仍必须被安全解析器拒绝。"""
    comment = httpx.MockTransport(lambda _: httpx.Response(200, text=DEVICE_INFO.replace("\n  <model>", "\n  <!-- device -->\n  <model>")))
    result = await authenticate_network_resource(ip="192.0.2.8", username="a", password="b", auth_type="BASIC", transport=comment)
    assert result["model"] == "DS-2CD"

    entity_xml = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE DeviceInfo [<!ENTITY model "DS-2CD">]>
<DeviceInfo><model>&model;</model><subSerialNumber>SN1</subSerialNumber>
<firmwareVersion>V5</firmwareVersion><firmwareReleasedDate>20240101</firmwareReleasedDate></DeviceInfo>""".encode("utf-16")
    entity = httpx.MockTransport(lambda _: httpx.Response(200, content=entity_xml))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.8", username="a", password="b", auth_type="BASIC", transport=entity)


@pytest.mark.asyncio
async def test_network_authentication_brackets_ipv6_url_for_basic_auth():
    """IPv6 字面量访问 ISAPI 时必须生成可解析的方括号 URL。"""
    async def respond(request):
        assert str(request.url) == "http://[2001:db8::8]/ISAPI/System/deviceInfo"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, text=DEVICE_INFO)

    result = await authenticate_network_resource(
        ip="2001:db8::8", username="admin", password="secret", auth_type="BASIC",
        transport=httpx.MockTransport(respond),
    )

    assert result["subSerialNumber"] == "ABC123"


@pytest.mark.asyncio
async def test_network_authentication_rejects_invalid_xml_and_maps_device_errors():
    """成功状态的非法设备信息和设备端失败都不能被当作已认证。"""
    invalid = httpx.MockTransport(lambda _: httpx.Response(200, text="<DeviceInfo>"))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=invalid)

    denied = httpx.MockTransport(lambda _: httpx.Response(401))
    with pytest.raises(PermissionError, match="设备凭据错误"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=denied)

    unavailable = httpx.MockTransport(lambda _: httpx.Response(503))
    with pytest.raises(RuntimeError, match="设备异常"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=unavailable)

    malformed = httpx.MockTransport(lambda _: httpx.Response(200, text="<OtherInfo/>"))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=malformed)

    missing_serial = httpx.MockTransport(lambda _: httpx.Response(200, text="""
        <DeviceInfo><model>DS-2CD</model><firmwareVersion>V5</firmwareVersion>
        <firmwareReleasedDate>20240101</firmwareReleasedDate></DeviceInfo>"""))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=missing_serial)

    entity = httpx.MockTransport(lambda _: httpx.Response(200, text="<!DOCTYPE foo [<!ENTITY x 'x'>]><DeviceInfo/>"))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=entity)

    timeout = httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    with pytest.raises(RuntimeError, match="设备异常"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=timeout)

    oversized = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (MAX_DEVICE_INFO_BYTES + 1)))
    with pytest.raises(ValueError, match="设备信息格式无效"):
        await authenticate_network_resource(ip="192.0.2.9", username="a", password="b", auth_type="BASIC", transport=oversized)


def test_resource_authenticate_create_list_and_hide_password(resource_client, monkeypatch):
    """网络设备只有服务端认证成功后才能持久化，公开结果绝不带口令。"""
    calls = 0

    async def verified(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["ip"] == "192.0.2.10"
        return {"model": "DS-2CD", "subSerialNumber": "SN1", "softwareVersion": "V5.7"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", verified)
    body = {"name": "大厅摄像机", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.10",
            "username": "admin", "password": "secret", "authType": "DIGEST"}

    preview = resource_client.post("/api/v1/resources/authenticate", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json() == {"model": "DS-2CD", "subSerialNumber": "SN1", "softwareVersion": "V5.7"}

    assert resource_client.post("/api/v1/resources", json=body).status_code == 422
    created = resource_client.post("/api/v1/resources", json=body, headers={"Idempotency-Key": "resource-create"})
    assert created.status_code == 201, created.text
    resource = created.json()
    assert resource["name"] == "大厅摄像机"
    assert resource["version"] == 1
    assert resource["model"] == "DS-2CD"
    assert "password" not in resource and "passwordEncrypted" not in resource
    repeated = resource_client.post("/api/v1/resources", json=body, headers={"Idempotency-Key": "resource-create"})
    assert repeated.json()["id"] == resource["id"]
    assert calls == 2
    assert resource_client.get(f"/api/v1/resources/{resource['id']}").json()["id"] == resource["id"]
    listed = resource_client.get("/api/v1/resources?search=大厅&kind=HIKVISION_NETWORK").json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == resource["id"]


def test_serial_resource_does_not_accept_network_credentials(resource_client):
    """串口服务器只保存名称和地址，避免把无效认证配置写入资源。"""
    response = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "serial-create"},
                                    json={"name": "串口机", "kind": "SERIAL_SERVER", "ip": "192.0.2.11"})
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "SERIAL_SERVER"
    assert resource_client.post("/api/v1/resources", json={"name": "bad", "kind": "SERIAL_SERVER", "ip": "192.0.2.12",
                                                              "username": "admin", "password": "secret"}).status_code == 422


def test_failed_authentication_does_not_save_resource(resource_client, monkeypatch):
    """认证失败时不得在数据库写入半成品资源记录。"""
    async def denied(**kwargs):
        raise PermissionError("设备凭据错误")

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", denied)
    response = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "failed-create"}, json={
        "name": "失败设备", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.12", "username": "a", "password": "b"})

    assert response.status_code == 401
    assert resource_client.portal.call(resource_client.app.state.repo.db.resources.count_documents, {}) == 0


def test_restricted_token_only_reads_resources_linked_to_allowed_tasks(resource_client):
    """受限令牌按获准任务投影资源，而不是把任务 ID 当作资源 ID。"""
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_many, [
        {"id": "resource-allowed", "name": "允许", "kind": "SERIAL_SERVER", "ip": "192.0.2.20"},
        {"id": "resource-denied", "name": "拒绝", "kind": "SERIAL_SERVER", "ip": "192.0.2.21"},
    ])
    resource_client.portal.call(repo.db.tasks.insert_many, [
        {"id": "task-allowed", "resourceId": "resource-allowed"},
        {"id": "task-hidden", "resourceId": "resource-allowed"},
        {"id": "task-denied", "resourceId": "resource-denied"},
    ])
    token = resource_client.post("/api/v1/service-tokens", json={
        "name": "资源只读", "scopes": ["tasks:read", "tasks:write", "tasks:control"], "taskIds": ["task-allowed"],
    }).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    listed = resource_client.get("/api/v1/resources", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == ["resource-allowed"]
    assert listed.json()["items"][0]["taskCount"] == 1
    assert resource_client.get("/api/v1/resources/resource-allowed", headers=headers).status_code == 200
    assert resource_client.get("/api/v1/resources/resource-denied", headers=headers).status_code == 403
    assert resource_client.post("/api/v1/resources/authenticate", headers=headers, json={
        "name": "探测", "kind": "SERIAL_SERVER", "ip": "192.0.2.22"}).status_code == 403
    patch = {"version": 1, "name": "改名", "kind": "SERIAL_SERVER", "ip": "192.0.2.20"}
    assert resource_client.patch("/api/v1/resources/resource-allowed", headers=headers, json=patch).status_code == 403
    assert resource_client.delete("/api/v1/resources/resource-allowed?version=1", headers=headers).status_code == 403


def test_network_resource_patch_reauthenticates_and_keeps_physical_identity(resource_client, monkeypatch):
    """资源名称或 HTTP 凭据可更新，但 IP、类型和设备身份不可借编辑改变。"""
    calls = []

    async def verified(**kwargs):
        calls.append(kwargs)
        return {"model": "DS-2CD", "subSerialNumber": "SN1", "softwareVersion": "V5.8"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", verified)
    body = {"name": "原名称", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.30", "username": "admin", "password": "old"}
    created = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "patch-create"}, json=body).json()
    patched_body = body | {"name": "新名称", "password": ""}

    patched = resource_client.patch(f"/api/v1/resources/{created['id']}", json=patched_body | {"version": 1})
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "新名称"
    assert patched.json()["version"] == 2
    assert calls[-1]["password"] == "old"
    assert resource_client.patch(f"/api/v1/resources/{created['id']}", json=patched_body | {"version": 1}).status_code == 409
    assert resource_client.patch(f"/api/v1/resources/{created['id']}", json=patched_body | {"version": 2, "ip": "192.0.2.31"}).status_code == 422


def test_soft_delete_stops_linked_tasks_and_keeps_deleted_resource_readable(resource_client):
    """软删除保留资源和历史任务，但停止主资源或串口资源引用的采集。"""
    resource = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "delete-serial"},
                                    json={"name": "串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.40"}).json()
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.tasks.insert_many, [
        {"id": "primary", "resourceId": resource["id"], "desiredState": "RUNNING", "restartRequested": True},
        {"id": "serial", "resourceId": "network", "serialServerResourceId": resource["id"], "desiredState": "RUNNING", "restartRequested": True},
        {"id": "paused", "resourceId": resource["id"], "status": "PAUSED", "nodeId": None,
         "runId": "paused-run", "desiredState": "RUNNING", "restartRequested": True},
    ])
    resource_client.portal.call(repo.db.endpoint_locks.insert_one, {"taskId": "paused", "runId": "paused-run"})
    resource_client.portal.call(repo.db.runs.insert_one, {"id": "paused-run"})
    resource_client.portal.call(repo.db.operations.insert_one, {
        "id": "resume", "taskId": "paused", "desiredState": "RUNNING", "status": "PENDING"})

    deleted = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["deletedAt"]
    assert deleted.json()["taskCount"] == 3
    assert deleted.json()["activeTaskCount"] == 0
    for identifier in ("primary", "serial", "paused"):
        task = resource_client.portal.call(repo.db.tasks.find_one, {"id": identifier})
        assert task["desiredState"] == "STOPPED"
        assert task["restartRequested"] is False
        assert task["resourceDeleted"] is True
    assert resource_client.portal.call(repo.db.endpoint_locks.count_documents, {"taskId": "paused"}) == 0
    assert resource_client.portal.call(repo.db.runs.find_one, {"id": "paused-run"})["endedAt"]
    assert resource_client.portal.call(repo.db.operations.find_one, {"id": "resume"})["status"] == "CANCELLED"
    assert resource_client.get("/api/v1/resources").json()["total"] == 0
    archived = resource_client.get("/api/v1/resources?includeDeleted=true").json()
    assert archived["items"][0]["id"] == resource["id"]
    assert resource_client.get(f"/api/v1/resources/{resource['id']}").json()["deletedAt"]
    assert deleted.json()["deletionState"] == "PENDING"
    resource_client.portal.call(repo.db.tasks.update_many, {"resourceId": resource["id"]}, {"$set": {"status": "STOPPED"}})
    resource_client.portal.call(repo.db.tasks.update_one, {"id": "serial"}, {"$set": {"status": "STOPPED"}})
    completed = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert completed.status_code == 202
    assert completed.json()["deletionState"] == "DONE"


def test_pending_deletion_retries_after_stop_sweep_failure(resource_client, monkeypatch):
    """首次删除的扫尾失败需保留 PENDING，重复删除能继续停止任务并标记完成。"""
    resource = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "retry-delete"},
                                    json={"name": "重试串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.50"}).json()
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "retry-task", "resourceId": resource["id"], "desiredState": "RUNNING", "status": "PENDING"})

    async def unavailable(*args, **kwargs):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr("camera_logs.resources.api.reconcile_resource_deletion", unavailable)
    with pytest.raises(RuntimeError, match="temporary database failure"):
        resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    stored = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    assert stored["deletionState"] == "PENDING"
    monkeypatch.setattr("camera_logs.resources.api.reconcile_resource_deletion", reconcile_resource_deletion)
    retried = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert retried.status_code == 202
    assert retried.json()["deletionState"] == "PENDING"
    assert resource_client.portal.call(repo.db.tasks.find_one, {"id": "retry-task"})["desiredState"] == "STOPPED"
    resource_client.portal.call(repo.db.tasks.update_one, {"id": "retry-task"}, {"$set": {"status": "STOPPED"}})
    completed = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert completed.json()["deletionState"] == "DONE"


def test_paused_lock_cleanup_retries_after_lock_release_failure():
    """暂停任务被调度器停止后，删除重试仍按同一 run 完成锁回收。"""
    async def scenario():
        database = AsyncMongoMockClient().db
        await database.resources.insert_one({"id": "retry-resource", "deletedAt": "then", "deletionState": "PENDING"})
        await database.tasks.insert_one({"id": "paused-retry", "resourceId": "retry-resource", "status": "PAUSED",
                                        "nodeId": None, "runId": "paused-retry-run", "desiredState": "RUNNING"})
        await database.endpoint_locks.insert_one({"taskId": "paused-retry", "runId": "paused-retry-run"})
        await database.runs.insert_one({"id": "paused-retry-run"})

        class FailingLocks:
            async def delete_one(self, query):
                raise RuntimeError("lock storage unavailable")

        failing_db = SimpleNamespace(resources=database.resources, tasks=database.tasks, operations=database.operations,
                                     endpoint_locks=FailingLocks(), runs=database.runs)
        with pytest.raises(RuntimeError, match="lock storage unavailable"):
            await reconcile_resource_deletion(SimpleNamespace(db=failing_db), "retry-resource")
        stopped = await database.tasks.find_one({"id": "paused-retry"})
        pending = await database.resources.find_one({"id": "retry-resource"})
        # 上轮失败后，调度器仍会把无节点的停止任务从 PAUSED 推进到 STOPPED。
        await database.tasks.update_one({"id": "paused-retry"}, {"$set": {"status": "STOPPED"}})
        await reconcile_resource_deletion(SimpleNamespace(db=database), "retry-resource")
        return (stopped, pending, await database.resources.find_one({"id": "retry-resource"}),
                await database.endpoint_locks.count_documents({"taskId": "paused-retry"}),
                await database.runs.find_one({"id": "paused-retry-run"}))

    stopped, pending, resource, lock_count, run = asyncio.run(scenario())
    assert stopped["desiredState"] == "STOPPED"
    assert pending["deletionState"] == "PENDING"
    assert resource["deletionState"] == "DONE"
    assert lock_count == 0
    assert run["endedAt"]


def test_active_task_keeps_resource_deletion_pending_until_node_and_lock_release(resource_client):
    """仍归属活跃节点的任务不会让软删除提前宣告完成。"""
    resource = resource_client.post("/api/v1/resources", headers={"Idempotency-Key": "active-delete"},
                                    json={"name": "活跃串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.61"}).json()
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "active-task", "resourceId": resource["id"], "nodeId": "node-1", "runId": "active-run",
        "desiredState": "RUNNING", "status": "COLLECTING",
    })
    resource_client.portal.call(repo.db.endpoint_locks.insert_one, {"taskId": "active-task", "runId": "active-run"})

    deleted = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert deleted.status_code == 202
    assert deleted.json()["deletionState"] == "PENDING"
    active = resource_client.portal.call(repo.db.tasks.find_one, {"id": "active-task"})
    assert active["nodeId"] == "node-1"
    assert active["desiredState"] == "STOPPED"

    resource_client.portal.call(repo.db.tasks.update_one, {"id": "active-task"},
                                    {"$set": {"nodeId": None, "status": "STOPPED"}})
    resource_client.portal.call(repo.db.endpoint_locks.delete_many, {"taskId": "active-task"})
    completed = resource_client.delete(f"/api/v1/resources/{resource['id']}?version=1")
    assert completed.status_code == 202
    assert completed.json()["deletionState"] == "DONE"


def test_scheduler_reconciles_pending_resource_deletions(tmp_path):
    """调度周期会重试 API 崩溃遗留的 PENDING 资源删除，不依赖用户再次点击。"""
    async def scenario():
        database = AsyncMongoMockClient().db
        await database.resources.insert_one({"id": "pending-resource", "deletedAt": "then", "deletionState": "PENDING"})
        await database.tasks.insert_one({"id": "pending-task", "resourceId": "pending-resource", "nodeId": None,
                                        "desiredState": "RUNNING", "status": "PENDING"})
        repo = SimpleNamespace(db=database, settings=SimpleNamespace(cluster_capacity=10))
        await schedule_once(repo)
        await schedule_once(repo)
        return await database.resources.find_one({"id": "pending-resource"}), await database.tasks.find_one({"id": "pending-task"})

    resource, task = asyncio.run(scenario())
    assert resource["deletionState"] == "DONE"
    assert task["desiredState"] == "STOPPED"

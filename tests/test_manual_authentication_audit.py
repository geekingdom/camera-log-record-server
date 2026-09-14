"""验证已有设备手动认证的历史和审计必须使用同一事务。"""

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from test_resources import resource_client  # noqa: F401


def _resource():
    """构造已认证网络设备，供手动认证接口在不访问真实设备时使用。"""
    return {
        "id": "manual-audit-device", "name": "手动认证审计设备", "kind": "HIKVISION_NETWORK",
        "ip": "192.0.2.91", "username": "admin", "passwordEncrypted": "", "authType": "DIGEST",
        "version": 1, "deletedAt": None, "authenticatedAt": now(),
    }


def _body():
    """返回认证接口所需的完整输入，不将凭据写入审计断言。"""
    return {
        "name": "手动认证审计设备", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.91",
        "username": "admin", "password": "test-password", "authType": "DIGEST",
    }


def _service_token(client, *, username, scopes):
    """创建带指定权限的普通用户服务令牌，供手动认证授权边界回归复用。"""
    user = client.post("/api/v1/users", json={
        "username": username, "displayName": username, "password": "example-password-123", "scopes": scopes,
    }).json()
    token = client.post("/api/v1/service-tokens", json={
        "name": f"{username}-token", "userId": user["id"]
    }).json()["token"]
    return user["id"], {"Authorization": f"Bearer {token}"}


def _transaction_probe(monkeypatch, repo):
    """以明确会话替身验证路由是否把两个写入置于同一提交单元。"""
    session = object()

    async def transaction(_repo, callback):
        return await callback(session)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", transaction)
    collection_type = type(repo.db.resources)
    original = collection_type.find_one_and_update

    async def update_without_mongomock_session(self, *args, **kwargs):
        """内存库不支持 session；保留路由传递的会话供认证和审计断言。"""
        kwargs.pop("session", None)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one_and_update", update_without_mongomock_session)
    return session


def test_manual_authentication_success_history_and_audit_share_transaction(resource_client, monkeypatch):  # noqa: F811
    """成功认证写入历史和成功审计时必须传递同一事务会话。"""
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_one, _resource())
    session = _transaction_probe(monkeypatch, repo)
    seen = []

    async def authenticated(**_kwargs):
        return {"model": "DS-2CD", "subSerialNumber": "SN-91", "softwareVersion": "V1"}

    async def record(_repo, _resource, **kwargs):
        seen.append(("history", kwargs["source"], kwargs["result"], kwargs.get("session")))

    async def audit(_actor, action, target, *, session=None, **_kwargs):
        seen.append(("audit", action, target, session))

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", authenticated)
    monkeypatch.setattr("camera_logs.resources.api.record_authentication", record)
    monkeypatch.setattr(repo, "audit", audit)

    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate", json=_body())

    assert response.status_code == 200, response.text
    assert seen == [
        ("history", "MANUAL", "SUCCESS", session),
        ("audit", "authenticate_resource_succeeded", "manual-audit-device", session),
    ]


def test_manual_authentication_failure_history_and_audit_share_transaction(resource_client, monkeypatch):  # noqa: F811
    """认证拒绝时，失败历史和失败审计也必须同事务提交，不能只留其一。"""
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_one, _resource())
    session = _transaction_probe(monkeypatch, repo)
    seen = []

    async def rejected(**_kwargs):
        raise PermissionError("invalid credential")

    async def record(_repo, _resource, **kwargs):
        seen.append(("history", kwargs["source"], kwargs["result"], kwargs.get("session")))

    async def audit(_actor, action, target, *, session=None, **_kwargs):
        seen.append(("audit", action, target, session))

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", rejected)
    monkeypatch.setattr("camera_logs.resources.api.record_authentication", record)
    monkeypatch.setattr(repo, "audit", audit)

    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate", json=_body())

    assert response.status_code == 401, response.text
    assert seen == [
        ("history", "MANUAL", "AUTH_FAILED", session),
        ("audit", "authenticate_resource_credentials_rejected", "manual-audit-device", session),
    ]


def test_manual_authentication_without_body_uses_saved_secret_and_resets_backoff(resource_client, monkeypatch):  # noqa: F811
    """资源列表不读取密码，空请求体认证成功后只由服务端清零周期失败退避。"""
    repo = resource_client.app.state.repo
    resource = _resource() | {"passwordEncrypted": repo.encrypt("saved-secret"), "healthFailureCount": 47,
                              "healthRevision": 4, "model": "old", "subSerialNumber": "old-serial"}
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "identity-task", "resourceId": resource["id"], "desiredState": "STOPPED",
        "storageIdentity": "old|old-serial",
    })
    captured = {}

    async def authenticated(**kwargs):
        captured.update(kwargs)
        return {"model": "DS-2CD", "subSerialNumber": "SN-91", "softwareVersion": "V1"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", authenticated)
    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate")

    assert response.status_code == 200, response.text
    assert captured["password"] == "saved-secret"
    changed = resource_client.portal.call(repo.db.resources.find_one, {"id": "manual-audit-device"})
    task = resource_client.portal.call(repo.db.tasks.find_one, {"id": "identity-task"})
    assert changed["healthFailureCount"] == 0 and changed["healthStatus"] == "ONLINE"
    assert (changed["model"], changed["subSerialNumber"]) == ("DS-2CD", "SN-91")
    assert task["storageIdentity"] != "old|old-serial"


def test_manual_empty_body_failure_updates_health_without_advancing_periodic_backoff(resource_client, monkeypatch):  # noqa: F811
    """即时认证失败停止运行任务，但不能把自动认证退避从既有次数推进一档。"""
    repo = resource_client.app.state.repo
    resource = _resource() | {"passwordEncrypted": repo.encrypt("saved-secret"), "healthFailureCount": 10,
                              "healthRevision": 2, "nextHealthCheckAt": now()}
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "running", "resourceId": resource["id"], "desiredState": "RUNNING", "status": "COLLECTING",
    })

    async def rejected(**_kwargs):
        raise PermissionError("rejected")

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", rejected)
    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate")

    assert response.status_code == 401
    changed = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    task = resource_client.portal.call(repo.db.tasks.find_one, {"id": "running"})
    assert (changed["healthStatus"], changed["healthFailureCount"]) == ("AUTH_FAILED", 10)
    assert task["desiredState"] == "STOPPED"


def test_body_authentication_preview_does_not_change_saved_resource_health(resource_client, monkeypatch):  # noqa: F811
    """编辑表单的未保存凭据只可预览，成功不能提前恢复资源或任务。"""
    repo = resource_client.app.state.repo
    resource = _resource() | {"healthStatus": "AUTH_FAILED", "healthFailureCount": 10, "healthRevision": 2}
    resource_client.portal.call(repo.db.resources.insert_one, resource)

    async def authenticated(**_kwargs):
        return {"model": "replacement", "subSerialNumber": "SN-new", "softwareVersion": "V2"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", authenticated)
    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate", json=_body())

    assert response.status_code == 200
    unchanged = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    assert (unchanged["healthStatus"], unchanged["healthFailureCount"], unchanged.get("model")) == (
        "AUTH_FAILED", 10, None,
    )


def test_manual_empty_body_rejects_concurrent_resource_revision(resource_client, monkeypatch):  # noqa: F811
    """认证请求期间保存的凭据或健康修订变化时，迟到结果不得覆盖新配置。"""
    repo = resource_client.app.state.repo
    resource = _resource() | {"passwordEncrypted": repo.encrypt("saved-secret"), "healthRevision": 2}
    resource_client.portal.call(repo.db.resources.insert_one, resource)

    async def authenticated(**_kwargs):
        await repo.db.resources.update_one({"id": resource["id"]}, {"$inc": {"version": 1, "healthRevision": 1}})
        return {"model": "late", "subSerialNumber": "late", "softwareVersion": "late"}

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", authenticated)
    response = resource_client.post("/api/v1/resources/manual-audit-device/authenticate")

    assert response.status_code == 409
    unchanged = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    assert unchanged.get("model") != "late" and unchanged["healthRevision"] == 3


def test_saved_credential_authentication_requires_task_control_before_contacting_device(resource_client, monkeypatch):  # noqa: F811
    """空请求体认证可停止任务，资源写入者缺少任务控制权限时不得访问设备或写入状态。"""
    repo = resource_client.app.state.repo
    user_id, headers = _service_token(resource_client, username="resource-operator", scopes=["resources:write"])
    resource = _resource() | {"createdBy": user_id, "passwordEncrypted": repo.encrypt("saved-secret")}
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "running", "resourceId": resource["id"], "desiredState": "RUNNING", "status": "COLLECTING",
    })

    async def must_not_authenticate(**_kwargs):
        raise AssertionError("缺少任务控制权限时不得访问设备")

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", must_not_authenticate)
    response = resource_client.post(f"/api/v1/resources/{resource['id']}/authenticate", headers=headers)

    assert response.status_code == 403
    unchanged = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    task = resource_client.portal.call(repo.db.tasks.find_one, {"id": "running"})
    assert unchanged.get("healthStatus") is None and task["desiredState"] == "RUNNING"
    assert resource_client.portal.call(repo.db.authentication_records.count_documents, {"resourceId": resource["id"]}) == 0
    assert resource_client.portal.call(repo.db.audit.count_documents, {"targetId": resource["id"]}) == 0


def test_saved_credential_authentication_rejects_foreign_related_tasks_in_transaction(resource_client, monkeypatch):  # noqa: F811
    """非管理员即使可控自己的任务，也不能以资源认证停止其他用户的关联任务。"""
    repo = resource_client.app.state.repo
    user_id, headers = _service_token(
        resource_client, username="resource-and-task-operator", scopes=["resources:write", "tasks:control"],
    )
    resource = _resource() | {"createdBy": user_id, "passwordEncrypted": repo.encrypt("saved-secret")}
    resource_client.portal.call(repo.db.resources.insert_one, resource)
    resource_client.portal.call(repo.db.tasks.insert_one, {
        "id": "foreign-running", "resourceId": resource["id"], "createdBy": "another-user",
        "desiredState": "RUNNING", "status": "COLLECTING",
    })

    async def rejected(**_kwargs):
        raise PermissionError("rejected")

    monkeypatch.setattr("camera_logs.resources.api.authenticate_network_resource", rejected)
    response = resource_client.post(f"/api/v1/resources/{resource['id']}/authenticate", headers=headers)

    assert response.status_code == 403
    unchanged = resource_client.portal.call(repo.db.resources.find_one, {"id": resource["id"]})
    task = resource_client.portal.call(repo.db.tasks.find_one, {"id": "foreign-running"})
    assert unchanged.get("healthStatus") is None and task["desiredState"] == "RUNNING"
    assert resource_client.portal.call(repo.db.authentication_records.count_documents, {"resourceId": resource["id"]}) == 0
    assert resource_client.portal.call(repo.db.audit.count_documents, {"targetId": resource["id"]}) == 0

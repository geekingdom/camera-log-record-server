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


def _transaction_probe(monkeypatch):
    """以明确会话替身验证路由是否把两个写入置于同一提交单元。"""
    session = object()

    async def transaction(_repo, callback):
        return await callback(session)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", transaction)
    return session


def test_manual_authentication_success_history_and_audit_share_transaction(resource_client, monkeypatch):  # noqa: F811
    """成功认证写入历史和成功审计时必须传递同一事务会话。"""
    repo = resource_client.app.state.repo
    resource_client.portal.call(repo.db.resources.insert_one, _resource())
    session = _transaction_probe(monkeypatch)
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
    session = _transaction_probe(monkeypatch)
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

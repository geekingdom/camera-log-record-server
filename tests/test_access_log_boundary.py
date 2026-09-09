"""正式应用访问边界回归：白名单拒绝也必须留下来源和关联编号，且不记录凭据。"""

import json
import logging

from camera_logs.common import observability
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


def test_ip_rejection_is_logged_once_with_source_and_request_id(tmp_path, monkeypatch):
    """策略在路由之前拒绝时，日志仍应覆盖完整 403 响应，不能漏记此次访问。"""
    records = []
    original = observability.log_request

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(json.loads(observability.JsonLineFormatter().format(record)))

    logger = logging.getLogger("test.access.boundary")
    monkeypatch.setattr(logger, "handlers", [Capture()])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)

    def log_request(request, **kwargs):
        original(request, **kwargs, logger=logger)

    monkeypatch.setattr(observability, "log_request", log_request)
    settings = Settings(_env_file=None, bootstrap_token="synthetic-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)
    with TestClient(app, client=("198.51.100.9", 40000)) as client:
        client.portal.call(app.state.repo.db.ip_policy.insert_one, {
            "id": "platform-ip-policy", "enabled": True,
            "rules": [{"network": "203.0.113.0/24", "scopes": ["*"]}],
        })
        response = client.post("/api/v1/auth/login?password=query-secret", headers={
            "X-Request-ID": "denied-request", "X-Forwarded-For": "203.0.113.1",
            "Authorization": "Bearer header-secret", "X-Requested-With": "XMLHttpRequest",
        }, json={"username": "admin", "password": "body-secret"})

    assert response.status_code == 403
    assert response.headers.get("X-Request-ID") == "denied-request"
    assert response.json()["error"]["requestId"] == "denied-request"
    assert len(records) == 1
    context = records[0]["context"]
    assert context["clientIp"] == "198.51.100.9"
    assert context["status"] == 403 and context["responseComplete"] is True
    assert context["requestId"] == "denied-request"
    assert not any(secret in json.dumps(records) for secret in ("query-secret", "header-secret", "body-secret"))


def test_new_credential_field_names_are_redacted_from_runtime_context():
    """登录模块和部署新增凭据字段同样受结构化脱敏保护。"""
    fields = {key: "synthetic-secret" for key in (
        "currentPassword", "new_password", "adminPassword", "passwordHash", "token_hash",
        "bootstrap_token", "internal-token", "encryption_key", "Cookie", "Set-Cookie",
    )}
    assert set(observability.redact(fields).values()) == {"[REDACTED]"}
    assert "synthetic-secret" not in observability.redact_text(json.dumps(fields))


def test_login_and_logout_access_records_identify_the_session_user(tmp_path, monkeypatch):
    """登录和退出不经过 actor 依赖，仍应将已确认的账号 ID 关联到访问记录。"""
    records = []

    def log_request(request, **kwargs):
        records.append((request.url.path, observability.request_actor(request), kwargs["status"]))

    monkeypatch.setattr(observability, "log_request", log_request)
    settings = Settings(_env_file=None, bootstrap_token="synthetic-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)
    with TestClient(app) as client:
        csrf = {"X-Requested-With": "XMLHttpRequest"}
        assert client.post("/api/v1/auth/login", headers=csrf,
                           json={"username": "admin", "password": "asdf!234"}).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=csrf).status_code == 204
    assert records == [("/api/v1/auth/login", "builtin-admin", 200),
                       ("/api/v1/auth/logout", "builtin-admin", 204)]

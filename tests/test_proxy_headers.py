"""验证可信前端代理恢复浏览器来源，且审计与 IP 策略使用同一地址。"""

import pytest
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

BROWSER_IP = "10.41.203.12"
TRUSTED_PROXIES = "127.0.0.1,172.21.0.2"


@pytest.fixture
def app(tmp_path):
    """构造包含正式请求记录与来源策略中间件的隔离应用。"""
    settings = Settings(_env_file=None, bootstrap_token="proxy-header-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                        start_background=False)
    application = create_app(settings, AsyncMongoMockClient().camera_logs)
    # 后注册的代理头中间件最先运行，策略与请求记录都会读取改写后的 ASGI client。
    application.add_middleware(ProxyHeadersMiddleware, trusted_hosts=TRUSTED_PROXIES)
    return application


@pytest.mark.parametrize("proxy_ip", ["127.0.0.1", "172.21.0.2"])
def test_trusted_proxy_restores_browser_ip_for_policy_and_persisted_request_event(app, proxy_ip):
    """受信任的本机或前端容器代理应同时影响策略响应和请求审计。"""
    request_id = f"trusted-{proxy_ip}"
    with TestClient(app, client=(proxy_ip, 39000)) as client:
        client.portal.call(app.state.repo.db.ip_policy.insert_one, {
            "id": "platform-ip-policy", "version": 1, "enabled": True,
            "rules": [{"label": "可信浏览器", "network": f"{BROWSER_IP}/32", "scopes": ["*"]}],
        })
        response = client.get("/api/v1/admin/ip-policy", headers={
            "Authorization": "Bearer proxy-header-bootstrap", "X-Forwarded-For": BROWSER_IP,
            "X-Request-ID": request_id,
        })
        event = client.portal.call(app.state.repo.db.request_events.find_one, {"requestId": request_id})

    assert response.status_code == 200
    assert response.json()["clientIp"] == BROWSER_IP
    assert event["clientIp"] == BROWSER_IP


def test_untrusted_direct_client_cannot_forge_browser_ip_for_policy_or_request_event(app):
    """非可信直连携带 XFF 时必须继续按实际对端拒绝并记录。"""
    with TestClient(app, client=("127.0.0.1", 39000)) as trusted:
        trusted.portal.call(app.state.repo.db.ip_policy.insert_one, {
            "id": "platform-ip-policy", "version": 1, "enabled": True,
            "rules": [{"label": "可信浏览器", "network": f"{BROWSER_IP}/32", "scopes": ["*"]}],
        })

    with TestClient(app, client=("198.51.100.9", 39000)) as direct:
        response = direct.get("/api/v1/admin/ip-policy", headers={
            "Authorization": "Bearer proxy-header-bootstrap", "X-Forwarded-For": BROWSER_IP,
            "X-Request-ID": "untrusted-direct",
        })
        event = direct.portal.call(app.state.repo.db.request_events.find_one, {"requestId": "untrusted-direct"})

    assert response.status_code == 403
    assert event["clientIp"] == "198.51.100.9"

"""通过正式路由执行部署认证验收脚本，防止脚本自身遗漏会话及 CSRF 合同。"""

import runpy
from pathlib import Path

import httpx
import pytest
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

verify = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_user_auth.py"))["verify"]


@pytest.mark.asyncio
async def test_auth_smoke_uses_real_routes_and_restores_ip_policy(tmp_path, monkeypatch):
    """隔离数据库中完成首登、规则启用、子账户撤销及规则关闭，不访问真实设备。"""
    env_file = tmp_path / ".env"
    env_file.write_text("ADMIN_USERNAME=admin\nADMIN_PASSWORD=asdf!234\n")
    settings = Settings(_env_file=None, bootstrap_token="smoke-test-bootstrap",
                        encryption_key=Fernet.generate_key().decode(), log_root=tmp_path / "logs",
                        start_background=False)
    app = create_app(settings, AsyncMongoMockClient().camera_logs)
    original_client = httpx.AsyncClient

    def isolated_client(**kwargs):
        return original_client(**kwargs, transport=httpx.ASGITransport(app=app, client=("198.51.100.9", 40000)))

    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    async with app.router.lifespan_context(app):
        result = await verify("http://testserver", env_file)
        assert all(result.values())
        policy = await app.state.repo.db.ip_policy.find_one({"id": "platform-ip-policy"})
        assert policy["enabled"] is False

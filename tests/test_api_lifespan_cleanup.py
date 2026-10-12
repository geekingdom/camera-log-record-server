"""API启动和退出异常不应跳过已经取得的数据库及日志资源收尾。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from camera_logs import main
from camera_logs.common import observability
from camera_logs.common.config import Settings
from cryptography.fernet import Fernet


def lifecycle_dependencies(tmp_path, monkeypatch, *, close_error=None):
    """隔离外部服务并记录真实lifespan是否调用各资源的关闭入口。"""
    client = Mock()
    client.__getitem__ = Mock(return_value=SimpleNamespace())
    client.close = AsyncMock(side_effect=close_error)
    listener = Mock()
    monkeypatch.setattr(main, "AsyncMongoClient", Mock(return_value=client))
    monkeypatch.setattr(observability, "setup_logging", Mock(return_value=listener))
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        bootstrap_token="lifecycle-test", log_root=tmp_path, start_background=False)
    return main.create_app(settings), client, listener


@pytest.mark.parametrize("stage", ["logging", "repository"])
async def test_api_early_startup_failure_closes_created_client(tmp_path, monkeypatch, stage):
    """日志目录不可写或仓库构造失败也必须关闭已经创建的Mongo客户端。"""
    app, client, _listener = lifecycle_dependencies(tmp_path, monkeypatch)
    failure = OSError("synthetic startup failure")
    if stage == "logging":
        monkeypatch.setattr(observability, "setup_logging", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(main, "Repository", Mock(side_effect=failure))
    with pytest.raises(OSError, match="synthetic startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("启动失败时不得进入服务阶段")
    client.close.assert_awaited_once()


async def test_api_client_close_failure_still_stops_log_listener(tmp_path, monkeypatch):
    """数据库关闭异常保留传播，同时仍刷新并停止运行日志监听器。"""
    app, client, listener = lifecycle_dependencies(
        tmp_path, monkeypatch, close_error=RuntimeError("synthetic close failure"))
    repo = SimpleNamespace(initialize=AsyncMock(side_effect=ValueError("synthetic initialize failure")))
    monkeypatch.setattr(main, "Repository", Mock(return_value=repo))
    with pytest.raises(RuntimeError, match="synthetic close failure") as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("启动失败时不得进入服务阶段")
    assert isinstance(caught.value.__context__, ValueError)
    client.close.assert_awaited_once()
    listener.stop.assert_called_once()


async def test_api_returns_503_before_lifespan_sets_repository(tmp_path):
    """进程启动窗口尚无本进程 repo 时，路由不得因属性缺失泄露 500。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        bootstrap_token="lifecycle-test", log_root=tmp_path, start_background=False)
    app = main.create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/tasks", headers={"X-Request-ID": "repo-not-ready"})

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "SERVICE_NOT_READY", "message": "服务正在初始化，请稍后重试", "requestId": "repo-not-ready",
    }


async def test_api_lifespan_refreshes_service_log_retention_after_repository_initialize(tmp_path, monkeypatch):
    """保留期先在仓库初始化后同步，后台启用时才创建跨进程刷新循环。"""
    calls, background = [], []
    repo = SimpleNamespace(settings=SimpleNamespace())

    async def initialize():
        calls.append("initialize")

    async def refresh(current_repo):
        assert current_repo is repo
        calls.append("refresh")

    async def initialize_admin(current_repo):
        assert current_repo is repo
        calls.append("initialize_admin")

    class Pool:
        """避免测试创建实际HTTP连接池。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_arguments):
            return False

    def capture_background(coroutine):
        """记录后台循环并关闭协程，验证生命周期的创建和回收分支。"""
        background.append(coroutine.cr_code.co_name)
        coroutine.close()
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(None)
        return completed

    repo.initialize = initialize
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        bootstrap_token="lifecycle-test", log_root=tmp_path, start_background=True)
    monkeypatch.setattr(main, "Repository", Mock(return_value=repo))
    monkeypatch.setattr(main, "NodeHttpPool", Pool)
    monkeypatch.setattr(observability, "setup_logging", Mock(return_value=Mock()))
    monkeypatch.setattr(observability, "refresh_service_log_retention", refresh)
    monkeypatch.setattr("camera_logs.users.sessions.initialize_admin", initialize_admin)
    monkeypatch.setattr(asyncio, "create_task", capture_background)

    app = main.create_app(settings, db=SimpleNamespace())
    async with app.router.lifespan_context(app):
        assert app.state.repo is repo

    assert calls == ["initialize", "refresh", "initialize_admin"]
    assert background == [
        "scheduler_loop", "health_loop", "recovery_loop", "record_maintenance_loop", "service_log_retention_loop",
    ]

"""API启动和退出异常不应跳过已经取得的数据库及日志资源收尾。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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

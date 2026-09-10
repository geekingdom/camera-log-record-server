"""节点内部服务装配失败时的资源释放回归。"""

from types import SimpleNamespace

import pytest
from camera_logs.common.config import Settings
from camera_logs.node import app as node_app
from cryptography.fernet import Fernet


class Listener:
    """记录日志 listener 是否始终停止。"""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class Client:
    """模拟仅生命周期测试需要的 Mongo client 关闭行为。"""

    def __init__(self, *, close_error=False, **_kwargs):
        self.close_error = close_error
        self.closed = False

    def __getitem__(self, _name):
        return SimpleNamespace()

    async def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


def settings(tmp_path):
    """提供不连接真实 Mongo 的最小节点配置。"""
    return Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                    node_id="node-test", start_background=False)


async def test_initialize_failure_closes_client_and_listener(tmp_path, monkeypatch):
    """Repository 初始化失败后仍必须释放已创建的 client 与日志 listener。"""
    listener, client = Listener(), Client()

    class FailingRepository:
        def __init__(self, *_args):
            pass

        async def initialize(self):
            raise RuntimeError("initialize failed")

    monkeypatch.setattr(node_app, "setup_logging", lambda _path: listener, raising=False)
    monkeypatch.setattr(node_app, "AsyncMongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(node_app, "Repository", FailingRepository)
    app = node_app.create_worker_app(settings(tmp_path))
    with pytest.raises(RuntimeError, match="initialize failed"):
        async with app.router.lifespan_context(app):
            pass
    assert client.closed and listener.stopped


async def test_client_close_failure_still_stops_listener(tmp_path, monkeypatch):
    """关闭 Mongo 失败也不能遗漏 listener.stop，避免进程退出后日志线程残留。"""
    listener, client = Listener(), Client(close_error=True)

    class FailingRepository:
        def __init__(self, *_args):
            pass

        async def initialize(self):
            raise RuntimeError("initialize failed")

    monkeypatch.setattr(node_app, "setup_logging", lambda _path: listener, raising=False)
    monkeypatch.setattr(node_app, "AsyncMongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(node_app, "Repository", FailingRepository)
    app = node_app.create_worker_app(settings(tmp_path))
    with pytest.raises(RuntimeError, match="close failed"):
        async with app.router.lifespan_context(app):
            pass
    assert client.closed and listener.stopped

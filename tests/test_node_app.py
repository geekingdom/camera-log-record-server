"""节点内部服务装配失败时的资源释放回归。"""

import asyncio
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


async def test_lifespan_cancels_and_awaits_shared_log_segment_cache(tmp_path, monkeypatch):
    """Worker 生命周期预热共享缓存，退出时必须取消并等待其后台刷新循环。"""
    listener, client = Listener(), Client()
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Cache:
        async def refresh(self, _repo):
            return 10 * 1024 * 1024

        async def loop(self, _repo):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Repository:
        def __init__(self, *_args):
            self.db = SimpleNamespace(coredump_exports=SimpleNamespace(update_many=self.update_exports))

        async def initialize(self):
            return None

        async def update_exports(self, *_args, **_kwargs):
            return None

    class Worker:
        def __init__(self, _repo):
            self.closed = False

        def start_maintenance(self):
            return None

        async def run(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    class Reads:
        async def close(self):
            return None

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(node_app, "setup_logging", lambda _path: listener)
    monkeypatch.setattr(node_app, "AsyncMongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(node_app, "Repository", Repository)
    monkeypatch.setattr(node_app, "Worker", Worker)
    monkeypatch.setattr(node_app, "segment_size_cache", lambda _repo: Cache())
    monkeypatch.setattr(node_app, "refresh_service_log_retention", no_op)
    monkeypatch.setattr(node_app, "service_log_retention_loop", no_op)
    from camera_logs.logs import compression, write_io
    from camera_logs.node import files, recovery, search_index_routes

    monkeypatch.setattr(recovery, "recover_planned_restart_tasks", no_op)
    monkeypatch.setattr(files, "install_node_routes", lambda *_args: Reads())
    monkeypatch.setattr(search_index_routes, "install_search_index_routes", lambda *_args: None)
    monkeypatch.setattr(compression, "shutdown_compression", no_op)
    monkeypatch.setattr(write_io, "shutdown", no_op)
    from camera_logs.logs import job_lease

    monkeypatch.setattr(job_lease, "recover_expired_jobs", no_op)
    app = node_app.create_worker_app(settings(tmp_path))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
    assert cancelled.is_set()
    assert client.closed and listener.stopped

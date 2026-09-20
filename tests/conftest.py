"""测试共享的事务接口替身，内存 Mongo 不支持会话事务。"""

import pytest
from mongomock_motor import AsyncMongoMockClient, AsyncMongoMockDatabase


@pytest.fixture
def known_node_resources(monkeypatch):
    """节点容量专项固定已采样资源，未知内存的保守拒绝由性能专项覆盖。"""
    import time

    from camera_logs.common.database import now
    from camera_logs.node.telemetry_runtime import TelemetryRuntime

    original = TelemetryRuntime.__init__

    def initialize(self, root):
        original(self, root)
        self.value = {"sampledAt": now(), "cpuPercent": 10, "memoryPercent": 20,
                      "resourceLimits": {"effectiveCpu": 4, "memoryLimitBytes": 1024**3,
                                         "memoryAvailableBytes": 512 * 1024**2, "source": "TEST"}}
        self.sample_started = time.monotonic()

    monkeypatch.setattr(TelemetryRuntime, "__init__", initialize)
    monkeypatch.setattr(TelemetryRuntime, "start_if_idle", lambda self: None)


@pytest.fixture(autouse=True)
def mock_audited_mutation_transaction(monkeypatch):
    """只为内存 Mongo 运行路由回调；真实数据库依然使用生产事务执行器。

    此替身不模拟回滚，不能作为原子性证据；副本集验证脚本负责故障与取消测试。
    """
    from camera_logs.common import audited_mutations

    # MongoMock 的 with_options 会退回同步集合；只在测试中保留其异步包装。
    monkeypatch.setattr(AsyncMongoMockDatabase, "with_options", lambda self, **_options: self, raising=False)
    original = audited_mutations.mutation_transaction

    async def run_callback(repo, callback):
        if isinstance(repo.db, AsyncMongoMockDatabase):
            return await callback(None)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", run_callback)


@pytest.fixture
def mock_claim_transaction(monkeypatch):
    """以空会话执行领取回调，仅供验证非事务性的调度状态语义。"""
    collection_type = type(AsyncMongoMockClient().db.operations)
    aggregate = collection_type.aggregate

    async def awaitable_aggregate(self, *args, **kwargs):
        """对齐 PyMongo Async 聚合接口；MongoMock 默认模拟的是 Motor 同步游标工厂。"""
        return aggregate(self, *args, **kwargs)

    monkeypatch.setattr(collection_type, "aggregate", awaitable_aggregate)

    async def run_callback(_repo, callback):
        """MongoMock 没有会话能力；真实提交和回滚由真实 Mongo 验证脚本覆盖。"""
        return await callback(None)

    monkeypatch.setattr("camera_logs.tasks.claim.claim_transaction", run_callback)


@pytest.fixture
def mock_reservation_transaction(monkeypatch):
    """以空会话执行定时预留回调，仅供 MongoMock 验证预算和执行记录语义。"""
    async def run_callback(_repo, callback):
        """真实 Mongo 的预算扣减和执行记录回滚由独立验证脚本覆盖。"""
        return await callback(None)

    monkeypatch.setattr("camera_logs.commands.reservation.reservation_transaction", run_callback)

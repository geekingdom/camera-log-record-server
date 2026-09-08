"""定时预算与迟到结果必须遵守当前运行归属和原会话身份。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection.collector import Collector
from pymongo.errors import ConnectionFailure
from test_collector import FakeConnection
from test_manual_command_ownership import runtime_and_command

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction")

DETAIL = {"taskId": "task", "runId": "run", "sessionId": "session"}


async def scheduled_runtime(tmp_path):
    """复用隔离数据库，配置两次定时预算且不建立设备连接。"""
    runtime, _ = await runtime_and_command(tmp_path)
    commands = [{"id": "periodic", "totalExecutions": 2}]
    runtime.task["scheduledCommands"] = commands
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"scheduledCommands": commands}})
    runtime.pending_executions = {}
    return runtime


@pytest.mark.parametrize("change", [
    {"generation": 2}, {"runId": "new"}, {"nodeId": "other"}, {"sessionId": "new"},
    {"desiredState": "STOPPED"}, {"desiredState": "PAUSED"}, {"status": "BLOCKED"},
])
async def test_invalid_owner_does_not_reserve_budget(tmp_path, change):
    """监督器尚未观察到失属或停止时，预算入口也必须拒绝旧实例。"""
    runtime = await scheduled_runtime(tmp_path)
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": change})
    assert await runtime.reserve("periodic", DETAIL) is False
    assert await runtime.repo.db.budgets.count_documents({}) == 0
    assert await runtime.repo.db.commands.count_documents({"kind": "SCHEDULED"}) == 0


async def test_late_result_preserves_unknown(tmp_path):
    """会话收尾已记录 UNKNOWN 后，迟到的成功回调不能覆盖证据。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    await runtime.repo.db.commands.update_many({"kind": "SCHEDULED"}, {"$set": {"status": "UNKNOWN"}})
    await runtime.update_execution("periodic", "SENT", DETAIL)
    assert (await runtime.repo.db.commands.find_one({"kind": "SCHEDULED"}))["status"] == "UNKNOWN"


async def test_old_callback_does_not_consume_new_session_execution(tmp_path):
    """相同定时配置跨重连沿用预算，两个会话回调只能更新自己的执行。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    runtime.collector = SimpleNamespace(session_id="new")
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"sessionId": "new"}})
    assert await runtime.reserve("periodic", DETAIL | {"sessionId": "new"})
    await runtime.update_execution("periodic", "SENT", DETAIL)
    assert (await runtime.repo.db.commands.find_one({"kind": "SCHEDULED", "sessionId": "new"}))["status"] == "SENDING"
    await runtime.update_execution("periodic", "SENT", DETAIL | {"sessionId": "new"})
    assert await runtime.repo.db.commands.count_documents({"kind": "SCHEDULED", "status": "SENT"}) == 2


async def test_old_session_callback_cannot_reserve_new_session_budget(tmp_path):
    """即使 Runtime 已重连成功，旧采集器传来的占用请求仍应拒绝。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL | {"sessionId": "old"}) is False
    assert await runtime.repo.db.budgets.count_documents({}) == 0


async def test_scheduled_budget_stops_at_configured_execution_limit(tmp_path):
    """同一会话反复预留只能创建配置次数内的执行记录，预算不会越界。"""
    runtime = await scheduled_runtime(tmp_path)

    results = [await runtime.reserve("periodic", DETAIL) for _ in range(3)]

    budget = await runtime.repo.db.budgets.find_one({"_id": "run:periodic"})
    assert results == [True, True, False]
    assert budget["attempts"] == 2
    assert await runtime.repo.db.commands.count_documents({"kind": "SCHEDULED"}) == 2


@pytest.mark.parametrize("failures", [1, 2])
async def test_execution_result_retries_connection_failure_without_consuming_budget(tmp_path, monkeypatch, failures):
    """结果写入短暂断连后重试同一执行记录，不再扣减预算或新建执行。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    key = ("session", "periodic")
    execution_id = runtime.pending_executions[key]
    collection_type = type(runtime.repo.db.commands)
    original_update = collection_type.update_one
    attempts = 0

    async def fail_before_sent(self, query, update, **kwargs):
        nonlocal attempts
        if self.name == "commands" and query.get("id") == execution_id and attempts < failures:
            attempts += 1
            raise ConnectionFailure("temporary result write failure")
        return await original_update(self, query, update, **kwargs)

    async def fast_sleep(_delay):
        """只替换运行时模块的退避等待，不调用被替换的全局 sleep。"""
        return

    monkeypatch.setattr(collection_type, "update_one", fail_before_sent)
    monkeypatch.setattr("camera_logs.collection.runtime.asyncio.sleep", fast_sleep)
    await runtime.update_execution("periodic", "SENT", DETAIL)

    execution = await runtime.repo.db.commands.find_one({"id": execution_id})
    budget = await runtime.repo.db.budgets.find_one({"_id": "run:periodic"})
    assert attempts == failures
    assert execution["status"] == "SENT"
    assert budget["attempts"] == 1
    assert key not in runtime.pending_executions


async def test_stopping_runtime_keeps_pending_execution_after_result_write_retry_failure(tmp_path, monkeypatch):
    """任务停止时退出结果写入重试，pending 映射留给会话收尾标记 UNKNOWN。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    key = ("session", "periodic")
    execution_id = runtime.pending_executions[key]
    collection_type = type(runtime.repo.db.commands)
    original_update = collection_type.update_one

    async def fail_result_write(self, query, update, **kwargs):
        if self.name == "commands" and query.get("id") == execution_id:
            raise ConnectionFailure("temporary result write failure")
        return await original_update(self, query, update, **kwargs)

    async def stop_during_backoff(_delay):
        """退避前模拟任务停止，不递归调用已替换的 sleep。"""
        runtime.stopping = True

    monkeypatch.setattr(collection_type, "update_one", fail_result_write)
    monkeypatch.setattr("camera_logs.collection.runtime.asyncio.sleep", stop_during_backoff)
    await runtime.update_execution("periodic", "SENT", DETAIL)

    execution = await runtime.repo.db.commands.find_one({"id": execution_id})
    assert execution["status"] == "SENDING"
    assert runtime.pending_executions[key] == execution_id


async def test_cancelling_result_write_retry_keeps_pending_execution(tmp_path, monkeypatch):
    """协程在结果写入退避中取消时传播取消，并保留 pending 映射供收尾处理。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    key = ("session", "periodic")
    execution_id = runtime.pending_executions[key]
    collection_type = type(runtime.repo.db.commands)
    original_update = collection_type.update_one

    async def fail_result_write(self, query, update, **kwargs):
        if self.name == "commands" and query.get("id") == execution_id:
            raise ConnectionFailure("temporary result write failure")
        return await original_update(self, query, update, **kwargs)

    async def cancel_during_backoff(_delay):
        """模拟调度任务在退避点被取消，不经由全局 sleep 递归。"""
        raise asyncio.CancelledError()

    monkeypatch.setattr(collection_type, "update_one", fail_result_write)
    monkeypatch.setattr("camera_logs.collection.runtime.asyncio.sleep", cancel_during_backoff)
    with pytest.raises(asyncio.CancelledError):
        await runtime.update_execution("periodic", "SENT", DETAIL)

    execution = await runtime.repo.db.commands.find_one({"id": execution_id})
    assert execution["status"] == "SENDING"
    assert runtime.pending_executions[key] == execution_id


async def test_execution_result_raises_after_three_connection_failures(tmp_path, monkeypatch):
    """连续三次结果回写失败后向上传播，预算和 pending 交由会话收尾处理。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL)
    key = ("session", "periodic")
    execution_id = runtime.pending_executions[key]
    collection_type = type(runtime.repo.db.commands)
    original_update = collection_type.update_one
    attempts = backoffs = 0

    async def always_fail_result_write(self, query, update, **kwargs):
        nonlocal attempts
        if self.name == "commands" and query.get("id") == execution_id:
            attempts += 1
            raise ConnectionFailure("persistent result write failure")
        return await original_update(self, query, update, **kwargs)

    async def allow_two_backoffs(_delay):
        """旧无限循环到第三次退避时中断，防止红测忙等。"""
        nonlocal backoffs
        backoffs += 1
        if backoffs == 3:
            raise AssertionError("第三次失败后不应继续退避")

    monkeypatch.setattr(collection_type, "update_one", always_fail_result_write)
    monkeypatch.setattr("camera_logs.collection.runtime.asyncio.sleep", allow_two_backoffs)
    with pytest.raises(ConnectionFailure):
        await runtime.update_execution("periodic", "SENT", DETAIL)

    budget = await runtime.repo.db.budgets.find_one({"_id": "run:periodic"})
    execution = await runtime.repo.db.commands.find_one({"id": execution_id})
    assert attempts == 3
    assert budget["attempts"] == 1
    assert execution["status"] == "SENDING"
    assert runtime.pending_executions[key] == execution_id


@pytest.mark.parametrize("change", [{"taskId": "other"}, {"runId": "other"}])
async def test_foreign_callback_cannot_reserve_or_consume_pending_execution(tmp_path, change):
    """误分发的其他任务或运行回调不占预算，也不能取走当前待回写记录。"""
    runtime = await scheduled_runtime(tmp_path)
    assert await runtime.reserve("periodic", DETAIL | change) is False
    assert await runtime.reserve("periodic", DETAIL)
    await runtime.update_execution("periodic", "SENT", DETAIL | change)
    assert (await runtime.repo.db.commands.find_one({"kind": "SCHEDULED"}))["status"] == "SENDING"
    await runtime.update_execution("periodic", "SENT", DETAIL)
    assert (await runtime.repo.db.commands.find_one({"kind": "SCHEDULED"}))["status"] == "SENT"


async def test_scheduled_waits_until_collecting_state_is_published(tmp_path):
    """状态持久化慢于定时间隔时，首次预算仍须等待成功发布后再触发。"""
    publishing, published, reserved, sent = (asyncio.Event() for _ in range(4))
    connection = FakeConnection()

    async def on_state(state, _details):
        if state == "COLLECTING":
            publishing.set()
            await published.wait()

    async def reserve(_identifier, _details):
        reserved.set()
        return True

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [
            {"id": "periodic", "command": "probe", "totalExecutions": 1, "intervalSeconds": .01},
        ]}, tmp_path, connection_factory=lambda _: connection, on_state=on_state,
        reserve_execution=reserve, update_execution=lambda *_: sent.set())
    starting = asyncio.create_task(collector.start())
    try:
        await asyncio.wait_for(publishing.wait(), 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reserved.wait(), .05)
        published.set()
        await asyncio.wait_for(starting, 1)
        await asyncio.wait_for(sent.wait(), 1)
        assert connection.sent == [b"probe\n"]
    finally:
        published.set()
        await asyncio.gather(starting, return_exceptions=True)
        await collector.stop()

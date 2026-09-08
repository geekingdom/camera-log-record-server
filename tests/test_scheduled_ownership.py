"""定时预算与迟到结果必须遵守当前运行归属和原会话身份。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection.collector import Collector
from test_collector import FakeConnection
from test_manual_command_ownership import runtime_and_command

DETAIL = {"taskId": "task", "runId": "run", "sessionId": "session"}


async def scheduled_runtime(tmp_path):
    """复用隔离数据库，配置两次定时预算且不建立设备连接。"""
    runtime, _ = await runtime_and_command(tmp_path)
    runtime.task["scheduledCommands"] = [{"id": "periodic", "totalExecutions": 2}]
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

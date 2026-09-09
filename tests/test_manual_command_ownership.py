"""手动命令领取回归：核对数据库会话身份，发送正文只取已领取的记录。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.collector import Collector
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.commands.manual_claim import ManualClaimUncertain
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.common.ownership import OwnershipLost
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import ConnectionFailure

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction")


async def runtime_and_command(tmp_path):
    """构造已登录的单会话，使用独立配置与命令发送替身，不连接设备。"""
    repo = Repository(AsyncMongoMockClient().db, Settings(
        _env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
    ))
    task = {"id": "task", "runId": "run", "nodeId": "node", "generation": 1,
            "sessionId": "session", "status": "COLLECTING", "desiredState": "RUNNING"}
    await repo.db.tasks.insert_one(task.copy())
    record = {"id": "command", "taskId": "task", "runId": "run", "sessionId": "session",
              "kind": "MANUAL", "status": "QUEUED", "command": "stored-command"}
    await repo.db.commands.insert_one(record.copy())
    runtime = object.__new__(SessionRuntime)
    runtime.repo, runtime.task, runtime.stopping, runtime.retired = repo, task, False, False
    runtime.collector = SimpleNamespace(session_id="session", enqueue_manual=AsyncMock())
    return runtime, record


@pytest.mark.parametrize("change", [{"runId": "new"}, {"generation": 2}, {"status": "BLOCKED"}])
async def test_old_runtime_cannot_claim_commands_after_task_changed(tmp_path, change):
    """监督器尚未观察到变化时，命令入口仍需拒绝当前归属已经失效的调用。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": change})
    await runtime.manual(record)
    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "QUEUED"


async def test_foreign_task_command_is_not_claimed(tmp_path):
    """命令标识即使被错误分发，也不能越过任务身份消费另一路命令。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"taskId": "other"}})
    await runtime.manual(record | {"taskId": "other"})
    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "QUEUED"


async def test_send_uses_claimed_database_body_not_callers_snapshot(tmp_path):
    """调用参数仅定位命令，实际发送采用数据库原子领取结果的正文和选项。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.manual(record | {"command": "outdated-command", "newline": "\r"})
    args = runtime.collector.enqueue_manual.await_args
    assert args.args == ("stored-command",)
    assert args.kwargs["newline"] == "\n"
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "SENT"


async def test_current_runtime_cancels_old_session_record(tmp_path):
    """旧会话迟到的排队记录明确取消，不能永久挡住当前会话后续命令。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"sessionId": "old"}})
    await runtime.manual(record | {"sessionId": "old"})
    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "CANCELLED"


async def test_late_sender_result_does_not_overwrite_cleanup_unknown(tmp_path):
    """会话收尾已经判定结果未知时，迟到的成功返回不能重新宣称确定发送成功。"""
    runtime, record = await runtime_and_command(tmp_path)

    async def cleanup_before_return(*_args, **_kwargs):
        await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"status": "UNKNOWN"}})

    runtime.collector.enqueue_manual.side_effect = cleanup_before_return
    await runtime.manual(record)
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "UNKNOWN"


async def test_claimed_command_is_cancelled_when_owner_changes_before_enqueue(tmp_path, monkeypatch):
    """领取后、实际入队前发生接管时，旧会话不得向仍存活的旧连接写入命令。"""
    runtime, record = await runtime_and_command(tmp_path)

    async def claimed_then_handoff(repo, task, _session_id, document):
        claimed = await repo.db.commands.find_one_and_update(
            {"id": document["id"], "status": "QUEUED"}, {"$set": {"status": "SENDING"}},
        )
        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"sessionId": "successor"}})
        return claimed

    monkeypatch.setattr("camera_logs.collection.runtime.claim_manual", claimed_then_handoff)
    await runtime.manual(record)

    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "CANCELLED"


async def test_claimed_command_stays_unknown_when_owner_recheck_database_fails(tmp_path, monkeypatch):
    """领取提交后的归属查询异常不能冒险发送，也不能把结果伪造为明确取消。"""
    runtime, record = await runtime_and_command(tmp_path)
    collection_type = type(runtime.repo.db.tasks)
    original = collection_type.find_one

    async def fail_task_recheck(self, query, *args, **kwargs):
        if self.name == "tasks":
            raise ConnectionFailure("database unavailable")
        return await original(self, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one", fail_task_recheck)

    await runtime.manual(record)

    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "UNKNOWN"


async def test_owner_recheck_cancels_when_runtime_stops_while_database_query_waits(tmp_path, monkeypatch):
    """归属查询返回前本地停止时，返回后的第二次本地复核必须阻止命令进入发送队列。"""
    runtime, record = await runtime_and_command(tmp_path)
    collection_type = type(runtime.repo.db.tasks)
    original = collection_type.find_one

    async def stop_before_result(self, query, *args, **kwargs):
        result = await original(self, query, *args, **kwargs)
        if self.name == "tasks":
            runtime.stopping = True
        return result

    monkeypatch.setattr(collection_type, "find_one", stop_before_result)
    await runtime.manual(record)

    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "CANCELLED"


async def test_unknown_claim_commit_never_enqueues_and_is_recorded_as_unknown(tmp_path, monkeypatch, caplog):
    """提交确认丢失时，固定命令 ID 绝不补发，已可能领取的记录只保守标为 UNKNOWN。"""
    runtime, record = await runtime_and_command(tmp_path)

    async def committed_then_unknown(repo, _task, _session_id, document):
        await repo.db.commands.update_one({"id": document["id"]}, {"$set": {"status": "SENDING"}})
        raise ManualClaimUncertain("commit acknowledgement unavailable")

    monkeypatch.setattr("camera_logs.collection.runtime.claim_manual", committed_then_unknown)
    caplog.set_level("ERROR", logger="camera_logs.collection.runtime")
    await runtime.manual(record)

    runtime.collector.enqueue_manual.assert_not_awaited()
    stored = await runtime.repo.db.commands.find_one({"id": "command"})
    assert stored["status"] == "UNKNOWN"
    assert stored["error"] == "命令领取状态未知，未发送"
    assert "task=task command=command" in caplog.text


async def test_collector_queue_rechecks_owner_before_business_write(tmp_path):
    """排队期间发生接管时，发送器最后关口拒绝业务命令，也不需要真实设备连接。"""
    runtime, _ = await runtime_and_command(tmp_path)
    release, entered = asyncio.Event(), asyncio.Event()

    class Connection:
        def __init__(self):
            self.received, self.writes = asyncio.Queue(), []

        async def read(self):
            return await self.received.get()

        async def write(self, data):
            self.writes.append(data)

        async def close(self):
            self.received.put_nowait(b"")

    connection = Connection()
    collector = Collector(runtime.task | {"storageIdentity": "manual-guard"}, tmp_path,
                          connection_factory=AsyncMock(return_value=connection))
    await collector.start()

    async def hold_then_reject():
        entered.set()
        await release.wait()
        raise OwnershipLost("barrier command must not send")

    async def owner_guard():
        current = await runtime.repo.db.tasks.find_one({"id": "task", "sessionId": "session"})
        if current is None:
            raise OwnershipLost("successor owns task")

    try:
        held = asyncio.create_task(collector.enqueue_manual("barrier", session_guard=hold_then_reject))
        await entered.wait()
        business = asyncio.create_task(collector.enqueue_manual("business", session_guard=owner_guard))
        await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"sessionId": "successor"}})
        release.set()
        with pytest.raises(OwnershipLost):
            await held
        with pytest.raises(OwnershipLost):
            await business
        assert b"business\n" not in connection.writes
    finally:
        await collector.stop()


async def test_scheduled_record_cannot_be_consumed_as_manual(tmp_path):
    """误分发的定时记录不能绕过自身预算处理而作为手动命令执行。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"kind": "SCHEDULED"}})
    await runtime.manual(record)
    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "QUEUED"

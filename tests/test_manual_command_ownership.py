"""手动命令领取回归：核对数据库会话身份，发送正文只取已领取的记录。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


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


async def test_scheduled_record_cannot_be_consumed_as_manual(tmp_path):
    """误分发的定时记录不能绕过自身预算处理而作为手动命令执行。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"kind": "SCHEDULED"}})
    await runtime.manual(record)
    runtime.collector.enqueue_manual.assert_not_awaited()
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "QUEUED"

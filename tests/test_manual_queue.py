"""重连后的手动命令队列不得被旧会话积压阻塞或占满配额。"""
# ruff: noqa: F811 - pytest 按名称注入复用的 client fixture。

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.node.manual_queue import next_manual_command
from test_api import client  # noqa: F401
from test_manual_command_ownership import runtime_and_command
from test_worker_supervision import setup_worker


async def test_current_session_dispatched_without_draining_old_queue(tmp_path, monkeypatch):
    """同一监督周期直接选择当前会话，旧命令不能跨会话发送。"""
    worker, repo, runtime = await setup_worker(tmp_path, monkeypatch)
    runtime.collector = SimpleNamespace(session_id="current")
    runtime.manual = AsyncMock()
    await repo.db.tasks.update_one({"id": "task"}, {"$set": {"sessionId": "current"}})
    await repo.db.commands.insert_many([
        {"id": f"old-{index}", "taskId": "task", "runId": "run", "sessionId": "old",
         "kind": "MANUAL", "status": "QUEUED", "createdAt": index}
        for index in range(150)
    ] + [{"id": "current", "taskId": "task", "runId": "run", "sessionId": "current",
          "kind": "MANUAL", "status": "QUEUED", "createdAt": 200}])
    await worker.tick()
    await asyncio.gather(*worker.manual_jobs.values())
    assert runtime.manual.await_args.args[0]["id"] == "current"
    assert await repo.db.commands.count_documents({"status": "CANCELLED"}) == 100
    assert await repo.db.commands.count_documents({"sessionId": "old", "status": "QUEUED"}) == 50


def test_previous_session_queue_does_not_consume_current_session_quota(client, mock_reservation_transaction):
    """满额旧队列不拒绝新会话；当前会话达到上限仍明确拒绝。"""
    task = {"id": "task", "status": "COLLECTING", "desiredState": "RUNNING",
            "runId": "run", "sessionId": "current"}
    client.portal.call(client.app.state.repo.db.tasks.insert_one, task)
    old = [{"id": f"old-{index}", "taskId": "task", "runId": "run", "sessionId": "old",
            "status": "QUEUED", "kind": "MANUAL"} for index in range(100)]
    client.portal.call(client.app.state.repo.db.commands.insert_many, old)
    response = client.post("/api/v1/tasks/task/commands", json={"command": "ls"},
                           headers={"Idempotency-Key": "new-command"})
    assert response.status_code == 202
    current = [{"id": f"new-{index}", "taskId": "task", "runId": "run", "sessionId": "current",
                "status": "QUEUED", "kind": "MANUAL"} for index in range(99)]
    client.portal.call(client.app.state.repo.db.commands.insert_many, current)
    assert client.post("/api/v1/tasks/task/commands", json={"command": "ls"},
                       headers={"Idempotency-Key": "over-limit"}).status_code == 429


def test_missing_session_rejects_command(client):
    """尚未发布有效会话身份时拒绝手动交互，避免命令进入无主队列。"""
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {
        "id": "task", "status": "COLLECTING", "desiredState": "RUNNING", "runId": "run",
    })
    assert client.post("/api/v1/tasks/task/commands", json={"command": "ls"},
                       headers={"Idempotency-Key": "no-session"}).status_code == 409


def test_reconnect_between_api_check_and_admission_rejects_old_snapshot(client, monkeypatch, mock_reservation_transaction):
    """正式路由首次读取后发生重连时，事务必须拒绝把命令写入旧会话。"""
    from camera_logs.commands import manual_submission

    original = manual_submission.admit_manual

    async def reconnect(repo, task, *args, **kwargs):
        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"sessionId": "successor"}})
        return await original(repo, task, *args, **kwargs)

    monkeypatch.setattr(manual_submission, "admit_manual", reconnect)
    client.portal.call(client.app.state.repo.db.tasks.insert_one, {
        "id": "task", "status": "COLLECTING", "desiredState": "RUNNING",
        "runId": "run", "sessionId": "old",
    })
    response = client.post("/api/v1/tasks/task/commands", json={"command": "ls"},
                           headers={"Idempotency-Key": "racing-reconnect"})
    assert response.status_code == 409
    assert client.portal.call(client.app.state.repo.db.commands.count_documents, {}) == 0


@pytest.mark.parametrize("change", [{"generation": 2}, {"sessionId": "new"}, {"desiredState": "STOPPED"}])
async def test_lost_ownership_does_not_cancel_queue(tmp_path, change):
    """过期实例扫描到旧记录后也必须复核任务，失属时不执行任何清理。"""
    runtime, _ = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"sessionId": "old"}})
    await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": change})
    assert await next_manual_command(runtime.repo, runtime) is None
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "QUEUED"


async def test_successor_insert_during_cleanup_is_preserved(tmp_path, monkeypatch):
    """清理前发生重连且插入新命令，快照 ID 条件必须排除新插入记录。"""
    runtime, record = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"sessionId": "old"}})
    collection_type = type(runtime.repo.db.commands)
    original = collection_type.update_many

    async def reconnect_before_cleanup(self, query, update, **kwargs):
        if self.name == "commands":
            await runtime.repo.db.tasks.update_one({"id": "task"}, {"$set": {"sessionId": "new"}})
            await self.insert_one(record | {"id": "successor", "sessionId": "new"})
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_many", reconnect_before_cleanup)
    assert await next_manual_command(runtime.repo, runtime) is None
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "CANCELLED"
    assert (await runtime.repo.db.commands.find_one({"id": "successor"}))["status"] == "QUEUED"


async def test_cleanup_does_not_overwrite_sending_record(tmp_path, monkeypatch):
    """快照中的记录已进入发送阶段时，不能把不确定发送改成明确取消。"""
    runtime, _ = await runtime_and_command(tmp_path)
    await runtime.repo.db.commands.update_one({"id": "command"}, {"$set": {"sessionId": "old"}})
    collection_type = type(runtime.repo.db.commands)
    original = collection_type.update_many

    async def start_before_cleanup(self, query, update, **kwargs):
        if self.name == "commands":
            await self.update_one({"id": "command"}, {"$set": {"status": "SENDING"}})
        return await original(self, query, update, **kwargs)

    monkeypatch.setattr(collection_type, "update_many", start_before_cleanup)
    assert await next_manual_command(runtime.repo, runtime) is None
    assert (await runtime.repo.db.commands.find_one({"id": "command"}))["status"] == "SENDING"

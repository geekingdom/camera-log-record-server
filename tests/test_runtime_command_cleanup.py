"""会话收尾隔离测试：旧连接只能结束自身尚未完成的命令记录。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection.runtime import SessionRuntime
from mongomock_motor import AsyncMongoMockClient


@pytest.mark.parametrize("construction_fails", [False, True])
@pytest.mark.parametrize("stop_fails", [False, True])
async def test_runtime_cleanup_does_not_touch_other_runs_or_sessions(monkeypatch, tmp_path, construction_fails, stop_fails):
    """构造或停止失败时，收尾仍只处理已创建旧会话的命令记录。"""
    database = AsyncMongoMockClient().db
    documents = []
    for task, run, session in [
        ("task", "old-run", "old-session"),
        ("task", "old-run", "new-session"),
        ("task", "new-run", "new-session"),
        ("other-task", "old-run", "old-session"),
    ]:
        for status in ("QUEUED", "SENDING", "SENT"):
            documents.append({
                "id": f"{task}:{run}:{session}:{status}", "taskId": task,
                "runId": run, "sessionId": session, "status": status,
            })
    await database.commands.insert_many(documents)
    started = asyncio.Event()

    class Collector:
        """保持连接直到测试取消运行，避免依赖真实设备及重连退避时间。"""

        def __init__(self, *_args, **_kwargs):
            if construction_fails:
                raise asyncio.CancelledError
            self.session_id = "old-session"

        async def start(self):
            started.set()

        async def wait_closed(self):
            await asyncio.Future()

        async def stop(self):
            if stop_fails:
                raise RuntimeError("collector stop failed")

    monkeypatch.setattr("camera_logs.collection.runtime.Collector", Collector)
    runtime = object.__new__(SessionRuntime)
    runtime.repo = SimpleNamespace(
        db=database, decrypt=lambda value: value,
        settings=SimpleNamespace(known_hosts=None, ssh_verify_host_key=False,
                                 psh_serial_character_interval=0, log_root=tmp_path),
    )
    runtime.task = {"id": "task", "runId": "old-run", "passwordEncrypted": ""}
    await database.tasks.insert_one(runtime.task.copy())
    runtime.collector, runtime.error = None, None
    runtime.factory = runtime.debug_passwords = None
    runtime.stopping = False
    runtime.pending_executions = {}
    running = asyncio.create_task(runtime.run())
    if not construction_fails:
        await asyncio.wait_for(started.wait(), 1)
        running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    for document in documents:
        stored = await database.commands.find_one({"id": document["id"]})
        own_session = (document["taskId"], document["runId"], document["sessionId"]) == (
            "task", "old-run", "old-session",
        )
        expected = document["status"]
        if own_session and not construction_fails:
            expected = {"QUEUED": "CANCELLED", "SENDING": "UNKNOWN"}.get(expected, expected)
        assert stored["status"] == expected, document["id"]

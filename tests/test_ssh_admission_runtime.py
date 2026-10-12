"""准入结果未知必须阻塞运行，不能把尚无连接对象当作已关闭证明。"""

import asyncio

import pytest
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.collection.ssh_admission import SshSlotUncertain
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.node.worker import Worker
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


@pytest.mark.asyncio
async def test_unknown_factory_reservation_never_produces_closed_receipt(tmp_path):
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        log_root=tmp_path, node_id="node-a")
    repo = Repository(AsyncMongoMockClient().db, settings)
    await repo.initialize()
    task = {"id": "task-a", "runId": "run-a", "generation": 1, "nodeId": "node-a",
            "protocol": "SSH", "ip": "192.0.2.1", "port": 22, "username": "user",
            "passwordEncrypted": repo.encrypt("test-only"), "storageIdentity": "test-device",
            "desiredState": "RUNNING", "status": "PENDING"}
    await repo.db.tasks.insert_one(task)

    async def unknown(_config):
        raise SshSlotUncertain("192.0.2.1", "unknown-token")

    runtime = SessionRuntime(repo, task, unknown)
    worker = Worker(repo)
    worker.active[task["id"]] = runtime
    await asyncio.wait_for(runtime.background, 2)
    with pytest.raises(SshSlotUncertain):
        await worker.finish_runtime(runtime, "release")
    saved = await repo.db.tasks.find_one({"id": task["id"]})
    assert saved["status"] == "BLOCKED" and saved["nodeId"] == "node-a"
    assert not saved.get("closedReceipt")
    assert runtime.catalog_task.done()


@pytest.mark.asyncio
async def test_repeated_unknown_stop_does_not_grow_stored_traceback(tmp_path):
    """未知连接不得假冒关闭；反复停止也不能把历史异常栈无限留在内存和日志。"""
    runtime = object.__new__(SessionRuntime)
    runtime.collector = None
    runtime.catalog_dirty = {}
    runtime.catalog_task = asyncio.create_task(asyncio.sleep(0))
    runtime.background = asyncio.create_task(asyncio.sleep(0))
    runtime.task = {"id": "uncertain"}
    runtime.connection_uncertain = SshSlotUncertain("192.0.2.1", "token")
    await asyncio.gather(runtime.catalog_task, runtime.background)
    lengths = []
    for _ in range(100):
        with pytest.raises(SshSlotUncertain) as info:
            await runtime.stop()
        import traceback
        lengths.append(len(traceback.extract_tb(info.value.__traceback__)))
    assert len(set(lengths)) == 1
    assert runtime.connection_uncertain.__traceback__ is None

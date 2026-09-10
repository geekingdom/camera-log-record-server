"""真实本地 Telnet 传输的暂停、恢复初始化与同运行预算回归。"""

import asyncio

import pytest
from camera_logs.collection.connections import _connect_telnet
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.worker import Worker
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction", "mock_claim_transaction")


async def test_telnet_device_pause_closes_tcp_and_resume_reuses_budget(tmp_path):
    """服务器观察到旧TCP EOF才恢复；两会话初始化且总次数不重置。"""
    received, eof, handlers = [], [], set()

    async def serve(reader, writer):
        handlers.add(asyncio.current_task())
        payload, closed = bytearray(), asyncio.Event()
        received.append(payload)
        eof.append(closed)
        try:
            while chunk := await reader.read(65536):
                payload.extend(chunk)
                writer.write(b"device log\n")
                await writer.drain()
        finally:
            closed.set()
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    async def until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    runtimes = []
    try:
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                            node_id="telnet-pause", start_background=False)
        repo = Repository(AsyncMongoMockClient().camera_logs, settings)
        await repo.initialize()
        port = server.sockets[0].getsockname()[1]
        task = {"id": "pause", "runId": "same-run", "nodeId": settings.node_id, "generation": 1,
                "status": "PENDING", "desiredState": "RUNNING", "protocol": "TELNET_DEVICE", "ip": "127.0.0.1",
                "port": port, "passwordEncrypted": "", "storageIdentity": "telnet-pause",
                "initialCommands": [{"command": "initialise"}], "scheduledCommands": [
                    {"id": "periodic", "command": "probe", "totalExecutions": 2, "intervalSeconds": 1}]}
        await repo.db.tasks.insert_one(task.copy())
        await repo.db.runs.insert_one({"id": "same-run", "taskId": "pause", "nodeId": settings.node_id, "endedAt": None})
        await repo.db.endpoint_locks.insert_one({"taskId": "pause", "runId": "same-run", "endpoint": f"127.0.0.1:{port}"})
        await repo.db.nodes.insert_one({"id": settings.node_id, "heartbeat": now(), "capacity": 100, "diskPercent": 1,
                                        "accepting": True})

        async def connect(config):
            return await _connect_telnet(config, config["ip"], config["port"])

        runtime = SessionRuntime(repo, task, connection_factory=connect)
        runtimes.append(runtime)
        await until(lambda: received and b"probe\n" in received[0])
        first_session = runtime.collector.session_id
        worker = Worker(repo)
        worker.active[task["id"]] = runtime
        await repo.db.tasks.update_one({"id": "pause"}, {"$set": {"desiredState": "PAUSED"}})
        await worker.pause(runtime)
        await asyncio.wait_for(eof[0].wait(), 1)
        assert runtime.background.done() and len(received) == 1
        assert (await repo.get("tasks", "pause"))["status"] == "PAUSED"
        assert (await repo.db.budgets.find_one({"_id": "same-run:periodic"}))["attempts"] == 1

        await repo.db.tasks.update_one({"id": "pause"}, {"$set": {"desiredState": "RUNNING"}})
        await schedule_once(repo)
        resumed = await repo.get("tasks", "pause")
        assert resumed["runId"] == "same-run" and resumed["status"] == "PENDING"
        runtime = SessionRuntime(repo, resumed, connection_factory=connect)
        runtimes.append(runtime)
        await until(lambda: len(received) == 2 and b"probe\n" in received[1])
        await runtime.stop()
        await asyncio.wait_for(eof[1].wait(), 1)
        assert runtime.collector.session_id != first_session
        assert all(payload.count(b"initialise\n") == 1 and payload.count(b"probe\n") == 1 for payload in received)
        assert (await repo.db.budgets.find_one({"_id": "same-run:periodic"}))["attempts"] == 2
    finally:
        for runtime in runtimes:
            await runtime.stop()
        server.close()
        await server.wait_closed()
        pending = list(handlers)
        for handler in pending:
            handler.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
